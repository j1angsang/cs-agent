import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import get_db
from core.deps import (
    can_view_any_user,
    get_actor_type,
    get_current_user,
    require_leader_or_admin,
    require_service_write,
)
from crud import query
from models.user import User
from schemas import RefundOut, RefundCreate, RefundPendingOut, RefundReview
from services.refunds import apply_refund_with_policy, review_refund_in_transaction

router = APIRouter(prefix="/refunds", tags=["退款"])
logger = logging.getLogger(__name__)


@router.get("/user/{user_id}", response_model=list[RefundOut])
async def list_user_refunds(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if user_id != current_user.id and not can_view_any_user(current_user):
        raise HTTPException(status_code=403, detail="无权查看他人退款")
    return await query.list_user_refunds(db, user_id)


@router.get("/pending", response_model=list[RefundPendingOut])
async def list_pending_refunds(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 审批仍仅 leader/admin；这里只是给客服看"待移交主管的单子"
    if not can_view_any_user(current_user):
        raise HTTPException(status_code=403, detail="无权查看退款审核列表")
    rows = await query.list_pending_refunds_with_order(db)
    return [
        RefundPendingOut(
            id=refund.id,
            order_no=order.order_no,
            username=user.username,
            amount=float(refund.amount),
            reason=refund.reason,
            created_at=refund.created_at,
        )
        for refund, order, user in rows
    ]


@router.get("/timeline/{order_no}")
async def refund_timeline(
    order_no: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await query.get_order(db, order_no)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.user_id != current_user.id and not can_view_any_user(current_user):
        raise HTTPException(status_code=404, detail="订单不存在")
    rows = await query.list_refund_timeline(db, order.id)
    events = [
        {
            "time": audit.created_at,
            "type": "audit",
            "action": audit.action,
            "actor_type": audit.actor_type,
            "comment": audit.comment,
            "rules_hit": (audit.rules_hit or {}).get("hit", []),
            "rules_missed": (audit.rules_hit or {}).get("missed", []),
            "refund_id": refund.id,
            "amount": float(refund.amount),
        }
        for refund, audit in rows
    ]
    events.sort(key=lambda e: e["time"])
    return {
        "order_no": order.order_no,
        "order_status": order.status,
        "total_price": float(order.total_price),
        "events": events,
    }


@router.get("/{refund_id}", response_model=RefundOut)
async def get_refund(
    refund_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    refund = await query.get_refund(db, refund_id)
    if refund is None:
        raise HTTPException(status_code=404, detail="退款不存在")
    if refund.user_id != current_user.id and not can_view_any_user(current_user):
        raise HTTPException(status_code=403, detail="无权查看他人退款")
    return refund


@router.post("", response_model=RefundOut)
async def create_refund(
    req: RefundCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_service_write),
    actor_type: str = Depends(get_actor_type),
):
    started = time.perf_counter()
    # 冲突路径内部会回滚 SAVEPOINT，结果出来后禁止再碰 ORM
    user_id = current_user.id
    outcome = await apply_refund_with_policy(
        db, current_user,
        reason=req.reason, actor_type=actor_type,
        order_id=req.order_id, order_no=req.order_no,
        session_id=req.session_id,
    )
    if outcome.level == "deny":
        logger.warning(
            "refunds.create_failed order=%s user_id=%s missed=%s status=%s elapsed_ms=%.1f",
            req.order_no or req.order_id, user_id, outcome.rules_missed,
            outcome.http_status, (time.perf_counter() - started) * 1000,
        )
        raise HTTPException(status_code=outcome.http_status, detail=outcome.detail)
    resp = RefundOut.model_validate(outcome.refund).model_copy(
        update={"decision": outcome.level, "reasons": outcome.reasons}
    )
    logger.info(
        "refunds.create_ok refund_id=%s user_id=%s level=%s rule_level=%s actor=%s elapsed_ms=%.1f",
        resp.id, user_id, outcome.level, outcome.rule_level, actor_type,
        (time.perf_counter() - started) * 1000,
    )
    return resp


@router.put("/{refund_id}/review", response_model=RefundOut)
async def review_refund(
    refund_id: int,
    req: RefundReview,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_leader_or_admin),
):
    started = time.perf_counter()
    refund = await query.get_refund(db, refund_id)
    if refund is None:
        logger.warning("refunds.review_failed refund_id=%s actor_id=%s reason=not_found", refund_id, current_user.id)
        raise HTTPException(status_code=404, detail="退款不存在")
    if refund.status != "pending":
        logger.warning("refunds.review_failed refund_id=%s actor_id=%s reason=not_pending status=%s", refund_id, current_user.id, refund.status)
        raise HTTPException(status_code=400, detail="该退款单已审核")

    order = await query.get_order_by_id(db, refund.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="关联订单不存在")

    refund = await review_refund_in_transaction(
        db, refund, order, current_user,
        approve=(req.action == "approve"), comment=req.comment,
    )
    logger.info(
        "refunds.review_ok refund_id=%s order_id=%s actor_id=%s action=%s result_status=%s order_status=%s elapsed_ms=%.1f",
        refund.id, order.id, current_user.id, req.action, refund.status, order.status,
        (time.perf_counter() - started) * 1000,
    )
    return refund
