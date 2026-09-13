# smtp_pool.py - SMTP 连接复用
"""
为什么需要这个模块：
    smtplib.SMTP_SSL 不是线程安全的，连接不能跨线程共享。
    而 email_tools 通过 asyncio.to_thread 发送，任务可能落在任意线程上，
    因此「线程本地连接」也无法保证复用。

方案：
    用一个专用工作线程独占一条 SMTP 连接，所有发送请求经队列排队处理。
      - 连接在多次发送之间保持复用，TLS 握手与登录只做一次
      - 天然串行化，彻底避免并发使用同一连接
      - 出队时若空闲超过 idle_ttl 秒，主动关闭重建，避免用到被服务端回收的连接

代价：
    发送变为串行。对单个 SMTP 账号而言这是可接受的——
    QQ 邮箱本身对发信频率有限制，并发发送只会更快触发限流。
"""

import logging
import queue
import smtplib
import ssl
import threading
import time
from typing import Any, Callable, Dict, Optional

from metrics import send_metrics

logger = logging.getLogger(__name__)

#: 连接空闲超过该秒数后，下次使用前重建
DEFAULT_IDLE_TTL = 60.0

#: 单次发送（含连接与登录）的超时
DEFAULT_TIMEOUT = 30.0

#: 排队等待连接的秒数
DEFAULT_ACQUIRE_TIMEOUT = 10.0


class SMTPConnectionPool:
    """
    拥有单条复用的 SMTP 连接，并在专用线程中串行执行发送任务。

    用法：
        pool = SMTPConnectionPool(host, port, user, password)
        result = pool.send(lambda server: server.send_message(msg))
        pool.close()
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        timeout: float = DEFAULT_TIMEOUT,
        idle_ttl: float = DEFAULT_IDLE_TTL,
        acquire_timeout: float = DEFAULT_ACQUIRE_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.timeout = timeout
        self.idle_ttl = idle_ttl
        self.acquire_timeout = acquire_timeout

        self._server: Optional[smtplib.SMTP_SSL] = None
        self._last_used: float = 0.0
        self._lock = threading.Lock()

    # -- 连接管理（仅在工作线程中调用）---------------------------------------

    def _connect(self) -> smtplib.SMTP_SSL:
        """建立新连接并登录，同时记录连接次数指标。"""
        context = ssl.create_default_context()
        server = smtplib.SMTP_SSL(
            self.host, self.port, context=context, timeout=self.timeout
        )
        server.login(self.user, self.password)
        send_metrics.incr("smtp_connects")
        logger.info(
            "SMTP 连接已建立 host=%s port=%s reused=false", self.host, self.port
        )
        return server

    def _is_stale(self) -> bool:
        """
        连接是否因空闲过久而不宜继续使用。

        注意：_last_used 为 0.0（尚未使用过）时不视为过期，
        否则刚建立的连接会在下一次检查时被误判为「空闲了极长时间」而丢弃。
        """
        if self._server is None:
            return True
        if self._last_used == 0.0:
            return False
        return (time.monotonic() - self._last_used) > self.idle_ttl

    def _drop(self) -> None:
        """丢弃当前连接（尽力关闭，忽略关闭过程中的错误）。"""
        if self._server is not None:
            try:
                self._server.quit()
            except Exception:  # noqa: BLE001
                try:
                    self._server.close()
                except Exception:  # noqa: BLE001
                    pass
            self._server = None

    def _acquire(self) -> smtplib.SMTP_SSL:
        if self._is_stale():
            self._drop()
        if self._server is None:
            self._server = self._connect()
            # 记录建立时刻：既作为空闲计时的起点，也避免被误判为过期
            self._last_used = time.monotonic()
        return self._server

    # -- 发送 ---------------------------------------------------------------

    def send(
        self,
        action: Callable[[smtplib.SMTP_SSL], Any],
        retry_on_disconnect: bool = False,
    ) -> Dict[str, Any]:
        """
        在复用的连接上执行 action(server)。

        action 抛出的异常会原样向上抛出，由调用方决定如何处理。

        连接失效的判定刻意保守：只有「传输层断裂」才丢弃连接。
        SMTPResponseException 属于服务端对本次投递的拒绝（例如 550 收件人不存在、
        451 稍后重试），连接本身仍然可用，不应因此重连。

        注意：不能在这里 catch OSError。smtplib 的 SMTPException 继承自 OSError，
        捕获它会把所有 SMTP 协议错误都误判为连接断裂，导致每封信都重连。

        retry_on_disconnect 控制「连接在操作期间断裂」时是否换新连接重试一次。
        调用方必须如实声明 action 是否可被安全地重复执行——对发信这类非幂等操作，
        只有在能够确定服务器尚未接收该邮件时才可置为 True，否则会造成重复发信。
        """
        with self._lock:
            server = self._acquire()
            try:
                result = action(server)
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError):
                # 连接已不可用：丢弃后下次自动重建
                logger.warning("SMTP 连接失效，已丢弃以便重建")
                self._drop()

                if not retry_on_disconnect:
                    raise

                logger.info("换用新连接重试一次（调用方声明该操作可安全重试）")
                send_metrics.incr("smtp_reconnects")
                fresh = self._acquire()
                try:
                    result = action(fresh)
                except BaseException:
                    self._drop()
                    raise
                self._last_used = time.monotonic()
                return {"result": result, "reused": False, "reconnected": True}

            self._last_used = time.monotonic()
            return {"result": result, "reused": True}

    def close(self) -> None:
        with self._lock:
            self._drop()

    # -- 线程封装 -----------------------------------------------------------

    def start_worker(self) -> "SMTPWorker":
        """创建一个在专用线程中串行处理发送请求的工作器。"""
        return SMTPWorker(self)


class SMTPWorker:
    """
    在专用线程中独占使用 SMTPConnectionPool 的工作器。

    所有发送请求经队列串行处理，因此连接始终只被一个线程使用。
    """

    def __init__(self, pool: SMTPConnectionPool):
        self.pool = pool
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="smtp-worker", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:  # 收到关闭信号
                self.pool.close()
                self._queue.task_done()
                return

            action, result_box, done, retry_on_disconnect = item
            try:
                result_box.append(
                    ("ok", self.pool.send(action, retry_on_disconnect))
                )
            except BaseException as exc:  # noqa: BLE001 - 原样转交给调用线程
                result_box.append(("error", exc))
            finally:
                done.set()
                self._queue.task_done()

    def send(
        self,
        action: Callable[[smtplib.SMTP_SSL], Any],
        retry_on_disconnect: bool = False,
    ) -> Dict[str, Any]:
        """
        把一次发送投递到工作线程并等待结果。

        注意：这里会阻塞调用线程。email_tools 通过 asyncio.to_thread 调用，
        因此不会阻塞事件循环。

        retry_on_disconnect 会一并转交给连接池，语义见 SMTPConnectionPool.send。
        """
        result_box: list = []
        done = threading.Event()
        self._queue.put((action, result_box, done, retry_on_disconnect))

        if not done.wait(timeout=self.pool.acquire_timeout):
            send_metrics.incr("worker_timeouts")
            raise TimeoutError("等待 SMTP 工作线程超时")

        kind, payload = result_box[0]
        if kind == "error":
            raise payload
        return payload

    def close(self, wait: bool = True) -> None:
        """请求工作线程关闭连接并退出。"""
        self._queue.put(None)
        if wait:
            self._thread.join(timeout=5)
