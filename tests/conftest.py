# tests/conftest.py - 共享夹具
"""
所有测试都必须在「离线」条件下运行：
  - 不连接真实 SMTP 服务器
  - 不发送真实邮件
  - 不依赖 Ollama

为此这里提供两类替身：
  1. FakeSMTPServer  —— 替换 smtplib.SMTP_SSL，模拟 QQ 邮箱的各种响应
  2. FakeEmailTools  —— 替换真实 email_tools，用于测试分发逻辑
"""

import smtplib
import sys
from pathlib import Path

import pytest

# 让 tests/ 下的用例能 import 项目根目录模块
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# SMTP 替身
# ---------------------------------------------------------------------------

class FakeSMTPServer:
    """
    记录所有 SMTP 交互，并可模拟特定响应。

    这是「单例」替身：整个用例内只存在一个实例对象，
    每次 smtplib.SMTP_SSL(...) 调用都返回它，因此可以直接断言
    connect_count 来判断连接是否被复用（复用生效时 connect_count == 1）。
    """

    _instance = None
    last_instance = None

    #: 类级行为开关与计数器（每个用例通过 fresh() 重置）
    login_exc = None
    send_message_exc = None
    noop_exc = None
    connect_count = 0

    def __init__(self, host=None, port=None, context=None, timeout=None, **kwargs):
        self.host = host
        self.port = port
        self.timeout = timeout

        # 计数器从类属性继承：__new__ 返回单例，因此 __init__ 每次连接都会被调用一次
        self.connect_count = type(self).connect_count + 1
        type(self).connect_count = self.connect_count

        # 仅在「本用例的首次连接」时初始化累计状态。
        # 若连接被重建（连接池主动丢弃后重连），这些累计量必须保留，
        # 否则断言 sent_messages 总数时会只看到重建之后的部分。
        if self.connect_count == 1:
            self.login_count = 0
            self.noop_called = False
            self.logged_in = None
            self.sent_messages = []

        # 行为开关每次连接都从类属性同步，便于用例中途改变行为
        self.login_exc = type(self).login_exc
        self.send_message_exc = type(self).send_message_exc
        self.noop_exc = type(self).noop_exc

    @classmethod
    def fresh(cls):
        """
        重置全部状态，开始一个新的用例。

        必须同时重置计数器：__new__ 返回单例，若只清空 _instance 引用，
        计数会累积到下一个用例，导致断言出现难以复现的偶发失败。
        """
        cls.login_exc = None
        cls.send_message_exc = None
        cls.noop_exc = None
        cls.connect_count = 0
        cls._instance = None
        cls.last_instance = None

    #: 行为开关的合法名称
    BEHAVIORS = ("login_exc", "send_message_exc", "noop_exc")

    @classmethod
    def set_behavior(cls, name: str, exc) -> None:
        """
        设置某个行为开关。

        必须经此方法而不是直接赋值给类：单例的 __init__ 会把类属性拷贝成同名
        实例属性，而实例属性优先级更高，直接改类属性对已存在的实例无效。
        """
        if name not in cls.BEHAVIORS:
            raise ValueError("未知行为开关: %s" % name)
        setattr(cls, name, exc)
        if cls._instance is not None:
            setattr(cls._instance, name, exc)

    @classmethod
    def reset_behaviors(cls) -> None:
        for name in cls.BEHAVIORS:
            cls.set_behavior(name, None)

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls.last_instance = cls._instance
        return cls._instance

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, user, password):
        self.login_count += 1
        if self.login_exc is not None:
            raise self.login_exc
        self.logged_in = (user, password)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        if self.send_message_exc is not None:
            raise self.send_message_exc
        self.sent_messages.append((from_addr, to_addrs, msg.as_string()))

    def sendmail(self, from_addr, to_addrs, msg):
        if self.send_message_exc is not None:
            raise self.send_message_exc
        self.sent_messages.append((from_addr, to_addrs, msg))

    def noop(self):
        if self.noop_exc is not None:
            raise self.noop_exc
        self.noop_called = True

    def quit(self):
        pass

    def close(self):
        pass


#: QQ 邮箱返回的非标准响应：错误码 -1，错误内容 b'\x00\x00\x00'
QQ_SPECIAL_RESPONSE = smtplib.SMTPResponseException(-1, b"\x00\x00\x00")


@pytest.fixture
def fake_smtp(monkeypatch):
    """
    把 smtplib.SMTP_SSL 替换成 FakeSMTPServer。

    注意：smtp_pool 与 email_tools 引用的是同一个 smtplib 模块对象，
    因此这里的一次 patch 对两者都生效。
    """
    FakeSMTPServer.fresh()
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPServer)
    return FakeSMTPServer


@pytest.fixture(autouse=True)
def reset_metrics():
    """每个用例前清空指标，避免用例之间互相污染。"""
    from metrics import send_metrics

    send_metrics.reset()
    yield


@pytest.fixture
def live_sender(fake_smtp):
    """
    提供一个会自动清理的 QQMailSender。

    连接池会启动后台线程，用例结束后必须显式关闭，否则线程泄漏到其他用例。
    """
    from email_tools import QQMailSender

    sender = QQMailSender()
    yield sender
    sender.close()


# ---------------------------------------------------------------------------
# email_tools 替身
# ---------------------------------------------------------------------------

def _ok(to=None):
    """构造一个成功结果。"""
    return {"success": True, "message": "ok", "to": to}


class FakeEmailTools:
    """
    记录调用参数的 email_tools 替身。

    通过 result_for 指定某个工具返回什么，用于测试分发与格式化逻辑。
    方法签名接受 **kwargs，因此新增工具参数（如 idempotency_key）不会让替身失效。
    """

    def __init__(self, result_for=None):
        self.calls = []
        self.result_for = result_for or {}

    async def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))
        result = self.result_for.get(name)
        if isinstance(result, Exception):
            raise result
        if result is not None:
            # 可调用对象用于模拟「耗时过长」等动态行为
            if callable(result):
                return await result(**kwargs)
            return result
        return _ok(kwargs.get("to_email"))

    async def send_text_email(self, **kwargs):
        return await self._record("send_text_email", **kwargs)

    async def send_html_email(self, **kwargs):
        return await self._record("send_html_email", **kwargs)

    async def send_email_with_attachment(self, **kwargs):
        return await self._record("send_email_with_attachment", **kwargs)

    async def check_email_config(self, **kwargs):
        return await self._record("check_email_config", **kwargs)


@pytest.fixture
def fake_email_tools():
    return FakeEmailTools()
