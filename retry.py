# retry.py - 可控重试
"""
SMTP 失败分为两类，处理方式完全不同：

  可重试（瞬时）：
    - 4xx 响应，例如 421 服务不可用、450/451/452 稍后重试
    - 连接/握手/超时类错误（SMTPServerDisconnected、socket.timeout 等）

  不可重试（永久）：
    - 5xx 响应，例如 550 收件人不存在、535 认证失败
    - 重试多少次都是同样的结果

重试**不保证幂等**，因此调用方必须额外保证：只有在能够确定
「服务器没有接收这封信」的情况下才允许重试。classification 只回答
「这个错误是否瞬时」，是否安全重试由调用方结合发送阶段判断。
"""

import logging
import smtplib
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class RetryPolicy:
    """重试策略。"""

    max_attempts: int = 3
    """总尝试次数（含首次）。1 表示不重试。"""

    initial_delay: float = 1.0
    """首次重试前的等待秒数。"""

    backoff: float = 2.0
    """退避倍数：每次等待时间乘以此值。"""

    max_delay: float = 8.0
    """单次等待的上限，防止退避时间失控。"""

    def delay_for(self, attempt: int) -> float:
        """
        第 attempt 次失败后应等待多久。

        attempt 从 1 开始（表示第 1 次尝试刚失败）。
        """
        delay = self.initial_delay * (self.backoff ** max(0, attempt - 1))
        return min(delay, self.max_delay)


#: 明确可重试的 SMTP 状态码（4xx 为瞬时故障）
TRANSIENT_SMTP_CODES = (421, 450, 451, 452)

#: 明确不可重试的连接类错误
PERMANENT_EXCEPTIONS = (
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPNotSupportedError,
)

#: 可重试的连接类错误
#: 注意 SMTPAuthenticationError 是 SMTPResponseException 的子类，
#: 因此必须先判断永久错误，顺序不能反。
TRANSIENT_EXCEPTIONS = (
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
)


def is_retryable(exc: BaseException) -> bool:
    """
    判断异常是否属于可重试的瞬时故障。

    判定顺序：永久错误 -> 4xx 响应 -> 连接类瞬时错误 -> 其他一律不重试。
    """
    if isinstance(exc, PERMANENT_EXCEPTIONS):
        return False

    if isinstance(exc, smtplib.SMTPResponseException):
        return exc.smtp_code in TRANSIENT_SMTP_CODES

    return isinstance(exc, TRANSIENT_EXCEPTIONS)


def retry_call(
    operation: Callable[[], Any],
    policy: RetryPolicy,
    description: str = "operation",
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> Any:
    """
    执行 operation，遇到可重试错误时按策略重试。

    不可重试的错误立即抛出。重试次数耗尽后抛出最后一次的异常。
    on_retry(attempt, exc, delay) 用于记录指标或日志。

    注意：本函数不判断重试是否安全（幂等性）。调用方必须确保
    operation 在"可能已经生效"的情况下不会被重复执行。

    sleep 默认在调用时解析 time.sleep，而不是作为默认参数求值——
    否则 monkeypatch retry.time.sleep 无法生效（默认参数在定义时就已绑定）。
    """
    if sleep is None:
        sleep = time.sleep

    attempts = max(1, policy.max_attempts)
    last_exc: Optional[BaseException] = None

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except BaseException as exc:  # noqa: BLE001 - 需要按类型分类
            last_exc = exc
            retryable = is_retryable(exc)

            if not retryable or attempt >= attempts:
                raise

            delay = policy.delay_for(attempt)
            logger.warning(
                "%s 第 %d/%d 次失败（%s），%.1fs 后重试",
                description, attempt, attempts, type(exc).__name__, delay,
            )
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)

    # 逻辑上不可达：上面的循环要么 return，要么 raise
    assert last_exc is not None
    raise last_exc
