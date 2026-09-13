# ollama_mcp_client.py - 基于 Ollama 的 QQ 邮箱智能助手
"""
工作流程：
    1. 通过官方 MCP 客户端连接服务器（POST /mcp），完成 initialize 握手
    2. 用 Ollama 解析自然语言指令，输出 {tool, parameters} JSON
    3. 经 MCP 协议调用对应工具

注意：官方 MCP 客户端基于 anyio，会话的 __aenter__/__aexit__ 必须发生在
同一个 asyncio task 中，因此 main() 里用 AsyncExitStack 把连接的生命周期
包裹在同一个 task 内，而不是简单的 await 前后各摆一次。
"""

import asyncio
import json
import re
import sys
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from config import settings

print("🤖 QQ邮箱智能助手 (基于Ollama Qwen2.5)")
print("=" * 60)

# --- 配置区域 ---
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:3b"
MCP_SERVER_URL = "http://localhost:8000/mcp"


# --- 核心：基于官方 MCP 客户端的连接封装 ---
class MCPEmailClient:
    """
    通过官方 MCP 协议客户端与服务器通信。

    与旧实现的区别：以前邮件发送被刻意绕开 MCP，直连 /api/send-email-sync；
    现在所有工具调用统一走 MCP 的 tools/call，后门端点已从服务端移除。
    """

    def __init__(self, server_url: str = MCP_SERVER_URL):
        self.server_url = server_url
        self.session: Optional[ClientSession] = None
        self._stack: Optional[AsyncExitStack] = None
        self.tools: List[Any] = []

    async def __aenter__(self) -> "MCPEmailClient":
        """建立连接并完成 MCP 握手。"""
        self._stack = AsyncExitStack()
        read, write, _ = await self._stack.enter_async_context(
            streamablehttp_client(self.server_url)
        )
        self.session = await self._stack.enter_async_context(
            ClientSession(read, write)
        )
        init = await self.session.initialize()
        print("✅ MCP 握手完成（协议版本 %s）" % init.protocolVersion)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return await self._stack.__aexit__(exc_type, exc, tb)

    def _make_invoker(self, tool_name: str):
        """为单个工具生成调用函数，保持与旧接口一致的 ainvoke(arguments)。"""

        async def invoke(arguments: Dict[str, Any]) -> str:
            if self.session is None:
                return "❌ MCP 会话未建立"

            # 邮件类工具不再有专用后门，统一走 MCP 协议
            args = dict(arguments or {})
            if tool_name == "send_html_email":
                args.setdefault("html_body", args.get("body", ""))
            if tool_name == "send_email_with_attachment":
                args.setdefault("body", "请查看附件")

            try:
                result = await self.session.call_tool(tool_name, args)
            except Exception as e:  # noqa: BLE001
                return "❌ 调用工具时异常: %s" % e

            text = "\n".join(
                getattr(c, "text", "") for c in result.content
            ).strip()
            return text or "✅ 操作完成"

        return invoke

    async def load_tools(self) -> List[Any]:
        """从服务器拉取工具清单，包装成带 ainvoke 的对象。"""
        if self.session is None:
            raise RuntimeError("MCP 会话未建立")

        listed = await self.session.list_tools()
        tools = []
        for t in listed.tools:
            tools.append(
                type(
                    "Tool",
                    (),
                    {
                        "name": t.name,
                        "description": t.description,
                        "_invoke": self._make_invoker(t.name),
                    },
                )()
            )
        self.tools = tools
        print("✅ 从 MCP 服务器加载了 %d 个工具" % len(tools))
        return tools


# --- 核心：Ollama 智能体 ---
class OllamaEmailAssistant:
    """使用 Ollama 模型解析意图并经 MCP 调用邮件工具的助手"""

    def __init__(self, session: ClientSession):
        self.session = session
        self.tools: List[Any] = []
        self.tool_descriptions = ""

    async def ainvoke(self, tool, arguments: Dict[str, Any]) -> str:
        """经 MCP 协议调用工具。"""
        args = dict(arguments or {})
        if tool.name == "send_html_email":
            args.setdefault("html_body", args.get("body", ""))
        if tool.name == "send_email_with_attachment":
            args.setdefault("body", "请查看附件")

        try:
            result = await self.session.call_tool(tool.name, args)
        except Exception as e:  # noqa: BLE001
            return "❌ 调用工具时异常: %s" % e

        text = "\n".join(getattr(c, "text", "") for c in result.content).strip()
        return text or "✅ 操作完成"

    async def initialize(self) -> bool:
        """从 MCP 服务器加载工具并构建描述。"""
        print("📡 正在获取 MCP 工具清单...")
        listed = await self.session.list_tools()

        self.tools = [
            type(
                "Tool",
                (),
                {"name": t.name, "description": t.description},
            )()
            for t in listed.tools
        ]

        if not self.tools:
            raise Exception("MCP 服务器未返回任何工具。")

        self.tool_descriptions = "\n".join(
            "- %s: %s" % (t.name, t.description) for t in self.tools
        )
        print("✅ 助手初始化完成，可以接受指令。")
        return True

    async def _call_ollama(self, prompt: str) -> str:
        """调用 Ollama 模型的 API"""
        async with httpx.AsyncClient(timeout=90.0) as client:
            try:
                response = await client.post(
                    f"{OLLAMA_BASE_URL}/api/generate",
                    json={
                        "model": OLLAMA_MODEL,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.3, "num_predict": 500},
                    },
                )

                if response.status_code == 200:
                    return response.json().get("response", "").strip()
                return f"❌ Ollama API错误: {response.status_code}"

            except httpx.ConnectError:
                return (
                    f"❌ 无法连接到Ollama服务，请确保Ollama已启动\n"
                    f"   地址: {OLLAMA_BASE_URL}\n   运行: ollama serve"
                )
            except Exception as e:  # noqa: BLE001
                return f"❌ 调用Ollama时出错: {e}"

    async def process_command(self, user_input: str) -> str:
        """处理用户自然语言指令的核心方法"""
        print(f"\n🧠 正在分析指令: '{user_input}'")

        analysis_prompt = f"""你是一个QQ邮箱助手，拥有以下工具：
        {self.tool_descriptions}

        用户指令："{user_input}"

        请严格按以下JSON格式回答，只输出JSON，不要额外解释：
        {{
          "tool": "工具名称",
          "reason": "选择此工具的原因，简短说明",
          "parameters": {{}}
        }}

        重要规则：
        1. 所有邮件工具必须包含 'to_email' 参数（若用户未指定，填 {settings.smtp_email}）
        2. 附件邮件工具需要 'body' 和 'attachment_paths' 参数
        3. 保存配置工具需要 'config_data' 参数
        4. 文件名参数为 'filename'

        具体工具参数要求：
        1. send_text_email: {{"to_email": "...", "subject": "...", "body": "..."}}
        2. send_html_email: {{"to_email": "...", "subject": "...", "html_body": "..."}}
        3. send_email_with_attachment: {{"to_email": "...", "subject": "...", "body": "...", "attachment_paths": ["..."]}}
        4. check_email_config: {{}}
        5. save_environment_config: {{"config_data": {{"key": "value"}}, "filename": "..."}}

        用户指令: "检查邮箱配置"
        回答: {{"tool": "check_email_config", "reason": "检查邮箱配置", "parameters": {{}}}}

        请根据用户指令，选择正确的工具并提供完整的参数："""

        ai_response = await self._call_ollama(analysis_prompt)
        print(f"  收到AI分析结果: {ai_response[:150]}...")

        try:
            json_match = re.search(r"\{.*\}", ai_response, re.DOTALL)
            ai_json = json.loads(json_match.group() if json_match else ai_response)

            tool_name = str(ai_json.get("tool", "")).strip()
            reason = ai_json.get("reason", "")
            parameters = ai_json.get("parameters", {}) or {}

            if not tool_name:
                return "❌ AI未指定要使用的工具。"

            print(f"  解析结果：使用工具 '{tool_name}'，原因：{reason}")

        except json.JSONDecodeError as e:
            return f"❌ 无法解析AI的响应为JSON: {e}\nAI原始响应:\n{ai_response}"

        target_tool = next((t for t in self.tools if t.name == tool_name), None)
        if not target_tool:
            return (
                f"❌ 找不到工具 '{tool_name}'。\n"
                f"可用工具: {[t.name for t in self.tools]}"
            )

        print(f"  正在经 MCP 调用工具 '{tool_name}'，参数: {parameters}")
        return await self.ainvoke(target_tool, parameters)

    async def quick_demo(self):
        """快速演示功能"""
        print("\n🚀 开始快速演示...")

        print("\n1. 🔧 检查邮箱配置...")
        check_tool = next((t for t in self.tools if t.name == "check_email_config"), None)
        if check_tool:
            print(f"   结果: {await self.ainvoke(check_tool, {})}")

        print("\n2. ✉️  发送测试邮件（经 MCP 协议，发给自己）...")
        send_tool = next((t for t in self.tools if t.name == "send_text_email"), None)
        if send_tool:
            result = await self.ainvoke(
                send_tool,
                {
                    # 默认发给自己（即 .env 中的 SMTP_EMAIL），
                    # 不要写死某个固定地址，否则他人 clone 后演示会把邮件发给原作者
                    "to_email": settings.smtp_email,
                    "subject": f"智能助手演示 {datetime.now().strftime('%H:%M:%S')}",
                    "body": (
                        "这是一封经 MCP 协议发出的演示邮件。\n"
                        f"发送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    ),
                },
            )
            print(f"   结果: {result}")
            print("   📧 请检查你的QQ邮箱收件箱！")

        print("\n✅ 演示完成！")


async def main(session: ClientSession):
    """主函数：启动智能助手（在已建立的 MCP 会话内运行）"""
    assistant = OllamaEmailAssistant(session)

    if not await assistant.initialize():
        print("❌ 助手初始化失败，请检查上述服务。")
        return

    print("\n💬 智能助手已就绪！你可以用自然语言指挥我发送邮件。")
    print("   例如：")
    print(f'        "发送你好给{settings.smtp_email}"')
    print('        "检查一下邮箱配置"')
    print('        "保存当前的环境设置"')
    print('        "输入 quit 或 exit 退出"')
    print("-" * 60)

    demo_choice = input("是否先运行演示任务？(y/n): ").strip().lower()
    if demo_choice == "y":
        await assistant.quick_demo()

    while True:
        try:
            user_input = input("\n📝 你的指令: ").strip()

            if user_input.lower() in ["quit", "exit", "退出", "q"]:
                print("👋 再见！")
                break

            if not user_input:
                continue

            print(f"\n📨 执行结果:\n{await assistant.process_command(user_input)}")

        except KeyboardInterrupt:
            print("\n\n👋 用户中断")
            break
        except Exception as e:  # noqa: BLE001
            print(f"\n❌ 处理指令时出错: {e}")


async def quick_test(session: ClientSession):
    """快速测试：直接跑一个固定例子"""
    print("🧪 运行快速测试...")

    assistant = OllamaEmailAssistant(session)
    if not await assistant.initialize():
        return

    test_command = "检查邮箱配置"
    print(f"\n测试指令: '{test_command}'")
    print(f"\n测试结果:\n{await assistant.process_command(test_command)}")


async def run():
    """
    在单个 task 内完成：建立 MCP 会话 -> 执行交互 -> 关闭会话。

    必须这样组织，因为官方 MCP 客户端基于 anyio，
    会话的 __aenter__ 与 __aexit__ 不允许跨 task。
    """
    try:
        async with MCPEmailClient() as client:
            print("\n⚠️  使用前请确认:")
            print("   1. MCP服务器已运行 (python run_server.py)")
            print(f"   2. Ollama 已运行且模型 '{OLLAMA_MODEL}' 可用")
            print(f"   3. 服务地址: Ollama->{OLLAMA_BASE_URL}, MCP->{MCP_SERVER_URL}")
            print("=" * 60)

            if len(sys.argv) > 1 and sys.argv[1] == "test":
                await quick_test(client.session)
            else:
                await main(client.session)
    except Exception as e:  # noqa: BLE001
        print(f"❌ 启动助手时发生严重错误: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n\n👋 程序退出")

    input("\n按回车键退出程序...")
