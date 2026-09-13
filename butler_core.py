# butler_core.py - 管家后端（CLI 与 Web 界面共用）
"""
把「智能体 + MCP 工具 + 对话历史」这套后端从界面里抽出来。

抽出来的理由不是「分层好看」，而是**避免两份实现各自漂移**：
提示词、工具标签、历史累积方式这些东西一旦复制成两份，
迟早出现「终端里能接住『主题改成…』，网页里接不住」这种
只在一边复现的缺陷。

界面层各管各的：
  - email_butler.py  —— 终端：打印 ⚙ / ↩ / ⏱
  - webui.py         —— 网页：把同样的事件推给浏览器

这里只放两边都必须一致的东西。
"""

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from config import settings

__all__ = [
    "TOOL_LABELS",
    "SYSTEM_PROMPT_TEMPLATE",
    "MAX_HISTORY_MESSAGES",
    "MCP_URL",
    "HEALTH_URL",
    "check_ollama",
    "list_ollama_models",
    "is_server_up",
    "list_attachments",
    "extract_reply",
    "tool_call_events",
    "tool_result_event",
    "MCPServerProcess",
    "AgentSession",
    "ROOT",
]

ROOT = Path(__file__).resolve().parent

MCP_HOST = "127.0.0.1"
MCP_PORT = 8000
MCP_URL = "http://%s:%d/mcp" % (MCP_HOST, MCP_PORT)
HEALTH_URL = "http://%s:%d/health" % (MCP_HOST, MCP_PORT)

#: 交给智能体的历史消息上限，避免越聊越长拖慢每轮推理
MAX_HISTORY_MESSAGES = 24

#: 等待 MCP 服务器就绪的上限（秒）
SERVER_READY_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# 术语
# ---------------------------------------------------------------------------

#: 把工具名转成中文动作，让用户看懂它在做什么。
#: 网页界面也用这一份，保证两处说法一致。
TOOL_LABELS = {
    "send_text_email": "发送邮件",
    "send_html_email": "发送 HTML 邮件",
    "send_email_with_attachment": "发送带附件的邮件",
    "check_email_config": "检查邮箱配置",
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
7. 中文回答、简洁、口语化，不要罗列步骤。"""


# ---------------------------------------------------------------------------
# 环境探测
# ---------------------------------------------------------------------------

def list_ollama_models() -> List[str]:
    """
    列出 Ollama 里已安装的模型名。

    取不到（Ollama 没启动等）时返回空列表，由调用方决定怎么提示。
    """
    try:
        resp = httpx.get("%s/api/tags" % settings.ollama_base_url, timeout=5.0)
        resp.raise_for_status()
        return sorted(m.get("name", "") for m in resp.json().get("models", []) if m.get("name"))
    except Exception:  # noqa: BLE001
        return []


def check_ollama(model: Optional[str] = None) -> Optional[str]:
    """
    检查 Ollama 是否可用、指定模型是否已拉取。

    model 省略时用配置里的默认模型。
    返回 None 表示一切正常；否则返回一段可直接展示的提示。
    """
    wanted = model or settings.ollama_model

    try:
        resp = httpx.get("%s/api/tags" % settings.ollama_base_url, timeout=5.0)
        resp.raise_for_status()
        models = [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception:  # noqa: BLE001
        return (
            "无法连接到 Ollama。请先启动它：\n"
            "    ollama serve\n"
            "（Windows 上安装 Ollama 后通常已在后台运行，可先打开一次应用）"
        )

    # 模型名可能带 :latest 等标签，做宽松匹配
    base = wanted.split(":")[0]
    if not any(m == wanted or m.startswith(base) for m in models):
        return (
            "Ollama 中找不到模型 %s。可用的有：%s\n"
            "请先拉取：\n"
            "    ollama pull %s" % (wanted, ", ".join(models) or "（无）", wanted)
        )
    return None


def is_server_up() -> bool:
    try:
        return httpx.get(HEALTH_URL, timeout=2.0).status_code == 200
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 附件列举
# ---------------------------------------------------------------------------

def list_attachments(directory) -> List[str]:
    """
    列出附件目录中的文件名，供提示词使用。

    把真实文件名告诉模型，是避免它凭空拼出「示例报表.xlsx」这类
    并不存在的路径——那样会被附件校验拦下，用户白等一场。
    """
    try:
        directory = Path(directory)
        if not directory.is_dir():
            return []
        return sorted(p.name for p in directory.iterdir() if p.is_file())
    except Exception:  # noqa: BLE001 - 列目录失败不应影响启动
        return []


# ---------------------------------------------------------------------------
# 消息解析（两个界面共用）
# ---------------------------------------------------------------------------

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


def tool_call_events(msg: Any) -> List[Dict[str, Any]]:
    """
    把一条消息里的工具调用转成结构化事件。

    返回 [{"tool": 原名, "label": 中文名, "args": {...}}, ...]。
    两个界面都基于这份数据渲染——终端打文字，网页画卡片。
    """
    events = []
    for call in getattr(msg, "tool_calls", None) or []:
        name = call.get("name", "?")
        events.append({
            "tool": name,
            "label": TOOL_LABELS.get(name, name),
            "args": call.get("args") or {},
        })
    return events


def tool_result_event(msg: Any) -> Dict[str, Any]:
    """
    把一条工具返回消息转成结构化事件。

    `artifact` 里可能带有附件自动修正的信息；取不到就留空，
    由展示层决定是否提示。
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
    return {
        "text": text,
        "substitutions": getattr(msg, "artifact", None),
    }


def final_reply(messages: List[Any]) -> str:
    """
    从消息列表里取出**最终要展示给用户的回复**。

    为什么要往回找而不是直接取最后一条：主流框架在工具执行后会把
    ToolMessage 放在末尾（工具结果本身就是一条消息）。直接取末条
    会把工具返回值当成回答，用户看到的就成了「配置正常」这种
    工具原文，而不是模型组织过的话。

    规则：取最后一条**有文字的 AI 消息**；一条都没有就退回末条。
    """
    for msg in reversed(messages or []):
        if type(msg).__name__ != "AIMessage":
            continue
        text = extract_reply(msg)
        if text:
            return text
    if not messages:
        return ""
    return extract_reply(messages[-1])


def drain_events(msg: Any) -> List[Dict[str, Any]]:
    """
    把一条消息里值得展示的东西转成事件列表。

    工具调用与工具返回都会产出事件；普通 AI 文本不产出
    （它是回复本身，由界面直接显示）。
    """
    events: List[Dict[str, Any]] = []
    if is_tool_call_message(msg):
        for event in tool_call_events(msg):
            events.append(dict(event, type="tool_call"))
    elif is_tool_result_message(msg):
        events.append(dict(tool_result_event(msg), type="tool_result"))
    return events


def is_tool_call_message(msg: Any) -> bool:
    return bool(getattr(msg, "tool_calls", None))


def is_tool_result_message(msg: Any) -> bool:
    return type(msg).__name__ == "ToolMessage"


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
            print("· 检测到运行中的 MCP 服务器，直接复用")
            self.reused = True
            self.process = None
            return

        creationflags = 0
        if sys.platform == "win32":
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
# 会话
# ---------------------------------------------------------------------------

class AgentSession:
    """
    一次「管家会话」：持有 MCP 工具、智能体与对话历史。

    界面无关——终端和网页都用它，因此「主题改成…」这类多轮改写
    在两边行为一致。

    模型可在运行中切换（网页的模型下拉用得到）。
    """

    def __init__(self, model: Optional[str] = None, start_server: bool = True):
        self.model = model or settings.ollama_model
        self.agent = None
        self.tools: List[Any] = []
        self.tool_names: List[str] = []
        self.history: List[Any] = []
        self.my_email = settings.smtp_email
        self.server = MCPServerProcess()
        self._start_server = start_server

    # -- 启动与切换模型 -----------------------------------------------------

    async def start(self, on_step=None) -> None:
        """
        拉起 MCP 服务器并构造智能体。

        on_step(文字) 是可选的进度回调，让界面把「正在加载工具…」显示出来。
        """
        def step(text: str) -> None:
            if on_step:
                on_step(text)
            else:
                print("· %s" % text)

        if self._start_server:
            self.server.start()

        step("正在从 MCP 服务器加载工具…")
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient(
            {"qqmail": {"url": MCP_URL, "transport": "streamable_http"}}
        )
        self.tools = await client.get_tools()
        if not self.tools:
            raise RuntimeError("MCP 服务器没有返回任何工具")
        self.tool_names = [t.name for t in self.tools]
        step("已加载 %d 个工具：%s" % (
            len(self.tools), "、".join(TOOL_LABELS.get(n, n) for n in self.tool_names)))

        await self.use_model(self.model, on_step=on_step)

    async def use_model(self, model: str, on_step=None) -> None:
        """
        切换模型并重建智能体。

        为什么必须重建：模型是**构造智能体时**绑定的，
        换模型只能重新 create_agent。对话历史保留，所以切换后
        「主题改成…」这类追问仍然接得住。
        """
        problem = check_ollama(model)
        if problem:
            raise RuntimeError(problem)

        from langchain.agents import create_agent
        from langchain_ollama import ChatOllama

        chat = ChatOllama(
            base_url=settings.ollama_base_url, model=model, temperature=0.3
        )

        # 把附件目录的真实内容写进提示词，避免模型凭空拼出不存在的路径
        attach_dir = Path(settings.attachment_dir)
        files = list_attachments(attach_dir)
        attach_list = (
            "\n".join("  - %s" % n for n in files) if files else "  （目录为空）"
        )

        self.agent = create_agent(
            chat, self.tools, system_prompt=SYSTEM_PROMPT_TEMPLATE.format(
                email=settings.smtp_email,
                attach_dir=attach_dir,
                attach_list=attach_list,
            )
        )
        self.model = model
        if on_step:
            on_step("已切换到模型 %s" % model)

    # -- 对话 ---------------------------------------------------------------

    async def ask_stream(self, text: str):
        """
        处理一轮对话，边走边产出事件；最后一个事件是最终回复。

        产出的事件类型：
          {"type": "tool_call",   "tool", "label", "args"}
          {"type": "tool_result", "text", "substitutions"}
          {"type": "reply",       "text"}          <- 一定是最后一个

        为什么用流式：`astream(stream_mode="updates")` 在**每个节点结束时**
        就产出内容，因此「它决定调用哪个工具」能在工具真正执行前
        就显示出来。用 ainvoke 只能等整轮结束才拿到全部消息，
        界面会有十几秒毫无反馈——用户不知道它是在工作还是卡死了。

        关键：把完整历史交给智能体，这样「主题改成…」这类追问才接得住。
        """
        self.history.append(("user", text))
        self.history = self.history[-MAX_HISTORY_MESSAGES:]

        # 流式产出的是**新增**消息（每个节点结束时的结果），
        # 收集起来后接到已有历史后面即可。
        messages: List[Any] = []

        async for chunk in self.agent.astream(
            {"messages": list(self.history)}, stream_mode="updates"
        ):
            for node_output in (chunk or {}).values():
                for msg in (node_output or {}).get("messages", []) or []:
                    messages.append(msg)
                    for event in drain_events(msg):
                        yield event

        # 整段做一次截断，避免越聊越长拖慢每轮推理
        merged = list(self.history) + messages
        self.history = merged[-MAX_HISTORY_MESSAGES:]
        yield {"type": "reply", "text": final_reply(merged)}

    async def ask(self, text: str, on_event=None) -> str:
        """
        处理一轮对话，返回要展示的回复文字。

        on_event(dict) 是可选的事件回调（不含最终的 reply 事件），
        供界面实时显示它的动作。终端与网页都用这个接口，
        因此两边行为一致。
        """
        reply = ""
        async for event in self.ask_stream(text):
            if event["type"] == "reply":
                reply = event["text"]
            elif on_event is not None:
                on_event(event)
        return reply

    async def reset(self) -> None:
        """清空对话历史（网页上的「新对话」）。"""
        self.history = []

    # -- 收尾 ---------------------------------------------------------------

    def shutdown(self) -> None:
        """关闭 SMTP 连接池与 MCP 服务器，避免残留线程与子进程。"""
        try:
            from email_tools import email_tools

            email_tools.sender.close()
        except Exception:  # noqa: BLE001
            pass
        self.server.stop()
