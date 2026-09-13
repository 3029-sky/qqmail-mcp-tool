# auth.py - /mcp 端点的 Bearer 令牌鉴权
"""
MCP 服务器默认监听 0.0.0.0，且工具具备真实发信能力。若不加鉴权就暴露到网络，
任何人都能借你的邮箱发信。本模块为 /mcp 提供最小可用的访问控制。

设计取舍：
  - 使用标准 `Authorization: Bearer <token>` 头，这是 MCP HTTP 传输的通行做法，
    各类 MCP 客户端都支持配置自定义请求头。
  - 令牌为空/未设置时**不启用**鉴权，便于本机开发；此时若监听在非回环地址，
    mcp_server 会打印醒目警告。
  - 比较使用 secrets.compare_digest，避免通过响应时间差逐字节猜测令牌。
  - 鉴权通过后会从 scope 中移除 Authorization 头，避免凭据被继续传给下游。

未实现（超出当前范围）：TLS、令牌轮换、按用户区分权限、审计日志。
"""

import secrets
from typing import Iterable, Optional

#: 需要鉴权的方式（OPTIONS 留给 CORS 预检，不应要求凭据）
PROTECTED_METHODS = ("POST", "GET", "DELETE")


def is_auth_enabled(configured_token: Optional[str]) -> bool:
    """
    是否启用鉴权：配置了非空令牌即启用。

    纯空白（例如 .env 里写成 `MCP_AUTH_TOKEN=   `）一律视为未配置，
    否则会出现「鉴权开着但谁也无法通过」的尴尬状态。
    """
    return bool(configured_token and configured_token.strip())


def extract_bearer_token(authorization_header: Optional[str]) -> Optional[str]:
    """
    从 Authorization 头中解析 Bearer 令牌。

    容忍大小写差异与多余空白；格式不符时返回 None。
    """
    if not authorization_header:
        return None
    parts = authorization_header.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def check_token(
    configured_token: Optional[str],
    authorization_header: Optional[str],
) -> bool:
    """
    判断本次请求是否放行。

    未配置令牌 -> 放行（鉴权关闭）。
    已配置令牌 -> 必须提供完全一致的 Bearer 令牌。
    """
    if not is_auth_enabled(configured_token):
        return True
    provided = extract_bearer_token(authorization_header)
    if not provided:
        return False
    # 恒时间比较，避免时序侧信道
    return secrets.compare_digest(provided, configured_token or "")


def _get_header(scope, name: bytes) -> Optional[str]:
    """从 ASGI scope 中读取指定请求头（同名头取最后一个）。"""
    value = None
    for key, raw in scope.get("headers") or []:
        if key.lower() == name:
            value = raw
    if value is None:
        return None
    return value.decode("latin-1")


def _strip_authorization(scope) -> None:
    """鉴权通过后移除 Authorization 头，避免凭据被传递给下游。"""
    headers = scope.get("headers")
    if not headers:
        return
    scope["headers"] = [(k, v) for k, v in headers if k.lower() != b"authorization"]


class BearerAuthMiddleware:
    """
    纯 ASGI 中间件：为受保护路径提供 Bearer 鉴权。

    以原生 ASGI 形式实现，因为 /mcp 本身就是原生 ASGI 应用，
    套用 Starlette 的请求/响应包装会破坏传输层的流式语义。
    """

    def __init__(
        self,
        app,
        token: Optional[str],
        protected_paths: Iterable[str] = ("/mcp",),
        protected_methods: Iterable[str] = PROTECTED_METHODS,
    ):
        self.app = app
        self.token = token
        self.protected_paths = tuple(protected_paths)
        self.protected_methods = tuple(m.upper() for m in protected_methods)

    def _is_protected(self, scope) -> bool:
        if scope.get("type") != "http":
            return False
        if scope.get("method", "").upper() not in self.protected_methods:
            return False
        path = scope.get("path", "")
        # Mount 可能把路径规范化为带尾斜杠，这里统一去掉再比较
        return path.rstrip("/") in {p.rstrip("/") for p in self.protected_paths}

    async def __call__(self, scope, receive, send):
        if not is_auth_enabled(self.token) or not self._is_protected(scope):
            return await self.app(scope, receive, send)

        auth_header = _get_header(scope, b"authorization")
        if not check_token(self.token, auth_header):
            return await self._reject(send)

        _strip_authorization(scope)
        return await self.app(scope, receive, send)

    async def _reject(self, send) -> None:
        """返回 401，并按规范带上 WWW-Authenticate。"""
        body = b'{"error":"unauthorized","message":"\\u7f3a\\u5c11\\u6216\\u9519\\u8bef\\u7684 Bearer \\u4ee4\\u724c"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer realm="qqmail-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
