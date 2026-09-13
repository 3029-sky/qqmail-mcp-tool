# tests/test_retry.py - 重试策略与错误分类
"""
重点是分类正确性：可重试的只是瞬时故障，永久错误绝不能重试。
分类错误会导致两个方向的严重后果——重试必然失败的操作（浪费时间与配额），
或放弃本可成功的操作。
"""

import smtplib
import socket

import pytest

from retry import RetryPolicy, is_retryable, retry_call


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", [421, 450, 451, 452])
def test_transient_smtp_codes_are_retryable(code):
    """4xx 是瞬时故障（服务不可用、限流、稍后重试）。"""
    assert is_retryable(smtplib.SMTPResponseException(code, b"try later")) is True


@pytest.mark.parametrize("code", [500, 550, 553, 554])
def test_permanent_smtp_codes_are_not_retryable(code):
    """5xx 是永久错误，重试多少次结果都一样。"""
    assert is_retryable(smtplib.SMTPResponseException(code, b"rejected")) is False


def test_auth_error_is_never_retryable():
    """
    认证失败不可重试。

    注意 SMTPAuthenticationError 是 SMTPResponseException 的子类，
    且其 535 不属于 4xx 集合；判定顺序必须让永久错误优先。
    """
    exc = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    assert isinstance(exc, smtplib.SMTPResponseException)
    assert is_retryable(exc) is False


def test_authentication_error_classified_permanent_even_with_4xx_code():
    """即便认证错误带了 4xx 码，也必须按永久错误处理。"""
    exc = smtplib.SMTPAuthenticationError(454, b"temporary auth failure")
    assert is_retryable(exc) is False


@pytest.mark.parametrize(
    "exc",
    [
        smtplib.SMTPServerDisconnected("gone"),
        smtplib.SMTPConnectError(421, b"cannot connect"),
        smtplib.SMTPHeloError(421, b"bad helo"),
        socket.timeout("timed out"),
        TimeoutError("timed out"),
        ConnectionError("reset"),
    ],
)
def test_connection_errors_are_retryable(exc):
    assert is_retryable(exc) is True


def test_unknown_exception_is_not_retryable():
    """无法归类的异常默认不重试，避免把编程错误当成网络抖动。"""
    assert is_retryable(ValueError("bug")) is False
    assert is_retryable(RuntimeError("bug")) is False


def test_not_supported_error_is_not_retryable():
    assert is_retryable(smtplib.SMTPNotSupportedError("nope")) is False


# ---------------------------------------------------------------------------
# 退避策略
# ---------------------------------------------------------------------------

def test_delay_grows_with_backoff():
    policy = RetryPolicy(initial_delay=1.0, backoff=2.0, max_delay=100.0)
    assert policy.delay_for(1) == 1.0
    assert policy.delay_for(2) == 2.0
    assert policy.delay_for(3) == 4.0
    assert policy.delay_for(4) == 8.0


def test_delay_is_capped():
    policy = RetryPolicy(initial_delay=1.0, backoff=10.0, max_delay=5.0)
    assert policy.delay_for(3) == 5.0, "退避时间必须有上限"


def test_zero_backoff_keeps_delay_constant():
    policy = RetryPolicy(initial_delay=0.5, backoff=1.0)
    assert policy.delay_for(1) == 0.5
    assert policy.delay_for(5) == 0.5


# ---------------------------------------------------------------------------
# 重试执行
# ---------------------------------------------------------------------------

def test_success_on_first_try_does_not_retry():
    calls = []

    def op():
        calls.append(1)
        return "ok"

    assert retry_call(op, RetryPolicy(max_attempts=3), sleep=lambda _: None) == "ok"
    assert len(calls) == 1


def test_transient_error_is_retried_then_succeeds():
    calls = []

    def op():
        calls.append(1)
        if len(calls) < 3:
            raise smtplib.SMTPResponseException(451, b"later")
        return "ok"

    result = retry_call(op, RetryPolicy(max_attempts=5), sleep=lambda _: None)
    assert result == "ok"
    assert len(calls) == 3


def test_permanent_error_is_not_retried():
    calls = []

    def op():
        calls.append(1)
        raise smtplib.SMTPResponseException(550, b"no such user")

    with pytest.raises(smtplib.SMTPResponseException):
        retry_call(op, RetryPolicy(max_attempts=3), sleep=lambda _: None)

    assert len(calls) == 1, "永久错误只能尝试一次"


def test_retries_are_exhausted_and_last_error_propagates():
    calls = []

    def op():
        calls.append(1)
        raise smtplib.SMTPServerDisconnected("always down")

    with pytest.raises(smtplib.SMTPServerDisconnected):
        retry_call(op, RetryPolicy(max_attempts=3), sleep=lambda _: None)

    assert len(calls) == 3


def test_max_attempts_one_disables_retry():
    calls = []

    def op():
        calls.append(1)
        raise smtplib.SMTPServerDisconnected("down")

    with pytest.raises(smtplib.SMTPServerDisconnected):
        retry_call(op, RetryPolicy(max_attempts=1), sleep=lambda _: None)

    assert len(calls) == 1


def test_sleep_is_called_with_backoff_delays():
    slept = []

    def op():
        raise smtplib.SMTPServerDisconnected("down")

    with pytest.raises(smtplib.SMTPServerDisconnected):
        retry_call(
            op,
            RetryPolicy(max_attempts=3, initial_delay=1.0, backoff=2.0),
            sleep=slept.append,
        )

    assert slept == [1.0, 2.0], "两次失败之间应各等待一次，末次失败后不再等待"


def test_on_retry_callback_reports_attempt_and_delay():
    seen = []

    def op():
        raise smtplib.SMTPServerDisconnected("down")

    with pytest.raises(smtplib.SMTPServerDisconnected):
        retry_call(
            op,
            RetryPolicy(max_attempts=2, initial_delay=3.0),
            on_retry=lambda attempt, exc, delay: seen.append((attempt, delay)),
            sleep=lambda _: None,
        )

    assert seen == [(1, 3.0)]


def test_default_sleep_is_resolved_at_call_time(monkeypatch):
    """
    回归测试：sleep 的默认值必须在调用时解析。

    若写成 `sleep: Callable = time.sleep`，默认参数会在函数定义时绑定，
    导致 monkeypatch retry.time.sleep 失效——测试会真的睡满退避时间。
    """
    slept = []
    monkeypatch.setattr("retry.time.sleep", lambda s: slept.append(s))

    def op():
        raise smtplib.SMTPServerDisconnected("down")

    with pytest.raises(smtplib.SMTPServerDisconnected):
        retry_call(op, RetryPolicy(max_attempts=2, initial_delay=0.25))

    assert slept == [0.25], "必须走被 patch 过的 sleep"
