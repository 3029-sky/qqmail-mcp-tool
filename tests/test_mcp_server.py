# tests/test_mcp_server.py - MCP 协议层集成测试
"""
用 httpx 的 ASGITransport 在进程内直接驱动 FastAPI 应用，
无需启动真实服务器、无需占用端口、无需网络。

覆盖：
  - 辅助 REST 端点
  - MCP 协议的 initialize 握手（旧手搓端点缺失的环节）
  - tools/list、tools/call 经真实协议栈往返
  - /api/send-email-sync 后门确实已不存在
"""

from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client import streamable_http as sh_client

from mcp_server import create_app
from tool_defs import TOOL_DEFS


# ---------------------------------------------------------------------------
# 进程内 HTTP 客户端
# ---------------------------------------------------------------------------

@pytest.fixture
def test_app():
    """
    每个用例都拿到一个全新的应用实例。

    必须如此：StreamableHTTPSessionManager.run() 每个实例只能调用一次，
    复用模块级单例会导致第二次进入 lifespan 时报错。
    """
    return create_app()


def _auth_headers() -> dict:
    """
    客户端连接时该带的请求头——**从真实配置读**，不写死。

    `.env.test` 里配了 MCP_AUTH_TOKEN，服务端因此要求鉴权。
    如果这里不带上，本文件所有协议用例都会 401。

    刻意从配置读而不是硬编码令牌：这样一旦「服务端要鉴权、客户端不发令牌」
    的接线再断掉，这些用例会立刻失败——它们同时也在验证那条链路。
    """
    from config import settings

    token = (settings.mcp_auth_token or "").strip()
    return {"Authorization": "Bearer %s" % token} if token else {}


@asynccontextmanager
async def in_process_client(app):
    """直接与应用对话的 httpx 客户端（不发真实网络请求）。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@asynccontextmanager
async def mcp_client_for(app):
    """
    在进程内建立一条完整的 MCP 会话。

    - 显式进入应用 lifespan：MCP 会话管理器在其中启动，
      而 ASGITransport 不会自动触发 lifespan。
    - 用官方 streamablehttp_client，但把 HTTP 层换成进程内 transport。
    """
    transport = httpx.ASGITransport(app=app)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        read, write, _ = await stack.enter_async_context(
            sh_client.streamablehttp_client(
                "http://testserver/mcp",
                headers=_auth_headers(),
                # 工厂必须返回「尚未打开」的客户端：
                # streamablehttp_client 内部会自行 async with client。
                #
                # 注意 headers 必须转发：SDK 是
                # `httpx_client_factory(headers=headers, timeout=...)` 调用的，
                # 早先用 `lambda **_:` 把它丢掉了 —— 于是鉴权头根本没到
                # HTTP 层，所有协议用例 401。这是测试的 bug，不是产品的。
                httpx_client_factory=lambda **kwargs: httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    headers=kwargs.get("headers"),
                    timeout=kwargs.get("timeout"),
                    # ASGITransport 默认不跟随重定向，
                    # 而 Mount("/mcp") 会把路径规范化为 /mcp/
                    follow_redirects=True,
                ),
            )
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        yield session


# ---------------------------------------------------------------------------
# 辅助 REST 端点
# ---------------------------------------------------------------------------

async def test_health_endpoint(test_app):
    async with in_process_client(test_app) as c:
        r = await c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["tools_available"] == len(TOOL_DEFS)


async def test_root_endpoint_advertises_mcp_endpoint(test_app):
    async with in_process_client(test_app) as c:
        r = await c.get("/")
    assert r.status_code == 200
    assert "/mcp" in r.json()["endpoints"]


async def test_tools_endpoint_matches_registry(test_app):
    async with in_process_client(test_app) as c:
        r = await c.get("/tools")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == len(TOOL_DEFS)
    assert {t["name"] for t in body["tools"]} == {t.name for t in TOOL_DEFS}


async def test_metrics_endpoint_exposes_send_stats(test_app):
    async with in_process_client(test_app) as c:
        r = await c.get("/metrics")
    assert r.status_code == 200
    body = r.json()
    # 指标结构必须稳定，便于外部监控采集
    assert "counters" in body
    assert "latency_ms" in body
    assert "failures_by_reason" in body
    assert "connects_per_send" in body
    assert "success_rate" in body


# ---------------------------------------------------------------------------
# 后门已移除
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/send-email-sync", "/api/send_email_sync"])
async def test_removed_backdoor_returns_404(test_app, path):
    async with in_process_client(test_app) as c:
        r = await c.post(path, json={"to_email": "a@b.com", "subject": "s", "body": "b"})
    assert r.status_code == 404, "旧的后门端点不应再存在"


async def test_openapi_schema_has_no_backdoor_route(test_app):
    async with in_process_client(test_app) as c:
        r = await c.get("/openapi.json")
    paths = r.json()["paths"]
    assert not any("send-email-sync" in p for p in paths)


# ---------------------------------------------------------------------------
# MCP 协议：握手
# ---------------------------------------------------------------------------

async def test_mcp_initialize_handshake(test_app):
    async with mcp_client_for(test_app) as session:
        init = await session.initialize()
    assert init.protocolVersion, "必须协商出协议版本"
    assert init.serverInfo.name == "qqmail-mcp-server"
    assert init.capabilities.tools is not None, "服务器应声明 tools 能力"


async def test_mcp_lists_all_tools(test_app):
    async with mcp_client_for(test_app) as session:
        await session.initialize()
        listed = await session.list_tools()
    assert {t.name for t in listed.tools} == {t.name for t in TOOL_DEFS}


async def test_mcp_tool_schemas_are_exposed(test_app):
    async with mcp_client_for(test_app) as session:
        await session.initialize()
        listed = await session.list_tools()
    by_name = {t.name: t for t in listed.tools}
    schema = by_name["send_text_email"].inputSchema
    assert schema["type"] == "object"
    assert "to_email" in schema["properties"]
    assert set(schema["required"]) == {"to_email", "subject", "body"}


# ---------------------------------------------------------------------------
# MCP 协议：工具调用
# ---------------------------------------------------------------------------

async def test_mcp_call_tool_rejects_missing_arguments(test_app):
    """
    参数缺失时由 SDK 依据 inputSchema 拦截，
    应返回一条错误结果，而不是抛异常打断连接。
    """
    async with mcp_client_for(test_app) as session:
        await session.initialize()
        result = await session.call_tool("send_text_email", {"to_email": "a@b.com"})

    text = " ".join(getattr(c, "text", "") for c in result.content).lower()
    assert "error" in text or "validation" in text


async def test_mcp_call_unknown_tool_is_reported(test_app):
    async with mcp_client_for(test_app) as session:
        await session.initialize()
        result = await session.call_tool("no_such_tool", {})
    text = " ".join(getattr(c, "text", "") for c in result.content).lower()
    assert "no_such_tool" in text or "unknown" in text


async def test_idempotency_key_is_exposed_in_send_tool_schema(test_app):
    """幂等键必须出现在发送类工具的 schema 中，否则调用方无法使用。"""
    async with mcp_client_for(test_app) as session:
        await session.initialize()
        listed = await session.list_tools()
    by_name = {t.name: t for t in listed.tools}
    for tool_name in ("send_text_email", "send_html_email", "send_email_with_attachment"):
        props = by_name[tool_name].inputSchema["properties"]
        assert "idempotency_key" in props, "%s 缺少 idempotency_key" % tool_name
        # 它必须是可选的，否则会破坏现有调用方
        assert "idempotency_key" not in by_name[tool_name].inputSchema.get(
            "required", []
        )
