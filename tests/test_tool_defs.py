# tests/test_tool_defs.py - 工具定义与分发逻辑的纯单元测试
"""这些用例完全不接触网络与 SMTP，只验证声明式层与分发层的正确性。"""

import asyncio

import pytest

from tool_defs import (
    REQUIRED_FIELDS,
    TOOLS_BY_NAME,
    TOOL_DEFS,
    MissingArgumentsError,
    UnknownToolError,
    dispatch_tool,
    validate_arguments,
)

EXPECTED_TOOLS = {
    "send_text_email",
    "send_html_email",
    "send_email_with_attachment",
    "check_email_config",
    "save_environment_config",
}


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------

def test_tool_names_are_complete_and_unique():
    names = [t.name for t in TOOL_DEFS]
    assert len(names) == len(set(names)), "工具名必须唯一"
    assert set(names) == EXPECTED_TOOLS


def test_required_fields_are_derived_from_schema():
    """REQUIRED_FIELDS 必须与 inputSchema 保持一致，防止两处定义漂移。"""
    for tool in TOOL_DEFS:
        assert REQUIRED_FIELDS[tool.name] == tuple(tool.inputSchema.get("required", []))


def test_every_tool_has_object_schema():
    for tool in TOOL_DEFS:
        assert tool.inputSchema["type"] == "object"
        assert "properties" in tool.inputSchema


def test_tools_by_name_is_indexable():
    for name in EXPECTED_TOOLS:
        assert TOOLS_BY_NAME[name].name == name


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------

def test_validate_accepts_valid_arguments():
    validate_arguments(
        "send_text_email",
        {"to_email": "a@b.com", "subject": "s", "body": "b"},
    )


def test_validate_rejects_missing_field():
    with pytest.raises(MissingArgumentsError) as exc:
        validate_arguments("send_text_email", {"to_email": "a@b.com", "subject": "s"})
    assert "body" in str(exc.value)


def test_validate_only_checks_presence_not_type():
    """
    照搬 JSON Schema 的 required 语义：只看键是否存在。
    类型不符（例如显式传 null）交给 SDK 的 jsonschema 校验拦截，
    避免手工校验与 schema 产生分歧。
    """
    validate_arguments(
        "send_text_email", {"to_email": None, "subject": "s", "body": "b"}
    )


def test_validate_rejects_absent_key():
    with pytest.raises(MissingArgumentsError):
        validate_arguments("send_text_email", {"subject": "s", "body": "b"})


def test_validate_allows_empty_dict_for_optional_object():
    """save_environment_config 的 config_data 允许空字典（0 参数场景）。"""
    validate_arguments("save_environment_config", {"config_data": {}})


def test_validate_rejects_unknown_tool():
    with pytest.raises(UnknownToolError):
        validate_arguments("no_such_tool", {})


def test_check_email_config_needs_no_arguments():
    validate_arguments("check_email_config", {})


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------

async def test_dispatch_send_text_email(fake_email_tools):
    result = await dispatch_tool(
        "send_text_email",
        {"to_email": "a@b.com", "subject": "主题", "body": "正文"},
        fake_email_tools,
    )
    assert len(result) == 1
    text = result[0].text
    assert "✅" in text
    assert "a@b.com" in text
    assert "主题" in text

    name, kwargs = fake_email_tools.calls[0]
    assert name == "send_text_email"
    assert kwargs["to_email"] == "a@b.com"
    assert kwargs["body"] == "正文"


async def test_dispatch_passes_cc_and_bcc(fake_email_tools):
    await dispatch_tool(
        "send_text_email",
        {
            "to_email": "a@b.com",
            "subject": "s",
            "body": "b",
            "cc": ["c@d.com"],
            "bcc": ["e@f.com"],
        },
        fake_email_tools,
    )
    _, kwargs = fake_email_tools.calls[0]
    assert kwargs["cc"] == ["c@d.com"]
    assert kwargs["bcc"] == ["e@f.com"]


async def test_dispatch_missing_argument_returns_readable_error(fake_email_tools):
    result = await dispatch_tool("send_text_email", {"to_email": "a@b.com"}, fake_email_tools)
    assert "❌" in result[0].text
    assert "缺少必要参数" in result[0].text
    assert fake_email_tools.calls == [], "校验失败时不应调用底层工具"


async def test_dispatch_unknown_tool_returns_error(fake_email_tools):
    result = await dispatch_tool("nope", {}, fake_email_tools)
    assert "❌" in result[0].text
    assert "未知工具" in result[0].text


async def test_dispatch_times_out(fake_email_tools):
    async def slow(**kwargs):
        await asyncio.sleep(5)
        return {"success": True}

    fake_email_tools.result_for["check_email_config"] = slow
    result = await dispatch_tool("check_email_config", {}, fake_email_tools, timeout=0.01)
    assert "超时" in result[0].text


async def test_dispatch_handles_unexpected_exception(fake_email_tools):
    fake_email_tools.result_for["check_email_config"] = RuntimeError("boom")
    result = await dispatch_tool("check_email_config", {}, fake_email_tools)
    assert "❌" in result[0].text
    assert "boom" in result[0].text


# ---------------------------------------------------------------------------
# 结果格式化：QQ 邮箱非标准响应
# ---------------------------------------------------------------------------

async def test_qq_special_response_is_reported_as_success(fake_email_tools):
    """
    QQ 邮箱可能返回 (-1, b'\\x00\\x00\\x00')，email_tools 已判定其实际发送成功。
    分发层必须把它呈现为成功，而不是报错。
    """
    fake_email_tools.result_for["send_text_email"] = {
        "success": False,
        "message": "邮件发送失败（QQ邮箱特殊响应）",
        "smtp_code": -1,
    }
    result = await dispatch_tool(
        "send_text_email",
        {"to_email": "a@b.com", "subject": "s", "body": "b"},
        fake_email_tools,
    )
    assert "✅" in result[0].text
    assert "特殊响应" in result[0].text


async def test_failure_result_is_reported_as_error(fake_email_tools):
    fake_email_tools.result_for["send_text_email"] = {
        "success": False,
        "message": "SMTP认证失败，请检查授权码",
    }
    result = await dispatch_tool(
        "send_text_email",
        {"to_email": "a@b.com", "subject": "s", "body": "b"},
        fake_email_tools,
    )
    assert result[0].text.startswith("❌")
    assert "认证失败" in result[0].text


# ---------------------------------------------------------------------------
# 默认值填充
# ---------------------------------------------------------------------------

async def test_attachment_tool_injects_default_body(fake_email_tools):
    await dispatch_tool(
        "send_email_with_attachment",
        {
            "to_email": "a@b.com",
            "subject": "s",
            "attachment_paths": ["x.txt"],
        },
        fake_email_tools,
    )
    _, kwargs = fake_email_tools.calls[0]
    assert kwargs["body"], "缺省正文应被自动填充"
    assert kwargs["is_html"] is False


async def test_save_config_injects_default_filename(fake_email_tools):
    await dispatch_tool("save_environment_config", {"config_data": {}}, fake_email_tools)
    _, kwargs = fake_email_tools.calls[0]
    assert kwargs["filename"].endswith(".json")


async def test_html_tool_passes_html_body(fake_email_tools):
    await dispatch_tool(
        "send_html_email",
        {"to_email": "a@b.com", "subject": "s", "html_body": "<b>hi</b>"},
        fake_email_tools,
    )
    _, kwargs = fake_email_tools.calls[0]
    assert kwargs["html_body"] == "<b>hi</b>"


# ---------------------------------------------------------------------------
# 结果文本：附件信息必须对调用方可见
# ---------------------------------------------------------------------------

async def test_result_lists_actual_attachments(fake_email_tools):
    """
    成功结果里要写出真实发出的附件名。

    否则调用方看到的是模型猜错的名字（例如 .xlsx），
    而实际发出的是 .csv，会以为发错了文件。
    """
    fake_email_tools.result_for["send_email_with_attachment"] = {
        "success": True,
        "message": "邮件发送成功",
        "to": "a@b.com",
        "attachments": [r"C:\data\测试数据.csv"],
    }
    result = await dispatch_tool(
        "send_email_with_attachment",
        {"to_email": "a@b.com", "subject": "s", "attachment_paths": ["x"]},
        fake_email_tools,
    )
    text = result[0].text
    assert "附件:" in text
    assert "测试数据.csv" in text


async def test_result_reports_attachment_substitution(fake_email_tools):
    """附件名被自动修正时必须在结果里说明，避免悄悄换了文件。"""
    fake_email_tools.result_for["send_email_with_attachment"] = {
        "success": True,
        "message": "邮件发送成功",
        "to": "a@b.com",
        "attachments": [r"C:\data\测试数据.csv"],
        "attachment_substitutions": ["测试数据.xlsx → 测试数据.csv"],
    }
    result = await dispatch_tool(
        "send_email_with_attachment",
        {"to_email": "a@b.com", "subject": "s", "attachment_paths": ["x"]},
        fake_email_tools,
    )
    text = result[0].text
    assert "自动修正" in text
    assert "测试数据.xlsx → 测试数据.csv" in text


async def test_result_omits_attachment_line_when_none(fake_email_tools):
    result = await dispatch_tool(
        "send_text_email",
        {"to_email": "a@b.com", "subject": "s", "body": "b"},
        fake_email_tools,
    )
    assert "附件:" not in result[0].text


async def test_missing_attachment_error_is_shown_to_caller(fake_email_tools):
    fake_email_tools.result_for["send_email_with_attachment"] = {
        "success": False,
        "message": "附件不存在，邮件未发送：/no/x.txt",
        "reason": "missing_attachment",
    }
    result = await dispatch_tool(
        "send_email_with_attachment",
        {"to_email": "a@b.com", "subject": "s", "attachment_paths": ["x"]},
        fake_email_tools,
    )
    assert "❌" in result[0].text
    assert "附件不存在" in result[0].text
