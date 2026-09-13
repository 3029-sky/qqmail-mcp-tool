# tests/test_butler_core.py - 共用后端的单元测试
"""
butler_core 是终端版与网页版**共用**的后端，所以它的行为必须两边一致。
这里覆盖它独有的、界面无关的部分：

  - final_reply()：从消息列表里挑出真正的回复（不是工具返回值）
  - drain_events()：把消息转成界面事件
  - AgentSession.ask_stream()：流式产出事件与历史累积
  - list_ollama_models()：模型列表

不需要 Ollama、不连网、不发信：智能体一律用替身。
"""

import pytest

from butler_core import (
    AgentSession,
    drain_events,
    extract_reply,
    final_reply,
    list_ollama_models,
    tool_call_events,
    tool_result_event,
)


# ---------------------------------------------------------------------------
# 测试替身：类名必须与真实消息一致
# ---------------------------------------------------------------------------

class AIMessage:
    """
    假 AI 消息。

    刻意用**真实类名**：butler_core 是按 `type(msg).__name__` 区分
    工具调用、工具返回与最终回复的，用裸类名会让判断全部走空。
    """

    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.artifact = None


class ToolMessage:
    def __init__(self, text):
        self.content = [{"type": "text", "text": text}]
        self.tool_calls = []
        self.artifact = None


def ai(content="", tool_calls=None):
    return AIMessage(content, tool_calls)


def tool(text):
    return ToolMessage(text)


class FakeAgent:
    """按脚本逐条产出消息，模拟 astream(stream_mode="updates")。"""

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    async def astream(self, payload, stream_mode=None):
        self.seen.append(list(payload["messages"]))
        for msg in self.turns.pop(0):
            yield {"model": {"messages": [msg]}}


def make_session(turns):
    s = AgentSession()
    s.agent = FakeAgent(turns)
    return s


# ---------------------------------------------------------------------------
# final_reply：不要取错消息
# ---------------------------------------------------------------------------

def test_final_reply_returns_last_ai_text():
    messages = [
        ai(tool_calls=[{"name": "check_email_config", "args": {}}]),
        tool("✅ 配置正常"),
        ai("你的邮箱配置正常。"),
    ]
    assert final_reply(messages) == "你的邮箱配置正常。"


def test_final_reply_skips_trailing_tool_message():
    """
    回归测试：工具返回也是一条消息，且常常排在最后。

    若直接取末条，用户看到的会是工具原文（「✅ 配置正常」）而不是
    模型组织过的话。真实 LangGraph 在工具执行后就会把 ToolMessage
    放在末尾，所以这条路径一定会走到。
    """
    messages = [ai("好的"), tool("✅ 邮件发送成功")]
    assert final_reply(messages) == "好的", "不应把工具返回值当成回答"


def test_final_reply_returns_empty_for_no_messages():
    assert final_reply([]) == ""


def test_final_reply_falls_back_when_no_ai_text():
    """只有工具消息时退回末条，总比什么都不显示好。"""
    assert final_reply([tool("只有工具结果")]) == "只有工具结果"


def test_final_reply_ignores_empty_ai_message():
    """模型有时产出空内容的 AI 消息（实测过），不能把它当成回答。"""
    messages = [ai("真正的回答"), ai("")]
    assert final_reply(messages) == "真正的回答"


# ---------------------------------------------------------------------------
# drain_events / tool_*：消息 -> 界面事件
# ---------------------------------------------------------------------------

def test_drain_events_emits_tool_call():
    msg = ai(tool_calls=[{"name": "send_text_email", "args": {"subject": "主题"}}])
    events = drain_events(msg)
    assert len(events) == 1
    assert events[0]["type"] == "tool_call"
    assert events[0]["tool"] == "send_text_email"
    assert events[0]["label"] == "发送邮件", "应翻译成中文动作"
    assert events[0]["args"]["subject"] == "主题"


def test_drain_events_emits_tool_result():
    events = drain_events(tool("✅ 已发送"))
    assert len(events) == 1
    assert events[0]["type"] == "tool_result"
    assert events[0]["text"] == "✅ 已发送"


def test_drain_events_ignores_plain_ai_message():
    """普通文字就是回复本身，不该再产出事件，否则界面会重复显示。"""
    assert drain_events(ai("这是一句话")) == []


def test_tool_call_events_keeps_unknown_tool_name():
    """新加了工具但还没加中文标签时，应显示原名而不是崩掉。"""
    events = tool_call_events(ai(tool_calls=[{"name": "brand_new_tool", "args": {}}]))
    assert events[0]["label"] == "brand_new_tool"


def test_tool_result_event_flattens_text_blocks():
    """工具返回常是 [{'type':'text','text':...}]，要抽成可读文本。"""
    event = tool_result_event(tool("第一段"))
    assert event["text"] == "第一段"


def test_tool_result_event_tolerates_plain_string():
    msg = ai("纯字符串内容")
    assert tool_result_event(msg)["text"] == "纯字符串内容"


# ---------------------------------------------------------------------------
# ask_stream：流式事件与历史累积
# ---------------------------------------------------------------------------

async def test_ask_stream_yields_tool_call_then_result_then_reply():
    """
    事件顺序必须是「决定调用 -> 执行结果 -> 回复」。

    这个顺序是界面能实时显示动作的前提：只要 astream 产出
    tool_call 事件，网页就能在工具真正执行前把卡片画出来。
    """
    s = make_session([[
        ai(tool_calls=[{"name": "check_email_config", "args": {}}]),
        tool("✅ 配置正常"),
        ai("配置没问题。"),
    ]])

    events = [ev async for ev in s.ask_stream("检查配置")]
    assert [ev["type"] for ev in events] == ["tool_call", "tool_result", "reply"]
    assert events[-1]["text"] == "配置没问题。", "最后一个事件必须是回复"


async def test_ask_stream_reply_is_always_last():
    s = make_session([[ai("直接回答，没有调用工具")]])
    events = [ev async for ev in s.ask_stream("你好")]
    assert events[-1]["type"] == "reply"


async def test_ask_returns_reply_and_reports_events():
    """ask() 是 ask_stream() 的包装，回调只该收到动作事件。"""
    s = make_session([[
        ai(tool_calls=[{"name": "send_text_email", "args": {"subject": "早"}}]),
        tool("✅ 邮件发送成功"),
        ai("已发送。"),
    ]])

    seen = []
    reply = await s.ask("发封邮件", on_event=seen.append)

    assert reply == "已发送。"
    assert [e["type"] for e in seen] == ["tool_call", "tool_result"], "不应把 reply 也回调出去"


async def test_ask_accumulates_history_across_turns():
    """
    回归测试：历史必须累积，且**不能重复**。

    「主题改成…」这类追问完全依赖历史——每轮只传当前一句就接不住。
    """
    s = make_session([
        [ai(tool_calls=[{"name": "send_text_email", "args": {"subject": "A"}}]),
         tool("✅ 发送成功"), ai("已发送 A")],
        [ai(tool_calls=[{"name": "send_text_email", "args": {"subject": "B"}}]),
         tool("✅ 发送成功"), ai("已发送 B")],
    ])

    await s.ask("发封邮件主题 A")
    first_len = len(s.history)
    assert first_len == 4, "用户消息 + 3 条新增"

    await s.ask("主题改成 B")
    assert len(s.history) == 8, "第二轮应再接 4 条，不能重复前面的"

    # 第二轮必须看到第一轮的内容
    blob = " ".join(str(getattr(m, "content", m)) for m in s.agent.seen[1])
    assert "已发送 A" in blob, "第二轮的历史里应含第一轮的回复"


async def test_ask_passes_full_history_each_turn():
    s = make_session([
        [ai("第一次回答")],
        [ai("第二次回答")],
    ])
    await s.ask("第一句")
    assert len(s.agent.seen[0]) == 1, "首轮只有当前这一句"
    await s.ask("第二句")
    assert len(s.agent.seen[1]) > 1, "第二轮必须带上历史"


async def test_history_is_bounded():
    """历史不能无限增长，否则每轮推理越来越慢。"""
    from butler_core import MAX_HISTORY_MESSAGES

    s = make_session([[ai("回答 %d" % i)] for i in range(MAX_HISTORY_MESSAGES + 5)])
    for i in range(MAX_HISTORY_MESSAGES + 5):
        await s.ask("第 %d 句" % i)
    assert len(s.history) <= MAX_HISTORY_MESSAGES


async def test_reset_clears_history():
    s = make_session([[ai("回答")]])
    await s.ask("一句话")
    assert s.history
    await s.reset()
    assert s.history == []


# ---------------------------------------------------------------------------
# 模型列表
# ---------------------------------------------------------------------------

def test_list_ollama_models_returns_sorted_names(monkeypatch):
    import butler_core
    import httpx

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "qwen2.5:3b"}, {"name": "gemma3:1b"}]}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Resp())
    assert list_ollama_models() == ["gemma3:1b", "qwen2.5:3b"]


def test_list_ollama_models_returns_empty_when_unreachable(monkeypatch):
    """Ollama 没启动时应返回空列表，而不是抛异常——界面靠它决定怎么提示。"""
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", boom)
    assert list_ollama_models() == []


def test_check_ollama_accepts_explicit_model(monkeypatch):
    """切换模型时要能校验**指定**的模型，而不只是配置里的默认值。"""
    import butler_core
    import httpx

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "gemma3:1b"}]}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Resp())

    assert butler_core.check_ollama("gemma3:1b") is None
    problem = butler_core.check_ollama("qwen2.5:7b")
    assert problem is not None and "qwen2.5:7b" in problem


# ---------------------------------------------------------------------------
# MCP 鉴权：客户端必须把令牌发出去
# ---------------------------------------------------------------------------

def test_mcp_headers_empty_without_token(monkeypatch):
    """没配令牌时不带任何头，行为与以前一致。"""
    import butler_core
    from config import settings

    monkeypatch.setattr(settings, "mcp_auth_token", None)
    assert butler_core.mcp_headers() == {}


def test_mcp_headers_blank_token_treated_as_unset(monkeypatch):
    """`.env` 里写成 `MCP_AUTH_TOKEN=   ` 应视为未配置，而不是发一个空令牌。"""
    import butler_core
    from config import settings

    monkeypatch.setattr(settings, "mcp_auth_token", "   ")
    assert butler_core.mcp_headers() == {}


def test_mcp_headers_sends_bearer_token(monkeypatch):
    """
    回归测试：配了令牌就必须带上 Authorization 头。

    这条曾经缺失，后果不小：服务端支持 Bearer 鉴权，但客户端从不发令牌——
    于是「开启鉴权」等于「管家自己先连不上」。用户只能关掉鉴权，
    服务器就一直 0.0.0.0 对外监听。一个正确的安全默认值
    被一处接线缺失挡住了。
    """
    import butler_core
    from config import settings

    monkeypatch.setattr(settings, "mcp_auth_token", "s3cret")
    assert butler_core.mcp_headers() == {"Authorization": "Bearer s3cret"}


def test_mcp_client_receives_headers(monkeypatch):
    """真正传给 MCP 客户端的配置里必须包含 headers 字段。"""
    import asyncio

    import butler_core
    from config import settings

    monkeypatch.setattr(settings, "mcp_auth_token", "tok-123")
    captured = {}

    class FakeClient:
        def __init__(self, connections):
            captured["connections"] = connections

        async def get_tools(self):
            class T:
                name = "t"
            return [T()]

    import langchain_mcp_adapters.client as client_module
    monkeypatch.setattr(client_module, "MultiServerMCPClient", FakeClient)

    session = butler_core.AgentSession(start_server=False)

    async def fake_use_model(ref, on_step=None):
        session.model = ref

    monkeypatch.setattr(session, "use_model", fake_use_model)
    asyncio.run(session.start(on_step=lambda _t: None))

    conn = captured["connections"]["qqmail"]
    assert conn["headers"] == {"Authorization": "Bearer tok-123"}, (
        "MCP 客户端配置里必须带上鉴权头，否则开启鉴权后管家连不上"
    )


# ---------------------------------------------------------------------------
# 模型引用 provider:model
# ---------------------------------------------------------------------------

def test_parse_ref_splits_on_first_colon():
    """
    Ollama 的模型名自带冒号标签（qwen2.5:3b），
    所以只能按**第一个**冒号切分，否则模型名会被切碎。
    """
    import butler_core

    assert butler_core.parse_ref("ollama:qwen2.5:3b") == ("ollama", "qwen2.5:3b")
    assert butler_core.parse_ref("deepseek:deepseek-chat") == ("deepseek", "deepseek-chat")


def test_parse_ref_defaults_to_ollama_without_prefix():
    """
    不带 provider 前缀时按 Ollama 处理。

    这样旧写法（.env 里只写 `OLLAMA_MODEL=qwen2.5:3b`）仍然可用，
    不会因为引入 provider 概念就把老配置作废。
    """
    import butler_core

    assert butler_core.parse_ref("qwen2.5:3b") == ("ollama", "qwen2.5:3b")
    assert butler_core.parse_ref("llama3") == ("ollama", "llama3")


def test_parse_ref_handles_empty():
    import butler_core

    provider, name = butler_core.parse_ref("")
    assert provider == "ollama"
    assert name, "空引用应回退到配置里的默认模型"


def test_make_ref_roundtrips():
    import butler_core

    ref = butler_core.make_ref("ollama", "qwen2.5:3b")
    assert ref == "ollama:qwen2.5:3b"
    assert butler_core.parse_ref(ref) == ("ollama", "qwen2.5:3b")


def test_active_ref_uses_configured_selection(monkeypatch):
    """界面选过模型就记住它，下次启动仍是那个。"""
    import butler_core
    from config import settings

    monkeypatch.setattr(settings, "active_model", "deepseek:deepseek-chat")
    assert butler_core.active_ref() == "deepseek:deepseek-chat"

    monkeypatch.setattr(settings, "active_model", None)
    monkeypatch.setattr(settings, "ollama_model", "qwen2.5:3b")
    assert butler_core.active_ref() == "ollama:qwen2.5:3b"


def test_check_model_requires_deepseek_key(monkeypatch):
    """没配 Key 时选 DeepSeek 要给出可操作的提示，而不是让它跑到报错。"""
    import butler_core
    from config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", None)
    problem = butler_core.check_model("deepseek:deepseek-chat")

    assert problem is not None
    assert "DEEPSEEK_API_KEY" in problem, "要指出该填哪个配置项"


def test_check_model_accepts_deepseek_with_key(monkeypatch):
    import butler_core
    from config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(butler_core, "deepseek_dependency_ready", lambda: True)
    assert butler_core.check_model("deepseek:deepseek-chat") is None


def test_check_model_reports_missing_dependency_with_install_command(monkeypatch):
    """
    配了 Key 但没装 langchain-openai 时，提示必须给出可照抄的安装命令。

    这条踩过：用户配好 Key 后选中 DeepSeek，只收到一句
    「切换模型失败」，完全不知道要装东西。
    """
    import butler_core
    from config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(butler_core, "deepseek_dependency_ready", lambda: False)

    problem = butler_core.check_model("deepseek:deepseek-chat")
    assert problem is not None
    assert "pip install langchain-openai" in problem, "要能直接照抄"


def test_deepseek_dependency_ready_reflects_reality():
    """当前环境确实装了它（本仓库把它列进了 requirements）。"""
    import butler_core

    assert butler_core.deepseek_dependency_ready() is True


def test_list_available_models_gates_on_dependency(monkeypatch):
    """
    缺依赖时 DeepSeek 选项要**提前标成不可用**。

    否则用户会先配好 Key、以为万事俱备，选中后才收到错误。
    能提前说清楚的事，不该等到操作失败才说。
    """
    import butler_core
    from config import settings

    monkeypatch.setattr(butler_core, "list_ollama_models", lambda: [])
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(butler_core, "deepseek_dependency_ready", lambda: False)

    deepseek = [m for m in butler_core.list_available_models()
                if m["provider"] == "deepseek"]
    assert deepseek
    assert all(not m["available"] for m in deepseek)
    assert all("langchain-openai" in m["reason"] for m in deepseek)


def test_list_available_models_marks_deepseek_ready(monkeypatch):
    import butler_core
    from config import settings

    monkeypatch.setattr(butler_core, "list_ollama_models", lambda: [])
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(butler_core, "deepseek_dependency_ready", lambda: True)

    deepseek = [m for m in butler_core.list_available_models()
                if m["provider"] == "deepseek"]
    assert deepseek and all(m["available"] for m in deepseek)


def test_list_available_models_includes_deepseek_always(monkeypatch):
    """
    DeepSeek 选项**始终**列出，没配 Key 时标成不可用并说明原因。

    藏起来的话用户根本不知道有这个能力。
    """
    import butler_core
    from config import settings

    monkeypatch.setattr(butler_core, "list_ollama_models", lambda: ["qwen2.5:3b"])
    monkeypatch.setattr(settings, "deepseek_api_key", None)

    items = butler_core.list_available_models()
    by_ref = {m["ref"]: m for m in items}

    assert by_ref["ollama:qwen2.5:3b"]["available"] is True
    deepseek = [m for m in items if m["provider"] == "deepseek"]
    assert deepseek
    assert all(not m["available"] for m in deepseek)
    assert all("API Key" in m["reason"] for m in deepseek)


def test_build_chat_model_makes_ollama():
    import butler_core

    model = butler_core.build_chat_model("ollama:qwen2.5:3b")
    assert type(model).__name__ == "ChatOllama"
    assert model.model == "qwen2.5:3b"


def test_build_chat_model_explains_missing_deepseek_dependency(monkeypatch):
    """
    没装 langchain-openai 时，提示要能直接照抄安装命令。

    否则用户只会看到一句 ModuleNotFoundError。
    """
    import builtins

    import butler_core
    from config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "langchain_openai":
            raise ImportError("No module named 'langchain_openai'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError) as e:
        butler_core.build_chat_model("deepseek:deepseek-chat")
    assert "pip install langchain-openai" in str(e.value)
