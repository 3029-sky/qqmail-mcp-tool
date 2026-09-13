# teacher_config_sample.py
"""
老师的环境配置示例
学生可以参考此配置设置自己的环境
"""

TEACHER_ENVIRONMENT_CONFIG = {
    "个人信息": {
        "姓名": "老师",
        "邮箱": "your_email@qq.com",   # 替换为你自己的邮箱
        "角色": "Python/MCP课程讲师"
    },

    "系统环境": {
        "操作系统": "Windows 11 专业版",
        "Python版本": "3.11.8",
        "包管理工具": "pip + venv",
        "终端": "Windows Terminal + PowerShell"
    },

    "开发环境": {
        "编辑器": "VS Code 1.85+",
        "Python扩展": "已安装",
        "Git": "2.42+",
        "数据库": "SQLite3（内置）"
    },

    "项目配置": {
        "项目名称": "qqmail-mcp-tool",
        "项目结构": [
            "qqmail-mcp-tool/",
            "├── .env                    # 配置文件（不入库）",
            "├── .env.example            # 配置模板",
            "├── .gitignore",
            "├── requirements.txt        # 运行时依赖",
            "├── requirements-dev.txt    # 测试依赖",
            "├── config.py              # 配置管理",
            "├── tool_defs.py           # 工具定义与分发的唯一真相来源",
            "├── email_tools.py         # 邮件发送核心（MIME 构建、指标、日志）",
            "├── smtp_pool.py           # SMTP 连接复用",
            "├── metrics.py             # 发送指标",
            "├── mcp_server.py          # MCP 服务器（官方 Streamable HTTP 传输）",
            "├── run_server.py          # 服务器启动脚本",
            "├── ollama_mcp_client.py   # 原生 MCP 客户端 + Ollama",
            "├── langchain_client.py    # LangChain/LangGraph 智能体客户端",
            "├── teacher_config_sample.py",
            "├── tests/                 # pytest 测试套件",
            "└── attachments/           # 附件目录",
        ],
        "虚拟环境": {
            "路径": "./venv",
            "激活命令_Windows": "venv\\Scripts\\activate",
            "激活命令_MacLinux": "source venv/bin/activate"
        }
    },

    "QQ邮箱设置": {
        "SMTP服务器": "smtp.qq.com",
        "端口": "465 (SSL)",
        "安全连接": "必须使用SSL/TLS",
        "认证方式": "授权码（不是登录密码）",
        "获取授权码": {
            "步骤1": "登录QQ邮箱网页版",
            "步骤2": "设置 → 账户",
            "步骤3": "找到『POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务』",
            "步骤4": "开启『IMAP/SMTP服务』",
            "步骤5": "生成16位授权码",
            "注意": "授权码只显示一次，请妥善保存"
        }
    },

    "Ollama配置": {
        "版本": "0.1.25+",
        "安装": "从 https://ollama.com 下载安装",
        "模型": "qwen2.5:3b",
        "拉取模型命令": "ollama pull qwen2.5:3b",
        "运行模型命令": "ollama run qwen2.5:3b",
        "API地址": "http://localhost:11434"
    },

    "MCP相关": {
        "MCP版本": "1.0.0+",
        "协议标准": "JSON-RPC 2.0",
        "通信方式": "HTTP (FastAPI)",
        "默认端口": "8000",
        "测试地址": "http://localhost:8000"
    },

    "常见问题解决": {
        "邮箱连接失败": [
            "1. 确认授权码正确（16位，非登录密码）",
            "2. 确认QQ邮箱已开启SMTP服务",
            "3. 检查防火墙是否阻止465端口",
            "4. 尝试关闭VPN或代理"
        ],
        "导入错误": [
            "1. 确保已激活虚拟环境",
            "2. 确保在项目根目录运行",
            "3. 运行 pip install -r requirements.txt",
            "4. 检查Python版本是否为3.11"
        ],
        "Ollama连接失败": [
            "1. 确认Ollama已安装并运行",
            "2. 运行 ollama serve 启动服务",
            "3. 检查端口11434是否被占用",
            "4. 确认模型已下载: ollama list"
        ]
    },

    "测试流程": {
        "步骤1": "配置 .env 文件（邮箱、授权码）",
        "步骤2": "安装依赖: pip install -r requirements.txt",
        "步骤3": "运行测试: python test_mcp.py",
        "步骤4": "启动服务器: python mcp_server.py",
        "步骤5": "启动客户端: python langchain_client.py",
        "验证": "检查邮箱是否收到测试邮件"
    },

    "学习资源": {
        "MCP文档": "https://modelcontextprotocol.io",
        "LangChain文档": "https://python.langchain.com",
        "QQ邮箱帮助": "https://service.mail.qq.com",
        "Ollama文档": "https://github.com/ollama/ollama",
        "Python异步编程": "https://docs.python.org/3/library/asyncio.html"
    },

    "时间戳": {
        "配置创建时间": "2024-01-15 10:30:00",
        "最后更新时间": "2024-01-15 10:30:00",
        "适用课程": "Python MCP工具开发实战"
    },

    "备注": [
        "此配置为示例，学生应根据自己的实际情况调整",
        "授权码等敏感信息不要提交到Git仓库",
        "建议使用 .gitignore 忽略 .env 和附件目录",
        "遇到问题时，先检查错误日志，再参考常见问题解决"
    ]
}


def save_config_to_file(filename: str = "老师环境配置详细版.json"):
    """将老师的环境配置保存到文件"""
    import json
    from pathlib import Path

    # 确保attachments目录存在
    attachments_dir = Path(__file__).parent / "attachments"
    attachments_dir.mkdir(exist_ok=True)

    file_path = attachments_dir / filename

    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(TEACHER_ENVIRONMENT_CONFIG, f, ensure_ascii=False, indent=2)

    print(f"✅ 老师环境配置已保存到: {file_path}")
    print(f"文件大小: {file_path.stat().st_size} 字节")

    return str(file_path)


if __name__ == "__main__":
    # 保存配置
    saved_path = save_config_to_file()

    print("\n📋 配置内容概览:")
    print(f"1. 个人信息: {TEACHER_ENVIRONMENT_CONFIG['个人信息']['姓名']}")
    print(f"2. 系统环境: Python {TEACHER_ENVIRONMENT_CONFIG['系统环境']['Python版本']}")
    print(
        f"3. QQ邮箱: {TEACHER_ENVIRONMENT_CONFIG['QQ邮箱设置']['SMTP服务器']}:{TEACHER_ENVIRONMENT_CONFIG['QQ邮箱设置']['端口']}")
    print(f"4. Ollama模型: {TEACHER_ENVIRONMENT_CONFIG['Ollama配置']['模型']}")
    print(f"5. MCP端口: {TEACHER_ENVIRONMENT_CONFIG['MCP相关']['默认端口']}")

    print("\n💡 此文件可供学生参考环境配置")