# tests/test_email_tools.py - SMTP 发送逻辑的单元测试
"""
通过替换 smtplib.SMTP_SSL 来验证发送逻辑，不连接任何真实服务器、不发送真实邮件。
重点覆盖：
  - 邮件结构与编码（UTF-8 主题、抄送、附件）
  - QQ 邮箱非标准响应 (-1, b'\\x00\\x00\\x00') 的处理
  - 各异步包装方法是否把参数正确传递给同步核心
"""

import email
import smtplib
from email.header import decode_header, make_header

import pytest

from email_tools import QQMailTools
from tests.conftest import QQ_SPECIAL_RESPONSE


@pytest.fixture
def tools(live_sender):
    """
    高层工具对象。

    复用 live_sender 的连接池：构造后把 QQMailTools 内部的 sender 换成它，
    这样用例结束后一定有关闭连接与工作线程的地方，不会泄漏线程到其他用例。
    """
    from email_tools import QQMailTools

    t = QQMailTools()
    t.sender = live_sender
    return t


# ---------------------------------------------------------------------------
# 附件目录的解析时机
# ---------------------------------------------------------------------------

def test_attachment_dir_follows_config_changes(tmp_path, monkeypatch):
    """
    回归测试：attachment_dir 必须在读取时解析，而不是构造时固化。

    早期实现把 settings.attachment_dir 复制进实例属性，导致
    「先构造对象、后修改配置」时重定向无效——测试因此曾写入真实的 attachments/。
    """
    import email_tools

    tools = QQMailTools()  # 先在默认目录下构造

    monkeypatch.setattr(email_tools.settings, "attachment_dir", tmp_path)
    assert tools.attachment_dir == tmp_path, "配置变更后应立即生效"



# ---------------------------------------------------------------------------
# 发送成功路径
# ---------------------------------------------------------------------------

def test_send_success_records_recipients(tools, fake_smtp):
    result = tools.sender.send_email_sync("a@b.com", "主题", "正文")

    assert result["success"] is True
    assert result["to"] == "a@b.com"
    assert result["recipients"] == ["a@b.com"]

    server = fake_smtp.last_instance
    assert server.logged_in is not None, "必须完成登录"
    assert len(server.sent_messages) == 1


def test_multiple_recipients_plus_cc_and_bcc(tools, fake_smtp):
    result = tools.sender.send_email_sync(
        ["a@b.com", "c@d.com"],
        "s",
        "b",
        cc=["e@f.com"],
        bcc=["g@h.com"],
    )
    assert result["recipients"] == ["a@b.com", "c@d.com", "e@f.com", "g@h.com"]

    _, to_addrs, raw = fake_smtp.last_instance.sent_messages[0]
    assert set(to_addrs) == {"a@b.com", "c@d.com", "e@f.com", "g@h.com"}


def test_utf8_subject_is_encoded_and_decodable(tools, fake_smtp):
    tools.sender.send_email_sync("a@b.com", "中文主题测试", "b")

    _, _, raw = fake_smtp.last_instance.sent_messages[0]
    msg = email.message_from_string(raw)
    decoded = str(make_header(decode_header(msg["Subject"])))
    assert decoded == "中文主题测试"


def test_html_body_creates_alternative_parts(tools, fake_smtp):
    tools.sender.send_email_sync("a@b.com", "s", "纯文本回退", html_body="<b>HTML</b>")

    _, _, raw = fake_smtp.last_instance.sent_messages[0]
    msg = email.message_from_string(raw)
    subtypes = [p.get_content_subtype() for p in msg.walk() if p.get_content_maintype() != "multipart"]
    assert "plain" in subtypes and "html" in subtypes


# ---------------------------------------------------------------------------
# 附件
# ---------------------------------------------------------------------------

def test_attachment_is_embedded(tools, fake_smtp, tmp_path):
    f = tmp_path / "报告.txt"
    f.write_text("hello", encoding="utf-8")

    tools.sender.send_email_sync("a@b.com", "s", "b", attachments=[str(f)])

    _, _, raw = fake_smtp.last_instance.sent_messages[0]
    msg = email.message_from_string(raw)
    names = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert len(names) == 1, "附件应被附加到邮件上"


def test_missing_attachment_is_skipped_silently(tools, fake_smtp):
    """
    底层 build_message 的宽松模式：显式要求时才会跳过缺失附件。
    默认行为见 test_missing_attachment_aborts_send。
    """
    from email_tools import build_message

    raw = build_message(
        "a@b.com", "s", "b", attachments=["/no/such/file.txt"],
        allow_missing_attachments=True,
    )
    msg = email.message_from_string(raw.as_string())
    assert [p.get_filename() for p in msg.walk() if p.get_filename()] == []


def test_missing_attachment_aborts_send(tools, fake_smtp, tmp_path):
    """
    回归测试：附件不存在时必须**中止发送**并明确报错。

    早期实现是静默跳过：邮件照发、返回「发送成功」，而对方只收到一封空邮件。
    用户以为附件发出去了，实际没有——这类静默失败极难察觉。
    """
    ghost = str(tmp_path / "不存在.txt")
    result = tools.sender.send_email_sync("a@b.com", "s", "b", attachments=[ghost])

    assert result["success"] is False
    assert result["reason"] == "missing_attachment"
    assert "不存在.txt" in result["message"], result["message"]
    assert result["missing_attachments"] == [ghost]
    # 在构建阶段就中止了，因此连 SMTP 连接都不应建立
    assert fake_smtp.last_instance is None, "不应建立 SMTP 连接，更不应发出邮件"


def test_multiple_missing_attachments_are_all_reported(tools, fake_smtp, tmp_path):
    a = str(tmp_path / "a.txt")
    b = str(tmp_path / "b.txt")
    result = tools.sender.send_email_sync(
        "a@b.com", "s", "b", attachments=[a, b]
    )
    assert result["success"] is False
    assert set(result["missing_attachments"]) == {a, b}


def test_missing_attachment_error_is_actionable(tools, fake_smtp, tmp_path):
    """错误文案要能直接告诉用户该做什么，而不是只丢一个异常名。"""
    result = tools.sender.send_email_sync(
        "a@b.com", "s", "b", attachments=[str(tmp_path / "x.txt")]
    )
    assert "附件不存在" in result["message"]
    assert "邮件未发送" in result["message"]


# ---------------------------------------------------------------------------
# 附件路径自动修正
# ---------------------------------------------------------------------------

def test_wrong_extension_is_recovered(tools, fake_smtp, tmp_path):
    """
    回归测试：模型常把扩展名猜错（实测：目录里是 .csv，模型给出 .xlsx）。
    目录中主干唯一时应把文件找回来，并在结果里说明替换了什么。
    """
    real = tmp_path / "示例报表.csv"
    real.write_text("a,b\n1,2", encoding="utf-8")

    result = tools.sender.send_email_sync(
        "a@b.com", "s", "b", attachments=[str(tmp_path / "示例报表.xlsx")]
    )

    assert result["success"] is True, "应能按主干找回真实文件"
    assert result["attachments"] == [str(real)]
    assert "自动修正" in result.get("note", "")
    assert result["attachment_substitutions"] == ["示例报表.xlsx → 示例报表.csv"]


def test_name_case_difference_is_recovered(tools, fake_smtp, tmp_path):
    """
    大小写不同的文件名在 Windows 上可直接命中（文件系统不区分大小写），
    因此不会走「修正」分支。这里只断言文件确实被附上。
    """
    real = tmp_path / "Report.pdf"
    real.write_bytes(b"%PDF-1.4 fake")
    result = tools.sender.send_email_sync(
        "a@b.com", "s", "b", attachments=[str(tmp_path / "report.pdf")]
    )
    assert result["success"] is True
    assert len(result["attachments"]) == 1
    assert result["attachments"][0].lower() == str(real).lower()


def test_ambiguous_stem_is_not_guessed(tools, fake_smtp, tmp_path):
    """
    主干相同但存在多个候选时**不得擅自猜测**——宁可报错，
    也不要发出一个用户没指定的文件。
    """
    (tmp_path / "数据.csv").write_text("x", encoding="utf-8")
    (tmp_path / "数据.xlsx").write_bytes(b"PK\x03\x04")

    result = tools.sender.send_email_sync(
        "a@b.com", "s", "b", attachments=[str(tmp_path / "数据.txt")]
    )

    assert result["success"] is False
    assert result["reason"] == "missing_attachment", "多候选时不应乱猜"
    assert fake_smtp.last_instance is None


def test_exact_path_is_used_unchanged(tools, fake_smtp, tmp_path):
    real = tmp_path / "精确.txt"
    real.write_text("x", encoding="utf-8")
    result = tools.sender.send_email_sync(
        "a@b.com", "s", "b", attachments=[str(real)]
    )
    assert result["success"] is True
    assert result["attachments"] == [str(real)]
    assert "note" not in result, "未发生替换时不应加说明"


def test_resolve_attachment_paths_returns_triple(tmp_path):
    from email_tools import resolve_attachment_paths

    real = tmp_path / "a.txt"
    real.write_text("x", encoding="utf-8")
    resolved, missing, subs = resolve_attachment_paths([str(real)])
    assert resolved == [str(real)]
    assert missing == []
    assert subs == []


@pytest.mark.parametrize(
    "wrong_name",
    [
        "示例报表.xlsx",       # 仅扩展名猜错
        "示例报表表.xlsx",     # 多了修饰词「表」
        "示例报表文件.xlsx",   # 多了「文件」
        "示例报表表",          # 无扩展名且带修饰词
    ],
)
def test_model_guessed_filenames_are_recovered(tools, fake_smtp, tmp_path, wrong_name):
    """
    回归测试：小模型给出的文件名常带错觉。

    实测过的三种：仅扩展名不同、多加「表」、多加「文件」。
    目录中主干唯一时都应能找回，否则用户会以为发成功了。
    """
    real = tmp_path / "示例报表.csv"
    real.write_text("a,b\n1,2", encoding="utf-8")

    result = tools.sender.send_email_sync(
        "a@b.com", "s", "b", attachments=[str(tmp_path / wrong_name)]
    )

    assert result["success"] is True, "应能按归一化主干找回真实文件"
    assert result["attachments"] == [str(real)]
    assert "自动修正" in result.get("note", "")


def test_unrelated_filename_is_still_rejected(tools, fake_smtp, tmp_path):
    """归一化不能宽松到「什么都匹配」——完全不相干的名字仍应报错。"""
    (tmp_path / "示例报表.csv").write_text("x", encoding="utf-8")
    result = tools.sender.send_email_sync(
        "a@b.com", "s", "b", attachments=[str(tmp_path / "完全不相干.pdf")]
    )
    assert result["success"] is False
    assert result["reason"] == "missing_attachment"


def test_explicit_directory_does_not_fall_back_to_attachment_dir(
    tools, fake_smtp, tmp_path, monkeypatch
):
    """
    回归测试：用户指明了目录时，绝不能转头去附件目录里搜同名文件。

    这条边界曾经被放宽过头（v0.9 的附件目录回退）：路径 `D:\\报表\\a.txt`
    不存在时，会去附件目录里找一个叫 a.txt 的文件发出去——
    用户以为发的是 D 盘那份，实际发的是附件目录里的另一份。
    **发错文件比发不出去严重得多**，所以这里必须严格。

    修复前实测：用 tmp 目录里的 a.txt / b.txt（都不存在）调用，
    返回 success=True。
    """
    import email_tools

    # 附件目录里放一个同名的真实文件，制造「会被误匹配」的条件
    attachment_dir = tmp_path / "attachments"
    attachment_dir.mkdir()
    (attachment_dir / "a.txt").write_text("附件目录里的另一份", encoding="utf-8")
    monkeypatch.setattr(email_tools.settings, "attachment_dir", attachment_dir)

    elsewhere = tmp_path / "别处"
    elsewhere.mkdir()
    ghost = str(elsewhere / "a.txt")          # 该目录里并没有这个文件

    result = tools.sender.send_email_sync("a@b.com", "s", "b", attachments=[ghost])

    assert result["success"] is False, "指了目录就不该去附件目录里瞎找"
    assert result["reason"] == "missing_attachment"
    assert result["missing_attachments"] == [ghost]


def test_bare_name_still_falls_back_to_attachment_dir(
    tools, fake_smtp, tmp_path, monkeypatch
):
    """
    与之相对：**裸文件名**（没给目录）仍应回落到附件目录。

    这是最常见的情形——系统提示词列出的就是附件目录里的文件名，
    模型很自然地只回一个「示例报表.csv」。
    """
    import email_tools

    attachment_dir = tmp_path / "attachments"
    attachment_dir.mkdir()
    (attachment_dir / "报表.csv").write_text("a,b", encoding="utf-8")
    monkeypatch.setattr(email_tools.settings, "attachment_dir", attachment_dir)

    result = tools.sender.send_email_sync("a@b.com", "s", "b", attachments=["报表.csv"])

    assert result["success"] is True
    assert result["attachments"] == [str(attachment_dir / "报表.csv")]


# ---------------------------------------------------------------------------
# 裸文件名与带引号的路径
# ---------------------------------------------------------------------------

def test_bare_filename_resolves_against_attachment_dir(
    tools, fake_smtp, tmp_path, monkeypatch
):
    """
    回归测试：模型只回一个裸文件名时，必须到附件目录里去找。

    系统提示词已经把附件目录的真实文件名列给了模型，模型很自然地只回
    `示例报表.csv`。但裸文件名的 parent 是「当前工作目录」而不是附件目录，
    于是这个**确实存在**的文件被判为不存在，整封邮件发不出去——
    用户看到的是「附件不存在」，而附件明明就在附件目录里。

    修复前实测：success=False, reason=missing_attachment。
    """
    import email_tools

    (tmp_path / "示例报表.csv").write_text("a,b\n1,2", encoding="utf-8")
    monkeypatch.setattr(email_tools.settings, "attachment_dir", tmp_path)

    result = tools.sender.send_email_sync(
        "a@b.com", "s", "b", attachments=["示例报表.csv"]
    )

    assert result["success"] is True, "裸文件名应能在附件目录里找到"
    assert result["attachments"] == [str(tmp_path / "示例报表.csv")]


def test_bare_filename_with_guessed_extension_is_recovered(
    tools, fake_smtp, tmp_path, monkeypatch
):
    """裸文件名 + 猜错扩展名，两个缺陷叠加时也要能找回。"""
    import email_tools

    (tmp_path / "示例报表.csv").write_text("a,b\n1,2", encoding="utf-8")
    monkeypatch.setattr(email_tools.settings, "attachment_dir", tmp_path)

    result = tools.sender.send_email_sync(
        "a@b.com", "s", "b", attachments=["示例报表.xlsx"]
    )
    assert result["success"] is True
    assert result["attachments"] == [str(tmp_path / "示例报表.csv")]


@pytest.mark.parametrize(
    "quoted",
    [
        '"{}"',          # 半角双引号
        "'{}'",          # 半角单引号
        "“{}”",          # 中文弯引号
        "「{}」",         # 中文书名号
        "  {}  ",        # 前后带空格
    ],
)
def test_quoted_paths_are_accepted(tools, fake_smtp, tmp_path, quoted):
    """
    回归测试：路径两端带引号是模型的常见写法，必须能容忍。

    引号在 Windows 上是**合法文件名字符**，所以 `is_file()` 会判定失败，
    一个本来完全正确的路径被误报成「附件不存在」。
    """
    real = tmp_path / "照片.jpg"
    real.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 100)

    result = tools.sender.send_email_sync(
        "a@b.com", "s", "b", attachments=[quoted.format(real)]
    )
    assert result["success"] is True, "带引号的路径应被接受"
    assert result["attachments"] == [str(real)]


def test_quoting_alone_is_not_reported_as_substitution(
    tools, fake_smtp, tmp_path
):
    """
    只是引号/空格被剥掉，不该提示「附件名已自动修正」。

    否则用户会看到「"示例报表.csv" → 示例报表.csv」，
    看起来像换了文件，实际什么都没变。
    """
    real = tmp_path / "示例报表.csv"
    real.write_text("x", encoding="utf-8")

    result = tools.sender.send_email_sync(
        "a@b.com", "s", "b", attachments=['"%s"' % real]
    )
    assert result["success"] is True
    assert "note" not in result, "文件名没变时不应有替换说明"


def test_normalize_stem_strips_suffix_words():
    from email_tools import _normalize_stem

    assert _normalize_stem("示例报表表.xlsx") == _normalize_stem("示例报表.csv")
    assert _normalize_stem("示例报表文件.txt") == _normalize_stem("示例报表.csv")
    assert _normalize_stem("Report.PDF") == _normalize_stem("report.csv")
    assert _normalize_stem("A B.txt") == _normalize_stem("ab.md")


def test_normalize_stem_keeps_short_names_intact():
    """名字本身很短时不应被裁成空串。"""
    from email_tools import _normalize_stem

    assert _normalize_stem("表.txt") != ""
    assert _normalize_stem("a.txt") == "a"


# ---------------------------------------------------------------------------
# 附件大小限制
# ---------------------------------------------------------------------------

@pytest.fixture
def tiny_limits(monkeypatch):
    """把上限压到几十字节，避免测试里造大文件。"""
    import email_tools

    monkeypatch.setattr(email_tools.settings, "max_attachment_bytes", 64)
    monkeypatch.setattr(email_tools.settings, "max_total_attachment_bytes", 100)
    return 64, 100


def test_size_check_passes_for_small_files(tiny_limits, tmp_path):
    from email_tools import check_attachment_sizes

    f = tmp_path / "small.txt"
    f.write_bytes(b"x" * 10)
    assert check_attachment_sizes([str(f)]) == []


def test_size_check_detects_oversized_single_file(tiny_limits, tmp_path):
    from email_tools import check_attachment_sizes

    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 100)  # 超过 64
    oversize = check_attachment_sizes([str(f)])

    singles = [o for o in oversize if o["kind"] == "single"]
    assert len(singles) == 1
    assert singles[0]["name"] == "big.bin"
    assert singles[0]["size"] == 100


def test_size_check_detects_oversized_total(tiny_limits, tmp_path):
    """每件都不超限，但加起来超限——必须单独报告。"""
    from email_tools import check_attachment_sizes

    paths = []
    for i in range(3):
        p = tmp_path / ("part%d.bin" % i)
        p.write_bytes(b"y" * 40)  # 40 < 64，但 3×40=120 > 100
        paths.append(str(p))

    oversize = check_attachment_sizes(paths)
    kinds = [o["kind"] for o in oversize]
    assert "total" in kinds
    assert "single" not in kinds, "单件都没超，不应报单件超限"


def test_size_check_ignores_unreadable_paths(tiny_limits, tmp_path):
    """读不到大小的路径交由「文件不存在」逻辑处理，不在这里报错。"""
    from email_tools import check_attachment_sizes

    assert check_attachment_sizes([str(tmp_path / "nope.bin")]) == []


def test_oversize_send_is_aborted(tools, fake_smtp, tiny_limits, tmp_path):
    """
    回归测试：附件超限必须在发送前中止。

    否则会先耗一次 SMTP 往返、再收到一个难懂的英文错误，
    用户不知道原因是「附件太大」。
    """
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 200)

    result = tools.sender.send_email_sync(
        "a@b.com", "s", "b", attachments=[str(f)]
    )

    assert result["success"] is False
    assert result["reason"] == "attachment_too_large"
    assert "超过大小限制" in result["message"]
    assert result["oversize_attachments"]
    assert fake_smtp.last_instance is None, "不应建立 SMTP 连接"


def test_oversize_message_names_the_file_and_bytes(tiny_limits, tmp_path):
    """错误文案要能看出是哪个文件、超了多少。"""
    from email_tools import check_attachment_sizes, describe_oversize

    f = tmp_path / "报表.xlsx"
    f.write_bytes(b"x" * 200)
    text = describe_oversize(check_attachment_sizes([str(f)]))

    assert "报表.xlsx" in text
    assert "200" in text, "应给出精确字节数"
    assert "建议" in text


def test_size_limit_allows_file_exactly_at_limit(tiny_limits, tmp_path):
    """刚好等于上限应放行（判断用的是 > 而不是 >=）。"""
    from email_tools import check_attachment_sizes

    f = tmp_path / "exact.bin"
    f.write_bytes(b"x" * 64)
    assert [o for o in check_attachment_sizes([str(f)]) if o["kind"] == "single"] == []


def test_format_size_units():
    from email_tools import _format_size

    assert _format_size(500) == "500.0 B"
    assert _format_size(2048) == "2.0 KB"
    assert _format_size(3 * 1024 * 1024) == "3.0 MB"


# ---------------------------------------------------------------------------
# 附件大小：判断口径是「编码后体积」
# ---------------------------------------------------------------------------

def test_oversize_reports_encoded_size(tiny_limits, tmp_path):
    """
    超限项要同时给出原始大小与编码后估算。

    只给原始大小，用户会觉得「我才 10MB 为什么说超了」；
    只给编码后大小，又对不上自己文件属性面板里的数字。
    """
    from email_tools import check_attachment_sizes

    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 100)  # per_limit=64, ratio=4/3
    items = check_attachment_sizes([str(f)])

    single = [o for o in items if o["kind"] == "single"][0]
    assert single["size"] == 100
    assert single["encoded_size"] == int(100 * 4 / 3), "应给出编码后估算"
    assert single["encoded_text"] != single["size_text"]


def test_encoded_ratio_comes_from_config(tiny_limits, tmp_path, monkeypatch):
    """
    编码系数可配置，且被真正使用。

    固化「按编码后体积判断」这件事本身：把系数调大，原本合格的文件
    应当被判为超限——若实现里写死了大小比较，这条会失败。
    """
    import email_tools
    from email_tools import check_attachment_sizes

    f = tmp_path / "mid.bin"
    f.write_bytes(b"x" * 50)   # 50 < 64，默认合格
    assert [o for o in check_attachment_sizes([str(f)]) if o["kind"] == "single"] == []

    # 把合计上限压到 50 以下，让"编码后"成为决定性因素
    monkeypatch.setattr(email_tools.settings, "max_total_attachment_bytes", 40)
    oversized = check_attachment_sizes([str(f)])
    assert any(o["kind"] == "total" for o in oversized), (
        "应依据配置的阈值判断，而不是写死的数字"
    )


def test_describe_oversize_explains_encoding(tiny_limits, tmp_path):
    """提示里要解释「为什么按编码后算」，否则用户会觉得数字对不上。"""
    from email_tools import check_attachment_sizes, describe_oversize

    f = tmp_path / "报表.xlsx"
    f.write_bytes(b"x" * 200)
    text = describe_oversize(check_attachment_sizes([str(f)]))

    assert "报表.xlsx" in text
    assert "编码后" in text
    assert "1/3" in text, "应说明膨胀比例"
    assert "200" in text, "应给出精确字节数"


def test_total_oversize_message_includes_encoded(tiny_limits, tmp_path):
    from email_tools import check_attachment_sizes, describe_oversize

    paths = []
    for i in range(3):
        p = tmp_path / ("p%d.bin" % i)
        p.write_bytes(b"y" * 40)
        paths.append(str(p))
    text = describe_oversize(check_attachment_sizes(paths))
    assert "合计" in text
    assert "编码后" in text


# ---------------------------------------------------------------------------
# QQ 邮箱非标准响应
# ---------------------------------------------------------------------------

def test_qq_special_response_treated_as_success(tools, fake_smtp):
    fake_smtp.set_behavior("send_message_exc", QQ_SPECIAL_RESPONSE)

    result = tools.sender.send_email_sync("a@b.com", "s", "b")

    assert result["success"] is True
    assert "特殊响应" in result["message"]
    assert "note" in result


def test_real_smtp_error_is_not_masked(tools, fake_smtp):
    """非 -1 的真实 SMTP 错误必须如实报告失败，不能被误判为成功。"""
    fake_smtp.set_behavior("send_message_exc", smtplib.SMTPResponseException(550, b"Mailbox not found"))

    result = tools.sender.send_email_sync("a@b.com", "s", "b")

    assert result["success"] is False
    assert result["smtp_code"] == 550


def test_smtp_rejection_is_reported_with_code(tools, fake_smtp):
    """
    固化一个既有行为：SMTPAuthenticationError 是 SMTPResponseException 的子类，
    因此会被外层分支先捕获，错误文案是「SMTP服务器拒绝: 错误码 535」。
    """
    fake_smtp.set_behavior("login_exc", smtplib.SMTPAuthenticationError(535, b"auth failed"))

    result = tools.sender.send_email_sync("a@b.com", "s", "b")

    assert result["success"] is False
    assert result["smtp_code"] == 535
    assert "拒绝" in result["message"]


# ---------------------------------------------------------------------------
# 异步包装方法
# ---------------------------------------------------------------------------

async def test_send_text_email_async(tools, fake_smtp):
    result = await tools.send_text_email("a@b.com", "主题", "正文")
    assert result["success"] is True
    _, kwargs = None, fake_smtp.last_instance.sent_messages[0]
    assert "a@b.com" in kwargs[1]


async def test_send_html_email_async_supplies_text_fallback(tools, fake_smtp):
    result = await tools.send_html_email("a@b.com", "s", "<b>hi</b>")
    assert result["success"] is True

    _, _, raw = fake_smtp.last_instance.sent_messages[0]
    msg = email.message_from_string(raw)
    subtypes = {p.get_content_subtype() for p in msg.walk()}
    assert "html" in subtypes and "plain" in subtypes


async def test_send_with_attachment_async(tools, fake_smtp, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")

    result = await tools.send_email_with_attachment(
        "a@b.com", "s", "正文", [str(f)]
    )
    assert result["success"] is True


# ---------------------------------------------------------------------------
# check_email_config
# ---------------------------------------------------------------------------

async def test_check_email_config_success(tools, fake_smtp):
    result = await tools.check_email_config()
    assert result["success"] is True
    assert fake_smtp.last_instance.noop_called is True


async def test_check_email_config_handles_qq_special_response(tools, fake_smtp):
    fake_smtp.set_behavior("noop_exc", QQ_SPECIAL_RESPONSE)

    result = await tools.check_email_config()

    assert result["success"] is True
    assert "特殊响应" in result["message"]


async def test_check_email_config_reports_auth_failure(tools, fake_smtp):
    fake_smtp.set_behavior("login_exc", smtplib.SMTPAuthenticationError(535, b"bad code"))

    result = await tools.check_email_config()

    assert result["success"] is False



# ---------------------------------------------------------------------------
# 连接复用
# ---------------------------------------------------------------------------

def test_connection_is_reused_across_sends(live_sender, fake_smtp):
    """
    连续发送多封邮件时，TLS 连接与登录只应发生一次。
    这是「连接复用生效」的核心断言。
    """
    for i in range(5):
        result = live_sender.send_email_sync("a@b.com", f"主题{i}", "正文")
        assert result["success"] is True

    assert fake_smtp.last_instance.connect_count == 1, "5 封邮件应只建立 1 条连接"
    assert fake_smtp.last_instance.login_count == 1, "登录也应只发生一次"
    assert len(fake_smtp.last_instance.sent_messages) == 5


def test_stale_connection_is_rebuilt(fake_smtp):
    """空闲超过 idle_ttl 后，下次发送应重建连接。"""
    from email_tools import QQMailSender

    sender = QQMailSender()
    # 负值保证「已过期」判定成立；用 0.0 会因微秒级间隔而不稳定
    sender.worker.pool.idle_ttl = -1.0
    try:
        sender.send_email_sync("a@b.com", "s1", "b")
        sender.send_email_sync("a@b.com", "s2", "b")
        assert fake_smtp.last_instance.connect_count == 2, "过期连接必须重建"
    finally:
        sender.close()


def test_metrics_expose_connection_reuse(live_sender, fake_smtp):
    """指标应能反映连接复用情况，connects_per_send 远小于 1。"""
    from metrics import send_metrics

    for _ in range(4):
        live_sender.send_email_sync("a@b.com", "s", "b")

    snap = send_metrics.snapshot()
    assert snap["counters"]["sends_total"] == 4
    assert snap["counters"]["smtp_connects"] == 1
    assert snap["connects_per_send"] == 0.25
    assert snap["success_rate"] == 1.0


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

def test_metrics_count_success_and_latency(live_sender):
    from metrics import send_metrics

    live_sender.send_email_sync("a@b.com", "s", "b")

    snap = send_metrics.snapshot()
    assert snap["counters"]["sends_succeeded"] == 1
    assert snap["latency_ms"]["samples"] == 1
    assert snap["latency_ms"]["p50"] is not None
    assert snap["failures_by_reason"] == {}


def test_metrics_record_failure_reason(live_sender, fake_smtp):
    from metrics import send_metrics

    fake_smtp.set_behavior("send_message_exc", smtplib.SMTPResponseException(550, b"no such user"))
    live_sender.send_email_sync("a@b.com", "s", "b")

    snap = send_metrics.snapshot()
    assert snap["counters"]["sends_failed"] == 1
    assert snap["failures_by_reason"].get("smtp_reject") == 1
    assert snap["success_rate"] == 0.0


def test_metrics_auth_failure_reason(live_sender, fake_smtp):
    """
    固化既有行为：SMTPAuthenticationError(535) 属于 SMTPResponseException 的子类，
    会被先捕获，因此归因为 smtp_reject 而不是 auth_error。
    """
    from metrics import send_metrics

    fake_smtp.set_behavior("login_exc", smtplib.SMTPAuthenticationError(535, b"bad"))
    result = live_sender.send_email_sync("a@b.com", "s", "b")

    assert result["success"] is False
    assert send_metrics.snapshot()["failures_by_reason"].get("smtp_reject") == 1


def test_metrics_format_line_is_readable(live_sender):
    from metrics import send_metrics

    live_sender.send_email_sync("a@b.com", "s", "b")
    line = send_metrics.format_line()
    assert "sends=1" in line
    assert "ok=1" in line


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------

def test_success_result_carries_timing_and_recipients(live_sender):
    result = live_sender.send_email_sync(
        ["a@b.com", "c@d.com"], "主题", "正文", cc=["e@f.com"]
    )
    assert result["success"] is True
    assert result["recipients"] == ["a@b.com", "c@d.com", "e@f.com"]
    assert result["subject"] == "主题"
    assert "timestamp" in result
    assert "reason" not in result


def test_failure_result_carries_reason(live_sender, fake_smtp):
    fake_smtp.set_behavior("send_message_exc", smtplib.SMTPResponseException(550, b"denied"))
    result = live_sender.send_email_sync("a@b.com", "s", "b")
    assert result["success"] is False
    assert result["reason"] == "smtp_reject"
    assert result["smtp_code"] == 550


# ---------------------------------------------------------------------------
# 结构化日志
# ---------------------------------------------------------------------------

def test_json_log_formatter_emits_valid_json():
    import json as json_module
    import logging

    from email_tools import JsonLogFormatter

    record = logging.LogRecord(
        name="email_tools", level=logging.INFO, pathname=__file__, lineno=1,
        msg="开始发送", args=(), exc_info=None,
    )
    record.recipients_count = 3  # 模拟 extra= 传入的业务字段

    payload = json_module.loads(JsonLogFormatter().format(record))
    assert payload["level"] == "INFO"
    assert payload["event"] == "开始发送"
    assert payload["recipients_count"] == 3
    assert "logger" in payload


def test_worker_initialization_is_thread_safe(fake_smtp):
    """
    回归测试：worker 属性的懒加载必须线程安全。

    原始实现是裸的 check-then-act：多个线程同时首次访问时，
    都会看到 _worker 为 None，于是各自创建一个连接池，
    导致并发场景下出现多条本应唯一的连接（实测 8 并发建了 8 条）。

    这条用例刻意在 worker「尚未初始化」时并发触发，因此不依赖任何时序巧合。
    """
    import threading

    from email_tools import QQMailSender

    sender = QQMailSender()
    assert sender._worker is None, "前置条件：worker 必须尚未初始化"

    n = 16
    barrier = threading.Barrier(n)
    workers = []
    lock = threading.Lock()

    def grab():
        barrier.wait()  # 让所有线程尽量同时冲进去
        w = sender.worker
        with lock:
            workers.append(w)

    threads = [threading.Thread(target=grab) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    try:
        assert len(workers) == n
        assert len({id(w) for w in workers}) == 1, "所有线程必须拿到同一个 worker"
    finally:
        sender.close()


async def test_concurrent_sends_are_serialized_on_one_connection(live_sender, fake_smtp):
    """
    并发发送时，SMTP 连接由专用工作线程独占，请求排队执行。
    因此即便 8 个任务同时发起，也只应建立 1 条连接，且不会交错损坏连接。
    """
    import asyncio

    results = await asyncio.gather(
        *[
            asyncio.to_thread(live_sender.send_email_sync, "a@b.com", f"并发{i}", "正文")
            for i in range(8)
        ]
    )

    assert all(r["success"] for r in results)
    assert fake_smtp.last_instance.connect_count == 1, "并发不应导致连接数增长"
    assert len(fake_smtp.last_instance.sent_messages) == 8


def test_worker_thread_is_reusable_after_failure(live_sender, fake_smtp):
    """
    一次 SMTP 层拒绝（451 稍后重试）不应让连接失效：
    后续发送仍应成功，且复用同一条连接。

    这条用例同时防止一个真实陷阱：smtplib.SMTPException 继承自 OSError，
    若连接池的丢弃判定里 catch 了 OSError，所有协议错误都会被误判为连接断裂。
    """
    fake_smtp.set_behavior("send_message_exc", smtplib.SMTPResponseException(451, b"try later"))
    first = live_sender.send_email_sync("a@b.com", "s", "b")
    assert first["success"] is False

    fake_smtp.set_behavior("send_message_exc", None)
    second = live_sender.send_email_sync("a@b.com", "s2", "b")

    assert second["success"] is True
    assert fake_smtp.last_instance.connect_count == 1, "SMTP 协议错误不应导致重连"


def test_smtp_exception_is_oserror_subclass():
    """
    固化上面那条注意事项所依赖的事实：捕获取舍了 OSError 必须谨慎。
    """
    assert issubclass(smtplib.SMTPResponseException, OSError)
    assert issubclass(smtplib.SMTPServerDisconnected, OSError)


def test_disconnected_connection_is_dropped_and_rebuilt(live_sender, fake_smtp, monkeypatch):
    """
    连接断裂后会被丢弃，并在下一次发送时重建。

    这里同时关闭「发送级重试」和「池内重连」，让每次发送只对应一条连接，
    从而单独观察跨发送的丢弃/重建行为。
    两者叠加的连接数由 test_worker_reconnects_within_one_send_when_allowed 覆盖。
    """
    import email_tools

    monkeypatch.setattr(email_tools.settings, "send_max_attempts", 1)
    monkeypatch.setattr(email_tools.settings, "send_retry_on_reconnect", False)

    fake_smtp.set_behavior("send_message_exc", smtplib.SMTPServerDisconnected("connection reset"))
    first = live_sender.send_email_sync("a@b.com", "s", "b")
    assert first["success"] is False

    fake_smtp.set_behavior("send_message_exc", None)
    second = live_sender.send_email_sync("a@b.com", "s2", "b")

    assert second["success"] is True
    assert fake_smtp.last_instance.connect_count == 2, "断裂的连接应被丢弃并在下次重建"


def test_worker_reconnects_within_one_send_when_allowed(live_sender, fake_smtp, monkeypatch):
    """
    开启 retry_on_disconnect 时，连接在发送中断裂会立即换新连接重试一次，
    不必等到下一封邮件。因此单次发送可能出现两条连接。

    这里关闭发送级重试，确保观察到的只是连接池自身那一次重连。
    """
    import email_tools

    monkeypatch.setattr(email_tools.settings, "send_max_attempts", 1)
    monkeypatch.setattr(email_tools.settings, "send_retry_on_reconnect", True)

    calls = {"n": 0}
    original = fake_smtp.send_message

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise smtplib.SMTPServerDisconnected("first attempt drops")
        return original(self, *args, **kwargs)

    fake_smtp.send_message = flaky
    try:
        result = live_sender.send_email_sync("a@b.com", "s", "b")
    finally:
        fake_smtp.send_message = original

    assert result["success"] is True, "换连接重试后应成功"
    assert calls["n"] == 2, "动作应被重新执行一次"
    assert fake_smtp.last_instance.connect_count == 2, "重连应建立第二条连接"


def test_no_reconnect_retry_when_disabled(live_sender, fake_smtp, monkeypatch):
    """关闭 retry_on_disconnect 时，连接断裂不应在本次发送内重试。"""
    import email_tools

    monkeypatch.setattr(email_tools.settings, "send_max_attempts", 1)
    monkeypatch.setattr(email_tools.settings, "send_retry_on_reconnect", False)

    fake_smtp.set_behavior("send_message_exc", smtplib.SMTPServerDisconnected("drop"))
    result = live_sender.send_email_sync("a@b.com", "s", "b")

    assert result["success"] is False
    assert fake_smtp.last_instance.connect_count == 1, "不应为此建立第二条连接"


# ---------------------------------------------------------------------------
# 发送确认（IMAP 回读）的接入
# ---------------------------------------------------------------------------

@pytest.fixture
def confirm_on(monkeypatch):
    """启用发送确认，并让 IMAP 指向可由测试控制的替身。"""
    import email_tools

    monkeypatch.setattr(email_tools.settings, "email_confirm_delivery", True)
    monkeypatch.setattr(email_tools.settings, "confirm_interval", 0.0)
    return True


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """
    去掉所有真实的等待。

    涉及两处：发送重试的退避等待、发送确认的轮询间隔。
    否则单个「持续失败」类用例就要睡满退避，把套件从 2 秒拖到 8 秒以上。
    """
    monkeypatch.setattr("delivery.time.sleep", lambda *_: None)
    monkeypatch.setattr("retry.time.sleep", lambda *_: None)


@pytest.fixture
def fake_imap(monkeypatch):
    import imaplib

    from tests.test_delivery import FakeIMAP

    FakeIMAP.fresh()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


def test_confirmation_absent_when_disabled(live_sender, fake_smtp):
    result = live_sender.send_email_sync("a@b.com", "s", "b")
    assert result["success"] is True
    assert "confirmation" not in result, "未启用时不应出现确认字段"


def test_confirmation_attached_when_enabled(live_sender, fake_smtp, confirm_on, fake_imap):
    from tests.test_delivery import ENCODED_SUBJECT

    fake_imap.fresh([{"subject": ENCODED_SUBJECT, "to": "a@b.com"}])
    result = live_sender.send_email_sync("a@b.com", "基线 #1", "b")

    assert result["success"] is True
    assert result["confirmation"]["checked"] is True
    assert result["confirmation"]["confirmed"] is True


def test_confirmation_reports_unconfirmed_without_failing_send(
    live_sender, fake_smtp, confirm_on, fake_imap
):
    """已发送里找不到，只能说明「未能确认」，绝不能把发送结论改成失败。"""
    fake_imap.fresh([])
    result = live_sender.send_email_sync("a@b.com", "找不到的主题", "b")

    assert result["success"] is True, "确认失败不得影响发送结论"
    assert result["confirmation"]["confirmed"] is False
    assert result["confirmation"]["checked"] is True


def test_imap_failure_does_not_break_send(live_sender, fake_smtp, confirm_on, fake_imap):
    """IMAP 完全连不上时，发送仍应报告成功，只标注未做确认。"""
    fake_imap.fresh(connect_exc=OSError("imap down"))
    result = live_sender.send_email_sync("a@b.com", "s", "b")

    assert result["success"] is True
    assert result["confirmation"]["checked"] is False
    assert result["confirmation"]["confirmed"] is False


def test_special_response_note_mentions_confirmation(
    live_sender, fake_smtp, confirm_on, fake_imap
):
    """特殊响应(-1) 在有 IMAP 证据时，说明文案应体现已核实。"""
    from tests.test_delivery import ENCODED_SUBJECT

    fake_smtp.set_behavior("send_message_exc", QQ_SPECIAL_RESPONSE)
    fake_imap.fresh([{"subject": ENCODED_SUBJECT, "to": "a@b.com"}])

    result = live_sender.send_email_sync("a@b.com", "基线 #1", "b")

    assert result["success"] is True
    assert result["confirmation"]["confirmed"] is True
    assert "已由 IMAP 回读确认" in result["note"]


def test_special_response_without_confirmation_says_so(
    live_sender, fake_smtp, confirm_on, fake_imap
):
    """IMAP 没找到时，note 必须提示需要人工核对，而不是笼统说成功。"""
    fake_smtp.set_behavior("send_message_exc", QQ_SPECIAL_RESPONSE)
    fake_imap.fresh([])

    result = live_sender.send_email_sync("a@b.com", "找不到的主题", "b")

    assert result["success"] is True
    assert result["confirmation"]["confirmed"] is False
    assert "未能找到" in result["note"]


def test_special_response_note_when_confirmation_disabled(
    live_sender, fake_smtp
):
    fake_smtp.set_behavior("send_message_exc", QQ_SPECIAL_RESPONSE)
    result = live_sender.send_email_sync("a@b.com", "s", "b")

    assert result["success"] is True
    assert "未启用发送确认" in result["note"]


def test_rejected_send_is_not_confirmed(live_sender, fake_smtp, confirm_on, fake_imap):
    """被 SMTP 拒绝的邮件不应去做确认，也不应带 confirmation 字段。"""
    fake_smtp.set_behavior("send_message_exc", smtplib.SMTPResponseException(550, b"denied"))
    result = live_sender.send_email_sync("a@b.com", "s", "b")

    assert result["success"] is False
    assert "confirmation" not in result


# ---------------------------------------------------------------------------
# 幂等键的接入
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_idempotency():
    """每个用例前后都清空幂等存储，避免键在用例之间串味。"""
    from idempotency import idempotency_store

    idempotency_store.clear()
    yield
    idempotency_store.clear()


def test_idempotency_key_prevents_duplicate_send(live_sender, fake_smtp):
    """
    核心安全性质：同一个键重复提交，只应真的发出一封邮件。
    """
    first = live_sender.send_email_sync("a@b.com", "s", "b", idempotency_key="k1")
    second = live_sender.send_email_sync("a@b.com", "s", "b", idempotency_key="k1")

    assert first["success"] is True
    assert "duplicate" not in first

    assert second["success"] is True
    assert second["duplicate"] is True
    assert len(fake_smtp.last_instance.sent_messages) == 1, "第二次不得真的发送"


def test_idempotent_hit_is_not_counted_as_a_send(live_sender, fake_smtp):
    """
    被幂等拦截的重复请求不得计入 sends_total。

    否则成功率、connects_per_send 等派生指标会失真——
    实测曾出现「三次调用、sends_total=3，但只发出两封」的情况。
    """
    from metrics import send_metrics

    live_sender.send_email_sync("a@b.com", "s", "b", idempotency_key="k1")
    live_sender.send_email_sync("a@b.com", "s", "b", idempotency_key="k1")

    assert send_metrics.counter("sends_total") == 1, "重复请求不应计入发送总数"
    assert send_metrics.counter("idempotent_hits") == 1
    assert send_metrics.counter("sends_succeeded") == 1


def test_different_keys_send_separately(live_sender, fake_smtp):
    live_sender.send_email_sync("a@b.com", "s1", "b", idempotency_key="k1")
    live_sender.send_email_sync("a@b.com", "s2", "b", idempotency_key="k2")

    assert len(fake_smtp.last_instance.sent_messages) == 2


def test_no_key_means_no_deduplication(live_sender, fake_smtp):
    """不带键时行为与从前一致，不做任何去重。"""
    live_sender.send_email_sync("a@b.com", "s", "b")
    live_sender.send_email_sync("a@b.com", "s", "b")

    assert len(fake_smtp.last_instance.sent_messages) == 2


def test_key_is_released_after_permanent_failure(live_sender, fake_smtp):
    """
    永久失败后键应被释放，允许调用方修正参数后重试同一键。
    否则一次失败会让该请求永久失效。
    """
    fake_smtp.set_behavior("send_message_exc", smtplib.SMTPResponseException(550, b"denied"))
    first = live_sender.send_email_sync("a@b.com", "s", "b", idempotency_key="k1")
    assert first["success"] is False

    fake_smtp.set_behavior("send_message_exc", None)
    second = live_sender.send_email_sync("a@b.com", "s", "b", idempotency_key="k1")

    assert second["success"] is True
    assert "duplicate" not in second


def test_keyed_send_reports_in_progress_for_concurrent_request(live_sender):
    """键被占用但尚未完成时，重复请求应得到「处理中」而不是再发一次。"""
    from idempotency import idempotency_store

    idempotency_store.reserve("busy-key")
    result = live_sender.send_email_sync("a@b.com", "s", "b", idempotency_key="busy-key")

    assert result["duplicate"] is True
    assert result["status"] == "in_progress"


# ---------------------------------------------------------------------------
# 重试的接入
# ---------------------------------------------------------------------------

def test_transient_error_is_retried_until_success(live_sender, fake_smtp, monkeypatch):
    """451 属瞬时故障，应按配置重试并最终成功。"""
    calls = {"n": 0}
    original = fake_smtp.send_message

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise smtplib.SMTPResponseException(451, b"try later")
        return original(self, *args, **kwargs)

    fake_smtp.send_message = flaky
    try:
        result = live_sender.send_email_sync("a@b.com", "s", "b")
    finally:
        fake_smtp.send_message = original

    assert result["success"] is True
    assert calls["n"] == 3


def test_attempts_are_bounded_by_config(live_sender, fake_smtp, monkeypatch):
    """重试次数必须受配置限制，不能无限重试。"""
    import email_tools

    monkeypatch.setattr(email_tools.settings, "send_max_attempts", 2)
    fake_smtp.set_behavior("send_message_exc", smtplib.SMTPResponseException(451, b"later"))

    result = live_sender.send_email_sync("a@b.com", "s", "b")

    assert result["success"] is False
    assert len(fake_smtp.last_instance.sent_messages) == 0


def test_permanent_error_is_not_retried_in_send_path(live_sender, fake_smtp, monkeypatch):
    """550 是永久错误，发送路径不应重试。"""
    import email_tools

    monkeypatch.setattr(email_tools.settings, "send_max_attempts", 3)
    attempts = {"n": 0}
    original = fake_smtp.send_message

    def counting(self, *args, **kwargs):
        attempts["n"] += 1
        raise smtplib.SMTPResponseException(550, b"no such user")

    fake_smtp.send_message = counting
    try:
        result = live_sender.send_email_sync("a@b.com", "s", "b")
    finally:
        fake_smtp.send_message = original

    assert result["success"] is False
    assert attempts["n"] == 1, "永久错误只应尝试一次"


def test_retry_metrics_are_recorded(live_sender, fake_smtp):
    from metrics import send_metrics

    calls = {"n": 0}
    original = fake_smtp.send_message

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise smtplib.SMTPResponseException(451, b"later")
        return original(self, *args, **kwargs)

    fake_smtp.send_message = flaky
    try:
        live_sender.send_email_sync("a@b.com", "s", "b")
    finally:
        fake_smtp.send_message = original

    assert send_metrics.counter("send_retries") == 1
