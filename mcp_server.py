# mcp_server.py - QQ邮箱 MCP 服务器
"""
对外提供一个符合 MCP 规范的 Streamable HTTP 端点（POST /mcp），
并附带若干用于人工排查的 REST 端点。

架构：
    FastAPI 应用
      ├── lifespan            -> 启动/关闭 MCP 会话管理器
      ├── POST /mcp           -> 官方 Streamable HTTP 传输层（真正的 MCP 协议）
      ├── GET  /              -> 服务信息
      ├── GET  /health        -> 健康检查
      ├── GET  /tools         -> 工具清单（供人工查看）
      └── POST /api/send-email-sync  -> 已移除（见下方说明）

关于「后门」的说明：
    早期版本为了让邮件发送绕过 MCP 传输层，额外暴露了一个直连接口
    /api/send-email-sync，客户端优先调用它。这导致项目虽然自称 MCP 服务器，
    核心功能却不经过 MCP 协议。现已删除，所有调用统一走 POST /mcp，
    由 SDK 完成 initialize 握手、会话管理与工具分发。
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from config import settings
from auth import BearerAuthMiddleware, is_auth_enabled
from email_tools import email_tools, get_send_metrics
from tool_defs import TOOL_DEFS, dispatch_tool

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP 服务器工厂
# ---------------------------------------------------------------------------
def _register_handlers(server: Server) -> None:
    """把工具清单与调用处理注册到指定的 MCP Server 实例上。"""

    @server.list_tools()
    async def handle_list_tools():
        """向 MCP 客户端声明可用工具（定义来自 tool_defs，唯一真相来源）。"""
        return TOOL_DEFS

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: Dict[str, Any]):
        """
        处理 MCP 工具调用。

        入参校验由 SDK 依据 inputSchema 完成（validate_input 默认开启），
        这里只负责分发，避免维护两份不一致的校验逻辑。
        """
        logger.info("MCP 调用工具: %s", name)
        return await dispatch_tool(name, arguments, email_tools)


class MCPAsgiApp:
    """
    原生 ASGI 桥接层。

    官方 Streamable HTTP 传输层直接读写 scope/receive/send，
    不能注册为普通 FastAPI 路径函数——Starlette 会把返回值当成 Response
    再序列化一次，导致 422。因此这里以纯 ASGI 应用的形式挂载。
    """

    def __init__(self, manager: StreamableHTTPSessionManager):
        self.manager = manager

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        await self.manager.handle_request(scope, receive, send)


def create_app() -> FastAPI:
    """
    构建一个全新的应用实例。

    之所以做成工厂而不是模块级单例：StreamableHTTPSessionManager 的
    run() 每个实例只允许调用一次，若把会话管理器绑在模块级单例上，
    应用就无法被重复启动（例如测试中多次进入 lifespan 会直接报错）。
    每次调用本函数都会得到独立的 Server 与 SessionManager。
    """
    server = Server("qqmail-mcp-server", version="2.0.0")
    _register_handlers(server)

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=False,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """应用生命周期：在其中运行 MCP 会话管理器。"""
        logger.info("启动 MCP 会话管理器...")
        async with session_manager.run():
            logger.info("MCP 服务器就绪，端点: POST /mcp")
            yield
        logger.info("MCP 会话管理器已关闭")

    app = FastAPI(
        title="QQ邮箱MCP服务器",
        description="提供QQ邮箱功能的MCP工具，遵循 MCP Streamable HTTP 传输规范",
        version="2.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # 让浏览器端能读到会话 ID，否则客户端无法维持 MCP 会话
        expose_headers=["mcp-session-id"],
    )

    # 以原生 ASGI 形式挂载官方传输层（不做 FastAPI 的请求/响应包装）。
    #
    # 先注册精确路径 /mcp：Starlette 的 Mount 会把 /mcp 规范化为 /mcp/ 并返回 307，
    # 而 MCP 客户端每次调用都携带 Authorization / mcp-session-id 等头部，
    # 多余的重定向既浪费往返也可能丢失头部。精确路由让 /mcp 直接命中。
    #
    # 鉴权包在传输层外面：令牌校验通过后才进入 MCP 会话处理，
    # 这样未授权的请求不会建立任何会话。
    bridge = BearerAuthMiddleware(
        MCPAsgiApp(session_manager),
        token=settings.mcp_auth_token,
    )
    app.router.routes.append(Route("/mcp", endpoint=bridge))
    app.router.routes.append(Mount("/mcp", app=bridge))

    _add_rest_routes(app)
    return app


#: 默认应用实例，供 uvicorn / run_server.py 使用
#: （定义在文件末尾，因为 _add_rest_routes 需先完成定义）


# ---------------------------------------------------------------------------
# 辅助 REST 端点（人工排查用，不参与 MCP 协议）
# ---------------------------------------------------------------------------
def _add_rest_routes(app: FastAPI) -> None:
    """注册辅助 REST 端点。这些端点仅供人工查看，不属于 MCP 协议。"""

    @app.get("/")
    async def root():
        return {
            "service": "QQ邮箱MCP服务器",
            "version": "2.0.0",
            "status": "running",
            "protocol": "MCP Streamable HTTP",
            "endpoints": {
                "/": "此页面",
                "/health": "健康检查",
                "/metrics": "发送指标快照",
                "/tools": "查看可用工具",
                "/mcp": "MCP 协议端点（POST，需先 initialize 握手）",
            },
            "email": settings.smtp_email,
            "timestamp": datetime.now().isoformat(),
        }

    @app.get("/health")
    async def health():
        """健康检查（快速响应，不触发外部连接）。"""
        return {
            "status": "healthy",
            "service": "QQ邮箱MCP服务器",
            "version": "2.0.0",
            "email": settings.smtp_email,
            "timestamp": datetime.now().isoformat(),
            "message": "服务运行正常，MCP 端点就绪",
            "tools_available": len(TOOL_DEFS),
        }

    @app.get("/metrics")
    async def metrics():
        """
        发送指标快照。

        connects_per_send 用于验证连接复用是否生效：
        该值远小于 1 说明 TLS 握手被复用，接近 1 说明每封信都在重连。
        """
        return get_send_metrics()

    @app.get("/tools")
    async def list_tools():
        """以纯 JSON 形式列出可用工具，便于人工查看与调试。"""
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.inputSchema,
                }
                for t in TOOL_DEFS
            ],
            "count": len(TOOL_DEFS),
            "timestamp": datetime.now().isoformat(),
        }


# ---------------------------------------------------------------------------
# 默认应用实例
# ---------------------------------------------------------------------------
#: 供 uvicorn / run_server.py 使用（"mcp_server:app"）
app = create_app()


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
#: 视为「仅本机」的监听地址
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _warn_if_insecure() -> None:
    """
    在「对外监听 + 未启用鉴权」的组合下给出醒目警告。

    这是最容易出事、也最容易被忽略的配置：服务器能真实发信，
    却没有访问控制。宁可吵一点，也不要静默地不安全。
    """
    if is_auth_enabled(settings.mcp_auth_token):
        logger.info("已启用 /mcp 鉴权（Bearer 令牌）")
        return

    if settings.mcp_host in _LOOPBACK_HOSTS:
        logger.info("未设置 MCP_AUTH_TOKEN，但仅监听回环地址，本机开发可接受")
        return

    logger.warning(
        "安全警告：MCP_HOST=%s 对外监听，且未设置 MCP_AUTH_TOKEN。"
        "任何能访问该端口的人都可以用你的邮箱发信。"
        "请在 .env 中设置 MCP_AUTH_TOKEN，或把 MCP_HOST 改为 127.0.0.1。",
        settings.mcp_host,
    )


def start_server():
    """启动服务器（使用 config 中的主机与端口）。"""
    logger.info("启动QQ邮箱MCP服务器...")
    logger.info("邮箱: %s", settings.smtp_email)
    logger.info("地址: http://%s:%s", settings.mcp_host, settings.mcp_port)
    logger.info("MCP 端点: http://%s:%s/mcp", settings.mcp_host, settings.mcp_port)
    _warn_if_insecure()

    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        # reload 会重复执行 lifespan，导致 session_manager.run() 二次调用而报错
        reload=False,
    )


if __name__ == "__main__":
    start_server()
