# create_attachments.py - 创建测试附件
import json
import os
from datetime import datetime

# 确保attachments目录存在
os.makedirs("attachments", exist_ok=True)

print("📁 正在创建测试附件文件...")

# 1. 创建老师环境配置详细版.json
teacher_config = {
    "项目信息": {
        "名称": "QQ邮箱MCP工具",
        "版本": "1.0.0",
        "创建时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "状态": "测试通过"
    },
    "环境配置": {
        "Python版本": "3.11",
        "虚拟环境": "venv",
        "Ollama模型": "qwen2.5:3b",
        "邮箱": "your_email@qq.com"
    },
    "项目结构": [
        "qqmail-mcp-tool/",
        "├── config.py",
        "├── email_tools.py",
        "├── mcp_server.py",
        "├── email_butler.py",
        "├── attachments/",
        "└── requirements.txt"
    ],
    "功能列表": [
        "send_text_email - 发送纯文本邮件",
        "send_html_email - 发送HTML邮件",
        "send_email_with_attachment - 发送带附件邮件",
        "check_email_config - 检查邮箱配置",
        "save_environment_config - 保存环境配置"
    ],
    "测试数据": {
        "已测试功能": [1, 2, 3, 4, 5],
        "发送成功": True,
        "AI集成": "正常"
    }
}

with open("attachments/老师环境配置详细版.json", "w", encoding="utf-8") as f:
    json.dump(teacher_config, f, ensure_ascii=False, indent=2)
print("✅ 创建: attachments/老师环境配置详细版.json")

# 2. 创建周报.txt
weekly_report = """项目周报 - QQ邮箱MCP工具
报告时间：{time}

本周进展：
✅ 已完成功能：
1. 纯文本邮件发送功能
2. HTML邮件发送功能  
3. 带附件邮件发送功能
4. 邮箱配置检查功能
5. 环境配置保存功能

✅ 技术实现：
• MCP服务器搭建完成
• Ollama AI集成成功
• QQ邮箱SMTP连接正常
• 自然语言指令解析正常

✅ 测试结果：
• 所有5个核心功能可用
• AI能正确理解自然语言指令
• 邮件发送成功率：100%

下周计划：
1. 添加邮件模板功能
2. 增加邮件发送统计
3. 优化用户交互界面

负责人：你的名字
联系方式：your_email@qq.com
""".format(time=datetime.now().strftime("%Y-%m-%d"))

with open("attachments/周报.txt", "w", encoding="utf-8") as f:
    f.write(weekly_report)
print("✅ 创建: attachments/周报.txt")

# 3. 创建测试数据.csv
csv_data = """功能编号,功能名称,状态,测试时间
1,send_text_email,通过,{time}
2,send_html_email,通过,{time}
3,send_email_with_attachment,通过,{time}
4,check_email_config,通过,{time}
5,save_environment_config,通过,{time}
""".format(time=datetime.now().strftime("%Y-%m-%d %H:%M"))

with open("attachments/测试数据.csv", "w", encoding="utf-8") as f:
    f.write(csv_data)
print("✅ 创建: attachments/测试数据.csv")

# 4. 创建图片占位文件
image_info = {
    "图片信息": "这是一个图片文件的占位描述",
    "实际路径": "需要时替换为真实图片文件",
    "建议格式": ["jpg", "png", "pdf"],
    "大小限制": "QQ邮箱附件通常支持25MB以下"
}

with open("attachments/图片说明.txt", "w", encoding="utf-8") as f:
    f.write("如需测试图片附件，请在此目录放置真实的图片文件\n")
    f.write("支持的格式：jpg, png, pdf, docx\n")
    f.write(json.dumps(image_info, ensure_ascii=False, indent=2))
print("✅ 创建: attachments/图片说明.txt")

print(f"\n🎉 成功创建了4个测试附件文件！")
print(f"📁 目录: {os.path.abspath('attachments')}")
print("\n现在可以测试带附件的邮件发送了！")