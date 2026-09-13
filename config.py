# config.py - 配置管理
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    全部配置经 .env 或环境变量注入。

    smtp_email 与 smtp_password **刻意不设默认值**：缺失时构造会抛
    ValidationError，避免静默回退到某个内置凭据。
    """

    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent / ".env",
        env_file_encoding="utf-8",
    )

    # QQ邮箱配置
    smtp_server: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_email: str
    smtp_password: str

    # MCP服务器配置
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    debug: bool = True

    #: /mcp 端点的 Bearer 访问令牌。
    #: 为 None 时不启用鉴权（仅适合本机开发）；
    #: 只要设置了值，所有对 /mcp 的请求都必须携带 Authorization: Bearer <token>。
    mcp_auth_token: Optional[str] = None

    # Ollama配置
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"

    # 发送确认（IMAP 回读）
    #: 是否在发送后用 IMAP 回读「已发送」来确认 QQ 确实接收并归档。
    #: 每次发送会多一次 IMAP 往返（约 1-3 秒），因此默认关闭。
    email_confirm_delivery: bool = False

    #: IMAP 服务器。QQ 邮箱与 SMTP 共用同一个授权码。
    imap_server: str = "imap.qq.com"
    imap_port: int = 993

    #: 「已发送」文件夹名（imap.qq.com 实测值）。
    #: imaplib 不会自动加引号，名称含空格，因此这里带引号。
    imap_sent_folder: str = '"Sent Messages"'

    #: 确认时的轮询次数与间隔（秒）。归档有延迟，单次查询易假阴性。
    confirm_attempts: int = 3
    confirm_interval: float = 2.0

    # 重试（仅针对瞬时故障）
    #: 发送的总尝试次数（含首次）。1 表示不重试。
    send_max_attempts: int = 3

    #: 首次重试前的等待秒数，以及退避倍数。
    send_retry_initial_delay: float = 1.0
    send_retry_backoff: float = 2.0

    #: 连接在发送过程中断裂时，是否换新连接重试一次。
    #:
    #: 这里有一个无法完全消除的取舍：SMTP 是 at-least-once 语义，
    #: 断开发生在「服务器已接收」之后时，盲目重连会重复发信。
    #: 默认开启，因为更常见的故障是空闲连接在传输正文前被回收（此时重发安全）。
    #: 若你的场景更不能容忍重复邮件，请置为 false，并改用幂等键。
    send_retry_on_reconnect: bool = True

    #: 幂等键的保留时长（秒），以及「处理中」被视为卡死的时长。
    idempotency_ttl: float = 3600.0
    idempotency_inflight_timeout: float = 300.0

    # 附件大小限制
    #
    # 判断口径是「编码后的体积」，而不是原始文件大小——
    # 因为 QQ 邮箱限制的是传输中的字节数，而 Base64 编码会让体积膨胀约 1/3。
    # 早期版本按原始大小比较（原始 16MB 实际传输约 21.9MB），余量已经偏薄。
    #
    #: Base64 编码带来的体积膨胀倍数（4/3）。
    #: 每 3 字节原文编码成 4 字节，因此实际传输量约为原始的 1.34 倍。
    attachment_encoding_ratio: float = 4 / 3

    #: 单封邮件内所有附件的**原始**字节上限。
    #: 20MB × 1.34 ≈ 26.8MB —— 与 QQ 的约 25MB 上限相当，因此 20MB 已是上限值；
    #: 默认取 18MB（编码后约 24.1MB），留出邮件头与正文的空间。
    max_total_attachment_bytes: int = 18 * 1024 * 1024

    #: 单个附件的原始字节上限。多数场景是「选错了文件」，设得比合计更严一些。
    #: 默认 10MB（编码后约 13.4MB）。
    max_attachment_bytes: int = 10 * 1024 * 1024

    # 附件存储路径
    attachment_dir: Path = Path(__file__).parent / "attachments"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # 确保附件目录存在
        self.attachment_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()