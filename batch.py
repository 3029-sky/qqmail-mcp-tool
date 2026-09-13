# batch.py - 批量发送（带限流保护）
"""
一份内容发给多个人。

**为什么必须带限流保护**：QQ 对单账号有真实的频率限制，触发后返回
`550 Too many attempts. Unable to send. Try again later`，恢复要等数小时
（本项目实测踩过）。而批量发送恰恰是最容易连续快发的场景——
用户选中 50 个联系人一键发出，很可能直接把当天额度打光。

因此这里的默认值刻意保守：
  - 每封之间**强制间隔**（默认 6 秒）
  - 单次**数量上限**（默认 30）
  - 连读**失败即中止**（认证失败/限流这类错误继续发只会更糟）
  - 支持**先预览再发送**（dry_run），确认收件人名单无误再真发

这些不是「可配置的性能参数」，而是**安全边界**，所以默认值偏保守，
放宽要用户自己明确去改。
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

__all__ = ["BatchPlan", "SendCancelled", "plan_batch", "run_batch", "MAX_BATCH"]

#: 单次批量最多发给多少人。防的不是性能，是「手一滑发出去一百封」。
MAX_BATCH = 30

#: 每封之间的默认间隔（秒）。
#: 6 秒是刻意取的偏保守值：30 封约需 3 分钟，比触发限流后等几小时划算得多。
DEFAULT_INTERVAL = 6.0

#: 间隔的**下限**。用户可以调，但不能调到危险值。
#: 留 2 秒是因为：QQ 的限制是按「单位时间内的发送量」算的，
#: 间隔低于这个数，30 封会在一分钟内全部发出，基本必然触发限流。
MIN_INTERVAL = 2.0


class SendCancelled(Exception):
    """用户中途取消了批量发送。"""


@dataclass
class BatchPlan:
    """一次批量发送的计划。"""

    recipients: List[Dict[str, str]]      # [{"name":..., "email":...}]
    subject: str
    body: str
    attach_names: List[str] = field(default_factory=list)
    interval: float = DEFAULT_INTERVAL

    @property
    def count(self) -> int:
        return len(self.recipients)


def plan_batch(
    recipients: List[Any],
    subject: str,
    body: str,
    attach_names: Optional[List[str]] = None,
    interval: float = DEFAULT_INTERVAL,
) -> BatchPlan:
    """
    校验并整理批量发送计划。

    recipients 可以是邮箱字符串，也可以是联系人字典。
    这里**不做任何地址猜测**：所有问题一次列全，让用户一次改完，
    而不是发到第 7 个才报错（那样前 6 封已经出去了）。
    """
    from userdata import _EMAIL_RE   # noqa: PLC0415 - 复用同一套校验

    cleaned: List[Dict[str, str]] = []
    seen = set()
    problems: List[str] = []

    for item in recipients or []:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            email = str(item.get("email", "")).strip()
        else:
            name = ""
            email = str(item).strip()

        if not email:
            problems.append("有一项没有邮箱地址：%r" % (item,))
            continue
        if not _EMAIL_RE.match(email):
            problems.append("邮箱格式不对：%s" % email)
            continue
        key = email.lower()
        if key in seen:
            continue          # 同一个人只发一封——重复地址多半是手误
        seen.add(key)
        cleaned.append({"name": name or email, "email": email})

    if not cleaned:
        raise ValueError("没有有效的收件人。%s" % ("；".join(problems) if problems else ""))

    # 有任何一个地址不合法就**整批拒绝**，而不是「跳过坏的、发好的」。
    # 静默跳过是最坏的选择：用户以为所有人都收到了，实际少发了几个人，
    # 而且中间没有任何提示。批量发送宁可整体失败、让人一次改对。
    if problems:
        raise ValueError(
            "以下收件人有问题，已取消发送（没有发出任何邮件）：\n  - %s"
            % "\n  - ".join(problems)
        )

    if len(cleaned) > MAX_BATCH:
        raise ValueError(
            "一次最多发给 %d 人（当前 %d 人）。\n"
            "这是刻意的限制：QQ 对单账号有频率限制，连续快发会导致当天无法再发送。\n"
            "请分几次发送。"
            % (MAX_BATCH, len(cleaned))
        )

    if not (subject or "").strip():
        raise ValueError("主题不能为空。")
    if not (body or "").strip():
        raise ValueError("正文不能为空。")

    interval = max(MIN_INTERVAL, float(interval)) if interval is not None else DEFAULT_INTERVAL

    return BatchPlan(
        recipients=cleaned,
        subject=subject.strip(),
        body=body,
        attach_names=list(attach_names or []),
        interval=interval,
    )


async def run_batch(
    plan: BatchPlan,
    dry_run: bool = True,
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    执行批量发送，逐条产出事件。

    事件类型：
      {"type": "plan",    "count", "interval", "estimate_seconds", "recipients"}
      {"type": "item",    "index", "total", "name", "email", "ok", "message"}
      {"type": "waiting", "seconds"}
      {"type": "done",    "sent", "failed", "skipped"}

    dry_run=True 时只产出 plan 与 done，**不发送任何邮件**。
    """
    def emit(event: Dict[str, Any]) -> None:
        if on_event:
            on_event(event)

    def cancelled() -> bool:
        return bool(is_cancelled and is_cancelled())

    estimate = plan.interval * max(0, plan.count - 1)
    emit({
        "type": "plan",
        "count": plan.count,
        "interval": plan.interval,
        "estimate_seconds": estimate,
        "recipients": plan.recipients,
        "dry_run": dry_run,
    })

    if dry_run:
        emit({"type": "done", "sent": 0, "failed": 0, "skipped": 0, "dry_run": True})
        return {"sent": 0, "failed": 0, "skipped": 0, "dry_run": True}

    from email_tools import email_tools

    sent = failed = 0
    stop_reason = ""

    for index, person in enumerate(plan.recipients):
        if cancelled():
            stop_reason = "已取消"
            break

        if index > 0 and plan.interval > 0:
            emit({"type": "waiting", "seconds": plan.interval})
            # 间隔也要响应取消：否则用户点了取消还要干等好几秒
            waited = 0.0
            step = 0.25
            while waited < plan.interval:
                if cancelled():
                    break
                await asyncio.sleep(min(step, plan.interval - waited))
                waited += step
            if cancelled():
                stop_reason = "已取消"
                break

        # 每封用独立的幂等键：批量发送里「重复」应当按人区分，
        # 否则第一个人发成功后，后面所有人都会被判成重复请求而全都发不出去。
        key = "batch-%s-%d-%s" % (int(time.time()), index, person["email"])

        try:
            if plan.attach_names:
                result = await email_tools.send_email_with_attachment(
                    to_email=person["email"],
                    subject=plan.subject,
                    attachment_paths=plan.attach_names,
                    body=plan.body,
                    idempotency_key=key,
                )
            else:
                # 没有附件就用纯文本工具：工具名会出现在日志与动作卡片里，
                # 明明没带附件却显示「发送带附件的邮件」会让人以为漏了东西。
                result = await email_tools.send_text_email(
                    to_email=person["email"],
                    subject=plan.subject,
                    body=plan.body,
                    idempotency_key=key,
                )
            ok = bool(result.get("success"))
            message = str(result.get("message", ""))[:200]
        except Exception as e:  # noqa: BLE001
            ok, message = False, str(e)[:200]

        if ok:
            sent += 1
        else:
            failed += 1

        emit({
            "type": "item", "index": index + 1, "total": plan.count,
            "name": person["name"], "email": person["email"],
            "ok": ok, "message": message,
        })

        # 连读失败就停：认证失败、限流这类问题继续发只会让情况更糟，
        # 而且会白白消耗当天剩余的额度。
        if not ok and failed >= 1:
            stop_reason = "发送失败，已中止（避免继续消耗额度）"
            break

    emit({
        "type": "done",
        "sent": sent, "failed": failed,
        "skipped": plan.count - sent - failed,
        "stopped": stop_reason,
    })
    return {
        "sent": sent, "failed": failed,
        "skipped": plan.count - sent - failed,
        "stopped": stop_reason,
    }
