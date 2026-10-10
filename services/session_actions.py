import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.actions import action_mode, effective_level, load_rule_config
from core.metrics import ACTION_DECISION_TOTAL
from models.chat import ChatSession
from models.user import User
from policies.action_rules import decide_agent_note
from policies.decision import LEVEL_AUTO, RuleConfig
from services.action_support import (
    ActionOutcome,
    auto_outcome,
    deny_outcome,
    human_pending_outcome,
    mark_decision,
    rules_snapshot,
    session_not_found,
    write_action_audit,
)

logger = logging.getLogger(__name__)


async def add_agent_note(
    db: AsyncSession, user: User, *,
    session_id: int, note: str,
    actor_type: str, cfg: RuleConfig | None = None,
) -> ActionOutcome:
    started_cfg = cfg or load_rule_config()
    session = (await db.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )).scalar_one_or_none()
    if session is None:
        return session_not_found("agent_note")

    decision = decide_agent_note(session, user, started_cfg)
    if decision.denied:
        outcome = deny_outcome("agent_note", decision,
                               not_owner_detail="无权备注他人会话",
                               default_detail=(400, "会话备注仅限本人会话"))
        logger.warning("actions.note_deny session_id=%s user_id=%s missed=%s", session_id, user.id, outcome.rules_missed)
        return outcome

    level = effective_level(decision.level, action_mode("agent_note"))
    if level != LEVEL_AUTO:
        write_action_audit(
            db, action="agent_note", actor_type=actor_type, actor=user,
            target_type="session", target_id=session.id, session_id=session.id,
            rules_hit=rules_snapshot(decision.reasons, decision.rules_missed),
            payload={"note": note, "suggested": "把该结论式备注写入会话"},
            result="pending_approval",
        )
        await db.commit()
        ACTION_DECISION_TOTAL.labels(action="agent_note", result="human").inc()
        return human_pending_outcome("agent_note", decision)

    session.agent_note = note
    write_action_audit(
        db, action="agent_note", actor_type=actor_type, actor=user,
        target_type="session", target_id=session.id, session_id=session.id,
        rules_hit=rules_snapshot(decision.reasons, decision.rules_missed),
        payload={"note": note},
        result="executed",
    )
    await db.commit()
    ACTION_DECISION_TOTAL.labels(action="agent_note", result="auto").inc()
    logger.info("actions.note_ok session_id=%s user_id=%s level=%s actor=%s note_len=%d",
                session_id, user.id, level, actor_type, len(note))
    return auto_outcome("agent_note", decision, data={"session_id": session.id})


async def execute_agent_note_approval(db: AsyncSession, session, reviewer: User, *,
                                      row, approve: bool, comment: str | None) -> str:
    payload = dict(row.payload or {})
    if not approve:
        mark_decision(row, reviewer, result="rejected", comment=comment)
        await db.commit()
        return "已驳回：会话备注未获批准。"
    session.agent_note = payload.get("note") or ""
    mark_decision(row, reviewer, result="executed", comment=comment,
                  extra={"note_len": len(session.agent_note)})
    await db.commit()
    return "会话备注已更新。"
