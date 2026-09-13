# langchain_client.py - 基于 LangChain + LangGraph 的 MCP 客户端
"""
与 ollama_mcp_client.py 的区别：
  - ollama_mcp_client.py：用字符串提示词让模型"猜"该调哪个工具，再手工解析 JSON。
  - 本文件：走标准 function-calling 路线——MCP 工具被自动转换成 LangChain 工具，
    由 LangGraph 的 ReAct 智能体负责推理与调用，无需手工解析模型输出。

依赖（见 requirements.txt）：
  langchain-mcp-adapters —— 把 MCP 工具适配成 LangChain 工具
  langchain-ollama       —— 提供支持 tool calling 的 ChatOllama
  langchain              —— 提供 create_agent

注意 langchain-community 里也有一个 ChatOllama，但它没有实现 bind_tools，
会导致 create_agent 抛出 NotImplementedError，因此这里必须用 langchain_ollama 的版本。

前置条件：
  1. MCP 服务器已启动：python run_server.py
  2. Ollama 已启动且模型可用：ollama serve && ollama pull qwen2.5:3b

用法：
  python langchain_client.py          # 交互模式
  python langchain_client.py demo     # 自动跑一个演示任务
  python langchain_client.py tools    # 只列出从 MCP 服务器加载到的工具
"""

import asyncio
import sys

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_ollama import ChatOllama

# --- 配置 ---
MCP_SERVER_URL = "http://localhost:8000/mcp"
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:3b"

SYSTEM_PROMPT = """你是一个 QQ 邮箱助手，可以调用工具帮用户处理邮件。

规则：
1. 发送邮件前先确认收件人、主题和内容是否齐全。
2. 附件需要完整的文件路径。
3. 一次只处理一个主要任务。
4. 用简洁的中文回答，并说明你调用了哪个工具。
"""


def build_mcp_client() -> MultiServerMCPClient:
    """创建指向本项目 MCP 服务器的多服务器客户端。"""
    return MultiServerMCPClient(
        {
            "qqmail": {
                "url": MCP_SERVER_URL,
                "transport": "streamable_http",
            }
        }
    )


async def load_tools(client: MultiServerMCPClient):
    """从 MCP 服务器加载工具并转换为 LangChain 工具。"""
    tools = await client.get_tools()
    print("✅ 已从 MCP 服务器加载 %d 个工具：" % len(tools))
    for t in tools:
        print("   - %s: %s" % (t.name, t.description))
    return tools


def build_agent(tools):
    """用 Ollama 模型与 MCP 工具构建 ReAct 智能体。"""
    model = ChatOllama(
        base_url=OLLAMA_BASE_URL,
        model=OLLAMA_MODEL,
        temperature=0.3,
    )
    return create_agent(model, tools, system_prompt=SYSTEM_PROMPT)


def _extract_text(message) -> str:
    """从智能体返回的消息中取出可读文本（兼容 content 为字符串或内容块列表）。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(content)


async def ask(agent, question: str) -> str:
    """向智能体提问并返回最终回答。"""
    result = await agent.ainvoke({"messages": [("user", question)]})
    return _extract_text(result["messages"][-1])


async def demo(agent):
    """运行一个不需要发送真实邮件的演示任务。"""
    print("\n🚀 演示：让智能体检查邮箱配置")
    answer = await ask(agent, "请检查一下我的邮箱配置是否正常。")
    print("\n📨 智能体回答：\n%s" % answer)


async def show_tools_only(client):
    """仅展示加载到的工具，不构建智能体（无需 Ollama）。"""
    await load_tools(client)


async def chat_loop(agent):
    """交互式对话循环。"""
    print("\n" + "=" * 56)
    print("QQ邮箱助手已就绪（输入 quit / exit 退出）")
    print("=" * 56)

    while True:
        try:
            user_input = input("\n💬 你想做什么？: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n👋 再见！")
            break

        if user_input.lower() in ["quit", "exit", "退出", "q"]:
            print("👋 再见！")
            break
        if not user_input:
            continue

        print("🔄 处理中...")
        try:
            print("\n📨 结果：%s" % await ask(agent, user_input))
        except Exception as e:  # noqa: BLE001
            print("❌ 处理失败：%s" % e)


async def main():
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "chat"
    client = build_mcp_client()

    if mode == "tools":
        await show_tools_only(client)
        return

    print("📡 正在从 MCP 服务器加载工具...")
    tools = await load_tools(client)

    print("🤖 正在构建智能体（模型：%s）..." % OLLAMA_MODEL)
    agent = build_agent(tools)

    if mode == "demo":
        await demo(agent)
    else:
        await chat_loop(agent)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 程序退出")
    except Exception as e:  # noqa: BLE001
        print("\n❌ 运行出错：%s: %s" % (type(e).__name__, e))
        print("   请确认 MCP 服务器（python run_server.py）与 Ollama 均已启动。")
        raise
