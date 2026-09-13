#!/usr/bin/env python
# email_butler.py - 邮件管家：对话式发信
"""
启动后直接跟它说话，它自己决定调用哪个工具。

    > 给我自己发封邮件，说早上好
    > 主题改成明天开会的时间
    > 算了，还是发给张三吧

本程序是项目推荐的日常入口。相比一个「一问一答」的简单客户端，它解决了
四件实际影响体验的事：

  1. **单入口**：本程序自己管理 MCP 服务器子进程，
     不需要你先开一个窗口跑 run_server.py。
     若端口上已有服务器在跑，会复用而不是重复启动。
  2. **多轮记忆**：每轮把完整对话历史交给智能体。
     若每轮只传当前一句，「主题改成XXX」这类追问完全接不住——
     它不知道你在说哪封邮件（这一点是实测出来的，不是推测）。
  3. **动作可见**：会把调用了哪个工具、参数是什么、返回什么打出来，
     而不是只给一句结论，便于判断它是真发了还是在敷衍。
  4. **缺依赖时讲人话**：Ollama 没起、模型没拉，会给出可直接照抄的命令。

另一个客户端 ollama_mcp_client.py 的定位不同：它演示**手写 MCP 协议交互**
（自己拼 JSON-RPC、自己解析模型输出），用于理解协议本身，不是日常工具。

前置条件：
  - Ollama 已启动，且已拉取模型（默认 qwen2.5:3b）
  - MCP 服务器由本程序自动启动，无需手动操作

运行：
  python email_butler.py
"""

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, List, Optional

import httpx

ROOT = Path(__file__).resolve().parent

MCP_HOST = "127.0.0.1"
MCP_PORT = 8000
MCP_URL = "http://%s:%d/mcp" % (MCP_HOST, MCP_PORT)
HEALTH_URL = "http://%s:%d/health" % (MCP_HOST, MCP_PORT)

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:3b"

#: 交给智能体的历史消息上限，避免越聊越长拖慢每轮推理
MAX_HISTORY_MESSAGES = 24

#: 等待 MCP 服务器就绪的上限（秒）
SERVER_READY_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# 依赖检查
# ---------------------------------------------------------------------------

def check_ollama() -> Optional[str]:
    """
    检查 Ollama 是否可用、模型是否已拉取。

    返回 None 表示一切正常；否则返回一段可直接展示的提示。
    """
    try:
        resp = httpx.get("%s/api/tags" % OLLAMA_BASE_URL, timeout=5.0)
        resp.raise_for_status()
        models = [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception:  # noqa: BLE001
        return (
            "无法连接到 Ollama。请先启动它：\n"
            "    ollama serve\n"
            "（Windows 上安装 Ollama 后通常已在后台运行，可先打开一次应用）"
        )

    # 模型名可能带 :latest 等标签，做宽松匹配
    base = OLLAMA_MODEL.split(":")[0]
    if not any(m == OLLAMA_MODEL or m.startswith(base) for m in models):
        return (
            "Ollama 中找不到模型 %s。可用的有：%s\n"
            "请先拉取：\n"
            "    ollama pull %s" % (OLLAMA_MODEL, ", ".join(models) or "（无）", OLLAMA_MODEL)
        )
    return None


def is_server_up() -> bool:
    try:
        return httpx.get(HEALTH_URL, timeout=2.0).status_code == 200
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# MCP 服务器子进程
# ---------------------------------------------------------------------------

class MCPServerProcess:
    """
    自动管理 MCP 服务器子进程。

    若端口上已经有服务器在跑（例如你手动启过），就直接复用，不重复启动。
    """

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.reused = False

    def start(self) -> None:
        if is_server_up():
            self.reused = True
            print("· 检测到已运行的 MCP 服务器，直接复用")
            return

        print("· 正在启动 MCP 服务器…")
        creationflags = 0
        if os.name == "nt":
            # 不弹出额外控制台窗口
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "mcp_server.py")],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

        deadline = time.time() + SERVER_READY_TIMEOUT
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("MCP 服务器启动后立即退出，请手动运行 mcp_server.py 查看原因")
            if is_server_up():
                print("· MCP 服务器就绪")
                return
            time.sleep(0.3)
        raise RuntimeError("等待 MCP 服务器就绪超时（%.0f 秒）" % SERVER_READY_TIMEOUT)

    def stop(self) -> None:
        if self.process is None:
            return  # 复用的服务器不归我们管，不要关掉别人的
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        print("· MCP 服务器已停止")


# ---------------------------------------------------------------------------
# 术语与展示
# ---------------------------------------------------------------------------

#: 把工具名转成中文动作，让用户看懂它在做什么
TOOL_LABELS = {
    "send_text_email": "发送邮件",
    "send_html_email": "发送 HTML 邮件",
    "send_email_with_attachment": "发送带附件的邮件",
    "check_email_config": "检查邮箱配置",
    "save_environment_config": "保存配置",
}

SYSTEM_PROMPT_TEMPLATE = """你是用户的邮件管家，名字叫「小邮」。你能调用工具帮用户发邮件。

当前用户的邮箱是：{email}
附件目录：{attach_dir}
附件目录里现有的文件：
{attach_list}

最重要的一条：
用户说了要发邮件，你就必须**立即调用工具把它真正发出去**。
不允许先反问确认再等用户回复——用户已经说清楚了就直接做。
（实测发现：只要提示词里留了「可以先问一下」的余地，小模型就会一直反问，
 结果一封邮件都发不出去。）

关于附件（务必遵守）：
- attachment_paths 只能填**上面列表里真实存在的文件**，或用户在消息里给出的完整路径。
- 绝对不要自己拼文件名或猜扩展名。用户说的名字与列表对不上时，
  选列表里最接近的那个；实在拿不准就发一封不带附件的邮件，
  并在回答里说明你没有找到该附件。
- 用户给的是完整路径时，原样使用。

其余要求：
1. 用户说「我自己」「给我自己」「我」时，收件人就是 {email}。
2. 用户没给全主题或正文时，自己合理补全，然后在回答里说明你补了什么，
   让用户有机会纠正。
3. 工具参数里的 subject / body 必须就是最终要发出的内容本身。
   只在回答里描述改动是没用的，回答不能改变已经发出的邮件。
   用户说「主题改成X」时，发出邮件的 subject 就必须是 X。
4. 用户用「改成…」「换成…」「还是…吧」这类说法时，指的是修改上一封邮件的
   内容——请结合对话历史理解。
5. 收件人地址必须来自用户提供的信息。用户明确没给地址时应当询问，
   不要凭空编造收件人。
6. 调用工具后，用一句话说明结果：发给了谁、主题是什么、有没有带附件。
7. 中文回答，简洁、口语化，不要罗列步骤。"""


def list_attachments(directory: Path) -> List[str]:
    """
    列出附件目录中的文件名，供提示词使用。

    把真实文件名告诉模型，是避免它凭空拼出「测试数据.xlsx」这类
    并不存在的路径——那样会被附件校验拦下，用户白等一场。
    """
    try:
        if not directory.is_dir():
            return []
        return sorted(p.name for p in directory.iterdir() if p.is_file())
    except Exception:  # noqa: BLE001 - 列目录失败不应影响启动
        return []


def _is_tool_call_message(msg: Any) -> bool:
    return bool(getattr(msg, "tool_calls", None))


def print_tool_calls(msg: Any) -> None:
    """打印智能体本轮决定调用的工具及其参数。"""
    for call in getattr(msg, "tool_calls", None) or []:
        name = call.get("name", "?")
        label = TOOL_LABELS.get(name, name)
        args = call.get("args") or {}
        print("  ⚙  %s" % label)
        if args.get("to_email"):
            print("       收件人: %s" % args["to_email"])
        if args.get("subject"):
            print("       主题: %s" % args["subject"])
        if args.get("attachment_paths"):
            print("       附件: %s" % ", ".join(args["attachment_paths"]))


def print_tool_result(msg: Any) -> None:
    """
    打印工具返回的内容。

    附件路径被自动修正时，这里会把**真实发出的文件名**补印出来——
    否则用户看到的是模型猜错的名字（例如 .xlsx），
    而实际发出的是 .csv，会以为发错了文件。
    """
    content = getattr(msg, "content", "")
    text = content if isinstance(content, str) else str(content)
    # 工具返回常是 [{'type': 'text', 'text': '...'}] 形式，抽出可读部分
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("text"):
                parts.append(block["text"])
        text = " ".join(parts) or text
    text = text.strip().replace("\n", " ")
    if text:
        print("  ↩  %s" % (text[:150] + ("…" if len(text) > 150 else "")))

    substitutions = getattr(msg, "artifact", None)
    # LangGraph 不保证把结构化结果带出来，这里尽力而为：
    # 若返回文本里已经说明了修正，就不再重复
    if substitutions and "自动修正" not in text:
        print("  ℹ  附件已自动修正: %s" % substitutions)


def extract_reply(msg: Any) -> str:
    """从最终消息里取出要展示给用户的文本。"""
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p).strip()
    return str(content).strip()


# ---------------------------------------------------------------------------
# 管家主程序
# ---------------------------------------------------------------------------

class EmailButler:
    def __init__(self):
        self.agent = None
        self.tool_names: List[str] = []
        self.history: List[Any] = []
        self.server = MCPServerProcess()
        self._busy = False

    # -- 启动 ---------------------------------------------------------------

    async def setup(self) -> None:
        # 注意：必须用 langchain_ollama 的 ChatOllama。
        # langchain_community 里有一个同名类，但它没有实现 bind_tools，
        # 会让 create_agent 抛 NotImplementedError（实测踩过）。
        from langchain.agents import create_agent
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_ollama import ChatOllama

        from config import settings

        problem = check_ollama()
        if problem:
            raise RuntimeError(problem)

        self.server.start()

        print("· 正在从 MCP 服务器加载工具…")
        client = MultiServerMCPClient(
            {"qqmail": {"url": MCP_URL, "transport": "streamable_http"}}
        )
        tools = await client.get_tools()
        if not tools:
            raise RuntimeError("MCP 服务器没有返回任何工具")
        self.tool_names = [t.name for t in tools]
        print("· 已加载 %d 个工具：%s" % (len(tools), "、".join(
            TOOL_LABELS.get(n, n) for n in self.tool_names)))

        model = ChatOllama(
            base_url=OLLAMA_BASE_URL, model=OLLAMA_MODEL, temperature=0.3
        )

        # 把附件目录的真实内容写进提示词，避免模型凭空拼出不存在的路径
        attach_dir = Path(settings.attachment_dir)
        files = list_attachments(attach_dir)
        attach_list = (
            "\n".join("  - %s" % n for n in files) if files else "  （目录为空）"
        )
        if files:
            print("· 附件目录可用文件：%s" % "、".join(files))

        self.agent = create_agent(
            model, tools, system_prompt=SYSTEM_PROMPT_TEMPLATE.format(
                email=settings.smtp_email,
                attach_dir=attach_dir,
                attach_list=attach_list,
            )
        )
        self.my_email = settings.smtp_email

    # -- 对话 ---------------------------------------------------------------

    async def ask(self, text: str) -> str:
        """
        处理一轮对话。

        关键：把完整历史交给智能体，这样「主题改成…」这类追问才接得住。
        """
        self.history.append(("user", text))
        self.history = self.history[-MAX_HISTORY_MESSAGES:]

        # 记住传入前的长度：ainvoke 返回的是**全部**消息（含历史），
        # 只处理其后的新消息，否则上一轮的工具调用会被重复打印，
        # 看起来就像「它用旧内容又发了一次」，实际并非如此。
        already_sent = len(self.history)

        started = time.monotonic()
        result = await self.agent.ainvoke({"messages": list(self.history)})
        messages = result["messages"]
        new_messages = messages[already_sent:]

        shown = 0
        for msg in new_messages:
            if _is_tool_call_message(msg):
                print_tool_calls(msg)
                shown += 1
            elif type(msg).__name__ == "ToolMessage":
                print_tool_result(msg)
                shown += 1

        reply = extract_reply(messages[-1]) if messages else ""
        if shown:
            print("  ⏱  %.1f 秒" % (time.monotonic() - started))

        # 累积历史，供下一轮使用
        self.history = messages[-MAX_HISTORY_MESSAGES:]
        return reply

    # -- 收尾 ---------------------------------------------------------------

    def shutdown(self) -> None:
        # 关闭 SMTP 连接池，避免残留连接与后台线程
        with contextlib.suppress(Exception):
            from email_tools import email_tools

            email_tools.sender.close()
        self.server.stop()


BANNER = """
╭──────────────────────────────────────────────╮
│  📬  邮件管家                                 │
╰──────────────────────────────────────────────╯
直接说人话就行，例如：
  给我自己发封邮件，说早上好
  给 zhangsan@example.com 发一封会议提醒
  主题改成明天下午三点

输入 exit / quit / 退出 结束
"""


async def chat(butler: EmailButler) -> None:
    print(BANNER)
    print("你好，我是小邮。发件邮箱是 %s。\n" % butler.my_email)

    while True:
        try:
            text = input("你 ▸ ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n再见。")
            return

        if text.lower() in ("exit", "quit", "退出", "q"):
            print("再见。")
            return
        if not text:
            continue

        print()
        try:
            reply = await butler.ask(text)
        except KeyboardInterrupt:
            print("  （已取消本轮）\n")
            continue
        except Exception as e:  # noqa: BLE001
            message = str(e)
            print("  ✗ 出错了：%s" % message[:200])
            if "connect" in message.lower() or "11434" in message:
                print("     提示：Ollama 似乎断开了，请确认 ollama serve 仍在运行。")
            print()
            continue

        if reply:
            print("\n小邮 ▸ %s\n" % reply.replace("\n", "\n      "))
        else:
            print("\n小邮 ▸ （没有返回内容）\n")


async def main() -> int:
    butler = EmailButler()
    try:
        await butler.setup()
    except Exception as e:  # noqa: BLE001
        print("\n✗ 启动失败：\n%s\n" % e)
        butler.shutdown()
        return 1

    try:
        await chat(butler)
    finally:
        butler.shutdown()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n再见。")
