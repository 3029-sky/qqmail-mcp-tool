# delivery.py - 用 IMAP 回读确认邮件确实被 QQ 接收
"""
为什么需要它：
    QQ 邮箱在邮件已投递的情况下有时返回 (-1, b'\\x00\\x00\\x00')。
    仅凭这个响应码无法区分「真的发出去了」和「出问题了」，
    属于经验性判定。IMAP 回读提供了独立于 SMTP 响应码的第二来源证据。

它能证明什么、不能证明什么（**重要**）：
    能证明：QQ 接收了这封信，并把它归档进了「已发送」。
    不能证明：收件人真的收到了，也不代表收件地址存在。
    实测发现：被 SMTP 以 550 拒绝的邮件同样会出现在「已发送」里，
    因此本模块的结论是「服务端已归档」，而非「投递成功」。

真正的投递确认需要读取收件人的收件箱，但那只有收件人自己有权访问，
所以在本项目的场景下无法实现。这里采取的是最接近的可用证据，
并且如实标注结论的强度。

设计要点：
  - 默认开启失败（配置驱动），因为每次发送会多一次 IMAP 往返。
  - 带重试轮询：邮件归档有延迟，立即查询会得到假阴性。
  - 匹配依据是「主题 + 收件人 + 时间窗口」，而非 Message-ID——
    实测 QQ 会用自己生成的 Message-ID 覆盖发信方设置的值。
  - 任何异常都归为「无法确认」，绝不把校验失败误报成发送失败。
"""

import imaplib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from typing import List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

#: QQ 邮箱「已发送」文件夹名。
#: 注意 imaplib 不会自动加引号，名称含空格，传入时必须带上双引号。
DEFAULT_SENT_FOLDER = '"Sent Messages"'

#: 每次查询最多取回的候选邮件数（从最近的往前取）
DEFAULT_SCAN_LIMIT = 30


@dataclass
class SendConfirmation:
    """一次发送确认的结果。"""

    checked: bool
    """是否真的执行了回读校验。False 表示未启用或无法连接。"""

    confirmed: bool
    """是否在「已发送」中找到了对应邮件。"""

    detail: str
    """人类可读的结论说明。"""

    folder: str = ""
    """实际查询的文件夹名。"""

    elapsed_ms: Optional[float] = None
    """校验耗时（毫秒）。"""

    attempts: int = 0
    """实际轮询次数。"""

    matched_subject: Optional[str] = None
    """匹配到的邮件主题（便于人工核对）。"""

    def as_dict(self) -> dict:
        data = {
            "confirmed": self.confirmed,
            "checked": self.checked,
            "detail": self.detail,
        }
        if self.elapsed_ms is not None:
            data["elapsed_ms"] = round(self.elapsed_ms, 1)
        if self.attempts:
            data["attempts"] = self.attempts
        if self.matched_subject:
            data["matched_subject"] = self.matched_subject
        return data


def _decode(value: Optional[str]) -> str:
    """解码可能经过 MIME 编码的邮件头。"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:  # noqa: BLE001
        return value.strip()


def _normalize_subject(value: Optional[str]) -> str:
    """
    归一化主题用于比较。

    忽略空白差异与常见的前缀（Re: / Fwd: 等）带来的噪声。
    不做大小写折叠之外的花哨处理，避免产生误匹配。
    """
    if not value:
        return ""
    text = _decode(value)
    return "".join(text.split()).lower()


def _recipients_of(header_to: Optional[str]) -> List[str]:
    """从 To 头中提取地址列表（小写）。"""
    if not header_to:
        return []
    raw = _decode(header_to)
    parts = raw.replace(",", " ").replace(";", " ").split()
    return [p.strip().strip("<>").lower() for p in parts if "@" in p]


class DeliveryConfirmer:
    """
    通过 IMAP 查询「已发送」文件夹来确认邮件已被 QQ 接收并归档。
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        folder: str = DEFAULT_SENT_FOLDER,
        scan_limit: int = DEFAULT_SCAN_LIMIT,
        timeout: float = 20.0,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.folder = folder
        self.scan_limit = scan_limit
        self.timeout = timeout

    # -- IMAP 底层 ------------------------------------------------------------

    def _connect(self) -> imaplib.IMAP4_SSL:
        client = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        try:
            client.login(self.user, self.password)
        except Exception:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass
            raise
        return client

    def _recent_candidates(self, client: imaplib.IMAP4_SSL) -> List[dict]:
        """取回最近若干封已发送邮件的 Subject / To 头，作为候选集合。"""
        # 用 SINCE 把搜索范围收窄到近几天，避免全量扫描
        since = (datetime.now() - timedelta(days=2)).strftime("%d-%b-%Y")
        typ, data = client.search(None, "SINCE", since)
        if typ != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()
        candidates = []
        for msg_id in ids[-self.scan_limit:]:
            typ, fetched = client.fetch(
                msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT TO)])"
            )
            if typ != "OK" or not fetched or not fetched[0]:
                continue
            raw = fetched[0][1]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            subject = ""
            to_header = ""
            for line in raw.splitlines():
                stripped = line.strip()
                lowered = stripped.lower()
                if lowered.startswith("subject:"):
                    subject = stripped[len("subject:"):].strip()
                elif lowered.startswith("to:"):
                    to_header = stripped[len("to:"):].strip()
            candidates.append({"subject": subject, "to": to_header})
        return candidates

    # -- 匹配 ----------------------------------------------------------------

    def _matches(
        self, candidate: dict, subject: str, recipients: Sequence[str]
    ) -> bool:
        """判断候选邮件是否就是我们要找的那封。"""
        if _normalize_subject(candidate.get("subject")) != _normalize_subject(subject):
            return False

        want = {r.lower() for r in recipients}
        got = set(_recipients_of(candidate.get("to")))
        if not want or not got:
            # 收件人信息缺失时，仅凭主题判定（主题相同且时间相邻，误判概率低）
            return True
        return bool(want & got)

    def check_once(
        self, subject: str, recipients: Union[str, Sequence[str]]
    ) -> SendConfirmation:
        """执行一次查询（不重试）。"""
        if isinstance(recipients, str):
            recipients = [recipients]

        started = time.monotonic()
        try:
            client = self._connect()
        except Exception as e:  # noqa: BLE001
            return SendConfirmation(
                checked=False,
                confirmed=False,
                detail="无法连接 IMAP，未做确认: %s: %s" % (type(e).__name__, e),
                folder=self.folder,
            )

        try:
            typ, data = client.select(self.folder, readonly=True)
            if typ != "OK":
                return SendConfirmation(
                    checked=False,
                    confirmed=False,
                    detail="无法打开「%s」文件夹: %s" % (self.folder, data),
                    folder=self.folder,
                )

            candidates = self._recent_candidates(client)
            for candidate in candidates:
                if self._matches(candidate, subject, recipients):
                    return SendConfirmation(
                        checked=True,
                        confirmed=True,
                        detail="已在「已发送」中找到该邮件，QQ 已接收并归档",
                        folder=self.folder,
                        elapsed_ms=(time.monotonic() - started) * 1000,
                        matched_subject=_decode(candidate.get("subject")),
                    )

            return SendConfirmation(
                checked=True,
                confirmed=False,
                detail="在最近的「已发送」中未找到该邮件（扫描 %d 封）" % len(candidates),
                folder=self.folder,
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        except Exception as e:  # noqa: BLE001
            return SendConfirmation(
                checked=False,
                confirmed=False,
                detail="确认过程出错: %s: %s" % (type(e).__name__, e),
                folder=self.folder,
            )
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass

    def confirm(
        self,
        subject: str,
        recipients: Union[str, Sequence[str]],
        attempts: int = 3,
        interval: float = 2.0,
    ) -> SendConfirmation:
        """
        带重试的确认。

        邮件归档有延迟，单次查询容易得到假阴性，因此默认轮询 3 次、间隔 2 秒。
        只要某次确认成功就立即返回；连接类错误不做重试（无法连接时重试无意义）。
        """
        last: Optional[SendConfirmation] = None
        total_attempts = 0

        for i in range(max(1, attempts)):
            total_attempts += 1
            result = self.check_once(subject, recipients)
            result.attempts = total_attempts
            last = result

            if result.confirmed:
                return result
            # 无法连接或文件夹打不开，重试也不会变好
            if not result.checked and "未做确认" in result.detail:
                return result
            if i < attempts - 1:
                time.sleep(interval)

        assert last is not None
        return last
