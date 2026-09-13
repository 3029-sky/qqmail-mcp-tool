# tests/test_idempotency.py - 幂等键
"""
核心安全性质：同一个幂等键在有效期内**绝不能**导致两封邮件被发出。
"""

import pytest

from idempotency import IdempotencyStore

KEY = "req-abc-123"


@pytest.fixture
def store():
    return IdempotencyStore()


# ---------------------------------------------------------------------------
# 存储语义
# ---------------------------------------------------------------------------

def test_unknown_key_is_available(store):
    assert store.lookup(KEY) is None


def test_reserved_key_reports_in_progress(store):
    store.reserve(KEY)
    result = store.lookup(KEY)

    assert result is not None
    assert result["duplicate"] is True
    assert result["status"] == "in_progress"


def test_completed_key_returns_recorded_response(store):
    owner = store.reserve(KEY)
    store.complete(KEY, owner, {"success": True, "message": "已发送"})

    result = store.lookup(KEY)
    assert result["success"] is True
    assert result["duplicate"] is True
    assert "未再次发送" in result["message"]


def test_complete_requires_matching_owner(store):
    store.reserve(KEY)
    assert store.complete(KEY, "wrong-owner", {"success": True}) is False
    # 未写入结果，键仍处于处理中
    assert store.lookup(KEY)["status"] == "in_progress"


def test_release_frees_key_for_retry(store):
    owner = store.reserve(KEY)
    assert store.release(KEY, owner) is True
    assert store.lookup(KEY) is None, "释放后应可重新使用"


def test_release_requires_matching_owner(store):
    store.reserve(KEY)
    assert store.release(KEY, "wrong-owner") is False
    assert store.lookup(KEY) is not None


def test_second_reserve_replaces_inflight_entry(store):
    """重新占用不应抛错（上层通过 lookup 拦截重复请求）。"""
    store.reserve(KEY)
    store.reserve(KEY)
    assert store.size() == 1


# ---------------------------------------------------------------------------
# 过期
# ---------------------------------------------------------------------------

def test_completed_entry_expires_after_ttl():
    now = {"t": 1000.0}
    store = IdempotencyStore(ttl=10.0, clock=lambda: now["t"])

    owner = store.reserve(KEY)
    store.complete(KEY, owner, {"success": True})

    now["t"] += 5
    assert store.lookup(KEY) is not None, "TTL 内应仍然命中"

    now["t"] += 6  # 累计 11 秒，超过 ttl=10
    assert store.lookup(KEY) is None, "过期后应释放"


def test_stuck_inflight_entry_eventually_frees():
    """卡死的「处理中」不能永久占用键。"""
    now = {"t": 1000.0}
    store = IdempotencyStore(inflight_timeout=5.0, clock=lambda: now["t"])

    store.reserve(KEY)
    assert store.lookup(KEY)["status"] == "in_progress"

    now["t"] += 6
    assert store.lookup(KEY) is None, "超时后应允许重新占用"


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------

def test_concurrent_reserve_leaves_single_entry(store):
    import threading

    store.reserve(KEY)

    def worker():
        store.lookup(KEY)  # 只查询，不占用

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert store.size() == 1
    assert store.lookup(KEY)["status"] == "in_progress"
