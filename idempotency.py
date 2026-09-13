# idempotency.py - 幂等键
"""
为什么需要幂等键：
    SMTP 是 at-least-once 语义。网络抖动或超时会让我们无法判断
    「服务器到底有没有收到这封信」；此时盲目重试就会重复发信。
    调用方可以带一个幂等键（例如 MCP 客户端生成的 request-id），
    同一键在有效期内只会真正发送一次，重复请求返回首次的结果。

安全性质（由测试固化）：
    - 同一键的第二次请求**不会**再发出邮件。
    - 首次请求仍在进行中时，重复请求立即返回「处理中」，不会重复发送。
    - 首次失败（可重试类）会释放键，允许调用方再次尝试。

局限：
    这是**单进程内存**实现。进程重启后键失效；多副本部署时各副本互不可见。
    要跨进程/跨重启生效，需要换成 Redis 之类的共享存储。
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: 默认的键保留时长（秒）
DEFAULT_TTL = 3600.0

#: 认为「处理中」已经卡死的时长（秒），超过后允许重新占用
DEFAULT_INFLIGHT_TIMEOUT = 300.0


#: 与工具结果结构保持一致的响应字典
@dataclass
class Entry:
    """一条幂等记录。"""

    status: str                      # "in_flight" 或 "done"
    created_at: float
    owner: str = ""
    response: Optional[Dict[str, Any]] = None
    completed_at: float = 0.0


class IdempotencyStore:
    """线程安全的内存幂等键存储。"""

    def __init__(
        self,
        ttl: float = DEFAULT_TTL,
        inflight_timeout: float = DEFAULT_INFLIGHT_TIMEOUT,
        clock=time.monotonic,
    ):
        self.ttl = ttl
        self.inflight_timeout = inflight_timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: Dict[str, Entry] = {}

    # -- 内部 ---------------------------------------------------------------

    def _purge_expired(self, now: float) -> None:
        """清理过期条目。调用方必须已持锁。"""
        expired = []
        for key, entry in self._entries.items():
            age = now - (entry.completed_at or entry.created_at)
            if entry.status == "in_flight":
                # 卡死的「处理中」视为失效，允许后续请求重新占用
                if age > self.inflight_timeout:
                    expired.append(key)
            elif age > self.ttl:
                expired.append(key)
        for key in expired:
            self._entries.pop(key, None)

    # -- 公开接口 -----------------------------------------------------------

    def lookup(self, key: str) -> Optional[Dict[str, Any]]:
        """
        查询键的既有结果。

        返回 None 表示可以继续执行（键不存在或已过期）。
        返回字典表示**不应再次发送**，直接把这个结果返回给调用方即可。
        """
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            entry = self._entries.get(key)
            if entry is None:
                return None

            if entry.status == "in_flight":
                return {
                    "success": True,
                    "duplicate": True,
                    "status": "in_progress",
                    "message": "相同请求正在处理中，未重复发送",
                    "idempotency_key": key,
                }

            response = dict(entry.response or {})
            response["duplicate"] = True
            response["message"] = (
                "%s（重复请求，未再次发送）"
                % (entry.response or {}).get("message", "已处理")
            )
            return response

    def reserve(self, key: str) -> str:
        """
        占用键并返回 owner 标识。

        调用方在发送前必须成功 reserve；发送完成后必须 complete 或 release。
        """
        owner = uuid.uuid4().hex
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            self._entries[key] = Entry(
                status="in_flight", created_at=now, owner=owner
            )
        return owner

    def complete(self, key: str, owner: str, response: Dict[str, Any]) -> bool:
        """
        记录最终结果。

        只有 owner 匹配时才写入，避免并发场景下把别人的结果覆盖掉。
        """
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.owner != owner:
                return False
            entry.status = "done"
            entry.response = dict(response)
            entry.completed_at = now
        return True

    def release(self, key: str, owner: str) -> bool:
        """
        释放键，使其可以被再次使用。

        仅用于「确定没有发送成功」的情况（例如连接都没建立起来），
        以便调用方稍后用同一个键重试。
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.owner != owner:
                return False
            self._entries.pop(key, None)
        return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


#: 全局实例
idempotency_store = IdempotencyStore()


def generate_key() -> str:
    """生成一个新的幂等键（供调用方在未提供时使用）。"""
    return uuid.uuid4().hex
