#!/usr/bin/env python
# webui.py - 邮件管家（网页界面）
"""
把管家做成一个本地网页应用：不用终端、可以直接粘贴图片、点选切换模型。

    python webui.py            # 启动服务并打开窗口
    python webui.py --no-open  # 只启动服务，不自动开窗口

架构：
    app.py（本文件）  —— FastAPI：静态页 + 附件上传 + SSE 对话流
    butler_core.py    —— 提示词、工具标签、MCP 子进程、会话（与终端版共用）
    static/           —— 前端（原生 HTML/CSS/JS，没有构建步骤）

为什么用网页而不是原生窗口：
  1. **浏览器原生支持粘贴图片/文件**。终端做不到——实测在资源管理器里
     复制文件后剪贴板里只有文件格式、没有文本格式，Ctrl+V 什么都得不到。
  2. **不引入新依赖**：FastAPI 与 uvicorn 为了 MCP 服务本来就要装。
  3. 用 Edge 的 `--app` 模式打开就是一个没有地址栏的独立窗口，
     看起来与普通桌面应用一样（见 open_window.py）。

安全性：**只监听 127.0.0.1**。这个界面能直接发邮件，不该暴露到局域网。
"""

import argparse
import asyncio
import json
import mimetypes
import os
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import butler_core
from clipboard import ClipboardItem, import_clipboard, is_supported as clipboard_supported
from config import settings
from envfile import RESTART_KEYS, SECRET_KEYS, EnvFile

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

#: 应用自己读写的 .env
ENV_FILE = EnvFile(ROOT / ".env")

#: 网页界面端口。刻意避开 8000（那是 MCP 服务器的端口），
#: 这样两个服务同时跑也不会打架。
WEB_HOST = "127.0.0.1"
WEB_PORT = 8765

#: 上传大小上限（字节）。与发信的单件上限一致，避免「传得上来、发不出去」。
MAX_UPLOAD_BYTES = settings.max_attachment_bytes


# ---------------------------------------------------------------------------
# 会话容器
# ---------------------------------------------------------------------------

class ButlerState:
    """
    进程内唯一的一份会话状态。

    这是**单用户本地应用**，因此不做多会话隔离——多用户隔离会引入
    「谁的历史是哪份」这类问题，而本应用只服务本机一个人。
    代价是同一时刻只允许一轮对话，用锁保证。
    """

    def __init__(self):
        self.session: Optional[butler_core.AgentSession] = None
        self.ready = False
        self.error: Optional[str] = None
        self.busy = asyncio.Lock()
        self.startup_log: List[str] = []
        self._start_lock = threading.Lock()

    def log(self, text: str) -> None:
        self.startup_log.append(text)

    async def ensure_started(self) -> None:
        """惰性启动会话（首次进入页面时才拉起 Ollama 与 MCP）。"""
        if self.ready:
            return
        if self.session is None:
            self.session = butler_core.AgentSession()
            await self.session.start(on_step=self.log)
            self.ready = True

    async def restart(self) -> None:
        """重建会话（切模型或失败后重试）。"""
        if self.session is not None:
            self.session.shutdown()
        self.session = butler_core.AgentSession()
        self.startup_log.clear()
        self.error = None
        await self.session.start(on_step=self.log)
        self.ready = True

    def shutdown(self) -> None:
        if self.session is not None:
            self.session.shutdown()
            self.session = None
        self.ready = False


state = ButlerState()

app = FastAPI(title="邮件管家")


# ---------------------------------------------------------------------------
# 附件
# ---------------------------------------------------------------------------

def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "%.1f %s" % (value, unit)
        value /= 1024
    return "%.1f GB" % value


def _describe_file(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    mime, _ = mimetypes.guess_type(path.name)
    return {
        "name": path.name,
        "size": stat.st_size,
        "size_text": _human_size(stat.st_size),
        "modified": int(stat.st_mtime),
        "is_image": bool(mime and mime.startswith("image/")),
    }


def list_attachment_files() -> List[Dict[str, Any]]:
    """按修改时间倒序列出附件——最近放的排最前，符合「刚粘贴的要用」的习惯。"""
    directory = Path(settings.attachment_dir)
    try:
        files = [p for p in directory.iterdir() if p.is_file()]
    except OSError:
        return []
    described = []
    for p in files:
        try:
            described.append(_describe_file(p))
        except OSError:
            continue
    described.sort(key=lambda item: item["modified"], reverse=True)
    return described


def _safe_attachment_path(name: str) -> Path:
    """
    把用户给的文件名解析成附件目录内的真实路径。

    只取 basename 并拒绝路径分隔符：否则 `../../.env` 这类名字
    能读写到附件目录之外的任意文件。这是本应用唯一一处接受
    外部文件名的入口，必须挡住。
    """
    if not name or name != Path(name).name or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="非法的文件名")
    path = (Path(settings.attachment_dir) / name).resolve()
    attachment_root = Path(settings.attachment_dir).resolve()
    if attachment_root not in path.parents:
        raise HTTPException(status_code=400, detail="非法的文件名")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="附件不存在")
    return path


@app.get("/api/attachments")
async def api_attachments():
    return {"files": list_attachment_files()}


@app.get("/api/attachments/{name}/raw")
async def api_attachment_raw(name: str):
    """回传附件内容，供界面显示图片缩略图。"""
    path = _safe_attachment_path(name)
    return FileResponse(path)


@app.get("/api/attachments/{name}/download")
async def api_attachment_download(name: str):
    path = _safe_attachment_path(name)
    return FileResponse(path, filename=path.name)


@app.delete("/api/attachments/{name}")
async def api_attachment_delete(name: str):
    path = _safe_attachment_path(name)
    try:
        path.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail="删除失败：%s" % e)
    return {"ok": True}


@app.post("/api/attachments")
async def api_attachment_upload(files: List[UploadFile] = File(...)):
    """
    接收粘贴或选择的文件。

    浏览器粘贴图片时给的是一个内存中的 Blob，因此必须由前端上传，
    后端只负责落盘——这和终端版的 /粘贴 是同一件事的两种入口。
    """
    directory = Path(settings.attachment_dir)
    directory.mkdir(parents=True, exist_ok=True)

    saved, problems = [], []
    for upload in files:
        raw = await upload.read()
        name = Path(upload.filename or "未命名").name

        if not raw:
            problems.append("%s：文件为空" % name)
            continue
        if len(raw) > MAX_UPLOAD_BYTES:
            problems.append("%s：%s 超过单件上限 %s" % (
                name, _human_size(len(raw)), _human_size(MAX_UPLOAD_BYTES)))
            continue

        target = directory / name
        # 避免覆盖同名文件，但保留扩展名，方便模型识别类型
        if target.exists():
            stem, suffix = target.stem, target.suffix
            for i in range(2, 1000):
                candidate = directory / ("%s_%d%s" % (stem, i, suffix))
                if not candidate.exists():
                    target = candidate
                    break

        try:
            target.write_bytes(raw)
        except OSError as e:
            problems.append("%s：写入失败 %s" % (name, e))
            continue
        saved.append(_describe_file(target))

    return {"saved": saved, "problems": problems}


@app.post("/api/paste")
async def api_paste_clipboard():
    """
    从**系统剪贴板**导入（终端版 /粘贴 的等价入口）。

    网页自己粘贴走的是浏览器事件，不需要这个；但如果用户是
    用截图工具复制、然后点了界面上的按钮，剪贴板里可能只有
    CF_DIB 而没有进入浏览器事件，这时用得上。
    """
    if not clipboard_supported():
        return {"saved": [], "problems": ["当前环境无法读取剪贴板（需要 Windows + pywin32）"]}

    items, problems = import_clipboard(
        Path(settings.attachment_dir), max_bytes=MAX_UPLOAD_BYTES
    )
    saved = [_describe_file(Path(item.path)) for item in items if Path(item.path).is_file()]
    return {"saved": saved, "problems": problems}


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------

@app.get("/api/models")
async def api_models():
    """
    列出可用模型与当前使用的模型，供界面下拉框使用。

    返回的每个条目都带 `available` 与 `reason`，界面据此说明
    「为什么这个选项不能选」——比只显示一个灰掉的项有用得多。
    """
    return {
        "models": butler_core.list_available_models(),
        "current": butler_core.active_ref(),
        "default_ollama": settings.ollama_model,
        "deepseek_configured": butler_core.deepseek_configured(),
        "deepseek_dependency": butler_core.deepseek_dependency_ready(),
    }


@app.post("/api/models")
async def api_switch_model(payload: Dict[str, Any]):
    """
    切换模型。

    这里**会按需启动会话**。早期实现是「会话没起来就返回 409 管家尚未启动」，
    而会话是首次对话时才惰性启动的——于是「刚打开应用就去切模型」
    必然失败，且报的是「管家尚未启动」，用户根本猜不到原因
    （以为是模型有问题）。一个能自己满足的前置条件，不该当成错误抛出去。
    """
    model = (payload or {}).get("model", "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="缺少 model 参数")

    # 用锁保护，避免与正在进行的对话同时改会话状态
    if state.busy.locked():
        raise HTTPException(status_code=409, detail="正在处理上一轮，请稍候再切换模型")

    async with state.busy:
        try:
            await state.ensure_started()
        except Exception as e:  # noqa: BLE001
            state.error = str(e)[:500]
            raise HTTPException(
                status_code=400,
                detail="管家启动失败，无法切换模型：%s" % state.error)

        try:
            await state.session.use_model(model, on_step=state.log)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(e)[:300])

        # 记住选择，下次启动仍是这个模型
        try:
            ENV_FILE.update({"ACTIVE_MODEL": model})
            apply_env_to_settings(ENV_FILE.as_dict())
        except Exception as e:  # noqa: BLE001 - 记不住选择不该让切换失败
            state.log("警告：无法把模型选择写入 .env（%s）" % str(e)[:80])

    return {"ok": True, "current": state.session.model}


# ---------------------------------------------------------------------------
# 配置编辑
# ---------------------------------------------------------------------------

def apply_env_to_settings(values: Dict[str, str]) -> List[str]:
    """
    把 .env 里的值**就地**应用到配置对象上，返回变化过的键。

    为什么必须「就地改」而不是重新构造一个 Settings：
    `from config import settings` 把对象绑定到每个模块自己的命名空间里
    （email_tools、butler_core、webui 各有一份引用）。新建对象的话，
    那些模块仍然指向旧对象——界面上显示已改、实际还在用旧凭据，
    这是最难排查的一类问题。

    `model_config` 上刻意没开 `validate_assignment`，
    所以这里的赋值不会触发校验；合法性由调用方先校验。
    """
    from config import Settings

    declared = set(Settings.model_fields.keys())
    changed: List[str] = []

    for key, raw in values.items():
        field = key.lower()
        if field not in declared:
            continue
        value: Any = raw
        if field in ("smtp_port", "mcp_port", "imap_port"):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
        elif field in ("debug", "email_confirm_delivery", "send_retry_on_reconnect"):
            value = str(raw).strip().lower() in ("1", "true", "yes", "on")
        elif field in ("deepseek_api_key", "active_model", "mcp_auth_token"):
            value = raw.strip() or None

        if getattr(settings, field, None) != value:
            setattr(settings, field, value)
            changed.append(key)

    return changed


def validate_smtp_input(values: Dict[str, str]) -> List[str]:
    """
    保存**之前**校验邮箱与授权码，返回可直接展示的问题列表。

    为什么值得单独校验：
      - 授权码不是 QQ 密码，用户很容易填成密码。长度是唯一能离线判断的特征。
      - 空值必须拦住，否则 Settings 构造会抛 ValidationError，
        而那条栈对用户毫无意义。
    """
    problems: List[str] = []

    email = values.get("SMTP_EMAIL")
    if email is not None:
        email = email.strip()
        if not email:
            problems.append("邮箱地址不能为空。")
        elif "@" not in email or "." not in email.split("@")[-1]:
            problems.append("邮箱地址看起来不对：%s" % email)

    password = values.get("SMTP_PASSWORD")
    if password is not None:
        password = password.strip()
        if not password:
            problems.append("授权码不能为空。")
        elif len(password) != 16:
            problems.append(
                "授权码长度是 %d，但 QQ 邮箱的授权码固定为 16 位。"
                "请确认填的不是 QQ 密码。" % len(password))

    port = values.get("SMTP_PORT")
    if port:
        try:
            int(port)
        except ValueError:
            problems.append("端口必须是数字：%s" % port)

    return problems


@app.get("/api/config")
async def api_get_config():
    """
    返回配置的**脱敏视图**：敏感项只报「是否已设置」与长度，绝不回内容。

    界面拿不到授权码内容，就不会因为前端被注入、误截图或日志而泄露。
    """
    view = ENV_FILE.masked()
    return {
        "values": view,
        "editable": sorted(view.keys()),
        "models": butler_core.list_available_models(),
    }


@app.put("/api/config")
async def api_put_config(payload: Dict[str, Any]):
    """
    保存配置并**即时生效**，不必重启应用。

    流程：校验 -> 写 .env -> 就地应用到 settings -> 重建受影响的部分。
    任何一步失败都会把原因说清楚，而不是只回一个 500。
    """
    if state.busy.locked():
        raise HTTPException(status_code=409, detail="正在处理上一轮，请稍候再改配置")

    raw = (payload or {}).get("values") or {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="values 必须是一个对象")

    # 敏感项留空 = 「不要改动这一项」，而不是「清空它」。
    # 界面根本拿不到内容，所以它只能留空；若把空值当成清空，
    # 用户每改一次别的字段就会把授权码抹掉。
    changes: Dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key not in ENV_FILE.masked():
            continue
        text = "" if value is None else str(value)
        if key in SECRET_KEYS and text.strip() == "":
            continue
        changes[key] = text.strip()

    # 「清空某个非敏感项」用显式标记表达，避免与「没填」混淆
    for key in (payload or {}).get("clear") or []:
        if isinstance(key, str) and key in ENV_FILE.masked():
            changes[key] = ""

    if not changes:
        return {"ok": True, "changed": [], "message": "没有需要保存的改动"}

    problems = validate_smtp_input(changes)
    if problems:
        raise HTTPException(status_code=400, detail="\n".join(problems))

    async with state.busy:
        try:
            result = ENV_FILE.update(changes)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except OSError as e:
            raise HTTPException(status_code=500, detail="写入 .env 失败：%s" % e)

        # 就地应用，让所有已绑定 settings 的模块立刻看到新值
        applied = apply_env_to_settings(ENV_FILE.as_dict())

        # 让发送器丢掉旧连接、用新凭据重连
        try:
            from email_tools import email_tools

            email_tools.reload_config()
        except Exception as e:  # noqa: BLE001
            state.log("警告：重建发送器失败（%s）" % str(e)[:80])

        # 模型相关配置改了要重建智能体
        session_keys = set(applied) & RESTART_KEYS
        rebuilt = False
        if session_keys and state.session is not None:
            try:
                await state.session.use_model(butler_core.active_ref(), on_step=state.log)
                rebuilt = True
            except Exception as e:  # noqa: BLE001
                state.error = str(e)[:300]

    return {
        "ok": True,
        "changed": result["changed"],
        "added": result["added"],
        "applied": sorted(applied),
        "backup": result["backup"],
        "rebuilt": rebuilt,
        "message": "已保存并生效。",
    }


@app.post("/api/config/test")
async def api_test_config(payload: Dict[str, Any]):
    """
    用**当前生效的配置**测一次 SMTP 连通性。

    刻意不接收调用方传来的凭据：那等于开了一个「拿任意凭据去连服务器」
    的接口。要测试就先保存，再测。
    """
    problems = validate_smtp_input(ENV_FILE.as_dict())
    if problems:
        raise HTTPException(status_code=400, detail="\n".join(problems))

    from email_tools import email_tools

    try:
        email_tools.reload_config()
        result = await email_tools.check_email_config()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="连接测试失败：%s" % str(e)[:300])

    return result


# ---------------------------------------------------------------------------
# 状态与对话
# ---------------------------------------------------------------------------

@app.get("/api/info")
async def api_info():
    return {
        "email": settings.smtp_email,
        "model": state.session.model if state.session else settings.ollama_model,
        "ready": state.ready,
        "attachments_dir": str(settings.attachment_dir),
        "max_attachment_bytes": MAX_UPLOAD_BYTES,
        "max_attachment_text": _human_size(MAX_UPLOAD_BYTES),
        "startup_log": state.startup_log,
        "error": state.error,
    }


@app.post("/api/chat")
async def api_chat(payload: Dict[str, Any]):
    """
    一轮对话，用 SSE 把「它做了什么」实时推给浏览器。

    为什么用 SSE 而不是等结果：一轮可能要十几秒（模型推理 + SMTP）。
    若什么都不显示，用户分不清它是在工作还是卡死了。
    而 astream 在每个节点结束时就产出内容，因此「决定调用哪个工具」
    能在工具真正执行前就显示出来。
    """
    text = (payload or {}).get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="内容为空")

    # 界面上附带的文件（粘贴进来的那些）明确告诉模型，
    # 否则「把这张图发给张三」里的「这张图」只能靠模型猜文件名。
    # 提示词里会列出附件目录的全部文件，但目录里可能有很多旧文件，
    # 这里点名本轮要发的那几个，能显著减少发错文件的概率。
    names = [str(n) for n in (payload or {}).get("attachments") or [] if n]
    if names:
        text = "%s\n\n（本轮要作为附件发送的文件：%s）" % (text, "、".join(names))

    if state.busy.locked():
        raise HTTPException(status_code=409, detail="正在处理上一轮，请稍候")

    async def event_stream():
        def send(event: Dict[str, Any]) -> str:
            return "data: %s\n\n" % json.dumps(event, ensure_ascii=False)

        async with state.busy:
            try:
                await state.ensure_started()
            except Exception as e:  # noqa: BLE001
                state.error = str(e)[:500]
                yield send({"type": "error", "text": state.error})
                return

            yield send({"type": "start"})
            try:
                async for event in state.session.ask_stream(text):
                    yield send(event)
            except Exception as e:  # noqa: BLE001
                message = str(e)
                hint = ""
                if "connect" in message.lower() or "11434" in message:
                    hint = "（Ollama 似乎断开了，请确认 ollama serve 仍在运行）"
                yield send({"type": "error", "text": message[:400] + hint})
                return

            # 附件可能刚被导入或删除，让界面刷新列表
            yield send({"type": "attachments", "files": list_attachment_files()})
            yield send({"type": "done"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",     # 关掉反向代理缓冲，保证逐条到达
            "Connection": "keep-alive",
        },
    )


@app.post("/api/reset")
async def api_reset():
    """开始新对话：清空历史，但保留模型与工具。"""
    if state.session is not None:
        await state.session.reset()
    return {"ok": True}


@app.post("/api/restart")
async def api_restart():
    """重试启动（例如用户刚启动 Ollama 就点了一下）。"""
    try:
        await state.restart()
    except Exception as e:  # noqa: BLE001
        state.error = str(e)[:500]
        raise HTTPException(status_code=400, detail=state.error)
    return {"ok": True, "startup_log": state.startup_log}


@app.get("/health")
async def health():
    return {"status": "ok", "ready": state.ready}


# ---------------------------------------------------------------------------
# 静态页
# ---------------------------------------------------------------------------

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    page = STATIC_DIR / "index.html"
    if not page.is_file():
        raise HTTPException(status_code=500, detail="缺少 static/index.html")
    return FileResponse(page)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def _start_server(port: int):
    import uvicorn

    uvicorn.run(app, host=WEB_HOST, port=port, log_level="warning")


def main() -> int:
    parser = argparse.ArgumentParser(description="邮件管家（网页界面）")
    parser.add_argument("--port", type=int, default=WEB_PORT, help="监听端口")
    parser.add_argument("--no-open", action="store_true", help="不自动打开窗口")
    args = parser.parse_args()

    url = "http://%s:%d/" % (WEB_HOST, args.port)

    # 服务跑在后台线程：主线程留给「打开窗口」和信号处理。
    # uvicorn 自己会装信号处理器，放主线程反而更麻烦。
    server = threading.Thread(target=_start_server, args=(args.port,), daemon=True)
    server.start()

    # 等端口真的起来再开窗口，否则会看到一个连接失败页
    import httpx

    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get("%shealth" % url, timeout=1.0).status_code == 200:
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    else:
        print("服务启动超时。", file=sys.stderr)
        return 1

    print("邮件管家已启动：%s" % url)
    print("（关闭这个窗口即停止服务）")

    if not args.no_open:
        try:
            from open_window import open_app_window

            if not open_app_window(url):
                webbrowser.open(url)
        except Exception:  # noqa: BLE001
            webbrowser.open(url)

    try:
        while server.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        state.shutdown()
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
