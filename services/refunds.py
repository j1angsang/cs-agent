import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config.actions import action_mode, effective_level, load_rule_config
from core.metrics import ACTION_DECISION_TOTAL
from crud import query
from models.order import Order
from models.refund import Refund
from models.refund_audit import RefundAuditLog
from models.user import User
from policies.decision import LEVEL_AUTO, LEVEL_DENY, LEVEL_HUMAN, RuleConfig
from policies.refund_policy import decide_refund, shanghai_day_start_utc

logger = logging.getLogger(__name__)

# deny 规则名映射既有 HTTP 话术，逐字保留。
_DENY_RESPONSES: dict[str, tuple[int, str]] = {
    "not_owner": (403, "无权对他人的订单发起退款"),
    "already_refunded": (400, "该订单已退款"),
    "already_refunding": (400, "该订单已在退款审核中"),
    "status_shipped": (400, "已发货订单暂不支持自助退款，请联系客服"),
    "status_unknown": (400, "已发货订单暂不支持自助退款，请联系客服"),
}


@dataclass
class RefundOutcome:
    action: str
    level: str
    rule_level: str
    reasons: list[str] = field(default_factory=list)
    rules_missed: list[str] = field(default_factory=list)
    refund: Refund | None = None
    order: Order | None = None
    http_status: int | None = None
    detail: str | None = None


def _thresholds_snapshot(cfg: RuleConfig) -> dict:
    return {
        "refund_amount_limit": float(cfg.refund_amount_limit),
        "refund_daily_max_count": cfg.refund_daily_max_count,
        "refund_daily_max_total": float(cfg.refund_daily_max_total),
    }


async def _read_order(db: AsyncSession, *, order_id: int | None, order_no: str | None) -> Order | None:
    if order_id is not None:
        return await query.get_order_by_id(db, order_id, for_update=True)
    return await query.get_order(db, order_no or "", for_update=True)


async def _todays_refunds(db: AsyncSession, user_id: int, now: datetime) -> list[Refund]:
    result = await db.execute(
        select(Refund).where(
            Refund.user_id == user_id,
            Refund.status.in_(("pending", "approved")),
            Refund.created_at >= shanghai_day_start_utc(now),
        )
    )
    return list(result.scalars().all())


def _deny_outcome(action: str, decision, rule_level: str) -> RefundOutcome:
    http_status, detail = _DENY_RESPONSES.get(
        next((m for m in decision.rules_missed if m in _DENY_RESPONSES), "status_unknown"),
    )
    ACTION_DECISION_TOTAL.labels(action=action, result="deny").inc()
    return RefundOutcome(
        action=action, level=LEVEL_DENY, rule_level=rule_level,
        reasons=list(decision.reasons), rules_missed=list(decision.rules_missed),
        http_status=http_status, detail=detail,
    )


async def apply_refund_with_policy(
    db: AsyncSession,
    user: User,
    *,
    reason: str,
    actor_type: str = "ai",
    order_id: int | None = None,
    order_no: str | None = None,
    action: str = "refund",
    session_id: int | None = None,
    cfg: RuleConfig | None = None,
    now: datetime | None = None,
) -> RefundOutcome:
    started = time.perf_counter()
    cfg = cfg or load_rule_config()
    now = now or datetime.now(timezone.utc)

    order = await _read_order(db, order_id=order_id, order_no=order_no)
    if order is None:
        logger.warning("refunds.apply_failed action=%s user_id=%s reason=order_not_found", action, user.id)
        return RefundOutcome(action=action, level=LEVEL_DENY, rule_level=LEVEL_DENY,
                             http_status=404, detail="订单不存在")

    # rules 只看 order.status 会漏 orphan 形状，这里在同一行锁事务内实读 refunds 兜底。
    has_active_refund = (await db.execute(
        select(Refund.id).where(
            Refund.order_id == order.id,
            Refund.status.in_(("pending", "approved")),
        ).limit(1)
    )).scalar_one_or_none() is not None
    rule_decision = decide_refund(
        order, user, await _todays_refunds(db, user.id, now), cfg,
        active_order_refund=has_active_refund,
    )
    if rule_decision.denied:
        outcome = _deny_outcome(action, rule_decision, rule_decision.level)
        logger.warning(
            "refunds.apply_deny action=%s order_no=%s user_id=%s missed=%s elapsed_ms=%.1f",
            action, order.order_no, user.id, outcome.rules_missed, (time.perf_counter() - started) * 1000,
        )
        return outcome

    mode = action_mode(action)
    level = effective_level(rule_decision.level, mode)

    order_no_val = order.order_no
    order_id_val = order.id
    user_id_val = user.id

    refund = Refund(
        order_id=order.id,
        user_id=user.id,
        amount=order.total_price,
        reason=reason,
        status="pending",
        original_status=order.status,
    )
    try:
        # 只回滚 SAVEPOINT：full rollback 会 expire 已加载实例，之后再碰 ORM 属性可能 MissingGreenlet。
        async with db.begin_nested():
            db.add(refund)
            await db.flush()
    except IntegrityError:
        ACTION_DECISION_TOTAL.labels(action=action, result="deny").inc()
        logger.warning("refunds.apply_failed order_no=%s user_id=%s reason=duplicate_active_refund",
                       order_no_val, user_id_val)
        return RefundOutcome(action=action, level=LEVEL_DENY, rule_level=rule_decision.level,
                             rules_missed=["already_refunding"],
                             http_status=409, detail="该订单已存在进行中的退款申请")

    if level == LEVEL_AUTO:
        order.status = "refunded"
        refund.status = "approved"
        audit_action = "approve"
    else:
        order.status = "refund_pending"
        audit_action = "submit"
    db.add(RefundAuditLog(
        refund_id=refund.id,
        actor_id=user.id,
        action=audit_action,
        actor_type=actor_type,
        session_id=session_id,
        rules_hit={"hit": rule_decision.reasons, "missed": rule_decision.rules_missed},
        thresholds=_thresholds_snapshot(cfg),
    ))
    await db.flush()
    await db.refresh(refund)

    await db.commit()
    ACTION_DECISION_TOTAL.labels(action=action, result="auto" if level == LEVEL_AUTO else "human").inc()
    logger.info(
        "refunds.apply_ok action=%s refund_id=%s order_no=%s user_id=%s level=%s rule_level=%s "
        "status=%s amount=%.2f actor=%s elapsed_ms=%.1f",
        action, refund.id, order_no_val, user.id, level, rule_decision.level,
        refund.status, float(refund.amount), actor_type, (time.perf_counter() - started) * 1000,
    )
    return RefundOutcome(
        action=action, level=level, rule_level=rule_decision.level,
        reasons=list(rule_decision.reasons), rules_missed=list(rule_decision.rules_missed),
        refund=refund, order=order,
    )


async def review_refund_in_transaction(
    db: AsyncSession, refund: Refund, order: Order, actor: User,
    *, approve: bool, comment: str | None,
) -> Refund:
    if approve:
        refund.status = "approved"
        order.status = "refunded"
    else:
        refund.status = "rejected"
        order.status = refund.original_status or "pending"

    db.add(RefundAuditLog(
        refund_id=refund.id,
        actor_id=actor.id,
        action="approve" if approve else "reject",
        comment=comment,
        actor_type="human_reviewer",
    ))
    await db.flush()
    await db.refresh(refund)
    await db.commit()
    return refund
