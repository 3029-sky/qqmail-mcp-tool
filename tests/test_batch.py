# tests/test_batch.py - 批量发送的测试
"""
批量发送是**最容易造成真实损失**的功能：发错范围、触发限流导致当天
完全发不出去。所以这里的测试重点全在「边界」上：

  - 计划阶段就把所有问题一次列全（而不是发到一半才报错）
  - 上限与间隔下限不可绕过
  - 中途失败立即中止
  - 取消能真的中断
  - dry_run 绝不发信

所有用例都用替身发送器，不连网、不发信。
"""

import asyncio

import pytest

import batch
from batch import BatchPlan, MAX_BATCH, MIN_INTERVAL, plan_batch, run_batch


# ---------------------------------------------------------------------------
# plan_batch：校验与去重
# ---------------------------------------------------------------------------

def test_plan_accepts_plain_emails():
    plan = plan_batch(["a@b.com", "c@d.com"], "主题", "正文")
    assert plan.count == 2
    assert plan.recipients[0]["email"] == "a@b.com"


def test_plan_accepts_contact_dicts():
    plan = plan_batch(
        [{"name": "张三", "email": "zs@b.com"}], "主题", "正文"
    )
    assert plan.recipients[0]["name"] == "张三"


def test_plan_falls_back_to_email_as_name():
    """只有地址时用地址当显示名，免得界面上出现空白。"""
    plan = plan_batch(["a@b.com"], "主题", "正文")
    assert plan.recipients[0]["name"] == "a@b.com"


def test_plan_deduplicates_ignoring_case():
    """
    同一个地址只发一封。

    重复地址多半是手误（或同一人在两个分组里），
    真发两封会显得很业余，而且白耗额度。
    """
    plan = plan_batch(["a@b.com", "A@B.COM", "a@B.com"], "主题", "正文")
    assert plan.count == 1


def test_plan_rejects_whole_batch_when_any_recipient_is_bad():
    """
    有任何一个地址不合法就整批拒绝，**不静默跳过**。

    静默跳过是最坏的选择：用户以为所有人都收到了，实际少发了几个人，
    中间没有任何提示。批量场景下宁可整体失败，让人一次改对。
    """
    with pytest.raises(ValueError) as e:
        plan_batch(["好@b.com", "坏邮箱", "另一个坏@"], "主题", "正文")

    message = str(e.value)
    assert "坏邮箱" in message, "要指出哪个地址有问题"
    assert "另一个坏@" in message, "所有问题要一次列全"
    assert "没有发出任何邮件" in message, "要明确说明一封都没发"


def test_plan_reports_all_problems_at_once():
    """问题一次列全，避免「改一个、再报下一个」。"""
    with pytest.raises(ValueError) as e:
        plan_batch(["坏一号", "坏二号", "坏三号"], "主题", "正文")
    message = str(e.value)
    for bad in ("坏一号", "坏二号", "坏三号"):
        assert bad in message


def test_plan_rejects_when_no_valid_recipient():
    with pytest.raises(ValueError) as e:
        plan_batch(["bad", "worse"], "主题", "正文")
    assert "没有有效的收件人" in str(e.value)


def test_plan_rejects_empty_recipients():
    with pytest.raises(ValueError):
        plan_batch([], "主题", "正文")


def test_plan_rejects_over_limit():
    """上限存在的理由就是「手一滑发出去一百封」。"""
    with pytest.raises(ValueError) as e:
        plan_batch(["u%d@b.com" % i for i in range(MAX_BATCH + 5)], "主题", "正文")
    assert "最多" in str(e.value)
    assert str(MAX_BATCH) in str(e.value), "要说明上限是多少"


def test_plan_allows_exactly_at_limit():
    plan = plan_batch(["u%d@b.com" % i for i in range(MAX_BATCH)], "主题", "正文")
    assert plan.count == MAX_BATCH


def test_plan_requires_subject_and_body():
    with pytest.raises(ValueError) as e:
        plan_batch(["a@b.com"], "", "正文")
    assert "主题" in str(e.value)

    with pytest.raises(ValueError) as e:
        plan_batch(["a@b.com"], "主题", "   ")
    assert "正文" in str(e.value)


def test_plan_clamps_interval_to_minimum():
    """
    间隔可以被调小，但有下限。

    低于下限时 30 封会在一分钟内全部发出，基本必然触发 QQ 限流，
    而恢复要等数小时——所以这不是可优化项，是安全边界。
    """
    plan = plan_batch(["a@b.com", "c@d.com"], "主题", "正文", interval=0.1)
    assert plan.interval == MIN_INTERVAL


def test_plan_default_interval():
    plan = plan_batch(["a@b.com"], "主题", "正文")
    assert plan.interval == batch.DEFAULT_INTERVAL


def test_plan_accepts_larger_interval():
    """调大间隔是无害的，不该被夹到默认值。"""
    plan = plan_batch(["a@b.com"], "主题", "正文", interval=30)
    assert plan.interval == 30


def test_plan_keeps_attachment_names():
    plan = plan_batch(["a@b.com"], "主题", "正文", attach_names=["x.pdf"])
    assert plan.attach_names == ["x.pdf"]


# ---------------------------------------------------------------------------
# run_batch：dry_run
# ---------------------------------------------------------------------------

def _collect(plan, **kwargs):
    """跑一次批量，收集全部事件。"""
    events = []
    result = asyncio.run(run_batch(plan, on_event=events.append, **kwargs))
    return events, result


def test_dry_run_emits_plan_and_done_only():
    """
    dry_run 绝不发信。

    「先看名单再发」是批量发送的默认动作——发错范围比单发错严重得多。
    """
    plan = plan_batch(["a@b.com", "c@d.com"], "主题", "正文")
    events, result = _collect(plan, dry_run=True)

    assert [e["type"] for e in events] == ["plan", "done"]
    assert result["dry_run"] is True
    assert result["sent"] == 0


def test_dry_run_reports_estimate():
    plan = plan_batch(["a@b.com", "c@d.com", "e@f.com"], "主题", "正文", interval=10)
    events, _ = _collect(plan, dry_run=True)
    plan_event = events[0]
    assert plan_event["count"] == 3
    assert plan_event["estimate_seconds"] == 20      # (3-1) * 10
    assert len(plan_event["recipients"]) == 3


# ---------------------------------------------------------------------------
# run_batch：真实发送（用替身）
# ---------------------------------------------------------------------------

class FakeTools:
    """记录调用的发送替身。"""

    def __init__(self, fail_on=None, fail_forever=False):
        self.calls = []
        self.fail_on = fail_on or set()
        self.fail_forever = fail_forever

    async def send_text_email(self, to_email, subject, body, idempotency_key=None):
        self.calls.append(("text", to_email, idempotency_key))
        if self.fail_forever or to_email in self.fail_on:
            return {"success": False, "message": "550 被拒绝"}
        return {"success": True, "message": "邮件发送成功"}

    async def send_email_with_attachment(self, to_email, subject, attachment_paths,
                                         body, idempotency_key=None):
        self.calls.append(("attach", to_email, idempotency_key))
        return {"success": True, "message": "邮件发送成功"}


@pytest.fixture
def fake_tools(monkeypatch):
    """把 email_tools 换成替身。run_batch 内部是延迟导入，所以要打模块属性。"""
    import email_tools as module

    tools = FakeTools()
    monkeypatch.setattr(module, "email_tools", tools)
    return tools


def _fast(plan):
    """把间隔压到 0，让测试跑得快（下限保护只在 plan_batch 里生效）。"""
    plan.interval = 0.0
    return plan


def test_run_batch_sends_to_everyone(fake_tools):
    plan = _fast(plan_batch(["a@b.com", "c@d.com", "e@f.com"], "主题", "正文"))
    events, result = _collect(plan, dry_run=False)

    assert result["sent"] == 3
    assert result["failed"] == 0
    assert [c[1] for c in fake_tools.calls] == ["a@b.com", "c@d.com", "e@f.com"]


def test_run_batch_uses_text_tool_without_attachments(fake_tools):
    """没带附件就用纯文本工具——工具名会显示在动作卡片里，不能张冠李戴。"""
    plan = _fast(plan_batch(["a@b.com"], "主题", "正文"))
    _collect(plan, dry_run=False)
    assert fake_tools.calls[0][0] == "text"


def test_run_batch_uses_attachment_tool_when_needed(fake_tools):
    plan = _fast(plan_batch(["a@b.com"], "主题", "正文", attach_names=["x.pdf"]))
    _collect(plan, dry_run=False)
    assert fake_tools.calls[0][0] == "attach"


def test_run_batch_uses_distinct_idempotency_keys(fake_tools):
    """
    每个人用独立的幂等键。

    若共用一个键，第一封发成功后其余全会被判成重复请求——
    表现就是「批量发送只发出去了一封」，而且不报错。
    """
    plan = _fast(plan_batch(["a@b.com", "c@d.com"], "主题", "正文"))
    _collect(plan, dry_run=False)

    keys = [c[2] for c in fake_tools.calls]
    assert len(set(keys)) == 2, "幂等键必须按人区分"


def test_run_batch_stops_after_first_failure(fake_tools, monkeypatch):
    """
    失败即中止。

    认证失败、限流这类问题继续发只会更糟，而且白白消耗当天剩余额度。
    """
    fake_tools.fail_on = {"a@b.com"}
    plan = _fast(plan_batch(["a@b.com", "c@d.com", "e@f.com"], "主题", "正文"))
    events, result = _collect(plan, dry_run=False)

    assert result["failed"] == 1
    assert result["sent"] == 0
    assert result["skipped"] == 2, "剩下的人应被跳过"
    assert "中止" in result["stopped"]
    assert len(fake_tools.calls) == 1, "失败后不该再发"


def test_run_batch_emits_progress_per_recipient(fake_tools):
    plan = _fast(plan_batch(["a@b.com", "c@d.com"], "主题", "正文"))
    events, _ = _collect(plan, dry_run=False)

    items = [e for e in events if e["type"] == "item"]
    assert len(items) == 2
    assert items[0]["index"] == 1 and items[0]["total"] == 2
    assert items[0]["ok"] is True
    assert all(e.get("email") for e in items)


def test_run_batch_reports_failure_detail(fake_tools):
    fake_tools.fail_on = {"a@b.com"}
    plan = _fast(plan_batch(["a@b.com"], "主题", "正文"))
    events, _ = _collect(plan, dry_run=False)

    item = next(e for e in events if e["type"] == "item")
    assert item["ok"] is False
    assert "550" in item["message"], "失败原因要带出来，否则用户不知道发生了什么"


def test_run_batch_can_be_cancelled(fake_tools):
    """
    取消要真的生效。

    用「已取消」标记模拟用户在发第一封之后点了中止。
    run_batch 在**每封开始前**检查一次，因此第 2 封不会再发出。
    """
    plan = _fast(plan_batch(["a@b.com", "c@d.com", "e@f.com"], "主题", "正文"))

    state = {"count": 0}

    def is_cancelled():
        state["count"] += 1
        return state["count"] > 1       # 第二次检查（即第二封之前）就算取消

    events, result = _collect(plan, dry_run=False, is_cancelled=is_cancelled)

    assert result["sent"] == 1, "取消后不该继续发"
    assert result["skipped"] == 2
    assert "取消" in result["stopped"]
    assert len(fake_tools.calls) == 1


def test_run_batch_emits_waiting_between_sends(fake_tools):
    """
    每封之间要有等待事件。

    批量可能要跑几分钟，界面上没有进度提示用户只会以为卡死了。
    """
    plan = plan_batch(["a@b.com", "c@d.com"], "主题", "正文", interval=0.05)
    events, _ = _collect(plan, dry_run=False)

    waiting = [e for e in events if e["type"] == "waiting"]
    assert len(waiting) == 1, "两封之间只等一次"
    assert waiting[0]["seconds"] > 0


def test_run_batch_survives_exception_from_sender(monkeypatch):
    """发送器抛异常不能把整个批量任务带崩，应记为该人失败。"""
    import email_tools as module

    class Boom:
        async def send_text_email(self, **kwargs):
            raise RuntimeError("连接断了")

    monkeypatch.setattr(module, "email_tools", Boom())

    plan = _fast(plan_batch(["a@b.com"], "主题", "正文"))
    events, result = _collect(plan, dry_run=False)

    assert result["failed"] == 1
    item = next(e for e in events if e["type"] == "item")
    assert "连接断了" in item["message"]


def test_run_batch_single_recipient_has_no_wait(fake_tools):
    plan = _fast(plan_batch(["a@b.com"], "主题", "正文"))
    events, _ = _collect(plan, dry_run=False)
    assert not [e for e in events if e["type"] == "waiting"]
