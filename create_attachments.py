# create_attachments.py - 生成用于测试的示例附件
"""
生成几个小体积的示例附件，方便试发带附件的邮件。

这些文件只用于演示与测试：内容简单、可随时重新生成，
因此不必手工准备，也不会把真实数据混进仓库。
"""

import csv
import json
import os
from datetime import datetime, timedelta

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attachments")


def write_text(name, content):
    path = os.path.join(OUTPUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("  已生成 %s（%d 字节）" % (name, os.path.getsize(path)))


def write_csv(name, header, rows):
    path = os.path.join(OUTPUT_DIR, name)
    # newline="" 是 csv 模块的官方建议，避免 Windows 上多出空行
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    print("  已生成 %s（%d 字节）" % (name, os.path.getsize(path)))


def write_json(name, payload):
    path = os.path.join(OUTPUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("  已生成 %s（%d 字节）" % (name, os.path.getsize(path)))


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today = datetime.now().date()

    print("正在生成示例附件…")

    # 1) 月度销售报表（CSV）—— 普通表格附件
    rows = [
        (6, "华东", "标准版授权", 12, 35880),
        (5, "华南", "标准版授权", 8, 23920),
        (4, "华北", "专业版授权", 3, 17970),
        (3, "华东", "专业版授权", 5, 29950),
        (2, "西南", "标准版授权", 6, 17940),
        (1, "华南", "增值服务", 20, 12000),
        (0, "华东", "增值服务", 15, 9000),
    ]
    write_csv(
        "示例报表.csv",
        ["日期", "区域", "产品", "数量", "金额"],
        [[str(today - timedelta(days=n)), r, p, q, a] for n, r, p, q, a in rows],
    )

    # 2) 会议纪要（纯文本）—— 正文较长的文本附件
    write_text(
        "示例会议纪要.txt",
        "项目例会纪要\n"
        "日期：{date}\n"
        "参会：产品、研发、测试\n"
        "\n"
        "一、上周进展\n"
        "1. 核心发送链路已联调通过，覆盖纯文本、HTML 与附件三类邮件。\n"
        "2. 异常路径已补充：区分可重试的瞬时故障与不可重试的永久错误。\n"
        "\n"
        "二、待办事项\n"
        "1. 补充发送指标的可视化展示。\n"
        "2. 评估大批量场景下的连接复用策略。\n"
        "\n"
        "三、下次会议\n"
        "{next_date}\n"
        "\n"
        "（本文档为示例文件，内容仅用于演示附件功能。）\n".format(
            date=today.isoformat(),
            next_date=(today + timedelta(days=7)).isoformat(),
        ),
    )

    # 3) 服务配置（JSON）—— 结构化数据附件。
    #    数值直接取自 config.settings，避免示例文件与真实默认值悄悄走偏。
    from config import settings

    write_json(
        "示例配置.json",
        {
            "service": "email-sender",
            "smtp": {
                "server": settings.smtp_server,
                "port": settings.smtp_port,
            },
            "limits": {
                "max_attachment_bytes": settings.max_attachment_bytes,
                "max_total_attachment_bytes": settings.max_total_attachment_bytes,
                "attachment_encoding_ratio": round(settings.attachment_encoding_ratio, 4),
            },
            "retry": {
                "max_attempts": settings.send_max_attempts,
                "initial_delay_seconds": settings.send_retry_initial_delay,
                "backoff": settings.send_retry_backoff,
            },
            "note": "示例文件，仅供演示附件功能使用。",
        },
    )

    print()
    print("完成。目录：%s" % OUTPUT_DIR)
    print("现在可以试着发送带附件的邮件了，例如：")
    print("    把 示例报表.csv 作为附件发给我自己")


if __name__ == "__main__":
    main()
