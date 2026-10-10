import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import get_db
from core.deps import can_view_any_user, get_current_user
from models.chat import ChatSession
from models.refund import Refund
from models.refund_audit import RefundAuditLog
from policies.refund_policy import shanghai_day_start_utc

router = APIRouter(prefix="/stats", tags=["统计"])
logger = logging.getLogger(__name__)


@router.get("/workbench")
async def workbench_stats(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not can_view_any_user(current_user):
        raise HTTPException(status_code=403, detail="无权查看统计数据")
    day_start = shanghai_day_start_utc(datetime.now(timezone.utc))

    pending_refunds = (await db.execute(
        select(func.count()).select_from(Refund).where(Refund.status == "pending")
    )).scalar_one()
    auto_refunds = (await db.execute(
        select(func.count()).select_from(RefundAuditLog).where(
            RefundAuditLog.action == "approve",
            RefundAuditLog.actor_type.in_(("ai", "user")),
            RefundAuditLog.created_at >= day_start,
        )
    )).scalar_one()
    human_reviews = (await db.execute(
        select(func.count()).select_from(RefundAuditLog).where(
            RefundAuditLog.action.in_(("approve", "reject")),
            RefundAuditLog.actor_type == "human_reviewer",
            RefundAuditLog.created_at >= day_start,
        )
    )).scalar_one()
    handoff_sessions = (await db.execute(
        select(func.count()).select_from(ChatSession).where(ChatSession.needs_human.is_(True))
    )).scalar_one()
    new_sessions_today = (await db.execute(
        select(func.count()).select_from(ChatSession).where(ChatSession.created_at >= day_start)
    )).scalar_one()

    return {
        "auto_refunds": auto_refunds,
        "human_reviews": human_reviews,
        "pending_refunds": pending_refunds,
        "handoff_sessions": handoff_sessions,
        "new_sessions_today": new_sessions_today,
    }
