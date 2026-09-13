# tests/test_email_butler.py - 邮件管家的可测单元
"""
email_butler.py 是日常唯一入口，此前完全没有测试——
改坏了只能靠手动跑一遍看输出，而它的显示逻辑恰恰出过两个真实缺陷：

  1. 遍历了 ainvoke 返回的**全部**消息（含历史），把上一轮的工具调用又印一遍，
     看起来像「说了改却用旧内容重发」。
  2. 附件路径被自动修正后，显示的还是模型猜错的名字，用户以为发错了文件。

本文件覆盖这些纯逻辑，不需要 Ollama，也不连网。
"""

import httpx
import pytest

from email_butler import (
    MCPServerProcess,
    check_ollama,
    extract_reply,
    is_server_up,
    list_attachments,
    print_tool_calls,
    print_tool_result,
)


# ---------------------------------------------------------------------------
# 附件目录列举
# ---------------------------------------------------------------------------

def test_list_attachments_returns_sorted_names(tmp_path):
    for name in ("b.txt", "a.txt", "c.csv"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert list_attachments(tmp_path) == ["a.txt", "b.txt", "c.csv"]


def test_list_attachments_ignores_subdirectories(tmp_path):
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    (tmp_path / "subdir").mkdir()
    assert list_attachments(tmp_path) == ["file.txt"]


def test_list_attachments_missing_directory_returns_empty(tmp_path):
    assert list_attachments(tmp_path / "不存在") == []


def test_list_attachments_empty_directory(tmp_path):
    assert list_attachments(tmp_path) == []


def test_list_attachments_survives_permission_error(tmp_path, monkeypatch):
    """列目录失败不应让管家启动不了。"""
    import email_butler

    monkeypatch.setattr(
        "pathlib.Path.iterdir",
        lambda self: (_ for _ in ()).throw(PermissionError("denied")),
    )
    assert list_attachments(tmp_path) == []


# ---------------------------------------------------------------------------
# Ollama 可用性检查
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


def test_check_ollama_ok(monkeypatch):
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _Resp({"models": [{"name": "qwen2.5:3b"}]}),
    )
    assert check_ollama() is None


def test_check_ollama_accepts_tagged_model_name(monkeypatch):
    """模型名可能带 :latest 等标签，应做宽松匹配。"""
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _Resp({"models": [{"name": "qwen2.5:3b-instruct-q4"}]}),
    )
    assert check_ollama() is None


def test_check_ollama_not_running(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", boom)
    problem = check_ollama()
    assert problem is not None
    assert "ollama serve" in problem, "应给出可直接照抄的命令"


def test_check_ollama_model_missing(monkeypatch):
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _Resp({"models": [{"name": "llama3:8b"}]}),
    )
    problem = check_ollama()
    assert problem is not None
    assert "ollama pull" in problem
    assert "llama3:8b" in problem, "应列出实际可用的模型"


def test_check_ollama_no_models_at_all(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp({"models": []}))
    problem = check_ollama()
    assert problem is not None
    assert "（无）" in problem


# ---------------------------------------------------------------------------
# 服务器探活
# ---------------------------------------------------------------------------

def test_is_server_up_true(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp({}, status=200))
    assert is_server_up() is True


def test_is_server_up_false_on_error_status(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp({}, status=503))
    assert is_server_up() is False


def test_is_server_up_false_on_exception(monkeypatch):
    def boom(*a, **k):
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(httpx, "get", boom)
    assert is_server_up() is False


# ---------------------------------------------------------------------------
# 服务器进程管理
# ---------------------------------------------------------------------------

def test_server_process_reuses_running_server(monkeypatch):
    """
    端口上已有服务器时应直接复用，不要重复启动——
    否则会起第二个进程并因端口占用而崩溃。
    """
    import email_butler

    monkeypatch.setattr(email_butler, "is_server_up", lambda: True)
    called = {"popen": 0}
    monkeypatch.setattr(
        email_butler.subprocess, "Popen",
        lambda *a, **k: called.__setitem__("popen", called["popen"] + 1),
    )

    proc = MCPServerProcess()
    proc.start()

    assert proc.reused is True
    assert called["popen"] == 0, "不应启动新进程"
    assert proc.process is None


def test_server_process_stop_is_noop_when_reused(monkeypatch):
    """复用的服务器不归我们管，stop() 不能把它关掉。"""
    import email_butler

    monkeypatch.setattr(email_butler, "is_server_up", lambda: True)
    proc = MCPServerProcess()
    proc.start()
    proc.stop()  # 不应抛异常，也不应有副作用
    assert proc.process is None


def test_server_process_raises_if_child_exits_immediately(monkeypatch):
    import email_butler

    class DeadProc:
        def poll(self):
            return 1  # 已退出

    monkeypatch.setattr(email_butler, "is_server_up", lambda: False)
    monkeypatch.setattr(email_butler.subprocess, "Popen", lambda *a, **k: DeadProc())

    proc = MCPServerProcess()
    with pytest.raises(RuntimeError) as e:
        proc.start()
    assert "立即退出" in str(e.value)


def test_server_process_times_out(monkeypatch):
    import email_butler

    class SlowProc:
        def poll(self):
            return None  # 一直在跑但没就绪

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(email_butler, "is_server_up", lambda: False)
    monkeypatch.setattr(email_butler.subprocess, "Popen", lambda *a, **k: SlowProc())
    monkeypatch.setattr(email_butler, "SERVER_READY_TIMEOUT", 0.3)
    monkeypatch.setattr(email_butler.time, "sleep", lambda _s: None)

    proc = MCPServerProcess()
    with pytest.raises(RuntimeError) as e:
        proc.start()
    assert "超时" in str(e.value)


# ---------------------------------------------------------------------------
# 回复文本提取
# ---------------------------------------------------------------------------

class _Msg:
    def __init__(self, content):
        self.content = content


def test_extract_reply_from_plain_string():
    assert extract_reply(_Msg("你好")) == "你好"


def test_extract_reply_from_content_blocks():
    """新版 LangChain 可能返回内容块列表。"""
    msg = _Msg([
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"},
    ])
    assert extract_reply(msg) == "第一段\n第二段"


def test_extract_reply_from_string_list():
    assert extract_reply(_Msg(["a", "b"])) == "a\nb"


def test_extract_reply_ignores_non_text_blocks():
    msg = _Msg([
        {"type": "tool_use", "name": "x"},
        {"type": "text", "text": "有效内容"},
    ])
    assert extract_reply(msg) == "有效内容"


def test_extract_reply_strips_whitespace():
    assert extract_reply(_Msg("  hi  ")) == "hi"


def test_extract_reply_empty():
    assert extract_reply(_Msg("")) == ""
    assert extract_reply(_Msg([])) == ""


def test_extract_reply_falls_back_to_str():
    assert "123" in extract_reply(_Msg(123))


# ---------------------------------------------------------------------------
# 动作展示
# ---------------------------------------------------------------------------

class _ToolCallMsg:
    def __init__(self, calls):
        self.tool_calls = calls


def test_print_tool_calls_shows_key_fields(capsys):
    msg = _ToolCallMsg([{
        "name": "send_text_email",
        "args": {"to_email": "a@b.com", "subject": "早安", "body": "x"},
    }])
    print_tool_calls(msg)
    out = capsys.readouterr().out

    assert "发送邮件" in out, "应显示中文动作名而不是原始工具名"
    assert "a@b.com" in out
    assert "早安" in out


def test_print_tool_calls_shows_attachments(capsys):
    msg = _ToolCallMsg([{
        "name": "send_email_with_attachment",
        "args": {"to_email": "a@b.com", "subject": "s",
                 "attachment_paths": ["/x/报表.pdf"]},
    }])
    print_tool_calls(msg)
    assert "报表.pdf" in capsys.readouterr().out


def test_print_tool_calls_handles_empty(capsys):
    print_tool_calls(_ToolCallMsg([]))
    assert capsys.readouterr().out == ""


def test_print_tool_calls_tolerates_missing_args(capsys):
    """args 缺失时不能崩——它只是显示，不该影响发送。"""
    print_tool_calls(_ToolCallMsg([{"name": "check_email_config"}]))
    assert "检查邮箱配置" in capsys.readouterr().out


def test_print_tool_result_extracts_text(capsys):
    msg = _Msg([{"type": "text", "text": "✅ 邮件发送成功\n收件人: a@b.com"}])
    print_tool_result(msg)
    out = capsys.readouterr().out
    assert "邮件发送成功" in out
    assert "\n" not in out.rstrip("\n"), "应压成一行"


def test_print_tool_result_truncates_long_text(capsys):
    msg = _Msg("x" * 500)
    print_tool_result(msg)
    out = capsys.readouterr().out
    assert "…" in out
    assert len(out) < 250


def test_print_tool_result_handles_empty(capsys):
    print_tool_result(_Msg(""))
    assert capsys.readouterr().out.strip() == ""


def test_tool_labels_cover_all_tools():
    """中文动作名要覆盖全部工具，否则会显示成英文原名。"""
    from email_butler import TOOL_LABELS
    from tool_defs import TOOL_DEFS

    for tool in TOOL_DEFS:
        assert tool.name in TOOL_LABELS, "缺少中文标签: %s" % tool.name


def test_prompt_template_has_no_stray_placeholders():
    """
    提示词模板的占位符必须都能被 setup() 提供。

    这里曾出过问题：模板里写了 {settings.smtp_email}（Python 表达式），
    在 str.format 下会直接 KeyError，管家一启动就崩。

    用 string.Formatter 解析字段名——正则 \\{(\\w+)\\} 会把
    {settings.smtp_email} 拆成两个合法单词，从而漏判。
    """
    import string

    from email_butler import SYSTEM_PROMPT_TEMPLATE

    allowed = {"email", "attach_dir", "attach_list"}
    names = set()
    for _literal, field, _spec, _conv in string.Formatter().parse(
        SYSTEM_PROMPT_TEMPLATE
    ):
        if field is None:
            continue
        # 字段名可能是 "email.name" 或 "email[0]"，只取根名字
        root = field.split(".")[0].split("[")[0].strip()
        names.add(root)

    assert names, "模板应包含占位符"
    assert names <= allowed, (
        "出现了 setup() 不提供的占位符: %s" % (names - allowed)
    )


def test_prompt_template_renders_with_expected_values():
    """用真实参数渲染一遍，确保 format 不会抛异常。"""
    from email_butler import SYSTEM_PROMPT_TEMPLATE

    rendered = SYSTEM_PROMPT_TEMPLATE.format(
        email="me@example.com",
        attach_dir=r"C:\data",
        attach_list="  - a.txt",
    )
    assert "me@example.com" in rendered
    assert "a.txt" in rendered
    assert "{" not in rendered, "渲染后不应残留花括号"


def test_prompt_requires_immediate_execution():
    """
    固化一条踩过的坑：提示词里必须明确「立即调用工具」。

    实测对比：措辞温和的版本会让 3b 模型只反问
    （「需要我添加正文内容吗？」），一封邮件都发不出去。
    """
    from email_butler import SYSTEM_PROMPT_TEMPLATE

    assert "立即调用工具" in SYSTEM_PROMPT_TEMPLATE
    assert "不允许先反问" in SYSTEM_PROMPT_TEMPLATE


def test_prompt_forbids_guessing_attachment_names():
    """附件名必须来自真实文件列表，不能自己拼。"""
    from email_butler import SYSTEM_PROMPT_TEMPLATE

    assert "不要自己拼文件名" in SYSTEM_PROMPT_TEMPLATE


# ---------------------------------------------------------------------------
# ask()：多轮记忆与「只展示本轮动作」
# ---------------------------------------------------------------------------

class _AIMsg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _ToolMsg:
    def __init__(self, text):
        self.content = [{"type": "text", "text": text}]
        self.tool_calls = []


class _FakeAgent:
    """
    假智能体：按脚本返回**累积**的消息列表（与真实 LangGraph 行为一致）。

    真实 ainvoke 会把传入的历史一并返回，这正是曾经导致
    「把上一轮的工具调用又打印一遍」的原因。
    """

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen_history_lengths = []
        self.seen_histories = []

    async def ainvoke(self, payload):
        incoming = payload["messages"]
        self.seen_history_lengths.append(len(incoming))
        self.seen_histories.append(list(incoming))
        # 累积：历史 + 本轮新增
        accumulated = list(incoming) + self.turns.pop(0)
        return {"messages": accumulated}


def _make_butler(turns):
    from email_butler import EmailButler

    b = EmailButler()
    b.agent = _FakeAgent(turns)
    b.my_email = "me@example.com"
    return b


async def test_ask_passes_full_history_each_turn(capsys):
    """
    回归测试：每轮必须把**完整历史**交给智能体。

    早期实现每轮只传当前一句，导致「主题改成…」这类追问完全接不住——
    它不知道你在说哪封邮件。
    """
    turn1 = [
        _AIMsg(tool_calls=[{"name": "send_text_email", "args": {"subject": "早上好"}}]),
        _ToolMsg("✅ 邮件发送成功"),
        _AIMsg("已发送"),
    ]
    turn2 = [
        _AIMsg(tool_calls=[{"name": "send_text_email", "args": {"subject": "会议提醒"}}]),
        _ToolMsg("✅ 邮件发送成功"),
        _AIMsg("已改并发送"),
    ]
    b = _make_butler([turn1, turn2])

    await b.ask("发封邮件，主题早上好")
    assert b.agent.seen_history_lengths[0] == 1, "首轮应只有当前一句"

    await b.ask("主题改成会议提醒")
    assert b.agent.seen_history_lengths[1] > 1, "第二轮必须带上历史"

    # 历史里应含第一轮的用户输入与工具结果
    second_history = b.agent.seen_histories[1]
    texts = [str(getattr(m, "content", m)) for m in second_history]
    assert any("早上好" in t for t in texts), "历史中应保留第一轮内容"


async def test_ask_only_displays_current_turn_actions(capsys):
    """
    回归测试：只打印**本轮新增**的工具调用。

    曾经的 bug：遍历 ainvoke 返回的全部消息（含历史），
    把上一轮的调用又打印一遍，看起来就像「说了改却用旧内容重发」。
    """
    turn1 = [
        _AIMsg(tool_calls=[{"name": "send_text_email",
                            "args": {"subject": "旧主题", "to_email": "a@b.com"}}]),
        _ToolMsg("✅ 邮件发送成功"),
        _AIMsg("已发送"),
    ]
    turn2 = [
        _AIMsg(tool_calls=[{"name": "send_text_email",
                            "args": {"subject": "新主题", "to_email": "a@b.com"}}]),
        _ToolMsg("✅ 邮件发送成功"),
        _AIMsg("已改并发送"),
    ]
    b = _make_butler([turn1, turn2])

    await b.ask("发封邮件")
    capsys.readouterr()  # 丢弃第一轮输出

    await b.ask("主题改成新主题")
    out = capsys.readouterr().out

    assert "新主题" in out, "应显示本轮的调用"
    assert "旧主题" not in out, "不应重复打印历史中的旧调用"


async def test_ask_returns_reply_text():
    b = _make_butler([[_AIMsg("这是回复")]])
    assert await b.ask("你好") == "这是回复"


async def test_ask_without_tool_calls_prints_no_action():
    b = _make_butler([[_AIMsg("只是回答，没有调用工具")]])
    assert await b.ask("你好") == "只是回答，没有调用工具"


async def test_ask_history_is_bounded():
    """历史不能无限增长，否则每轮推理越来越慢。"""
    from email_butler import MAX_HISTORY_MESSAGES

    long_turn = [_AIMsg("x%d" % i) for i in range(MAX_HISTORY_MESSAGES + 10)]
    b = _make_butler([long_turn])

    await b.ask("你好")
    assert len(b.history) <= MAX_HISTORY_MESSAGES


async def test_ask_records_user_message_in_history():
    b = _make_butler([[_AIMsg("ok")]])
    await b.ask("记住这句话")
    first = " ".join(str(getattr(m, "content", m)) for m in b.history)
    assert "记住这句话" in first
