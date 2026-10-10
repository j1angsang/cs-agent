import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import get_db
from core.deps import can_view_any_user, get_current_user, require_service_identity
from models.chat import ChatSession
from models.ticket import Ticket
from models.user import User
from schemas import TicketCreate, TicketOut
from services.tickets import create_ticket, get_session_tickets

router = APIRouter(prefix="/tickets", tags=["工单"])
logger = logging.getLogger(__name__)


async def _ticket_by_no(db: AsyncSession, ticket_no: str) -> Ticket:
    ticket = (await db.execute(
        select(Ticket).where(Ticket.ticket_no == ticket_no)
    )).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return ticket


@router.post("")
async def post_ticket(
    req: TicketCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_service_identity),
):
    outcome = await create_ticket(
        db, current_user,
        session_id=req.session_id, type=req.type, subject=req.subject,
        actor_type="ai",
    )
    if outcome.level == "deny":
        raise HTTPException(status_code=outcome.http_status, detail=outcome.detail)
    if outcome.level == "human":
        return {
            "action": "ticket",
            "level": outcome.level,
            "reasons": outcome.reasons,
            "existing": False,
        }
    ticket = await _ticket_by_no(db, outcome.data["ticket_no"])
    return {
        "action": "ticket",
        "level": outcome.level,
        "reasons": outcome.reasons,
        "existing": bool(outcome.data.get("existing")),
        "ticket_no": ticket.ticket_no,
        "id": ticket.id,
        "type": ticket.type,
        "subject": ticket.subject,
        "status": ticket.status,
    }


@router.get("/session/{session_id}", response_model=list[TicketOut])
async def list_session_tickets(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    session = (await db.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != current_user.id and not can_view_any_user(current_user):
        raise HTTPException(status_code=403, detail="无权查看他人会话工单")
    return await get_session_tickets(db, session_id)
