"""退款动作写 refund_audit_logs，其余动作统一写 action_audit_logs，禁止双写。"""
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from core.metrics import ACTION_DECISION_TOTAL
from models.action_audit import ActionAuditLog
from models.user import User
from policies.decision import LEVEL_AUTO, LEVEL_DENY, LEVEL_HUMAN


@dataclass
class ActionOutcome:
    action: str
    level: str
    rule_level: str
    reasons: list[str] = field(default_factory=list)
    rules_missed: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    http_status: int | None = None
    detail: str | None = None


def deny_outcome(action: str, decision, *, not_owner_detail: str, default_detail: tuple[int, str]) -> ActionOutcome:
    missed = decision.rules_missed
    if "not_owner" in missed:
        http_status, detail = 403, not_owner_detail
    else:
        http_status, detail = default_detail
    ACTION_DECISION_TOTAL.labels(action=action, result="deny").inc()
    return ActionOutcome(action=action, level=LEVEL_DENY, rule_level=LEVEL_DENY,
                         reasons=list(decision.reasons), rules_missed=list(missed),
                         http_status=http_status, detail=detail)


def write_action_audit(
    db: AsyncSession, *,
    action: str,
    actor_type: str,
    actor: User,
    target_type: str,
    target_id: int | None,
    session_id: int | None,
    rules_hit: dict | None = None,
    payload: dict | None = None,
    result: str,
) -> ActionAuditLog:
    row = ActionAuditLog(
        actor_type=actor_type,
        actor_id=actor.id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        session_id=session_id,
        rules_hit=rules_hit,
        payload=payload,
        result=result,
    )
    db.add(row)
    return row


def rules_snapshot(reasons: list[str], missed: list[str]) -> dict:
    return {"hit": reasons, "missed": missed}


def mark_decision(row: ActionAuditLog, reviewer: User, *,
                  result: str, comment: str | None, extra: dict | None = None) -> None:
    # 人工决定落在原审计行上，不写第二行；result 收敛为 executed/rejected，actor 改为 human_reviewer。
    payload = dict(row.payload or {})
    payload.setdefault("requester_id", row.actor_id)
    payload["reviewer_id"] = reviewer.id
    payload["resolution"] = comment
    if extra:
        payload.update(extra)
    row.result = result
    row.payload = payload
    row.actor_type = "human_reviewer"
    row.actor_id = reviewer.id


def order_not_found(action: str) -> ActionOutcome:
    return ActionOutcome(action=action, level=LEVEL_DENY, rule_level=LEVEL_DENY,
                         http_status=404, detail="订单不存在")


def session_not_found(action: str) -> ActionOutcome:
    return ActionOutcome(action=action, level=LEVEL_DENY, rule_level=LEVEL_DENY,
                         http_status=404, detail="会话不存在")


def human_pending_outcome(action: str, decision) -> ActionOutcome:
    return ActionOutcome(action=action, level=LEVEL_HUMAN, rule_level=decision.level,
                         reasons=list(decision.reasons), rules_missed=list(decision.rules_missed))


def auto_outcome(action: str, decision, data: dict) -> ActionOutcome:
    return ActionOutcome(action=action, level=LEVEL_AUTO, rule_level=decision.level,
                         reasons=list(decision.reasons), rules_missed=list(decision.rules_missed),
                         data=data)
