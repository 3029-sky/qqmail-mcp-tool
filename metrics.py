# metrics.py - 轻量级发送指标
"""
不引入 prometheus_client 等外部依赖，用一个带锁的计数器即可满足单进程服务的需求。

统计口径：
  - sends_total       发起的发送次数
  - sends_succeeded   判定为成功的次数（含 QQ 邮箱特殊响应）
  - sends_failed      判定为失败的次数
  - latency_ms        每次发送耗时（毫秒），只保留最近 N 条以限制内存
  - smtp_connects     实际建立的 SMTP 连接数（用于验证连接复用是否生效）

连接复用是否生效，直接看 smtp_connects 是否远小于 sends_total。
"""

import threading
from collections import deque
from typing import Any, Dict, List, Optional

#: 延迟样本保留条数，避免长时间运行导致内存无限增长
MAX_LATENCY_SAMPLES = 1000


class SendMetrics:
    """线程安全的发送指标收集器。"""

    def __init__(self, max_samples: int = MAX_LATENCY_SAMPLES):
        self._lock = threading.Lock()
        self._max_samples = max_samples
        self._counters: Dict[str, int] = {}
        self._latencies: deque = deque(maxlen=max_samples)
        self._failures_by_reason: Dict[str, int] = {}

    # -- 写入 ---------------------------------------------------------------

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def observe_latency(self, ms: float) -> None:
        with self._lock:
            self._latencies.append(ms)

    def record_failure(self, reason: str) -> None:
        with self._lock:
            self._failures_by_reason[reason] = (
                self._failures_by_reason.get(reason, 0) + 1
            )

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._latencies.clear()
            self._failures_by_reason.clear()

    # -- 读取 ---------------------------------------------------------------

    def counter(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    def _percentile(self, sorted_values: List[float], pct: float) -> Optional[float]:
        if not sorted_values:
            return None
        # 最近邻取值，样本量足够时与插值法差异可忽略
        index = int(round((pct / 100.0) * (len(sorted_values) - 1)))
        return sorted_values[index]

    def snapshot(self) -> Dict[str, Any]:
        """返回当前指标快照，可直接序列化为 JSON。"""
        with self._lock:
            counters = dict(self._counters)
            latencies = sorted(self._latencies)
            failures = dict(self._failures_by_reason)

        total = counters.get("sends_total", 0)
        succeeded = counters.get("sends_succeeded", 0)
        connects = counters.get("smtp_connects", 0)

        return {
            "counters": counters,
            "latency_ms": {
                "samples": len(latencies),
                "min": latencies[0] if latencies else None,
                "p50": self._percentile(latencies, 50),
                "p95": self._percentile(latencies, 95),
                "max": latencies[-1] if latencies else None,
                "avg": round(sum(latencies) / len(latencies), 2) if latencies else None,
            },
            "failures_by_reason": failures,
            # 平均每封信建立多少次连接：1.0 表示完全没复用，越小越好
            "connects_per_send": round(connects / total, 3) if total else None,
            "success_rate": round(succeeded / total, 4) if total else None,
        }

    def format_line(self) -> str:
        """一行摘要，便于写进日志。"""
        s = self.snapshot()
        lat = s["latency_ms"]
        return (
            "sends=%s ok=%s fail=%s connects=%s p50=%sms p95=%sms rate=%s"
            % (
                s["counters"].get("sends_total", 0),
                s["counters"].get("sends_succeeded", 0),
                s["counters"].get("sends_failed", 0),
                s["counters"].get("smtp_connects", 0),
                lat["p50"],
                lat["p95"],
                s["success_rate"],
            )
        )


#: 全局指标实例（进程内共享）
send_metrics = SendMetrics()
