# tool_defs.py - 工具声明与调用分发（纯逻辑，不依赖 Web 框架）
"""
本模块把「工具定义」与「工具调用分发」从 mcp_server.py 中抽离出来。

设计要点：
1. TOOL_DEFS 是唯一的工具真相来源。MCP 的 tools/list、REST 的 /tools、
   以及调用校验都从它派生，不会出现多处定义漂移。
2. dispatch_tool() 不依赖 FastAPI、不依赖全局单例，email_tools 通过参数注入，
   因此可以用假的 email_tools 做纯单元测试（无需真实 SMTP、无需网络）。
3. 对 QQ 邮箱 SMTP 返回非标准响应 (-1, b'\\x00\\x00\\x00') 的情况，
   沿用 email_tools 层的判定结论，在此统一转换为可读文本。
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import mcp.types as types

# ---------------------------------------------------------------------------
# 工具定义（单一真相来源）
# ---------------------------------------------------------------------------

# 邮件工具共用的收件人 / 抄送 / 密送字段
_TO_RECIPIENT = {
    "type": ["string", "array"],
    "items": {"type": "string"},
    "description": "收件人邮箱地址（单个或多个）",
    "format": "email",
}
_CC = {
    "type": "array",
    "items": {"type": "string", "format": "email"},
    "description": "抄送邮箱列表（可选）",
}
_BCC = {
    "type": "array",
    "items": {"type": "string", "format": "email"},
    "description": "密送邮箱列表（可选）",
}
#: 幂等键：同一键在有效期内只会真正发送一次，用于避免重复发信
_IDEMPOTENCY_KEY = {
    "type": "string",
    "description": (
        "幂等键（可选）。同一次业务请求请使用同一个键；"
        "重复提交不会再次发信，而是返回首次的结果。"
    ),
}

TOOL_DEFS: List[types.Tool] = [
    types.Tool(
        name="send_text_email",
        description="发送纯文本邮件到指定邮箱",
        inputSchema={
            "type": "object",
            "properties": {
                "to_email": _TO_RECIPIENT,
                "subject": {"type": "string", "description": "邮件主题"},
                "body": {"type": "string", "description": "邮件正文内容"},
                "cc": _CC,
                "bcc": _BCC,
                "idempotency_key": _IDEMPOTENCY_KEY,
            },
            "required": ["to_email", "subject", "body"],
        },
    ),
    types.Tool(
        name="send_html_email",
        description="发送HTML格式邮件",
        inputSchema={
            "type": "object",
            "properties": {
                "to_email": _TO_RECIPIENT,
                "subject": {"type": "string", "description": "邮件主题"},
                "html_body": {"type": "string", "description": "HTML格式的邮件正文"},
                "cc": _CC,
                "bcc": _BCC,
                "idempotency_key": _IDEMPOTENCY_KEY,
            },
            "required": ["to_email", "subject", "html_body"],
        },
    ),
    types.Tool(
        name="send_email_with_attachment",
        description="发送带附件的邮件",
        inputSchema={
            "type": "object",
            "properties": {
                "to_email": _TO_RECIPIENT,
                "subject": {"type": "string", "description": "邮件主题"},
                "body": {
                    "type": "string",
                    "description": "邮件正文内容（可选，缺省时自动生成）",
                    "default": "",
                },
                "attachment_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "附件文件路径列表",
                },
                "cc": _CC,
                "bcc": _BCC,
                "idempotency_key": _IDEMPOTENCY_KEY,
                "is_html": {
                    "type": "boolean",
                    "description": "是否为HTML格式正文（默认false）",
                    "default": False,
                },
            },
            # body 不列为必填：缺省时由分发层自动填充
            "required": ["to_email", "subject", "attachment_paths"],
        },
    ),
    types.Tool(
        name="check_email_config",
        description="检查QQ邮箱配置和连接状态",
        inputSchema={"type": "object", "properties": {}},
    ),
]

#: 工具名 -> 定义，便于 O(1) 查找与 schema 校验
TOOLS_BY_NAME: Dict[str, types.Tool] = {t.name: t for t in TOOL_DEFS}

#: 每个工具的必填字段（从 inputSchema 派生，避免重复维护）
REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    t.name: tuple(t.inputSchema.get("required", [])) for t in TOOL_DEFS
}

#: 每个工具的调用超时（秒）
TOOL_TIMEOUTS: Dict[str, float] = {
    "send_text_email": 30.0,
    "send_html_email": 30.0,
    "send_email_with_attachment": 45.0,
    "check_email_config": 20.0,
}


# ---------------------------------------------------------------------------
# 调用校验
# ---------------------------------------------------------------------------

class MissingArgumentsError(ValueError):
    """必填参数缺失。"""

    def __init__(self, missing: List[str]):
        self.missing = missing
        super().__init__("缺少必要参数: " + ", ".join(missing))


class UnknownToolError(ValueError):
    """请求了未注册的工具。"""

    def __init__(self, name: str):
        self.name = name
        super().__init__("未知工具: %s" % name)


def validate_arguments(name: str, arguments: Dict[str, Any]) -> None:
    """
    按 schema 校验必填字段。

    完全照搬 JSON Schema 的 required 语义：只有当键「缺失」才算未提供。
    显式传入 null 由 SDK 的 jsonschema 校验负责拦截（类型不符），
    这里不重复判定，以免与 schema 产生分歧。
    """
    if name not in TOOLS_BY_NAME:
        raise UnknownToolError(name)
    missing = [f for f in REQUIRED_FIELDS[name] if f not in arguments]
    if missing:
        raise MissingArgumentsError(missing)


# ---------------------------------------------------------------------------
# 结果格式化
# ---------------------------------------------------------------------------

def _format_result(name: str, arguments: Dict[str, Any], result: Dict[str, Any]) -> str:
    """把工具返回的 dict 转成面向调用方的一行文本。"""
    if result.get("success", False):
        text = "✅ %s" % result.get("message", "操作成功")
        if name.startswith("send_"):
            text += "\n收件人: %s" % result.get("to", "N/A")
            text += "\n主题: %s" % arguments.get("subject", "N/A")
        # 附件名被自动修正时把真实文件名显式写出来：
        # 否则调用方看到的是模型猜错的名字（例如 .xlsx），
        # 而实际发出的是 .csv，会以为发错了文件。
        used = result.get("attachments") or []
        if used:
            text += "\n附件: %s" % "、".join(Path(p).name for p in used)
        subs = result.get("attachment_substitutions") or []
        if subs:
            text += "\n（附件名已自动修正：%s）" % "；".join(subs)
        return text

    message = result.get("message", "未知错误")
    # QQ 邮箱可能返回非标准响应，email_tools 已判定其实际发送成功
    if "特殊响应" in message or result.get("smtp_code") == -1:
        return "✅ 邮件发送成功（QQ邮箱特殊响应）"
    return "❌ %s" % message


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------

def _build_call(name: str, arguments: Dict[str, Any], email_tools: Any) -> Callable:
    """把工具名映射为对 email_tools 的具体调用（返回可 await 的协程）。"""
    # 幂等键对所有发送类工具通用；未提供时为 None，行为与不带键时一致
    key = arguments.get("idempotency_key")

    if name == "send_text_email":
        return email_tools.send_text_email(
            to_email=arguments["to_email"],
            subject=arguments["subject"],
            body=arguments["body"],
            cc=arguments.get("cc"),
            bcc=arguments.get("bcc"),
            idempotency_key=key,
        )
    if name == "send_html_email":
        return email_tools.send_html_email(
            to_email=arguments["to_email"],
            subject=arguments["subject"],
            html_body=arguments["html_body"],
            cc=arguments.get("cc"),
            bcc=arguments.get("bcc"),
            idempotency_key=key,
        )
    if name == "send_email_with_attachment":
        body = arguments.get("body") or (
            "附件已发送，请查收。\n发送时间: %s"
            % datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        return email_tools.send_email_with_attachment(
            to_email=arguments["to_email"],
            subject=arguments["subject"],
            body=body,
            attachment_paths=arguments["attachment_paths"],
            cc=arguments.get("cc"),
            bcc=arguments.get("bcc"),
            is_html=arguments.get("is_html", False),
            idempotency_key=key,
        )
    if name == "check_email_config":
        return email_tools.check_email_config()
    raise UnknownToolError(name)


async def dispatch_tool(
    name: str,
    arguments: Optional[Dict[str, Any]],
    email_tools: Any,
    timeout: Optional[float] = None,
) -> List[types.TextContent]:
    """
    执行一个工具调用并返回 MCP 文本内容。

    这是唯一的分发入口：MCP 传输层与 REST 层都调用它，
    因此两条路径的行为（校验、超时、错误文案）保证一致。
    """
    arguments = dict(arguments or {})
    try:
        validate_arguments(name, arguments)
        coro = _build_call(name, arguments, email_tools)
        result = await asyncio.wait_for(
            coro, timeout=timeout or TOOL_TIMEOUTS.get(name, 30.0)
        )
        return [types.TextContent(type="text", text=_format_result(name, arguments, result))]
    except asyncio.TimeoutError:
        return [
            types.TextContent(
                type="text", text="❌ 工具调用超时（可能邮件服务器响应慢）"
            )
        ]
    except MissingArgumentsError as e:
        return [types.TextContent(type="text", text="❌ %s" % e)]
    except Exception as e:  # noqa: BLE001 - 统一转成可读错误返回给调用方
        return [types.TextContent(type="text", text="❌ 工具调用异常: %s" % e)]
