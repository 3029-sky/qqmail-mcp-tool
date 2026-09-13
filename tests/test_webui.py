# tests/test_webui.py - 网页界面的后端测试
"""
用 httpx.ASGITransport 在进程内驱动真实的 FastAPI 应用——
不启动服务器、不占端口、不连网、不发信。

重点覆盖三处最容易出问题的地方：

  1. **路径穿越**。`/api/attachments/{name}` 是本应用唯一接受外部
     文件名的入口，若只做字符串拼接，`../../.env` 就能把授权码读走。
  2. **上传落盘**：重名不覆盖、超限拒绝、空文件拒绝。
  3. **对话流**：SSE 事件顺序必须与终端版一致（都用 butler_core）。
"""

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import batch
import webui


@pytest.fixture
def attachment_dir(tmp_path, monkeypatch):
    """
    把附件目录指到临时目录，避免测试污染真实的 attachments/。

    注意要打桩**所有**已经 import 过 settings 的模块：`from config import
    settings` 是把对象绑定到各模块自己的命名空间里的，只改 `config.settings`
    不会影响已经绑定过的模块。而个别用例会 reload config（测配置来源），
    那会让两边指向不同的对象——所以这里挨个打一遍。
    """
    import butler_core

    for module in (webui, butler_core):
        monkeypatch.setattr(module.settings, "attachment_dir", tmp_path)

    monkeypatch.setattr(webui, "MAX_UPLOAD_BYTES", 1024 * 100)   # 100 KB，便于测超限
    return tmp_path


@pytest.fixture
async def client(attachment_dir):
    transport = ASGITransport(app=webui.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """
    把应用的 .env 指到临时文件。

    绝对不能让测试写到项目真实的 .env —— 那里面是用户的授权码，
    一次写坏就得重新申请。这条与本文件里「不许污染真实附件目录」
    是同一类要求。

    配置接口会**就地改写** `settings`（这是刻意的：不做就地改，
    已绑定 settings 的模块就看不到新值）。代价是它会影响后续用例，
    所以这里按字段快照、逐个还原——覆盖所有 EDITABLE_KEYS 对应的字段，
    漏一个就会出现「前一个用例的授权码漏到后一个用例」这种幽灵失败。
    """
    from envfile import EDITABLE_KEYS, EnvFile
    from config import Settings, settings

    path = tmp_path / ".env"
    path.write_text(
        "# 用户自己的注释，必须保留\n"
        "SMTP_EMAIL=old@qq.com\n"
        "SMTP_PASSWORD=0123456789abcdef\n"
        "SMTP_SERVER=smtp.qq.com\n"
        "SMTP_PORT=465\n"
        "OLLAMA_MODEL=qwen2.5:3b\n",
        encoding="utf-8",
        newline="",
    )

    snapshot = {
        key.lower(): getattr(settings, key.lower())
        for key in EDITABLE_KEYS
        if key.lower() in Settings.model_fields
    }

    monkeypatch.setattr(webui, "ENV_FILE", EnvFile(path))
    yield EnvFile(path)

    for field, value in snapshot.items():
        setattr(settings, field, value)


# ---------------------------------------------------------------------------
# 基础端点
# ---------------------------------------------------------------------------

async def test_index_serves_the_page(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "邮件管家" in resp.text
    assert "Ctrl+V" in resp.text, "页面要告诉用户可以直接粘贴"


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_info_reports_email_and_limits(client, attachment_dir):
    resp = await client.get("/api/info")
    assert resp.status_code == 200
    data = resp.json()
    assert "email" in data
    assert data["attachments_dir"] == str(attachment_dir)
    assert data["max_attachment_bytes"] == 1024 * 100


async def test_api_never_leaks_credentials(client):
    """
    回归测试：接口绝不能把授权码泄出去。

    这个界面能直接发邮件，授权码就是「以你名义发信」的钥匙。
    这里不只看键名，还把响应全文与真实凭据比对——多一层保险：
    就算将来有人把 settings 整个 dump 出去，这条也会拦住。
    """
    from config import settings

    secret = str(settings.smtp_password)

    for path in ("/api/info", "/api/models", "/api/attachments", "/health"):
        resp = await client.get(path)
        body = resp.text

        assert secret not in body, "%s 泄露了授权码" % path
        lowered = body.lower()
        for bad in ("password", "authorization", "auth_token", "mcp_auth_token"):
            assert bad not in lowered, "%s 出现了可疑字段名 %r" % (path, bad)


async def test_info_does_not_dump_settings_object(client):
    """界面只需要展示字段，不该把整个配置对象丢出去。"""
    from config import settings

    keys = set((await client.get("/api/info")).json().keys())
    allowed = {
        "email", "model", "ready", "attachments_dir",
        "max_attachment_bytes", "max_attachment_text", "startup_log", "error",
    }
    assert keys <= allowed, "出现了未预期的字段：%s" % sorted(keys - allowed)


async def test_models_are_listed_with_availability(client, monkeypatch):
    """
    模型列表要带「能不能用」与原因。

    只给一个名字不够：DeepSeek 需要 API Key，界面得能说明
    「为什么这个选项不能选」，否则用户只会看到切换失败。
    """
    import butler_core

    monkeypatch.setattr(butler_core, "list_ollama_models", lambda: ["a:1b", "b:2b"])
    monkeypatch.setattr(butler_core, "deepseek_configured", lambda: False)

    data = (await client.get("/api/models")).json()
    by_ref = {m["ref"]: m for m in data["models"]}

    assert "ollama:a:1b" in by_ref
    assert by_ref["ollama:a:1b"]["available"] is True
    assert by_ref["ollama:a:1b"]["provider"] == "ollama"

    # 没配 Key 时 DeepSeek 选项要标成不可用并说明原因
    deepseek = [m for m in data["models"] if m["provider"] == "deepseek"]
    assert deepseek, "应始终列出 DeepSeek 选项，否则用户不知道有这个能力"
    assert all(m["available"] is False for m in deepseek)
    assert all("API Key" in m["reason"] for m in deepseek)


async def test_models_marks_deepseek_available_when_configured(client, monkeypatch):
    import butler_core

    monkeypatch.setattr(butler_core, "list_ollama_models", lambda: [])
    monkeypatch.setattr(butler_core, "deepseek_configured", lambda: True)

    data = (await client.get("/api/models")).json()
    deepseek = [m for m in data["models"] if m["provider"] == "deepseek"]
    assert deepseek and all(m["available"] for m in deepseek)


async def test_models_reports_current_selection(client):
    data = (await client.get("/api/models")).json()
    assert data["current"], "必须告诉界面当前用的是哪个模型"
    assert ":" in data["current"], "当前模型应是 provider:model 形式"


# ---------------------------------------------------------------------------
# 附件：路径穿越防护（安全关键）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "evil",
    [
        "../.env",
        "../../.env",
        "..\\..\\.env",
        "sub/dir/file.txt",
        "/etc/passwd",
        "",
    ],
)
async def test_attachment_path_traversal_is_blocked(client, evil):
    """
    回归测试：绝不能通过文件名爬到附件目录之外。

    这个界面能直接发邮件，还能读到 .env（里面有 QQ 授权码）——
    只要路径处理是拼接，`../.env` 就能把授权码读走。
    """
    for action in ("raw", "download"):
        resp = await client.get("/api/attachments/%s/%s" % (evil, action))
        assert resp.status_code in (400, 404, 405), (
            "危险路径 %r 的 %s 应被拒绝，实际 %d" % (evil, action, resp.status_code)
        )


async def test_delete_path_traversal_is_blocked(client, tmp_path):
    outside = tmp_path.parent / "不该被删.txt"
    outside.write_text("x", encoding="utf-8")
    resp = await client.delete("/api/attachments/..%2F不该被删.txt")
    assert resp.status_code in (400, 404)
    assert outside.is_file(), "目录外的文件不能被删掉"


async def test_attachment_raw_serves_file(client, attachment_dir):
    (attachment_dir / "图纸.png").write_bytes(b"\x89PNG\r\n\x1a\nDATA")
    resp = await client.get("/api/attachments/%E5%9B%BE%E7%BA%B8.png/raw")
    assert resp.status_code == 200
    assert resp.content.endswith(b"DATA")


async def test_attachment_delete_removes_file(client, attachment_dir):
    target = attachment_dir / "待删.txt"
    target.write_text("x", encoding="utf-8")
    resp = await client.delete("/api/attachments/%E5%BE%85%E5%88%A0.txt")
    assert resp.status_code == 200
    assert not target.exists()


async def test_attachments_are_sorted_newest_first(client, attachment_dir):
    """最近放的排最前——刚粘贴的附件应该一眼看到。"""
    import os
    import time

    old = attachment_dir / "旧.txt"
    old.write_text("old", encoding="utf-8")
    os.utime(old, (1000, 1000))

    new = attachment_dir / "新.txt"
    new.write_text("new", encoding="utf-8")
    os.utime(new, (time.time(), time.time()))

    resp = await client.get("/api/attachments")
    names = [f["name"] for f in resp.json()["files"]]
    assert names[0] == "新.txt"


async def test_attachments_marks_images(client, attachment_dir):
    (attachment_dir / "照片.jpg").write_bytes(b"\xff\xd8\xff\xe0x")
    (attachment_dir / "文档.md").write_text("x", encoding="utf-8")

    files = {f["name"]: f for f in (await client.get("/api/attachments")).json()["files"]}
    assert files["照片.jpg"]["is_image"] is True
    assert files["文档.md"]["is_image"] is False


async def test_attachments_include_human_readable_size(client, attachment_dir):
    (attachment_dir / "f.bin").write_bytes(b"x" * 2048)
    files = (await client.get("/api/attachments")).json()["files"]
    assert files[0]["size_text"] == "2.0 KB"


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------

async def test_upload_saves_file(client, attachment_dir):
    resp = await client.post(
        "/api/attachments",
        files={"files": ("报表最终.csv", b"a,b\n1,2", "text/csv")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["problems"] == []
    assert data["saved"][0]["name"] == "报表最终.csv"
    assert (attachment_dir / "报表最终.csv").read_bytes() == b"a,b\n1,2"


async def test_upload_does_not_overwrite_existing(client, attachment_dir):
    """重名要走新名字，不能把已有附件覆盖掉。"""
    (attachment_dir / "同名.png").write_bytes(b"OLD")

    resp = await client.post(
        "/api/attachments",
        files={"files": ("同名.png", b"NEW", "image/png")},
    )
    saved = resp.json()["saved"][0]["name"]
    assert saved != "同名.png"
    assert saved.endswith(".png"), "换名也要保留扩展名，否则模型认不出类型"
    assert (attachment_dir / "同名.png").read_bytes() == b"OLD"


async def test_upload_rejects_oversized_file(client, attachment_dir):
    resp = await client.post(
        "/api/attachments",
        files={"files": ("巨大.zip", b"x" * (1024 * 200), "application/zip")},
    )
    data = resp.json()
    assert data["saved"] == []
    assert len(data["problems"]) == 1
    assert "超过单件上限" in data["problems"][0]
    assert not (attachment_dir / "巨大.zip").exists()


async def test_upload_rejects_empty_file(client, attachment_dir):
    resp = await client.post(
        "/api/attachments",
        files={"files": ("空.txt", b"", "text/plain")},
    )
    data = resp.json()
    assert data["saved"] == []
    assert "空" in data["problems"][0]


async def test_upload_handles_multiple_files(client, attachment_dir):
    resp = await client.post(
        "/api/attachments",
        files=[
            ("files", ("a.txt", b"aaa", "text/plain")),
            ("files", ("b.txt", b"bbb", "text/plain")),
        ],
    )
    names = sorted(f["name"] for f in resp.json()["saved"])
    assert names == ["a.txt", "b.txt"]


async def test_upload_strips_directory_from_filename(client, attachment_dir):
    """浏览器可能带上路径，必须只取文件名。"""
    resp = await client.post(
        "/api/attachments",
        files={"files": ("../../evil.txt", b"x", "text/plain")},
    )
    saved = resp.json()["saved"][0]["name"]
    assert saved == "evil.txt"
    assert (attachment_dir / "evil.txt").is_file()


async def test_tests_never_write_to_the_real_attachment_dir(client, attachment_dir):
    """
    回归测试：测试绝不能往项目真实的 attachments/ 里写文件。

    这条已经踩过一次：`from config import settings` 把 settings 对象
    绑定到各模块自己的命名空间里，只打桩 `config.settings` 时，
    已经绑定过的模块仍指向旧对象——于是测试把 a.txt、evil.txt
    这类文件写进了真实附件目录（是靠 `_2` 后缀的重名避让暴露的）。

    注意这里**不能**用 settings.attachment_dir 取「真实目录」：
    它正是被 fixture 打桩的那个对象，读出来一定是临时目录。
    直接从 config.py 的位置推导才可靠。
    """
    import config as config_module

    real_dir = Path(config_module.__file__).resolve().parent / "attachments"

    # 临时目录必须真的被用上了，否则下面的断言毫无意义
    assert Path(attachment_dir).resolve() != real_dir.resolve(), "fixture 没生效"

    await client.post(
        "/api/attachments",
        files={"files": ("不该出现在真实目录.txt", b"x", "text/plain")},
    )

    leaked = real_dir / "不该出现在真实目录.txt"
    assert not leaked.exists(), "测试污染了真实附件目录：%s" % leaked


# ---------------------------------------------------------------------------
# 对话（SSE）
# ---------------------------------------------------------------------------

class _FakeAgent:
    """按脚本产出的假智能体，类名与真实消息一致（见 test_butler_core）。"""

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    async def astream(self, payload, stream_mode=None):
        self.seen.append(list(payload["messages"]))
        for msg in self.turns.pop(0):
            yield {"model": {"messages": [msg]}}


class AIMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.artifact = None


class ToolMessage:
    def __init__(self, text):
        self.content = [{"type": "text", "text": text}]
        self.tool_calls = []
        self.artifact = None


@pytest.fixture
def fake_session(monkeypatch):
    """把管家会话换成替身，并标记为已启动，避免真的去拉 Ollama 与 MCP。"""
    import butler_core

    session = butler_core.AgentSession()
    session.agent = _FakeAgent([[
        AIMessage(tool_calls=[{"name": "send_text_email",
                               "args": {"to_email": "a@b.com", "subject": "早"}}]),
        ToolMessage("✅ 邮件发送成功"),
        AIMessage("已发给你。"),
    ]])
    session.model = "test-model"

    monkeypatch.setattr(webui.state, "session", session)
    monkeypatch.setattr(webui.state, "ready", True)
    return session


def parse_sse(text):
    events = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


async def test_chat_streams_tool_call_then_result_then_reply(client, fake_session):
    """
    事件顺序与终端版一致：先「决定调用」，再「执行结果」，最后回复。

    顺序错了界面就会先显示结果后显示动作，读起来莫名其妙。
    """
    resp = await client.post("/api/chat", json={"text": "给我自己发封邮件"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(resp.text)
    types = [e["type"] for e in events]

    assert types[0] == "start"
    assert "tool_call" in types and "tool_result" in types and "reply" in types
    assert types.index("tool_call") < types.index("tool_result") < types.index("reply")
    assert types[-1] == "done"


async def test_chat_tool_call_carries_labels_and_args(client, fake_session):
    resp = await client.post("/api/chat", json={"text": "发邮件"})
    events = parse_sse(resp.text)
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["label"] == "发送邮件", "界面直接显示中文动作"
    assert call["args"]["to_email"] == "a@b.com"


async def test_chat_reply_is_the_assistant_text(client, fake_session):
    """回复必须是模型的话，不是工具原文。"""
    resp = await client.post("/api/chat", json={"text": "发邮件"})
    events = parse_sse(resp.text)
    reply = next(e for e in events if e["type"] == "reply")
    assert reply["text"] == "已发给你。"


async def test_chat_mentions_pending_attachments(client, fake_session):
    """
    界面上附带的文件要明确告诉模型。

    否则「把这张图发给张三」里的「这张图」只能靠模型猜文件名——
    目录里可能有很多旧文件，很容易发错。
    """
    resp = await client.post(
        "/api/chat",
        json={"text": "发给我自己", "attachments": ["图纸.png", "报价.pdf"]},
    )
    assert resp.status_code == 200

    sent = fake_session.agent.seen[0][0]
    content = sent[1] if isinstance(sent, tuple) else str(sent)
    assert "图纸.png" in content and "报价.pdf" in content


async def test_chat_returns_attachments_after_turn(client, fake_session, attachment_dir):
    """一轮结束后要回传最新附件列表，界面才能刷新（模型可能刚导入了文件）。"""
    (attachment_dir / "新加入.txt").write_text("x", encoding="utf-8")
    resp = await client.post("/api/chat", json={"text": "发邮件"})
    events = parse_sse(resp.text)
    ev = next(e for e in events if e["type"] == "attachments")
    assert any(f["name"] == "新加入.txt" for f in ev["files"])


async def test_chat_rejects_empty_text(client, fake_session):
    resp = await client.post("/api/chat", json={"text": "   "})
    assert resp.status_code == 400


async def test_chat_passes_template_as_structure(client, fake_session):
    """
    模板的主题与收件人要作为**结构化说明**传给模型，而不是拼成一句话。

    修之前实测的坏情况（正文与指令拼在一起、让模型再拆开）：
      - 正文含「主题：」时，模型把正文里的那行当成主题
      - 正文含换行时，正文后半段可能被丢掉
    现在主题与收件人由代码明确给出，模型不需要猜边界在哪。
    """
    resp = await client.post("/api/chat", json={
        "text": "核对结果如下：\n主题：季度报表\n备注：已确认",
        "template": {"name": "数据核对", "subject": "核对结果", "to": ""},
    })
    assert resp.status_code == 200

    sent = fake_session.agent.seen[0][0]
    content = sent[1] if isinstance(sent, tuple) else str(sent)

    assert "模板" in content, "要说明这是模板带来的"
    assert "核对结果" in content, "模板主题要明确给出"
    assert "季度报表" in content, "正文本身仍要在（它来自用户那句话）"


async def test_chat_template_hint_does_not_duplicate_body(client, fake_session):
    """
    模板正文**不重复传**——它已经在用户那句话里了。

    重复传会让模型以为要发两份，或者纠结用哪一份。
    """
    resp = await client.post("/api/chat", json={
        "text": "请准时参加。",
        "template": {"name": "开会通知", "subject": "明天九点开会", "to": ""},
    })
    assert resp.status_code == 200

    sent = fake_session.agent.seen[0][0]
    content = sent[1] if isinstance(sent, tuple) else str(sent)
    assert content.count("请准时参加") == 1, "正文被重复传了"


async def test_chat_without_template_has_no_hint(client, fake_session):
    """没用模板时不该出现模板说明。"""
    resp = await client.post("/api/chat", json={"text": "发封邮件"})
    assert resp.status_code == 200
    sent = fake_session.agent.seen[0][0]
    content = sent[1] if isinstance(sent, tuple) else str(sent)
    assert "模板" not in content


async def test_chat_reports_error_event(client, monkeypatch):
    """模型环节出错要作为 error 事件推给界面，而不是让连接无声中断。"""
    import butler_core

    class BoomAgent:
        async def astream(self, payload, stream_mode=None):
            raise RuntimeError("Ollama 挂了")
            yield  # pragma: no cover - 让它是生成器

    session = butler_core.AgentSession()
    session.agent = BoomAgent()
    monkeypatch.setattr(webui.state, "session", session)
    monkeypatch.setattr(webui.state, "ready", True)

    resp = await client.post("/api/chat", json={"text": "发邮件"})
    events = parse_sse(resp.text)
    assert any(e["type"] == "error" and "Ollama 挂了" in e["text"] for e in events)


# ---------------------------------------------------------------------------
# 会话管理
# ---------------------------------------------------------------------------

async def test_reset_clears_history(client, fake_session):
    fake_session.history = [("user", "旧对话")]
    resp = await client.post("/api/reset")
    assert resp.status_code == 200
    assert fake_session.history == []


async def test_switch_model_rejects_unknown_model(client, fake_session, monkeypatch):
    """切到没装的模型要报错，且不能把当前模型改坏。"""
    resp = await client.post("/api/models", json={"model": "不存在:99b"})
    assert resp.status_code == 400
    assert "不存在:99b" in resp.json()["detail"]
    assert fake_session.model == "test-model", "失败的切换不该改掉当前模型"


async def test_switch_model_requires_model_field(client, fake_session):
    resp = await client.post("/api/models", json={})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 配置编辑
# ---------------------------------------------------------------------------

async def test_get_config_masks_secrets(client, fake_env):
    """
    配置视图必须脱敏：授权码只回长度，不回内容。
    """
    data = (await client.get("/api/config")).json()
    values = data["values"]

    assert values["SMTP_EMAIL"]["value"] == "old@qq.com", "非敏感项要回显"
    assert values["SMTP_PASSWORD"]["secret"] is True
    assert values["SMTP_PASSWORD"]["length"] == len("0123456789abcdef")
    assert values["SMTP_PASSWORD"]["value"] is None, "授权码绝不能回内容"

    # 整个响应里都不该出现授权码
    assert "0123456789abcdef" not in (await client.get("/api/config")).text


async def test_get_config_lists_editable_keys(client, fake_env):
    data = (await client.get("/api/config")).json()
    assert "SMTP_EMAIL" in data["editable"]
    assert "DEEPSEEK_API_KEY" in data["editable"]
    assert "MCP_AUTH_TOKEN" not in data["editable"], "认证令牌不该能从这里改"


async def test_put_config_saves_and_applies_immediately(client, fake_env):
    """
    保存后必须**立刻生效**，不是等重启。

    这里验证的是关键路径：.env 文件被更新，且运行中的 settings
    也跟着变了——否则界面显示已改、实际还在用旧值。

    断言对象刻意用 `webui.settings` 而不是 `config.settings`：
    `from config import settings` 把对象绑定到各模块自己的命名空间，
    而个别用例会 reload config 模块。用 webui 自己那份，才是
    「这段代码实际读的那一个」。
    """
    resp = await client.put("/api/config", json={
        "values": {"SMTP_EMAIL": "new@qq.com", "OLLAMA_MODEL": "qwen2.5:7b"},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "SMTP_EMAIL" in body["changed"]

    assert fake_env.as_dict()["SMTP_EMAIL"] == "new@qq.com"
    assert webui.settings.smtp_email == "new@qq.com", "必须就地应用到运行中的配置"
    assert webui.settings.ollama_model == "qwen2.5:7b"


async def test_put_config_preserves_comments(client, fake_env):
    """改配置不能把用户在 .env 里写的注释抹掉。"""
    await client.put("/api/config", json={"values": {"SMTP_EMAIL": "new@qq.com"}})
    assert "用户自己的注释，必须保留" in fake_env.read_raw()


async def test_put_config_blank_secret_means_keep(client, fake_env):
    """
    敏感项留空 = 不修改，而不是清空。

    界面拿不到授权码内容，所以密码框永远是空的；若把空值当成清空，
    用户每改一次别的字段就会把授权码抹掉，下次发信直接失败。
    """
    resp = await client.put("/api/config", json={
        "values": {"SMTP_EMAIL": "new@qq.com", "SMTP_PASSWORD": ""},
    })
    assert resp.status_code == 200
    assert fake_env.as_dict()["SMTP_PASSWORD"] == "0123456789abcdef", "授权码不该被清空"


async def test_put_config_accepts_new_secret(client, fake_env):
    resp = await client.put("/api/config", json={
        "values": {"SMTP_PASSWORD": "fedcba9876543210"},
    })
    assert resp.status_code == 200
    assert fake_env.as_dict()["SMTP_PASSWORD"] == "fedcba9876543210"
    assert webui.settings.smtp_password == "fedcba9876543210"


async def test_put_config_rejects_wrong_length_authorization_code(client, fake_env):
    """
    授权码固定 16 位。填成 QQ 密码是最常见的错误，必须当场拦住。

    否则错误会推迟到发信时才以 SMTP 认证失败的形式出现，
    而那条英文报错对用户毫无帮助。
    """
    resp = await client.put("/api/config", json={
        "values": {"SMTP_PASSWORD": "我的QQ密码不是授权码"},
    })
    assert resp.status_code == 400
    assert "16" in resp.json()["detail"]
    assert fake_env.as_dict()["SMTP_PASSWORD"] == "0123456789abcdef", "校验失败不该写入"


async def test_put_config_rejects_empty_email(client, fake_env):
    resp = await client.put("/api/config", json={"values": {"SMTP_EMAIL": ""}})
    assert resp.status_code == 400
    assert "不能为空" in resp.json()["detail"]


async def test_put_config_rejects_malformed_email(client, fake_env):
    resp = await client.put("/api/config", json={"values": {"SMTP_EMAIL": "不是邮箱"}})
    assert resp.status_code == 400


async def test_put_config_rejects_non_numeric_port(client, fake_env):
    resp = await client.put("/api/config", json={"values": {"SMTP_PORT": "abc"}})
    assert resp.status_code == 400


async def test_put_config_ignores_unknown_keys(client, fake_env):
    """未知键直接忽略，不报错也不写入（界面可能带多余字段）。"""
    resp = await client.put("/api/config", json={
        "values": {"SMTP_EMAIL": "new@qq.com", "完全不存在的键": "x"},
    })
    assert resp.status_code == 200
    assert "完全不存在的键" not in fake_env.read_raw()


async def test_put_config_creates_backup(client, fake_env):
    resp = await client.put("/api/config", json={"values": {"SMTP_EMAIL": "new@qq.com"}})
    assert resp.json()["backup"], "改配置前要留一份备份"


async def test_put_config_no_change_reports_clearly(client, fake_env):
    resp = await client.put("/api/config", json={"values": {"SMTP_EMAIL": "old@qq.com"}})
    assert resp.status_code == 200
    assert resp.json()["changed"] == []
    assert resp.json()["message"], "应说明「没有需要保存的改动」"


async def test_put_config_never_writes_real_env(client, fake_env):
    """
    回归测试：配置接口绝不能碰到项目真实的 .env。

    那里面是用户的授权码，写坏一次就得重新申请。
    """
    import config as config_module

    real = Path(config_module.__file__).resolve().parent / ".env"
    before = real.read_bytes() if real.is_file() else None

    await client.put("/api/config", json={"values": {"SMTP_EMAIL": "new@qq.com"}})

    after = real.read_bytes() if real.is_file() else None
    assert after == before, "测试改动了项目真实的 .env"


async def test_config_test_endpoint_does_not_accept_credentials(client, fake_env):
    """
    连接测试接口不接受调用方传来的凭据。

    否则它就变成一个「拿任意凭据去连外部服务器」的接口。
    """
    resp = await client.post("/api/config/test", json={
        "smtp_email": "attacker@evil.com", "smtp_password": "x" * 16,
    })
    assert resp.status_code in (200, 400, 500)
    # 不管结果如何，都不该因为「传了凭据」而改变行为；
    # 这里只断言它没有把这些凭据写进配置
    assert fake_env.as_dict()["SMTP_EMAIL"] == "old@qq.com"


# ---------------------------------------------------------------------------
# 联系人 / 模板 / 签名
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_userdata(tmp_path, monkeypatch):
    """
    把用户数据指到临时文件。

    与 fake_env 同理：绝不能写进项目真实的 data/userdata.json。
    """
    import userdata as module
    from userdata import UserData

    store = UserData(tmp_path / "userdata.json")
    monkeypatch.setattr(module, "userdata", store)
    # webui 里是 `from userdata import userdata`，也要换掉
    monkeypatch.setattr(webui, "userdata", store)
    return store


async def test_userdata_starts_empty(client, fake_userdata):
    data = (await client.get("/api/userdata")).json()
    assert data == {"contacts": [], "templates": [], "signature": ""}


async def test_add_and_list_contact(client, fake_userdata):
    resp = await client.post("/api/contacts", json={
        "name": "张三", "email": "zs@example.com", "note": "同事",
    })
    assert resp.status_code == 200

    contacts = (await client.get("/api/userdata")).json()["contacts"]
    assert contacts[0]["name"] == "张三"
    assert contacts[0]["email"] == "zs@example.com"


@pytest.mark.parametrize("payload, keyword", [
    ({"name": "", "email": "a@b.com"}, "姓名"),
    ({"name": "张三", "email": "不是邮箱"}, "邮箱"),
])
async def test_add_contact_validates(client, fake_userdata, payload, keyword):
    """地址编错了邮件发不出去，且报错很难懂，所以在入口拦住。"""
    resp = await client.post("/api/contacts", json=payload)
    assert resp.status_code == 400
    assert keyword in resp.json()["detail"]


async def test_remove_contact(client, fake_userdata):
    await client.post("/api/contacts", json={"name": "张三", "email": "zs@b.com"})
    resp = await client.delete("/api/contacts/%E5%BC%A0%E4%B8%89")
    assert resp.status_code == 200
    assert (await client.get("/api/userdata")).json()["contacts"] == []


async def test_remove_missing_contact_returns_404(client, fake_userdata):
    resp = await client.delete("/api/contacts/%E4%B8%8D%E5%AD%98%E5%9C%A8")
    assert resp.status_code == 404


async def test_add_and_remove_template(client, fake_userdata):
    resp = await client.post("/api/templates", json={
        "name": "开会通知", "subject": "明天九点开会", "body": "请准时参加。",
    })
    assert resp.status_code == 200

    templates = (await client.get("/api/userdata")).json()["templates"]
    assert templates[0]["name"] == "开会通知"

    resp = await client.delete("/api/templates/%E5%BC%80%E4%BC%9A%E9%80%9A%E7%9F%A5")
    assert resp.status_code == 200


async def test_template_requires_name(client, fake_userdata):
    resp = await client.post("/api/templates", json={"name": "  "})
    assert resp.status_code == 400


async def test_set_signature(client, fake_userdata):
    resp = await client.put("/api/signature", json={"signature": "—— 张三"})
    assert resp.status_code == 200
    assert (await client.get("/api/userdata")).json()["signature"] == "—— 张三"


# ---------------------------------------------------------------------------
# 批量发送
# ---------------------------------------------------------------------------

async def test_batch_limits_exposes_safety_bounds(client, fake_userdata):
    """
    界面要能提前知道能发多少、最少等多久。

    否则用户会自己猜一个节奏，而猜错的代价是当天发不出邮件。
    """
    data = (await client.get("/api/batch/limits")).json()
    assert data["max_batch"] == batch.MAX_BATCH
    assert data["min_interval"] == batch.MIN_INTERVAL


def parse_sse_events(text):
    events = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


async def test_batch_dry_run_does_not_send(client, fake_userdata):
    """预览绝不发信——这是「先看名单再发」的前提。"""
    resp = await client.post("/api/batch", json={
        "recipients": ["a@example.com", "b@example.com"],
        "subject": "主题", "body": "正文", "dry_run": True,
    })
    assert resp.status_code == 200

    events = parse_sse_events(resp.text)
    assert [e["type"] for e in events] == ["plan", "done"]
    assert events[0]["count"] == 2
    assert events[-1]["dry_run"] is True


async def test_batch_rejects_over_limit(client, fake_userdata):
    resp = await client.post("/api/batch", json={
        "recipients": ["u%d@b.com" % i for i in range(batch.MAX_BATCH + 5)],
        "subject": "主题", "body": "正文", "dry_run": True,
    })
    assert resp.status_code == 400
    assert "最多" in resp.json()["detail"]


async def test_batch_rejects_empty_body(client, fake_userdata):
    resp = await client.post("/api/batch", json={
        "recipients": ["a@b.com"], "subject": "主题", "body": "  ", "dry_run": True,
    })
    assert resp.status_code == 400


async def test_batch_resolves_contact_names(client, fake_userdata):
    """界面只传联系人姓名，由后端解析成地址——避免前端自己拼错。"""
    await client.post("/api/contacts", json={"name": "张三", "email": "zs@example.com"})

    resp = await client.post("/api/batch", json={
        "recipients": ["张三"], "subject": "主题", "body": "正文", "dry_run": True,
    })
    assert resp.status_code == 200
    plan = parse_sse_events(resp.text)[0]
    assert plan["recipients"][0]["email"] == "zs@example.com"


async def test_batch_never_writes_real_userdata(client, fake_userdata):
    """回归测试：批量接口不能碰到项目真实的 data/userdata.json。"""
    import userdata as module

    real = module.UserData.__init__.__globals__["__file__"]
    real_path = Path(real).resolve().parent / "data" / "userdata.json"
    before = real_path.read_bytes() if real_path.is_file() else None

    await client.post("/api/contacts", json={"name": "测试", "email": "t@b.com"})

    after = real_path.read_bytes() if real_path.is_file() else None
    assert after == before, "测试写到了真实的 userdata.json"
