from decimal import Decimal

from policies.decision import Decision, LEVEL_AUTO, LEVEL_DENY, LEVEL_HUMAN


def decide_urge(order, user, cfg) -> Decision:
    if order.user_id != user.id:
        return Decision(LEVEL_DENY, [], ["not_owner"])
    if order.status == "shipped":
        return Decision(LEVEL_AUTO, ["owner_ok", "status_shipped"], [])
    return Decision(LEVEL_DENY, [], ["status_not_shipped"])


def decide_agent_note(session, user, cfg) -> Decision:
    if session.user_id != user.id:
        return Decision(LEVEL_DENY, [], ["not_owner"])
    return Decision(LEVEL_AUTO, ["owner_ok"], [])


def decide_ticket(session, user, order, cfg) -> Decision:
    if session.user_id != user.id:
        return Decision(LEVEL_DENY, [], ["not_owner"])
    hit = ["owner_ok"]
    if order is not None:
        if order.user_id != user.id:
            return Decision(LEVEL_DENY, [], ["not_owner"])
        hit.append("order_owner_ok")
    return Decision(LEVEL_AUTO, hit, [])


def decide_coupon(order, user, now, cfg, *, coupon_exists: bool) -> Decision:
    if order.user_id != user.id:
        return Decision(LEVEL_DENY, [], ["not_owner"])
    hit: list[str] = ["owner_ok"]
    missed: list[str] = []

    if order.status != "shipped":
        return Decision(LEVEL_DENY, [], ["status_not_shipped"])
    hit.append("status_shipped")

    # shipped_at 为空视为信息不足降 human，不许当超期。
    if getattr(order, "shipped_at", None) is None:
        missed.append("shipped_at_missing")
    elif (now - order.shipped_at).total_seconds() <= cfg.coupon_delay_days * 86400:
        missed.append("delay_not_overdue")
    else:
        hit.append("delay_over_threshold")

    if coupon_exists:
        missed.append("already_issued")
    else:
        hit.append("not_already_issued")

    computed = min(cfg.coupon_amount_limit, Decimal(str(order.total_price)))
    if computed > cfg.coupon_amount_limit:
        missed.append("amount_over_limit")
    else:
        hit.append("amount_within_limit")

    if "shipped_at_missing" in missed or "amount_over_limit" in missed:
        return Decision(LEVEL_HUMAN, hit, missed)
    if missed:
        return Decision(LEVEL_DENY, hit, missed)
    return Decision(LEVEL_AUTO, hit, [])


def decide_address_change(order, user, cfg) -> Decision:
    if order.user_id != user.id:
        return Decision(LEVEL_DENY, [], ["not_owner"])
    if order.status == "pending":
        return Decision(LEVEL_AUTO, ["owner_ok", "status_pending"], [])
    return Decision(LEVEL_DENY, [], ["status_not_pending"])
