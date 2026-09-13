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
    "SIGNATURE_NOTE",
    "build_system_prompt",
    "MAX_HISTORY_MESSAGES",
    "MCP_URL",
    "mcp_headers",
    "HEALTH_URL",
    "OLLAMA",
    "DEEPSEEK",
    "DEEPSEEK_MODELS",
    "make_ref",
    "parse_ref",
    "active_ref",
    "check_model",
    "build_chat_model",
    "deepseek_configured",
    "deepseek_dependency_ready",
    "deepseek_ready",
    "list_available_models",
    "check_ollama",
    "list_ollama_models",
    "is_server_up",
    "list_attachments",
    "extract_reply",
    "final_reply",
    "drain_events",
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


def mcp_headers() -> Dict[str, str]:
    """
    连接 MCP 服务器时要带的请求头。

    为什么必须有这个函数：服务端支持 Bearer 令牌鉴权，但客户端一直
    没把令牌发出去——于是「开启鉴权」就等于「管家自己先连不上」。
    结果只能是关掉鉴权，服务器便一直裸奔对外监听。
    正确的默认值被一个接线缺失挡住了，这里补上。

    未配置令牌时返回空字典，行为与之前完全一致。
    """
    token = (settings.mcp_auth_token or "").strip()
    if not token:
        return {}
    return {"Authorization": "Bearer %s" % token}

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

SYSTEM_PROMPT_TEMPLATE = """你是用户的邮件管家，名字叫「信使鸟」。你能调用工具帮用户发邮件。

当前用户的邮箱是：{email}
联系人名单（「发给名单里的人」时用这里的地址）：
{contact_list}
附件目录：{attach_dir}
附件目录里现有的文件：
{attach_list}
{signature_note}
最重要的一条：
用户说了要发邮件，你就必须**立即调用工具把它真正发出去**。
不允许先反问确认再等用户回复——用户已经说清楚了就直接做。
（实测发现：只要提示词里留了「可以先问一下」的余地，小模型就会一直反问，
 结果一封邮件都发不出去。）

关于联系人：
- 用户说的人名如果在上面名单里，就用名单里的邮箱地址，**不要自己编**。
- 名单里没有这个人时，请照第 5 条处理（询问），不要猜地址。

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

#: 签名已经由发送层自动追加时，加进提示词的一段说明。
#: 不告诉模型的话，它常会自己再写一遍签名，用户就收到两个。
SIGNATURE_NOTE = """
关于签名：
- 签名已经由系统自动加到正文末尾，**你绝对不要在 body 里再写签名**。
- 也不要在回答里说「已加上签名」之外多余的话。
"""


def build_system_prompt(email: str, attach_dir, attach_list: List[str]) -> str:
    """
    组装系统提示词。

    联系人名单与签名说明都从这里注入——提示词的每一段都有对应测试，
    单独一个函数便于断言「该有的约束都在」。
    """
    from userdata import userdata

    contacts = userdata.load()["contacts"]
    if contacts:
        lines = []
        for item in contacts[:200]:        # 名单过长会挤占上下文，截断
            note = ("（%s）" % item["note"]) if item.get("note") else ""
            lines.append("  - %s：%s%s" % (item["name"], item["email"], note))
        contact_list = "\n".join(lines)
    else:
        contact_list = "  （还没有保存联系人）"

    signature = userdata.get_signature().strip()
    return SYSTEM_PROMPT_TEMPLATE.format(
        email=email,
        contact_list=contact_list,
        attach_dir=attach_dir,
        attach_list=attach_list,
        signature_note=SIGNATURE_NOTE if signature else "",
    )


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


# ---------------------------------------------------------------------------
# 模型引用：provider:model
# ---------------------------------------------------------------------------

OLLAMA = "ollama"
DEEPSEEK = "deepseek"

#: DeepSeek 用 OpenAI 兼容接口，这些是它当前提供的对话模型。
#: 不写死成「只认某一个」，是为了官方上新模型时不必改代码。
DEEPSEEK_MODELS = ["deepseek-chat", "deepseek-reasoner"]


def make_ref(provider: str, model: str) -> str:
    """把 provider 与模型名拼成引用串，例如 ollama:qwen2.5:3b。"""
    return "%s:%s" % (provider, model)


def parse_ref(ref: str) -> tuple:
    """
    解析模型引用，返回 (provider, model_name)。

    格式为 `provider:model`，其中 model 本身可以带冒号
    （Ollama 的标签就是这样，例如 `qwen2.5:3b`），
    所以这里只按**第一个**冒号切分。

    不带 provider 前缀时按 Ollama 处理——这样旧写法
    （`.env` 里只写 `OLLAMA_MODEL=qwen2.5:3b`）仍然可用。
    """
    ref = (ref or "").strip()
    if not ref:
        return OLLAMA, settings.ollama_model
    if ":" not in ref:
        return OLLAMA, ref
    head, rest = ref.split(":", 1)
    if head in (OLLAMA, DEEPSEEK):
        return head, rest
    return OLLAMA, ref


def deepseek_configured() -> bool:
    """是否配好了 DeepSeek 的 API Key。"""
    return bool((settings.deepseek_api_key or "").strip())


def deepseek_dependency_ready() -> bool:
    """
    是否装了 `langchain-openai`。

    单独检测它，是为了把「缺依赖」这件事提前暴露在界面上：
    否则用户会先把 API Key 配好、以为万事俱备，选中后才收到一句错误。
    能提前说清楚的事，不该等到操作失败才说。
    """
    import importlib.util  # noqa: PLC0415

    try:
        return importlib.util.find_spec("langchain_openai") is not None
    except (ImportError, ValueError):
        return False


def deepseek_ready() -> bool:
    """DeepSeek 是否可用：既要 Key，也要依赖。"""
    return deepseek_configured() and deepseek_dependency_ready()


def active_ref() -> str:
    """
    当前模型引用。

    优先用 `ACTIVE_MODEL`（界面切模型时写入），
    没设过就退回本地默认模型 `OLLAMA_MODEL`。
    """
    configured = (settings.active_model or "").strip()
    if configured:
        return configured
    return make_ref(OLLAMA, settings.ollama_model)


def check_model(ref: str) -> Optional[str]:
    """
    校验某个模型引用是否可用。

    返回 None 表示可用；否则返回一段可直接展示给用户的说明。
    """
    provider, name = parse_ref(ref)

    if provider == DEEPSEEK:
        if not deepseek_configured():
            return (
                "还没有配置 DeepSeek API Key。\n"
                "请在「设置」里填入 DEEPSEEK_API_KEY（在 platform.deepseek.com 申请），"
                "或改回本地模型。"
            )
        if not deepseek_dependency_ready():
            return (
                "使用 DeepSeek 还需要安装一个依赖（只需装一次）：\n"
                "    .\\venv\\Scripts\\python.exe -m pip install langchain-openai\n"
                "装完后重启应用。本地 Ollama 模型不需要它。"
            )
        return None

    return check_ollama(name)


def build_chat_model(ref: str):
    """
    按引用构造可供 create_agent 使用的对话模型。

    必须用 langchain_ollama / langchain_openai 的类：
    langchain_community 里的同名 ChatOllama 没有实现 bind_tools，
    会让 create_agent 抛 NotImplementedError（实测踩过）。
    """
    provider, name = parse_ref(ref)

    if provider == DEEPSEEK:
        try:
            from langchain_openai import ChatOpenAI   # noqa: PLC0415
        except ImportError as e:  # noqa: BLE001
            raise RuntimeError(
                "使用 DeepSeek 需要先安装 langchain-openai：\n"
                "    .\\venv\\Scripts\\python.exe -m pip install langchain-openai\n"
                "（本地 Ollama 模型不需要它）"
            ) from e

        return ChatOpenAI(
            model=name,
            base_url=settings.deepseek_base_url,
            api_key=(settings.deepseek_api_key or "").strip(),
            temperature=0.3,
        )

    from langchain_ollama import ChatOllama       # noqa: PLC0415

    return ChatOllama(
        base_url=settings.ollama_base_url, model=name, temperature=0.3,
    )


def list_available_models() -> List[Dict[str, Any]]:
    """
    列出界面上可选的模型，含本地的与云端的。

    每个条目带 `available` 与 `reason`，让界面能直接说明
    「为什么这个选项不能选」——比只显示一个灰掉的项有用得多。
    """
    items: List[Dict[str, Any]] = []

    for name in list_ollama_models():
        items.append({
            "ref": make_ref(OLLAMA, name),
            "provider": OLLAMA,
            "name": name,
            "label": "%s（本地）" % name,
            "available": True,
            "reason": "",
        })

    configured = deepseek_configured()
    dependency = deepseek_dependency_ready()

    for name in DEEPSEEK_MODELS:
        if not configured:
            available, reason = False, "需要先填 DeepSeek API Key"
        elif not dependency:
            available, reason = False, "需要安装 langchain-openai（见应用内提示）"
        else:
            available, reason = True, ""
        items.append({
            "ref": make_ref(DEEPSEEK, name),
            "provider": DEEPSEEK,
            "name": name,
            "label": "%s（DeepSeek 云端）" % name,
            "available": available,
            "reason": reason,
        })

    return items


def check_ollama(model: Optional[str] = None) -> Optional[str]:
    """
    检查 Ollama 是否可用、指定模型是否已拉取。

    model 省略时用配置里的默认本地模型。
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
        #: 模型引用（provider:model）。省略时用配置里的当前选择。
        self.model = model or active_ref()
        self.provider, self.model_name = parse_ref(self.model)
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

        client = MultiServerMCPClient({
            "qqmail": {
                "url": MCP_URL,
                "transport": "streamable_http",
                "headers": mcp_headers(),
            }
        })
        self.tools = await client.get_tools()
        if not self.tools:
            raise RuntimeError("MCP 服务器没有返回任何工具")
        self.tool_names = [t.name for t in self.tools]
        step("已加载 %d 个工具：%s" % (
            len(self.tools), "、".join(TOOL_LABELS.get(n, n) for n in self.tool_names)))

        await self.use_model(self.model, on_step=on_step)

    async def use_model(self, ref: str, on_step=None) -> None:
        """
        切换模型并重建智能体。

        ref 是模型引用（`ollama:qwen2.5:3b` 或 `deepseek:deepseek-chat`）。
        不带 provider 前缀时按 Ollama 处理，因此旧的纯模型名写法仍然可用。

        为什么必须重建：模型是**构造智能体时**绑定的，
        换模型只能重新 create_agent。对话历史保留，所以切换后
        「主题改成…」这类追问仍然接得住。
        """
        problem = check_model(ref)
        if problem:
            raise RuntimeError(problem)

        from langchain.agents import create_agent

        chat = build_chat_model(ref)
        provider, name = parse_ref(ref)

        # 换模型时一并重建提示词：附件目录、联系人、签名都可能已经变了。
        attach_dir = Path(settings.attachment_dir)
        files = list_attachments(attach_dir)

        self.agent = create_agent(
            chat, self.tools,
            system_prompt=build_system_prompt(
                email=settings.smtp_email,
                attach_dir=attach_dir,
                attach_list=files,
            ),
        )
        self.model = ref
        self.provider = provider
        self.model_name = name
        if on_step:
            on_step("已切换到模型 %s" % name)

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
