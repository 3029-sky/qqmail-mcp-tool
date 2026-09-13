# tests/test_delivery.py - IMAP 回读确认的测试
"""
用假的 IMAP 客户端替换 imaplib.IMAP4_SSL，不连接真实邮箱。

重点覆盖：
  - 主题匹配（含 MIME 编码主题的解码）
  - 收件人匹配（防止把发给别人的邮件误认为自己的）
  - 各种失败路径一律归为「未做确认」，绝不误报为发送失败
"""

import imaplib

import pytest

from delivery import (
    DeliveryConfirmer,
    SendConfirmation,
    _normalize_subject,
    _recipients_of,
)

#: 与 QQ 实际返回格式一致的编码主题
ENCODED_SUBJECT = "=?utf-8?b?5Z+657q/ICMx?="  # 「基线 #1」


class FakeIMAP:
    """记录调用的假 IMAP 客户端。"""

    #: 类级配置（每个用例通过 fresh 重置）
    messages = []          # [{"subject":..., "to":...}]
    connect_exc = None
    login_exc = None
    select_status = "OK"
    list_search_ids = None  # 为 None 时按 messages 生成
    last_selected = None

    def __init__(self, host=None, port=None, timeout=None, **kwargs):
        self.host = host
        self.port = port
        self.logged_in = None
        self.selected = None
        self.search_calls = []
        self.fetch_calls = 0
        self.logged_out = False

        if type(self).connect_exc is not None:
            raise type(self).connect_exc

    @classmethod
    def fresh(cls, messages=None, **overrides):
        cls.messages = messages if messages is not None else []
        cls.connect_exc = None
        cls.login_exc = None
        cls.select_status = "OK"
        cls.list_search_ids = None
        cls.last_selected = None
        for k, v in overrides.items():
            setattr(cls, k, v)

    def login(self, user, password):
        if type(self).login_exc is not None:
            raise type(self).login_exc
        self.logged_in = (user, password)

    def logout(self):
        self.logged_out = True

    def select(self, mailbox, readonly=False):
        self.selected = mailbox
        type(self).last_selected = mailbox
        return type(self).select_status, [b"1"]

    def search(self, charset, *criteria):
        self.search_calls.append((charset, criteria))
        if type(self).list_search_ids is not None:
            ids = type(self).list_search_ids
        else:
            ids = [str(i + 1).encode() for i in range(len(type(self).messages))]
        return "OK", [b" ".join(ids)]

    def fetch(self, msg_id, spec):
        self.fetch_calls += 1
        idx = int(msg_id) - 1
        msgs = type(self).messages
        if idx < 0 or idx >= len(msgs):
            return "OK", [None]
        m = msgs[idx]
        raw = "Subject: %s\r\nTo: %s\r\n\r\n" % (m["subject"], m.get("to", ""))
        return "OK", [(b"1 (BODY[HEADER.FIELDS (SUBJECT TO)] {%d}" % len(raw), raw.encode())]


@pytest.fixture
def fake_imap(monkeypatch):
    FakeIMAP.fresh()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


def make_confirmer(**kwargs):
    params = dict(host="imap.test", port=993, user="a@b.com", password="pw")
    params.update(kwargs)
    return DeliveryConfirmer(**params)


# ---------------------------------------------------------------------------
# 头部解析
# ---------------------------------------------------------------------------

def test_normalize_subject_decodes_mime_encoding():
    # QQ 返回的是编码形式，必须解码后比较，否则永远匹配不上
    assert _normalize_subject(ENCODED_SUBJECT) == _normalize_subject("基线 #1")


def test_normalize_subject_ignores_whitespace():
    assert _normalize_subject("Hello  World") == _normalize_subject("HelloWorld")


def test_recipients_parsing():
    assert _recipients_of("A@B.com") == ["a@b.com"]
    assert _recipients_of("a@b.com, c@d.com") == ["a@b.com", "c@d.com"]
    assert _recipients_of('"Name" <x@y.com>') == ["x@y.com"]
    assert _recipients_of("") == []
    assert _recipients_of(None) == []


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------

def test_confirms_when_subject_and_recipient_match(fake_imap):
    fake_imap.fresh([{"subject": ENCODED_SUBJECT, "to": "me@qq.com"}])
    result = make_confirmer().check_once("基线 #1", "me@qq.com")

    assert result.checked is True
    assert result.confirmed is True
    assert result.matched_subject == "基线 #1"
    assert result.folder == '"Sent Messages"'


def test_rejects_when_subject_differs(fake_imap):
    fake_imap.fresh([{"subject": ENCODED_SUBJECT, "to": "me@qq.com"}])
    result = make_confirmer().check_once("完全不同的主题", "me@qq.com")

    assert result.checked is True
    assert result.confirmed is False


def test_rejects_when_recipient_differs(fake_imap):
    """发给别人的邮件不能被误认为本次发送的凭据。"""
    fake_imap.fresh([{"subject": ENCODED_SUBJECT, "to": "someone@else.com"}])
    result = make_confirmer().check_once("基线 #1", "me@qq.com")

    assert result.checked is True
    assert result.confirmed is False


def test_matches_when_recipient_header_missing(fake_imap):
    """收件人头缺失时退化为仅按主题匹配。"""
    fake_imap.fresh([{"subject": ENCODED_SUBJECT, "to": ""}])
    result = make_confirmer().check_once("基线 #1", "me@qq.com")
    assert result.confirmed is True


def test_matches_one_of_multiple_recipients(fake_imap):
    fake_imap.fresh([{"subject": ENCODED_SUBJECT, "to": "a@x.com, me@qq.com"}])
    result = make_confirmer().check_once("基线 #1", ["other@y.com", "me@qq.com"])
    assert result.confirmed is True


def test_uses_quoted_sent_folder(fake_imap):
    """文件夹名含空格，必须带引号传入，否则 QQ 会报 BAD 参数错误。"""
    fake_imap.fresh([])
    make_confirmer().check_once("x", "a@b.com")
    assert fake_imap.last_selected == '"Sent Messages"'


def test_custom_folder_is_passed_through(fake_imap):
    fake_imap.fresh([])
    make_confirmer(folder='"Custom Sent"').check_once("x", "a@b.com")
    assert fake_imap.last_selected == '"Custom Sent"'


# ---------------------------------------------------------------------------
# 失败路径：一律「未做确认」，绝不误报
# ---------------------------------------------------------------------------

def test_connect_failure_is_not_confirmed(fake_imap):
    fake_imap.fresh(connect_exc=OSError("network down"))
    result = make_confirmer().check_once("s", "a@b.com")

    assert result.checked is False
    assert result.confirmed is False
    assert "无法连接" in result.detail


def test_login_failure_is_not_confirmed(fake_imap):
    fake_imap.fresh(login_exc=imaplib.IMAP4.error("auth failed"))
    result = make_confirmer().check_once("s", "a@b.com")

    assert result.checked is False
    assert result.confirmed is False


def test_select_failure_is_reported_as_unchecked(fake_imap):
    fake_imap.fresh(select_status="NO")
    result = make_confirmer().check_once("s", "a@b.com")

    assert result.checked is False
    assert "文件夹" in result.detail


def test_search_error_is_reported_as_unchecked(fake_imap, monkeypatch):
    fake_imap.fresh([])

    def boom(*a, **k):
        raise imaplib.IMAP4.error("search exploded")

    monkeypatch.setattr(FakeIMAP, "search", boom)
    result = make_confirmer().check_once("s", "a@b.com")

    assert result.checked is False
    assert result.confirmed is False


def test_empty_sent_folder_is_unconfirmed(fake_imap):
    fake_imap.fresh([])
    result = make_confirmer().check_once("s", "a@b.com")
    assert result.checked is True
    assert result.confirmed is False
    assert "未找到" in result.detail


# ---------------------------------------------------------------------------
# 重试轮询
# ---------------------------------------------------------------------------

def test_confirm_retries_until_found(fake_imap, monkeypatch):
    """首次查不到时应继续轮询，而不是立即放弃。"""
    calls = {"n": 0}
    original = FakeIMAP.search

    def delayed_search(self, charset, *criteria):
        calls["n"] += 1
        # 第 1 次返回空，第 2 次返回命中
        if calls["n"] == 1:
            return "OK", [b""]
        return original(self, charset, *criteria)

    FakeIMAP.fresh([{"subject": ENCODED_SUBJECT, "to": "me@qq.com"}])
    monkeypatch.setattr(FakeIMAP, "search", delayed_search)
    monkeypatch.setattr("delivery.time.sleep", lambda *_: None)

    result = make_confirmer().confirm("基线 #1", "me@qq.com", attempts=3, interval=0)

    assert result.confirmed is True
    assert result.attempts == 2


def test_confirm_gives_up_after_attempts(fake_imap, monkeypatch):
    FakeIMAP.fresh([])
    monkeypatch.setattr("delivery.time.sleep", lambda *_: None)

    result = make_confirmer().confirm("s", "a@b.com", attempts=3, interval=0)

    assert result.confirmed is False
    assert result.attempts == 3


def test_confirm_does_not_retry_on_connect_failure(fake_imap, monkeypatch):
    """连不上 IMAP 时重试没有意义，应立即返回。"""
    FakeIMAP.fresh(connect_exc=OSError("down"))
    slept = []
    monkeypatch.setattr("delivery.time.sleep", lambda s: slept.append(s))

    result = make_confirmer().confirm("s", "a@b.com", attempts=3, interval=1)

    assert result.checked is False
    assert result.attempts == 1
    assert slept == [], "连接失败不应触发重试等待"


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------

def test_as_dict_shape():
    c = SendConfirmation(
        checked=True, confirmed=True, detail="d", elapsed_ms=12.345, attempts=2,
        matched_subject="s",
    )
    d = c.as_dict()
    assert d["confirmed"] is True
    assert d["checked"] is True
    assert d["elapsed_ms"] == 12.3
    assert d["attempts"] == 2
    assert d["matched_subject"] == "s"


def test_as_dict_omits_empty_optionals():
    d = SendConfirmation(checked=False, confirmed=False, detail="d").as_dict()
    assert "elapsed_ms" not in d
    assert "attempts" not in d
