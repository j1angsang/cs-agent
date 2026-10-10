import logging
import random
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config.actions import action_mode, effective_level, load_rule_config
from core.metrics import ACTION_DECISION_TOTAL
from models.chat import ChatSession
from models.ticket import Ticket
from models.user import User
from policies.action_rules import decide_ticket
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


def _make_ticket_no() -> str:
    return f"TK{datetime.now():%Y%m%d%H%M%S}{random.randint(100, 999)}"


async def _insert_ticket(
    db: AsyncSession, *, session_id: int, user_id: int, type: str, subject: str,
) -> tuple[Ticket | None, Ticket | None]:
    # 撞 (session_id, type, open) 部分唯一索引时返回既有工单；其余冲突原样抛出。
    ticket = Ticket(
        ticket_no=_make_ticket_no(),
        session_id=session_id,
        user_id=user_id,
        type=type,
        subject=subject,
    )
    try:
        async with db.begin_nested():
            db.add(ticket)
            await db.flush()
    except IntegrityError:
        existing = (await db.execute(
            select(Ticket).where(
                Ticket.session_id == session_id, Ticket.type == type, Ticket.status == "open"
            )
        )).scalar_one_or_none()
        if existing is None:
            raise
        return None, existing
    return ticket, None


async def get_session_tickets(db: AsyncSession, session_id: int) -> list[Ticket]:
    result = await db.execute(
        select(Ticket).where(Ticket.session_id == session_id).order_by(Ticket.id.desc())
    )
    return list(result.scalars().all())


async def create_ticket(
    db: AsyncSession, user: User, *,
    session_id: int, type: str, subject: str,
    actor_type: str, cfg: RuleConfig | None = None,
) -> ActionOutcome:
    started_cfg = cfg or load_rule_config()
    session = (await db.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )).scalar_one_or_none()
    if session is None:
        return session_not_found("ticket")

    decision = decide_ticket(session, user, None, started_cfg)
    if decision.denied:
        outcome = deny_outcome("ticket", decision,
                               not_owner_detail="无权操作他人会话",
                               default_detail=(400, "工单仅限本人会话创建"))
        logger.warning("actions.ticket_deny session_id=%s user_id=%s missed=%s", session_id, user.id, outcome.rules_missed)
        return outcome

    level = effective_level(decision.level, action_mode("ticket"))
    if level != LEVEL_AUTO:
        write_action_audit(
            db, action="ticket", actor_type=actor_type, actor=user,
            target_type="session", target_id=session.id, session_id=session.id,
            rules_hit=rules_snapshot(decision.reasons, decision.rules_missed),
            payload={"type": type, "subject": subject, "suggested": "创建该售后工单"},
            result="pending_approval",
        )
        await db.commit()
        ACTION_DECISION_TOTAL.labels(action="ticket", result="human").inc()
        return human_pending_outcome("ticket", decision)

    sid = session.id
    ticket, existing = await _insert_ticket(
        db, session_id=sid, user_id=user.id, type=type, subject=subject,
    )
    if ticket is None:
        logger.info("actions.ticket_existing session_id=%s type=%s ticket_no=%s",
                    sid, type, existing.ticket_no)
        return auto_outcome("ticket", decision,
                            data={"session_id": sid, "ticket_no": existing.ticket_no,
                                  "existing": True})

    write_action_audit(
        db, action="ticket", actor_type=actor_type, actor=user,
        target_type="ticket", target_id=ticket.id, session_id=session.id,
        rules_hit=rules_snapshot(decision.reasons, decision.rules_missed),
        payload={"type": type, "subject": subject, "ticket_no": ticket.ticket_no},
        result="executed",
    )
    await db.commit()
    ACTION_DECISION_TOTAL.labels(action="ticket", result="auto").inc()
    logger.info("actions.ticket_ok session_id=%s user_id=%s ticket_no=%s level=%s actor=%s",
                session_id, user.id, ticket.ticket_no, level, actor_type)
    return auto_outcome("ticket", decision,
                        data={"session_id": session.id, "ticket_no": ticket.ticket_no, "existing": False})


async def execute_ticket_approval(db: AsyncSession, session, reviewer: User, *,
                                  row, approve: bool, comment: str | None) -> str:
    payload = dict(row.payload or {})
    if not approve:
        mark_decision(row, reviewer, result="rejected", comment=comment)
        await db.commit()
        return "已驳回：工单创建未获批准。"

    ticket, existing = await _insert_ticket(
        db, session_id=session.id, user_id=session.user_id,
        type=payload.get("type") or "其他",
        subject=payload.get("subject") or "人工批准工单",
    )
    if ticket is None:
        mark_decision(row, reviewer, result="executed", comment=comment,
                      extra={"ticket_no": existing.ticket_no, "existing": True})
        await db.commit()
        return f"已有同类型未关闭工单：{existing.ticket_no}，无需重复创建。"
    mark_decision(row, reviewer, result="executed", comment=comment,
                  extra={"ticket_no": ticket.ticket_no, "existing": False})
    await db.commit()
    logger.info("actions.ticket_approved session_id=%s ticket_no=%s reviewer_id=%s",
                session.id, ticket.ticket_no, reviewer.id)
    return f"工单已创建：{ticket.ticket_no}。"
