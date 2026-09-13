# run_server.py - 放在项目根目录
import uvicorn
import sys
import os

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(__file__))

if __name__ == "__main__":
    print("🚀 启动QQ邮箱MCP服务器...")
    print("🌐 访问地址: http://localhost:8000")
    print("按 Ctrl+C 停止服务器\n")

    # 直接运行uvicorn
    uvicorn.run(
        "mcp_server:app",  # 注意这里的格式
        host="0.0.0.0",
        port=8000,
        reload=False
    )