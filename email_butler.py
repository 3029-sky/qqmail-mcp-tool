#!/usr/bin/env python
# email_butler.py - 邮件管家（终端界面）
"""
启动后直接跟它说话，它自己决定调用哪个工具。

    > 给我自己发封邮件，说早上好
    > 主题改成明天开会的时间
    > 算了，还是发给张三吧

本程序是项目的**终端入口**。网页界面见 webui.py，两者共用 butler_core.py：

    butler_core.py  —— 提示词、工具标签、MCP 子进程、会话与历史（两个界面共用）
    email_butler.py —— 终端：打印 ⚙ / ↩ / ⏱
    webui.py        —— 网页：把同样的事件推给浏览器

拆开的原因不是「分层好看」，而是避免两份实现各自漂移——
提示词或历史累积方式一旦复制成两份，迟早出现「终端里能接住
『主题改成…』，网页里接不住」这种只在一边复现的缺陷。

它解决的问题：

  1. **单入口**：自己管理 MCP 服务器子进程，不需要先开窗口跑 run_server.py。
     若端口上已有服务器在跑，会复用而不是重复启动。
  2. **多轮记忆**：每轮把完整对话历史交给智能体。
     若每轮只传当前一句，「主题改成XXX」这类追问完全接不住——
     它不知道你在说哪封邮件（这一点是实测出来的，不是推测）。
  3. **动作可见**：会把调用了哪个工具、参数是什么、返回什么打出来，
     而不是只给一句结论，便于判断它是真发了还是在敷衍。
  4. **缺依赖时讲人话**：Ollama 没起、模型没拉，会给出可直接照抄的命令。

前置条件：
  - Ollama 已启动，且已拉取模型（默认 qwen2.5:3b，可在 .env 中改）
  - MCP 服务器由本程序自动启动，无需手动操作

运行：
  python email_butler.py
"""

import asyncio
import subprocess          # noqa: F401 - butler_core 用它启动子进程；测试会 monkeypatch 本模块的同名属性
import sys
import time
from pathlib import Path
from typing import Any

from butler_core import (      # noqa: F401 - 一部分是给外部与测试用的再导出
    HEALTH_URL,
    MAX_HISTORY_MESSAGES,
    MCP_URL,
    MCPServerProcess,
    SYSTEM_PROMPT_TEMPLATE,
    TOOL_LABELS,
    AgentSession,
    check_ollama,
    extract_reply,
    is_server_up,
    list_attachments,
    tool_call_events,
    tool_result_event,
)
from clipboard import clipboard_summary, import_clipboard, is_supported as clipboard_supported
from config import settings

ROOT = Path(__file__).resolve().parent


def _make_output_resilient() -> None:
    """
    让输出遇到无法编码的字符时降级，而不是让整个程序崩掉。

    为什么需要：Windows 上 Python 的 stdout 编码取的是系统 ANSI 代码页
    （中文系统是 GBK），而本文件用了 emoji（📬 ⚙ ↩ ⏱）。
    GBK 编不出这些字符，`print` 会抛 UnicodeEncodeError —— 而且是
    在**打印启动横幅时**就抛，管家连界面都没出来就退出了。

    触发条件比想象中常见：把输出重定向到文件、管道给别的程序，
    或设置 PYTHONIOENCODING=gbk，都会走到这条路（实测过）。

    `reconfigure(errors="replace")` 保留原有编码（中文照常显示），
    只把编不出的字符换成 '?'，因此不会影响正常使用。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 - 拿不到就维持原样，不该阻断启动
            pass


_make_output_resilient()

MCP_HOST = "127.0.0.1"
MCP_PORT = 8000

#: Ollama 地址与模型名从配置读取（即 .env 生效）。
#: 早期版本把它们写死在这里，导致 MANUAL 里教的 `OLLAMA_MODEL=qwen2.5:7b` 根本不生效。
#: 这两个名字保留下来是因为测试与外部脚本会引用它们。
OLLAMA_BASE_URL = settings.ollama_base_url
OLLAMA_MODEL = settings.ollama_model


# ---------------------------------------------------------------------------
# 终端展示
# ---------------------------------------------------------------------------

def print_tool_calls(msg: Any) -> None:
    """打印智能体本轮决定调用的工具及其参数。"""
    for event in tool_call_events(msg):
        args = event["args"]
        print("  ⚙  %s" % event["label"])
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
    event = tool_result_event(msg)
    text = event["text"]
    if text:
        print("  ↩  %s" % (text[:150] + ("…" if len(text) > 150 else "")))

    substitutions = event["substitutions"]
    # LangGraph 不保证把结构化结果带出来，这里尽力而为：
    # 若返回文本里已经说明了修正，就不再重复
    if substitutions and "自动修正" not in text:
        print("  ℹ  附件已自动修正: %s" % substitutions)


# ---------------------------------------------------------------------------
# 终端会话
# ---------------------------------------------------------------------------

class EmailButler(AgentSession):
    """
    终端版的管家：在共用会话之上，把它的动作打印出来。

    行为（提示词、历史累积、工具调用时机）全部继承自 AgentSession，
    因此与网页版一致；这里只负责「怎么显示」。
    """

    async def setup(self) -> None:
        await self.start(on_step=lambda text: print("· %s" % text))

    async def ask(self, text: str) -> str:
        """处理一轮对话，并在终端打印它做了什么。"""
        started = time.monotonic()
        shown = 0

        def on_event(event):
            nonlocal shown
            if event["type"] == "tool_call":
                print_tool_calls(_EventMessage(event))
                shown += 1
            elif event["type"] == "tool_result":
                print_tool_result(_EventMessage(event))
                shown += 1

        reply = await super().ask(text, on_event=on_event)
        if shown:
            print("  ⏱  %.1f 秒" % (time.monotonic() - started))
        return reply


class _EventMessage:
    """
    把结构化事件包装成「像一条消息」的对象。

    这样 print_tool_calls / print_tool_result 只依赖工具函数，
    不必再各自解析一遍原始消息——两处显示逻辑因此共用同一份实现。
    """

    def __init__(self, event):
        self._event = event
        if event["type"] == "tool_call":
            self.tool_calls = [{
                "name": event["tool"],
                "args": event["args"],
            }]
            self.content = ""
            self.artifact = None
        else:
            self.tool_calls = None
            self.content = event["text"]
            self.artifact = event["substitutions"]


BANNER = """
╭──────────────────────────────────────────────╮
│  🕊  信使鸟                                    │
╰──────────────────────────────────────────────╯
直接说人话就行，例如：
  给我自己发封邮件，说早上好
  给 zhangsan@example.com 发一封会议提醒
  主题改成明天下午三点

要发图片或压缩包？先复制它们，再输入 /粘贴：
  截图后在对话里复制图片        -> /粘贴
  在资源管理器里复制文件/压缩包  -> /粘贴
然后说「把它发给我自己」即可。

输入 exit / quit / 退出 结束
"""


def handle_clipboard_paste() -> None:
    """
    处理 /粘贴：把剪贴板内容落地成附件，并打印结果。

    刻意不做成自动检测：自动导入意味着「你没打算发的东西也可能被带走」，
    而这个工具是真能往外发邮件的。让用户明确说一句，代价很小。

    注：网页界面不需要这条指令——浏览器原生支持粘贴图片与文件。
    """
    if not clipboard_supported():
        print("  剪贴板不可用（需要 Windows + pywin32）。\n")
        return

    print("  剪贴板里是：%s" % clipboard_summary())

    items, problems = import_clipboard(
        settings.attachment_dir,
        max_bytes=settings.max_attachment_bytes,
    )

    for problem in problems:
        print("  ⚠  %s" % problem)

    if not items:
        if not problems:
            print("  没有可导入的内容。")
        print("  提示：先在资源管理器里复制文件，或截图后复制图片，再输入 /粘贴。\n")
        return

    print("  ✅ 已放入附件目录：")
    for item in items:
        action = "已复制进来" if item.copied else "本来就在附件目录"
        print("     %s（%.1f KB，%s）" % (item.name, item.size / 1024, action))
    print()
    print("  现在可以说：「把 %s 发给我自己」" % items[0].name)
    print()


async def chat(butler: EmailButler) -> None:
    print(BANNER)
    print("你好，我是信使鸟。发件邮箱是 %s。\n" % butler.my_email)

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

        # /粘贴、/paste：把剪贴板里的图片或文件导入附件目录。
        # 这是本地操作，不经过模型，因此不消耗推理时间也不会被误解。
        if text.lower() in ("/粘贴", "/paste", "/p"):
            print()
            handle_clipboard_paste()
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
            print("\n信使鸟 ▸ %s\n" % reply.replace("\n", "\n      "))
        else:
            print("\n信使鸟 ▸ （没有返回内容）\n")


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
