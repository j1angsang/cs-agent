from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from policies.decision import Decision, LEVEL_AUTO, LEVEL_DENY, LEVEL_HUMAN

# 时间判定一律 timezone-aware UTC，naive 本地时间会按 UTC 落库偏移 8 小时。
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def shanghai_day_start_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now 必须 timezone-aware（禁止 naive datetime）")
    local = now.astimezone(BUSINESS_TZ)
    day_start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start_local.astimezone(timezone.utc)


def _today_amount(recent_refunds) -> Decimal:
    total = Decimal("0")
    for r in recent_refunds:
        total += Decimal(str(getattr(r, "amount", 0)))
    return total


def decide_refund(order, user, recent_refunds, cfg, *, active_order_refund: bool = False) -> Decision:
    # active_order_refund 与 ix_refunds_order_id_active 保持一致，只判 status 会漏判撞索引
    hit: list[str] = []
    missed: list[str] = []

    if order.user_id != user.id:
        return Decision(LEVEL_DENY, [], ["not_owner"])
    if order.status == "refunded":
        return Decision(LEVEL_DENY, [], ["already_refunded"])
    if order.status == "refund_pending":
        return Decision(LEVEL_DENY, [], ["already_refunding"])
    if active_order_refund:
        # 有进行中退款行就必须拒绝，复用 already_refunding 的 400 话术。
        return Decision(LEVEL_DENY, [], ["already_refunding"])
    if order.status == "shipped":
        return Decision(LEVEL_DENY, [], ["status_shipped"])
    if order.status not in ("pending", "delivered"):
        return Decision(LEVEL_DENY, [], ["status_unknown"])

    hit.append("owner_ok")
    if order.status == "pending":
        hit.append("status_pending")
    else:
        missed.append("status_delivered_review")

    if Decimal(str(order.total_price)) > cfg.refund_amount_limit:
        missed.append("amount_over_limit")
    else:
        hit.append("amount_within_limit")

    if len(recent_refunds) >= cfg.refund_daily_max_count:
        missed.append("daily_quota_count_exceeded")
    elif _today_amount(recent_refunds) + Decimal(str(order.total_price)) > cfg.refund_daily_max_total:
        missed.append("daily_quota_total_exceeded")
    else:
        hit.append("daily_quota_ok")

    if missed:
        return Decision(LEVEL_HUMAN, hit, missed)
    return Decision(LEVEL_AUTO, hit, [])
