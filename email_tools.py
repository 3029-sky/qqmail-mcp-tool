# email_tools.py - QQ邮箱邮件发送核心
"""
本模块负责：
  1. 构建 MIME 邮件（纯文本 / HTML / 附件）
  2. 通过 SMTP 发送，并处理 QQ 邮箱的非标准响应
  3. 记录结构化日志与发送指标

关于 QQ 邮箱的非标准响应：
    QQ 邮箱在邮件实际已投递的情况下，偶尔会返回 (-1, b'\\x00\\x00\\x00')。
    本模块将该情形判定为成功，并在返回值中标记 note 字段；
    真实错误（其他 smtp_code）一律如实报告为失败，不做掩盖。

关于连接复用：
    底层使用 SMTPConnectionPool（见 smtp_pool.py）。smtplib 的连接不能跨线程，
    因此池内用一个专用工作线程独占连接，避免每封信都重新做 TLS 握手与登录。
"""

import asyncio
import json
import logging
import mimetypes
import os
import smtplib
import tempfile
import threading
import time
from datetime import datetime
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from config import settings
from metrics import send_metrics
from retry import RetryPolicy, retry_call

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 日志：支持结构化 JSON 输出
# ---------------------------------------------------------------------------

class JsonLogFormatter(logging.Formatter):
    """把日志记录序列化为单行 JSON，便于被日志系统采集。"""

    #: LogRecord 的内置属性，不需要作为附加字段输出
    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        # 把调用方通过 extra= 传入的业务字段一并输出
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(json_logs: Optional[bool] = None) -> None:
    """按需把根日志配置为 JSON 格式。json_logs 默认取环境变量 JSON_LOGS。"""
    if json_logs is None:
        import os

        json_logs = os.getenv("JSON_LOGS", "").lower() in ("1", "true", "yes")

    if json_logs:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# 发送结果
# ---------------------------------------------------------------------------

class MissingAttachmentError(ValueError):
    """
    附件找不到。

    单独定义成异常，是为了让上层能给出「哪个文件找不到」这种可操作的提示，
    而不是笼统地报一句发送失败。若被静默忽略，用户会以为附件已经发出。
    """

    def __init__(self, missing: List[str]):
        self.missing = list(missing)
        super().__init__("附件不存在: %s" % "、".join(self.missing))


def _result(
    success: bool,
    message: str,
    to: Optional[Union[str, List[str]]] = None,
    subject: Optional[str] = None,
    recipients: Optional[List[str]] = None,
    reason: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """
    构造统一的返回结构，并集中记录指标与日志。

    所有返回路径都经过这里，因此成功率、失败原因、日志格式不会出现分歧。
    """
    payload: Dict[str, Any] = {
        "success": success,
        "message": message,
        "to": to,
        "subject": subject,
        "recipients": recipients or [],
        "timestamp": _now(),
    }
    if reason:
        payload["reason"] = reason
    payload.update(extra)

    if success:
        send_metrics.incr("sends_succeeded")
    else:
        send_metrics.incr("sends_failed")
        send_metrics.record_failure(reason or "unknown")

    return payload


def _now() -> str:
    return datetime.now().isoformat()


# ---------------------------------------------------------------------------
# MIME 构建
# ---------------------------------------------------------------------------

#: 归一化文件名主干时裁掉的后缀词。
#: 小模型常给文件名加修饰词，例如把「测试数据.csv」说成「测试数据表.xlsx」，
#: 裁掉这些词后两侧就能对上。
_STEM_SUFFIXES = (
    "文件", "表格", "文档", "附件", "那份", "这个", "那个", "表", "档",
)


def _normalize_stem(name: str) -> str:
    """把文件名主干归一化，便于宽松比较：去空白、转小写、裁掉常见后缀词。"""
    stem = Path(name).stem.strip().lower().replace(" ", "")
    changed = True
    while changed and stem:
        changed = False
        for suffix in _STEM_SUFFIXES:
            if stem.endswith(suffix) and len(stem) > len(suffix):
                stem = stem[: -len(suffix)]
                changed = True
    return stem


def resolve_attachment_paths(paths: List[str]) -> tuple:
    """
    把用户/模型给出的附件路径尽量解析成真实存在的文件。

    为什么需要它：小模型经常把文件名拼错或猜错扩展名
    （实测：目录里是 `测试数据.csv`，模型给出 `测试数据.xlsx`）。
    直接报错会让用户白跑一趟，而用户的本意显然是那个真实存在的文件。

    解析顺序：
      1. 原路径存在 -> 原样使用
      2. 目录里同名文件（大小写不敏感）
      3. 文件名主干相同、仅扩展名不同 —— 仅当**只有一个**候选时才采用，
         以免在多候选时猜错文件

    返回 (resolved, missing, substitutions)：
      substitutions 记录「原名 -> 实际使用的名字」，用于在结果里告知用户，
      避免它悄悄换成别的文件却不说明。
    """
    #: 归一化时裁掉的后缀词。小模型加修饰词时能对上，
    #: 例如把「测试数据.csv」说成「测试数据表.xlsx」
    resolved: List[str] = []
    missing: List[str] = []
    substitutions: List[str] = []

    for raw in paths or []:
        original = Path(raw)
        if original.is_file():
            resolved.append(str(original))
            continue

        directory = original.parent
        stem = original.stem
        if not (directory.is_dir() and stem):
            missing.append(str(original))
            continue

        entries = [e for e in directory.iterdir() if e.is_file()]

        # 第一优先：主干完全一致（仅扩展名可能不同）
        exact = [
            e for e in entries
            if Path(e.name).stem.lower() == stem.lower()
            or e.name.lower() == original.name.lower()
        ]
        # 第二优先：归一化后一致（能吸收「测试数据表」这类修饰词）
        normal = [
            e for e in entries if _normalize_stem(e.name) == _normalize_stem(original.name)
        ]

        chosen = None
        for candidates in (exact, normal):
            if len(candidates) == 1:
                chosen = candidates[0]
                break
            if len(candidates) > 1:
                # 多个候选 -> 无法确定用户要哪个，宁可报错也不乱猜
                break

        if chosen is not None:
            resolved.append(str(chosen))
            substitutions.append("%s → %s" % (original.name, chosen.name))
        else:
            missing.append(str(original))

    return resolved, missing, substitutions


class OversizedAttachmentError(ValueError):
    """
    附件超过大小限制。

    在发送前就拦下来，而不是等 SMTP 返回一个难懂的英文错误——
    否则用户只会看到「邮件发不出去」，却不知道原因是附件太大。
    """

    def __init__(self, message: str, oversize: List[Dict[str, Any]]):
        self.oversize = oversize
        super().__init__(message)


def _format_size(num_bytes: float) -> str:
    """把字节数格式化成便于阅读的形式。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f %s" % (size, unit)
        size /= 1024
    return "%.1f GB" % size


def check_attachment_sizes(paths: List[str]) -> List[Dict[str, Any]]:
    """
    检查附件大小，返回超限项（空列表表示都合格）。

    同时检查单件上限与合计上限，两者分开报告：
      - 单件超限通常是选错了文件（例如误选整个数据库）
      - 合计超限则是「每件都不大，但加起来太多」
    分开说明更利于用户判断该怎么处理。

    判断口径是**编码后**的体积，不是原始文件大小——QQ 限制的是传输中的
    字节数，而 Base64 会让体积膨胀约 1/3。按原始大小比较会低估实际占用。
    """
    ratio = settings.attachment_encoding_ratio
    per_limit = settings.max_attachment_bytes
    total_limit = settings.max_total_attachment_bytes

    oversize: List[Dict[str, Any]] = []
    total_raw = 0

    for raw in paths or []:
        path = Path(raw)
        try:
            size = path.stat().st_size
        except OSError:
            continue  # 读不到大小就交给上层的「文件不存在」逻辑处理
        total_raw += size

        if size > per_limit:
            oversize.append({
                "path": str(path),
                "name": path.name,
                "size": size,
                "size_text": _format_size(size),
                "encoded_size": int(size * ratio),
                "encoded_text": _format_size(size * ratio),
                "limit": per_limit,
                "limit_text": _format_size(per_limit),
                "kind": "single",
            })

    if total_raw > total_limit:
        oversize.append({
            "path": "",
            "name": "（合计）",
            "size": total_raw,
            "size_text": _format_size(total_raw),
            "encoded_size": int(total_raw * ratio),
            "encoded_text": _format_size(total_raw * ratio),
            "limit": total_limit,
            "limit_text": _format_size(total_limit),
            "kind": "total",
        })

    return oversize


def describe_oversize(oversize: List[Dict[str, Any]]) -> str:
    """
    把超限项整理成可直接展示给用户的说明。

    同时给出原始大小与编码后估算：
      - 只给原始大小，用户会觉得「我才 10MB，为什么说超了」
      - 只给编码后大小，用户对不上自己文件的属性面板
    两个都给，才解释得清。
    另外附上精确字节数——格式化后可能显示成「10.0 MB（上限 10.0 MB）」而看不出差多少。
    """
    single = [o for o in oversize if o["kind"] == "single"]
    total = [o for o in oversize if o["kind"] == "total"]

    lines: List[str] = []
    if single:
        lines.append("以下附件过大，超出单件上限 %s：" % single[0]["limit_text"])
        for o in single:
            lines.append(
                "  · %s：%s（编码后约 %s，即 %s 字节）"
                % (o["name"], o["size_text"], o["encoded_text"], o["size"])
            )
    if total:
        o = total[0]
        lines.append(
            "全部附件合计 %s（编码后约 %s），超出上限 %s。"
            % (o["size_text"], o["encoded_text"], o["limit_text"])
        )
    lines.append(
        "说明：邮件附件经 Base64 编码后体积约增加 1/3，因此按编码后计算。"
    )
    lines.append("建议：压缩后重发、拆成多封邮件，或改用网盘链接。")
    return "\n".join(lines)


def build_message(
    to_email: Union[str, List[str]],
    subject: str,
    body: str,
    html_body: Optional[str] = None,
    attachments: Optional[List[str]] = None,
    cc: Optional[List[str]] = None,
    allow_missing_attachments: bool = False,
) -> MIMEMultipart:
    """
    构建 MIME 邮件。

    附件处理：
      默认（allow_missing_attachments=False）遇到不存在的路径会**抛
      MissingAttachmentError**。这是刻意选择的：
        「默默跳过附件、照样回一句发送成功」会让用户以为附件发出去了，
        而对方实际上只收到一封空邮件——这类静默失败很难被发现。
      只有明确知道自己要宽松处理时才传 True。
    """
    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, "plain", "utf-8"))

    msg["From"] = formataddr(("QQ邮箱助手", settings.smtp_email))
    msg["To"] = ", ".join(to_email) if isinstance(to_email, list) else to_email
    msg["Subject"] = Header(subject, "utf-8")
    if cc:
        msg["Cc"] = ", ".join(cc)

    missing: List[str] = []
    for file_path in attachments or []:
        path = Path(file_path)
        if not path.is_file():
            if allow_missing_attachments:
                logger.warning("附件不存在，已跳过 path=%s", file_path)
                continue
            missing.append(str(file_path))
            continue

        mime_type, _ = mimetypes.guess_type(str(path))
        main_type, sub_type = (mime_type or "application/octet-stream").split("/", 1)
        attachment = MIMEApplication(path.read_bytes(), _subtype=sub_type)
        attachment.add_header(
            "Content-Disposition",
            "attachment",
            filename=Header(path.name, "utf-8").encode(),
        )
        msg.attach(attachment)

    if missing:
        raise MissingAttachmentError(missing)

    return msg


def _normalize_recipients(
    to_email: Union[str, List[str]],
    cc: Optional[List[str]],
    bcc: Optional[List[str]],
) -> List[str]:
    """合并收件人、抄送、密送，供 SMTP 的 RCPT TO 使用。"""
    if isinstance(to_email, str):
        recipients = [to_email.strip()]
    else:
        recipients = [e.strip() for e in to_email]

    for extra in (cc or [], bcc or []):
        recipients.extend(e.strip() for e in extra)
    return recipients


# ---------------------------------------------------------------------------
# 发送器
# ---------------------------------------------------------------------------

class QQMailSender:
    """基于复用连接的 QQ 邮箱发送器。"""

    def __init__(self):
        self.smtp_server = settings.smtp_server
        self.smtp_port = settings.smtp_port
        self.smtp_email = settings.smtp_email
        self.smtp_password = settings.smtp_password
        self._worker = None          # 延迟创建，避免未使用连接时也起线程
        self._worker_lock = threading.Lock()

    # -- 连接 ---------------------------------------------------------------

    @property
    def worker(self):
        """
        延迟创建 SMTP 工作线程（首次发送时才建立连接）。

        必须加锁：发送经 asyncio.to_thread 执行，多个线程可能同时首次访问本属性。
        缺少保护时它们会同时看到 _worker 为 None，从而各自创建一个连接池，
        并发场景下出现多条本应唯一的连接（实测 8 并发会建立 8 条连接）。
        """
        if self._worker is None:
            with self._worker_lock:
                # 双重检查：等锁期间可能已被其他线程创建
                if self._worker is None:
                    from smtp_pool import SMTPConnectionPool, SMTPWorker

                    pool = SMTPConnectionPool(
                        host=self.smtp_server,
                        port=self.smtp_port,
                        user=self.smtp_email,
                        password=self.smtp_password,
                    )
                    self._worker = SMTPWorker(pool)
        return self._worker

    def close(self) -> None:
        """关闭底层连接与工作线程。"""
        if self._worker is not None:
            self._worker.close()
            self._worker = None

    # -- 幂等 ---------------------------------------------------------------

    @staticmethod
    def _store():
        """取全局幂等存储，并按配置同步 TTL。"""
        from idempotency import idempotency_store

        idempotency_store.ttl = settings.idempotency_ttl
        idempotency_store.inflight_timeout = settings.idempotency_inflight_timeout
        return idempotency_store

    # -- 发送 ---------------------------------------------------------------

    def send_email_sync(
        self,
        to_email: Union[str, List[str]],
        subject: str,
        body: str,
        html_body: Optional[str] = None,
        attachments: Optional[List[str]] = None,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        发送邮件（同步）。返回统一结构的结果字典。

        idempotency_key 非空时启用幂等保护：同一个键在有效期内只会真正发送一次，
        重复请求直接返回首次的结果（带 duplicate=true），不会重复发信。
        """
        started = time.monotonic()

        # 1) 幂等检查必须在最前面：重复请求不应产生任何副作用，
        #    也不应计入发送总数（否则成功率等派生指标会失真）
        store = None
        owner = None
        if idempotency_key:
            store = self._store()
            existing = store.lookup(idempotency_key)
            if existing is not None:
                send_metrics.incr("idempotent_hits")
                logger.info("幂等命中，未重复发送 key=%s", idempotency_key)
                return existing
            owner = store.reserve(idempotency_key)

        send_metrics.incr("sends_total")

        def finish(result: Dict[str, Any], keep_key: bool) -> Dict[str, Any]:
            """统一收尾：成功则记录结果，失败则释放键以便重试。"""
            if store is not None and owner is not None:
                if keep_key:
                    store.complete(idempotency_key, owner, result)
                else:
                    store.release(idempotency_key, owner)
            return result

        recipients = _normalize_recipients(to_email, cc, bcc)

        # 先尝试把附件路径解析成真实文件：小模型常把扩展名猜错，
        # 按主干唯一匹配可以救回来，并在结果里说明替换了什么。
        substitutions: List[str] = []
        if attachments:
            attachments, missing_paths, substitutions = resolve_attachment_paths(
                attachments
            )
            if missing_paths:
                logger.error("附件不存在，已中止发送: %s", missing_paths)
                return finish(
                    _result(
                        False,
                        "附件不存在，邮件未发送：%s\n"
                        "请确认文件路径是否正确，或先确认文件是否仍然存在。"
                        % "、".join(missing_paths),
                        to_email, subject, recipients,
                        reason="missing_attachment",
                        missing_attachments=missing_paths,
                    ),
                    keep_key=False,
                )
            if substitutions:
                logger.info("附件路径已按文件名主干修正: %s", substitutions)

            # 大小检查放在路径解析之后：只对真实存在的文件称重。
            # 提前拒绝比等 QQ 回一个 SMTP 错误更可读，也省掉一次无用的传输。
            oversize = check_attachment_sizes(attachments)
            if oversize:
                detail = describe_oversize(oversize)
                logger.error("附件超限，已中止发送: %s", detail.replace("\n", " "))
                return finish(
                    _result(
                        False,
                        "附件超过大小限制，邮件未发送。\n%s" % detail,
                        to_email, subject, recipients,
                        reason="attachment_too_large",
                        oversize_attachments=oversize,
                    ),
                    keep_key=False,
                )

        try:
            msg = build_message(to_email, subject, body, html_body, attachments, cc)
        except MissingAttachmentError as e:
            # 兜底：解析后仍缺失（例如解析与构建之间文件被删）
            logger.error("附件不存在，已中止发送: %s", e.missing)
            return finish(
                _result(
                    False,
                    "附件不存在，邮件未发送：%s\n"
                    "请确认文件路径是否正确，或先确认文件是否仍然存在。"
                    % "、".join(e.missing),
                    to_email, subject, recipients,
                    reason="missing_attachment",
                    missing_attachments=e.missing,
                ),
                keep_key=False,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("构建邮件失败: %s", e, exc_info=True)
            return finish(
                _result(
                    False, f"构建邮件失败: {e}", to_email, subject, recipients,
                    reason="build_error",
                ),
                keep_key=False,
            )

        logger.info(
            "开始发送 subject=%s recipients=%d attachments=%d html=%s",
            subject, len(recipients), len(attachments or []), bool(html_body),
        )
        if substitutions:
            logger.info("附件路径已修正，实际使用: %s", attachments)

        def action(server):
            return server.send_message(
                msg, from_addr=self.smtp_email, to_addrs=recipients
            )

        policy = RetryPolicy(
            max_attempts=settings.send_max_attempts,
            initial_delay=settings.send_retry_initial_delay,
            backoff=settings.send_retry_backoff,
        )

        def on_retry(attempt, exc, delay):
            send_metrics.incr("send_retries")
            send_metrics.record_failure("retryable_%s" % type(exc).__name__)

        try:
            retry_call(
                lambda: self.worker.send(
                    action,
                    retry_on_disconnect=settings.send_retry_on_reconnect,
                ),
                policy=policy,
                description="发送邮件",
                on_retry=on_retry,
            )
        except smtplib.SMTPResponseException as e:
            # 服务器明确拒绝：键可以释放，允许调用方修正后重试
            return finish(
                self._handle_response_exception(
                    e, to_email, subject, recipients, started
                ),
                keep_key=False,
            )
        except Exception as e:  # noqa: BLE001
            reason = (
                "auth_error"
                if isinstance(e, smtplib.SMTPAuthenticationError)
                else "transport_error"
            )
            message = (
                "SMTP认证失败，请检查授权码"
                if reason == "auth_error"
                else f"邮件发送失败: {e}"
            )
            return finish(
                self._fail(
                    message, to_email, subject, recipients,
                    started, reason=reason, error=str(e),
                ),
                keep_key=False,
            )

        result = self._succeed(
            "邮件发送成功", to_email, subject, recipients, started,
            attachments=attachments,
        )
        if substitutions:
            # 明确告知替换了什么，避免「悄悄用了另一个文件」而用户不知情
            result["note"] = "附件名已自动修正：%s" % "；".join(substitutions)
            result["attachment_substitutions"] = substitutions
        return finish(result, keep_key=True)

    # -- 发送确认（IMAP 回读）-------------------------------------------------

    def confirm_delivery(
        self, subject: str, recipients: List[str]
    ) -> Optional[Dict[str, Any]]:
        """
        用 IMAP 回读「已发送」确认 QQ 确实接收并归档了这封邮件。

        未启用时返回 None（调用方不加确认字段）。
        任何异常都只记录日志并返回「未做确认」，绝不影响发送本身的结论。
        """
        if not settings.email_confirm_delivery:
            return None

        try:
            from delivery import DeliveryConfirmer
        except Exception as e:  # noqa: BLE001
            logger.error("无法加载 delivery 模块: %s", e)
            return {"checked": False, "confirmed": False, "detail": "确认模块不可用"}

        confirmer = DeliveryConfirmer(
            host=settings.imap_server,
            port=settings.imap_port,
            user=self.smtp_email,
            password=self.smtp_password,
            folder=settings.imap_sent_folder,
        )
        confirmation = confirmer.confirm(
            subject=subject,
            recipients=recipients,
            attempts=settings.confirm_attempts,
            interval=settings.confirm_interval,
        )
        logger.info(
            "发送确认 confirmed=%s checked=%s detail=%s",
            confirmation.confirmed, confirmation.checked, confirmation.detail,
        )
        return confirmation.as_dict()

    # -- 结果处理 -----------------------------------------------------------

    def _handle_response_exception(
        self, e: smtplib.SMTPResponseException, to_email, subject, recipients, started
    ) -> Dict[str, Any]:
        """区分 QQ 邮箱的非标准响应与真实 SMTP 错误。"""
        if e.smtp_code == -1 and e.smtp_error == b"\x00\x00\x00":
            # 这个响应码无法自证成功，因此额外做一次 IMAP 回读作为独立证据。
            logger.info("QQ邮箱特殊响应(-1)，将尝试 IMAP 回读确认")
            result = self._succeed(
                "邮件发送成功（QQ邮箱特殊响应）", to_email, subject, recipients,
                started,
            )
            confirmation = self.confirm_delivery(subject, recipients)
            if confirmation is None:
                result["note"] = (
                    "QQ邮箱返回特殊响应(-1)，已按成功处理；"
                    "未启用发送确认，无法进一步核实"
                )
            elif confirmation["confirmed"]:
                result["note"] = "QQ邮箱返回特殊响应(-1)，已由 IMAP 回读确认为已归档"
            else:
                result["note"] = (
                    "QQ邮箱返回特殊响应(-1)，已按成功处理；"
                    "但 IMAP 回读未能找到该邮件，建议核对「已发送」"
                )
            if confirmation is not None:
                result["confirmation"] = confirmation
            return result

        # 非 -1 一律视为真实错误，不做掩盖
        logger.error("SMTP服务器拒绝 code=%s", e.smtp_code)
        server_text = ""
        try:
            server_text = e.smtp_error.decode("utf-8", "replace") if isinstance(
                e.smtp_error, bytes
            ) else str(e.smtp_error)
        except Exception:  # noqa: BLE001 - 解码失败不影响失败判定
            server_text = ""
        server_text = server_text.strip()
        # 把服务端原文一并带上：像「Too many attempts（限流）」这类信息只在原文里，
        # 只给错误码会让上层无法区分「收件人不存在」和「发得太频繁」。
        message = "SMTP服务器拒绝: 错误码 %s" % e.smtp_code
        if server_text:
            message += " - %s" % server_text
        return self._fail(
            message, to_email, subject, recipients,
            started, reason="smtp_reject", error=server_text or str(e.smtp_error),
            smtp_code=e.smtp_code,
        )

    def _succeed(
        self, message, to_email, subject, recipients, started, attachments=None
    ) -> Dict[str, Any]:
        elapsed = (time.monotonic() - started) * 1000
        send_metrics.observe_latency(elapsed)
        logger.info("发送成功 elapsed_ms=%.1f recipients=%d", elapsed, len(recipients))
        result = _result(True, message, to_email, subject, recipients)
        if attachments:
            result["attachments"] = list(attachments)

        confirmation = self.confirm_delivery(subject, recipients)
        if confirmation is not None:
            result["confirmation"] = confirmation
        return result

    def _fail(
        self, message, to_email, subject, recipients, started, reason=None, **extra
    ) -> Dict[str, Any]:
        elapsed = (time.monotonic() - started) * 1000
        send_metrics.observe_latency(elapsed)
        logger.error("发送失败 reason=%s elapsed_ms=%.1f", reason, elapsed)
        return _result(
            False, message, to_email, subject, recipients, reason=reason, **extra
        )


# ---------------------------------------------------------------------------
# 异步包装与高层工具
# ---------------------------------------------------------------------------

def _write_json_atomically(path: Path, data: Dict[str, Any]) -> int:
    """
    原子地写入 JSON 文件，返回写入字节数。

    先写同目录下的临时文件再 os.replace 覆盖目标，因此：
      - 进程中途被终止时，目标文件要么是旧内容、要么是新内容，不会出现半个文件
      - 读取方永远不会看到写了一半的 JSON

    注意：临时文件必须与目标同目录，否则 os.replace 可能跨文件系统而失去原子性。
    若目标被其他进程占用（Windows 上文件锁较严格），退回为直接写入并记录警告——
    宁可丢原子性，也不要让保存功能整体失败。
    """
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(
        dir=str(directory), prefix=".%s." % path.name, suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())  # 确保数据落盘后再替换

        try:
            os.replace(tmp_path, path)
        except (PermissionError, OSError) as e:
            logger.warning("原子替换失败（%s），退回直接写入: %s", type(e).__name__, e)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return path.stat().st_size

        return path.stat().st_size
    finally:
        # 成功替换后临时文件已不存在；失败路径需要清理残留
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:  # noqa: BLE001 - 清理失败不应影响主流程
            pass


class QQMailTools:
    """面向 MCP 工具层的高层封装（异步接口）。"""

    def __init__(self):
        self.sender = QQMailSender()

    @property
    def attachment_dir(self) -> Path:
        """
        附件目录，每次读取时从配置解析。

        刻意不做成构造时固化的实例属性：那样在构造之后再修改配置就不生效，
        测试里如果想重定向目录会非常容易踩坑（本项目早期就因此出现过
        测试写入真实 attachments/ 的隔离事故）。
        """
        return settings.attachment_dir

    async def send_text_email(
        self,
        to_email: Union[str, List[str]],
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """发送纯文本邮件。"""
        return await asyncio.to_thread(
            self.sender.send_email_sync,
            to_email=to_email,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            idempotency_key=idempotency_key,
        )

    async def send_html_email(
        self,
        to_email: Union[str, List[str]],
        subject: str,
        html_body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """发送 HTML 邮件（附带纯文本回退）。"""
        text_body = (
            "这是一封HTML邮件，如果您的邮件客户端不支持HTML，"
            "请使用支持HTML的客户端查看。"
        )
        return await asyncio.to_thread(
            self.sender.send_email_sync,
            to_email=to_email,
            subject=subject,
            body=text_body,
            html_body=html_body,
            cc=cc,
            bcc=bcc,
            idempotency_key=idempotency_key,
        )

    async def send_email_with_attachment(
        self,
        to_email: Union[str, List[str]],
        subject: str,
        body: str,
        attachment_paths: List[str],
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        is_html: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """发送带附件的邮件。"""
        html_body = body if is_html else None
        text_body = "请查看附件" if is_html else body
        return await asyncio.to_thread(
            self.sender.send_email_sync,
            to_email=to_email,
            subject=subject,
            body=text_body,
            html_body=html_body,
            attachments=attachment_paths,
            cc=cc,
            bcc=bcc,
            idempotency_key=idempotency_key,
        )

    async def check_email_config(self) -> Dict[str, Any]:
        """检查邮箱配置与连接状态。"""
        try:
            await asyncio.to_thread(self.sender.worker.send, lambda server: server.noop())
            attachments = list(self.attachment_dir.glob("*"))
            return _result(
                True, "邮箱配置有效，连接正常",
                to=self.sender.smtp_email,
                recipients=[],
                server=f"{self.sender.smtp_server}:{self.sender.smtp_port}",
                attachments_count=len(attachments),
            )
        except smtplib.SMTPResponseException as e:
            if e.smtp_code == -1 and e.smtp_error == b"\x00\x00\x00":
                return _result(
                    True, "邮箱连接成功（QQ邮箱特殊响应）",
                    to=self.sender.smtp_email, recipients=[],
                    note="QQ邮箱返回特殊响应，但连接正常",
                )
            return _result(
                False, f"SMTP连接失败: {e.smtp_code}",
                to=self.sender.smtp_email, recipients=[],
                reason="smtp_reject", error=str(e.smtp_error),
            )
        except Exception as e:  # noqa: BLE001
            return _result(
                False, f"配置检查失败: {e}",
                to=self.sender.smtp_email, recipients=[],
                reason="transport_error", error=str(e),
            )

    async def save_environment_config(
        self, config_data: Dict[str, Any], filename: str = "teacher_environment_config.json"
    ) -> Dict[str, Any]:
        """把环境配置保存为附件文件。"""
        try:
            if not config_data:
                config_data = {
                    "student": "示例用户",
                    "project": "QQ邮箱MCP工具",
                    "setup_time": _now(),
                    "status": "测试成功",
                }

            config_data["_metadata"] = {
                "created_at": _now(),
                "created_by": "QQ邮箱MCP工具",
                "purpose": "老师的环境配置参考",
            }

            file_path = self.attachment_dir / filename
            file_size = _write_json_atomically(file_path, config_data)

            logger.info("环境配置已保存 path=%s size=%d", file_path, file_size)
            return _result(
                True, "环境配置已保存为附件", recipients=[],
                file_path=str(file_path), filename=filename,
                file_size=file_size,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("保存环境配置失败: %s", e)
            return _result(False, f"保存失败: {e}", recipients=[], reason="write_error")


def get_send_metrics() -> Dict[str, Any]:
    """返回发送指标快照（供健康检查等端点使用）。"""
    return send_metrics.snapshot()


#: 全局实例
email_tools = QQMailTools()
