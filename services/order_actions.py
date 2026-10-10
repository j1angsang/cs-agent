import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config.actions import action_mode, effective_level, load_rule_config
from core.metrics import ACTION_DECISION_TOTAL
from crud import query
from models.coupon import UserCoupon
from models.user import User
from policies.action_rules import decide_address_change, decide_coupon, decide_urge
from policies.decision import LEVEL_AUTO, RuleConfig
from services.action_support import (
    ActionOutcome,
    auto_outcome,
    deny_outcome,
    human_pending_outcome,
    mark_decision,
    order_not_found,
    rules_snapshot,
    write_action_audit,
)

logger = logging.getLogger(__name__)


async def _insert_coupon(
    db: AsyncSession, *, order_id: int, user_id: int, amount: Decimal,
    now: datetime, cfg: RuleConfig,
) -> UserCoupon | None:
    # SAVEPOINT 回滚并发冲突，外层事务与已加载实例不受影响，避免 MissingGreenlet。
    coupon = UserCoupon(
        user_id=user_id,
        order_id=order_id,
        amount=amount,
        source="delay_compensation",
        issued_at=now,
        expires_at=now + timedelta(days=cfg.coupon_expire_days),
    )
    try:
        async with db.begin_nested():
            db.add(coupon)
            await db.flush()
    except IntegrityError:
        return None
    return coupon


async def urge_shipping(
    db: AsyncSession, user: User, *,
    order_no: str, session_id: int | None,
    actor_type: str, cfg: RuleConfig | None = None,
) -> ActionOutcome:
    started_cfg = cfg or load_rule_config()
    order = await query.get_order(db, order_no, for_update=True)
    if order is None:
        return order_not_found("urge")

    decision = decide_urge(order, user, started_cfg)
    if decision.denied:
        outcome = deny_outcome("urge", decision,
                               not_owner_detail="无权操作他人订单",
                               default_detail=(400, "仅已发货订单支持催单加急"))
        logger.warning("actions.urge_deny order_no=%s user_id=%s missed=%s", order_no, user.id, outcome.rules_missed)
        return outcome

    level = effective_level(decision.level, action_mode("urge"))
    if level != LEVEL_AUTO:
        write_action_audit(
            db, action="urge", actor_type=actor_type, actor=user,
            target_type="order", target_id=order.id, session_id=session_id,
            rules_hit=rules_snapshot(decision.reasons, decision.rules_missed),
            payload={"order_no": order.order_no, "suggested": "为该已发货订单标记加急"},
            result="pending_approval",
        )
        await db.commit()
        ACTION_DECISION_TOTAL.labels(action="urge", result="human").inc()
        return human_pending_outcome("urge", decision)

    if order.urgent:
        logger.info("actions.urge_idempotent order_no=%s user_id=%s", order_no, user.id)
        return auto_outcome("urge", decision,
                            data={"order_no": order.order_no, "urgent": True,
                                  "urgent_at": order.urgent_at, "already_urgent": True})

    order.urgent = True
    order.urgent_at = datetime.now(timezone.utc)
    write_action_audit(
        db, action="urge", actor_type=actor_type, actor=user,
        target_type="order", target_id=order.id, session_id=session_id,
        rules_hit=rules_snapshot(decision.reasons, decision.rules_missed),
        payload={"order_no": order.order_no},
        result="executed",
    )
    await db.commit()
    ACTION_DECISION_TOTAL.labels(action="urge", result="auto").inc()
    logger.info("actions.urge_ok order_no=%s user_id=%s level=%s actor=%s", order_no, user.id, level, actor_type)
    return auto_outcome("urge", decision,
                        data={"order_no": order.order_no, "urgent": True, "urgent_at": order.urgent_at})


async def execute_urge_approval(db: AsyncSession, session, reviewer: User, *,
                                row, approve: bool, comment: str | None) -> str:
    from types import SimpleNamespace

    payload = dict(row.payload or {})
    order_no = payload.get("order_no")
    if not approve:
        mark_decision(row, reviewer, result="rejected", comment=comment)
        await db.commit()
        return f"已驳回：订单 {order_no} 的加急请求未获批准。"

    order = await query.get_order(db, order_no or "", for_update=True) if order_no else None
    decision = decide_urge(order, SimpleNamespace(id=session.user_id), load_rule_config()) \
        if order is not None else None
    if order is None or decision is None or decision.denied:
        mark_decision(row, reviewer, result="rejected", comment="state_changed",
                      extra={"review_comment": comment})
        await db.commit()
        return f"订单 {order_no} 状态已变化，加急请求无法执行，已关闭。"

    order.urgent = True
    order.urgent_at = datetime.now(timezone.utc)
    mark_decision(row, reviewer, result="executed", comment=comment)
    await db.commit()
    logger.info("actions.urge_approved order_no=%s reviewer_id=%s", order_no, reviewer.id)
    return f"订单 {order_no} 已标记加急，物流将优先处理。"


async def issue_delay_coupon(
    db: AsyncSession, user: User, *,
    order_no: str, session_id: int | None,
    actor_type: str, cfg: RuleConfig | None = None,
    now: datetime | None = None,
) -> ActionOutcome:
    # 面额由服务层按 min(上限, 订单金额) 算，调用方不能传金额；shipped_at IS NULL 判 human，不许当超期。
    started_cfg = cfg or load_rule_config()
    now = now or datetime.now(timezone.utc)
    order = await query.get_order(db, order_no, for_update=True)
    if order is None:
        return order_not_found("delay_coupon")

    coupon_exists = (await db.execute(
        select(UserCoupon.id).where(UserCoupon.order_id == order.id)
    )).scalar_one_or_none() is not None
    decision = decide_coupon(order, user, now, started_cfg, coupon_exists=coupon_exists)
    if decision.denied:
        detail = ((400, "该订单已发放过补偿券") if "already_issued" in decision.rules_missed
                  else (400, "该订单不符合补偿券发放条件"))
        outcome = deny_outcome("delay_coupon", decision,
                               not_owner_detail="无权操作他人订单",
                               default_detail=detail)
        logger.warning("actions.coupon_deny order_no=%s user_id=%s missed=%s",
                       order_no, user.id, outcome.rules_missed)
        return outcome

    level = effective_level(decision.level, action_mode("delay_coupon"))
    amount = min(started_cfg.coupon_amount_limit, Decimal(str(order.total_price)))
    if level != LEVEL_AUTO:
        write_action_audit(
            db, action="delay_coupon", actor_type=actor_type, actor=user,
            target_type="order", target_id=order.id, session_id=session_id,
            rules_hit=rules_snapshot(decision.reasons, decision.rules_missed),
            payload={"order_no": order.order_no, "amount": float(amount),
                     "suggested": "为该超期订单发放补偿券"},
            result="pending_approval",
        )
        await db.commit()
        ACTION_DECISION_TOTAL.labels(action="delay_coupon", result="human").inc()
        return human_pending_outcome("delay_coupon", decision)

    order_id_val = order.id
    order_no_val = order.order_no
    user_id_val = user.id
    coupon = await _insert_coupon(
        db, order_id=order_id_val, user_id=user_id_val, amount=amount,
        now=now, cfg=started_cfg,
    )
    if coupon is None:
        ACTION_DECISION_TOTAL.labels(action="delay_coupon", result="deny").inc()
        logger.warning("actions.coupon_duplicate order_no=%s user_id=%s", order_no_val, user_id_val)
        return ActionOutcome(action="delay_coupon", level="deny", rule_level=decision.level,
                             rules_missed=["already_issued"],
                             http_status=400, detail="该订单已发放过补偿券")

    write_action_audit(
        db, action="delay_coupon", actor_type=actor_type, actor=user,
        target_type="order", target_id=order_id_val, session_id=session_id,
        rules_hit=rules_snapshot(decision.reasons, decision.rules_missed),
        payload={"order_no": order_no_val, "coupon_id": coupon.id, "amount": float(amount)},
        result="executed",
    )
    await db.commit()
    ACTION_DECISION_TOTAL.labels(action="delay_coupon", result="auto").inc()
    logger.info("actions.coupon_ok order_no=%s user_id=%s coupon_id=%s amount=%.2f actor=%s",
                order_no_val, user_id_val, coupon.id, float(amount), actor_type)
    return auto_outcome("delay_coupon", decision,
                        data={"order_no": order_no_val, "coupon_id": coupon.id, "amount": float(amount)})


async def change_address(
    db: AsyncSession, user: User, *,
    order_no: str, new_address: str, session_id: int | None,
    actor_type: str, cfg: RuleConfig | None = None,
) -> ActionOutcome:
    # old/new 只入审计 payload，不进日志。
    started_cfg = cfg or load_rule_config()
    order = await query.get_order(db, order_no, for_update=True)
    if order is None:
        return order_not_found("change_address")

    decision = decide_address_change(order, user, started_cfg)
    if decision.denied:
        outcome = deny_outcome("change_address", decision,
                               not_owner_detail="无权操作他人订单",
                               default_detail=(400, "仅未发货订单支持修改收货地址，已发货订单请联系人工协助"))
        logger.warning("actions.address_deny order_no=%s user_id=%s missed=%s",
                       order_no, user.id, outcome.rules_missed)
        return outcome

    level = effective_level(decision.level, action_mode("change_address"))
    old_address = order.address
    if level != LEVEL_AUTO:
        write_action_audit(
            db, action="change_address", actor_type=actor_type, actor=user,
            target_type="order", target_id=order.id, session_id=session_id,
            rules_hit=rules_snapshot(decision.reasons, decision.rules_missed),
            payload={"order_no": order.order_no, "new_address": new_address,
                     "suggested": "将收货地址变更为用户确认的新地址"},
            result="pending_approval",
        )
        await db.commit()
        ACTION_DECISION_TOTAL.labels(action="change_address", result="human").inc()
        return human_pending_outcome("change_address", decision)

    order.address = new_address
    write_action_audit(
        db, action="change_address", actor_type=actor_type, actor=user,
        target_type="order", target_id=order.id, session_id=session_id,
        rules_hit=rules_snapshot(decision.reasons, decision.rules_missed),
        payload={"order_no": order.order_no, "old_address": old_address, "new_address": new_address},
        result="executed",
    )
    await db.commit()
    ACTION_DECISION_TOTAL.labels(action="change_address", result="auto").inc()
    logger.info("actions.address_ok order_no=%s user_id=%s level=%s actor=%s old_present=%s",
                order.order_no, user.id, level, actor_type, old_address is not None)
    return auto_outcome("change_address", decision, data={"order_no": order.order_no})


async def execute_delay_coupon_approval(db: AsyncSession, session, reviewer: User, *,
                                        row, approve: bool, comment: str | None) -> str:
    from types import SimpleNamespace

    payload = dict(row.payload or {})
    if not approve:
        mark_decision(row, reviewer, result="rejected", comment=comment)
        await db.commit()
        return "已驳回：补偿券发放未获批准。"

    order_no = payload.get("order_no") or ""
    order = await query.get_order(db, order_no, for_update=True)
    cfg = load_rule_config()
    owner = SimpleNamespace(id=session.user_id)
    decision = None
    if order is not None:
        coupon_exists = (await db.execute(
            select(UserCoupon.id).where(UserCoupon.order_id == order.id)
        )).scalar_one_or_none() is not None
        decision = decide_coupon(
            order, owner, datetime.now(timezone.utc), cfg, coupon_exists=coupon_exists,
        )
    if order is None or decision is None or decision.denied:
        mark_decision(row, reviewer, result="rejected", comment="state_changed",
                      extra={"review_comment": comment})
        await db.commit()
        return f"订单 {order_no} 状态已变化，补偿券无法发放，该审批已关闭。"

    order_id_val = order.id
    order_no_val = order.order_no
    amount = min(cfg.coupon_amount_limit, Decimal(str(order.total_price)))
    coupon = await _insert_coupon(
        db, order_id=order_id_val, user_id=session.user_id, amount=amount,
        now=datetime.now(timezone.utc), cfg=cfg,
    )
    if coupon is None:
        mark_decision(row, reviewer, result="rejected", comment="already_issued",
                      extra={"review_comment": comment})
        await db.commit()
        return f"订单 {order_no_val} 已发放过补偿券，无需重复发放。"
    mark_decision(row, reviewer, result="executed", comment=comment,
                  extra={"coupon_id": coupon.id, "amount": float(amount)})
    await db.commit()
    logger.info("actions.coupon_approved order_no=%s coupon_id=%s reviewer_id=%s",
                order_no_val, coupon.id, reviewer.id)
    return f"补偿券已发放（{float(amount):.2f} 元），有效期以券详情为准。"


async def execute_change_address_approval(db: AsyncSession, session, reviewer: User, *,
                                          row, approve: bool, comment: str | None) -> str:
    from types import SimpleNamespace

    payload = dict(row.payload or {})
    if not approve:
        mark_decision(row, reviewer, result="rejected", comment=comment)
        await db.commit()
        return "已驳回：地址变更未获批准。"

    new_address = payload.get("new_address") or ""
    order_no = payload.get("order_no") or ""
    order = await query.get_order(db, order_no, for_update=True)
    owner = SimpleNamespace(id=session.user_id)
    decision = decide_address_change(order, owner, load_rule_config()) if order is not None else None
    if order is None or decision is None or decision.denied or not new_address:
        mark_decision(row, reviewer, result="rejected", comment="state_changed",
                      extra={"review_comment": comment})
        await db.commit()
        return f"订单 {order_no} 状态已变化，地址变更无法执行，该审批已关闭。"

    old_address = order.address
    order.address = new_address
    mark_decision(row, reviewer, result="executed", comment=comment,
                  extra={"old_address": old_address, "new_address": new_address})
    await db.commit()
    logger.info("actions.address_approved order_no=%s reviewer_id=%s old_present=%s",
                order_no, reviewer.id, old_address is not None)
    return "收货地址已更新。"
