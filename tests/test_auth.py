# tests/test_auth.py - /mcp 端点鉴权测试
"""
覆盖三层：
  1. auth.py 的纯逻辑（是否启用、令牌解析、常量时间比较）
  2. BearerAuthMiddleware 作为 ASGI 中间件的行为（401 响应、放行、剥离凭据）
  3. 接入 mcp_server 后的端到端效果（HTTP 状态码）
"""

import httpx
import pytest

from auth import (
    BearerAuthMiddleware,
    check_token,
    extract_bearer_token,
    is_auth_enabled,
)

TOKEN = "s3cret-token-value"


# ---------------------------------------------------------------------------
# 纯逻辑
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "   "])
def test_auth_disabled_for_empty_token(value):
    assert is_auth_enabled(value) is False
    # 未配置令牌时一律放行
    assert check_token(value, None) is True
    assert check_token(value, "Bearer anything") is True


@pytest.mark.parametrize("value", [TOKEN, "x"])
def test_auth_enabled_for_non_empty_token(value):
    assert is_auth_enabled(value) is True


def test_extract_bearer_token_variants():
    assert extract_bearer_token("Bearer abc") == "abc"
    assert extract_bearer_token("bearer abc") == "abc"     # scheme 大小写不敏感
    assert extract_bearer_token("BEARER abc") == "abc"
    assert extract_bearer_token("Bearer   abc  ") == "abc"  # 容忍多余空白
    assert extract_bearer_token("Basic abc") is None        # 其他 scheme 不接受
    assert extract_bearer_token("abc") is None              # 缺少 scheme
    assert extract_bearer_token("Bearer") is None           # 缺少令牌
    assert extract_bearer_token("Bearer ") is None          # 空令牌
    assert extract_bearer_token(None) is None
    assert extract_bearer_token("") is None


def test_check_token_requires_exact_match():
    assert check_token(TOKEN, f"Bearer {TOKEN}") is True
    assert check_token(TOKEN, f"Bearer {TOKEN}x") is False
    assert check_token(TOKEN, "Bearer wrong") is False
    assert check_token(TOKEN, None) is False
    assert check_token(TOKEN, f"Basic {TOKEN}") is False


def test_check_token_is_not_fooled_by_prefix():
    """前缀相同的错误令牌必须被拒绝。"""
    assert check_token("abcdef", "Bearer abc") is False
    assert check_token("abc", "Bearer abcdef") is False


# ---------------------------------------------------------------------------
# ASGI 中间件
# ---------------------------------------------------------------------------

async def _call_asgi(app, method="POST", path="/mcp", headers=None):
    """直接驱动 ASGI 应用，返回 (status, body)。"""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or [])],
        "query_string": b"",
    }
    sent = []
    received = {"done": False}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return status, body, sent


async def _ok_app(scope, receive, send):
    """代表「下游应用」：记录收到的请求头并返回 200。"""
    scope.setdefault("_seen_headers", []).append(dict(scope.get("headers") or []))
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


async def test_middleware_rejects_missing_token():
    mw = BearerAuthMiddleware(_ok_app, token=TOKEN)
    status, body, messages = await _call_asgi(mw)
    assert status == 401
    assert b"unauthorized" in body
    # 必须按规范带上 WWW-Authenticate
    headers = dict(messages[0]["headers"])
    assert headers[b"www-authenticate"] == b'Bearer realm="qqmail-mcp"'


async def test_middleware_rejects_wrong_token():
    mw = BearerAuthMiddleware(_ok_app, token=TOKEN)
    status, _, _ = await _call_asgi(mw, headers=[("authorization", "Bearer nope")])
    assert status == 401


async def test_middleware_accepts_correct_token():
    mw = BearerAuthMiddleware(_ok_app, token=TOKEN)
    status, body, _ = await _call_asgi(
        mw, headers=[("authorization", f"Bearer {TOKEN}")]
    )
    assert status == 200
    assert body == b"ok"


async def test_middleware_allows_all_when_auth_disabled():
    mw = BearerAuthMiddleware(_ok_app, token=None)
    status, _, _ = await _call_asgi(mw)
    assert status == 200


async def test_middleware_ignores_other_paths():
    """未受保护路径不应被拦。"""
    mw = BearerAuthMiddleware(_ok_app, token=TOKEN, protected_paths=("/mcp",))
    status, _, _ = await _call_asgi(mw, path="/health")
    assert status == 200


async def test_middleware_allows_cors_preflight():
    """OPTIONS 属于 CORS 预检，浏览器不会带凭据，必须放行。"""
    mw = BearerAuthMiddleware(_ok_app, token=TOKEN)
    status, _, _ = await _call_asgi(mw, method="OPTIONS")
    assert status == 200


async def test_middleware_strips_authorization_before_downstream():
    """鉴权通过后必须移除 Authorization，避免凭据被继续传递。"""
    seen = {}

    async def downstream(scope, receive, send):
        seen["headers"] = dict(scope.get("headers") or [])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = BearerAuthMiddleware(downstream, token=TOKEN)
    await _call_asgi(mw, headers=[("authorization", f"Bearer {TOKEN}"), ("x-keep", "1")])

    assert b"authorization" not in seen["headers"], "凭据不应传给下游"
    assert seen["headers"].get(b"x-keep") == b"1", "其他请求头必须保留"


async def test_middleware_accepts_trailing_slash_path():
    """Mount 可能把路径规范化为 /mcp/，同样必须受保护。"""
    mw = BearerAuthMiddleware(_ok_app, token=TOKEN)
    status, _, _ = await _call_asgi(mw, path="/mcp/")
    assert status == 401


# ---------------------------------------------------------------------------
# 接入服务器后的端到端效果
# ---------------------------------------------------------------------------

@pytest.fixture
def secured_app(monkeypatch):
    """构造一个启用了鉴权的应用实例。"""
    import mcp_server

    monkeypatch.setattr(mcp_server.settings, "mcp_auth_token", TOKEN)
    return mcp_server.create_app()


async def test_server_rejects_unauthenticated_mcp_request(secured_app):
    transport = httpx.ASGITransport(app=secured_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
    assert r.status_code == 401


async def test_server_health_stays_open(secured_app):
    """健康检查用于探活，不应要求凭据。"""
    transport = httpx.ASGITransport(app=secured_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.get("/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 启动期安全提示
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "host,token,expect_warning",
    [
        ("0.0.0.0", None, True),      # 对外监听 + 无令牌 -> 必须告警
        ("0.0.0.0", TOKEN, False),    # 有令牌 -> 不告警
        ("127.0.0.1", None, False),   # 仅本机 + 无令牌 -> 可接受
        ("localhost", None, False),
    ],
)
def test_insecure_startup_warning(monkeypatch, caplog, host, token, expect_warning):
    import logging

    import mcp_server

    monkeypatch.setattr(mcp_server.settings, "mcp_host", host)
    monkeypatch.setattr(mcp_server.settings, "mcp_auth_token", token)

    with caplog.at_level(logging.WARNING, logger=mcp_server.logger.name):
        mcp_server._warn_if_insecure()

    warned = any(r.levelno == logging.WARNING for r in caplog.records)
    assert warned is expect_warning
