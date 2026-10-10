import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import get_db
from core.deps import (
    can_view_any_user,
    get_current_user,
    require_leader_or_admin,
    require_service_identity,
)
from crud import query
from crud.query import create_order, InsufficientStockError, ProductNotFoundError
from models.user import User
from schemas import (
    AddressChangeRequest,
    CancelRequest,
    OrderActionRef,
    OrderCreate,
    OrderOut,
    OrderStatusUpdate,
)
from services.order_actions import change_address, issue_delay_coupon, urge_shipping
from services.refunds import apply_refund_with_policy

router = APIRouter(prefix="/orders", tags=["订单"])
logger = logging.getLogger(__name__)

ALLOWED_STATUS_TRANSITIONS = {
    "pending": "shipped",
    "shipped": "delivered",
}


# 身份/归属参数一律从 current_user 拿，前端传的一律忽略
@router.get("/{order_no}", response_model=OrderOut)
async def get_order(
    order_no: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await query.get_order(db, order_no)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.user_id != current_user.id and not can_view_any_user(current_user):
        raise HTTPException(status_code=403, detail="无权查看他人订单")
    return order


@router.get("/user/{user_id}", response_model=list[OrderOut])
async def list_user_orders(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if user_id != current_user.id and not can_view_any_user(current_user):
        raise HTTPException(status_code=403, detail="无权查看他人订单")
    return await query.list_user_orders(db, user_id)


@router.post("")
async def create_new_order(
    req: OrderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    started = time.perf_counter()
    try:
        order = await create_order(db, current_user.id, req.product_id, req.quantity)
    except ProductNotFoundError:
        logger.warning(
            "orders.create_failed user_id=%s product_id=%s quantity=%s reason=product_not_found elapsed_ms=%.1f",
            current_user.id, req.product_id, req.quantity, (time.perf_counter() - started) * 1000,
        )
        raise HTTPException(status_code=404, detail="商品不存在")
    except InsufficientStockError as e:
        logger.warning(
            "orders.create_failed user_id=%s product_id=%s quantity=%s reason=insufficient_stock error=%s elapsed_ms=%.1f",
            current_user.id, req.product_id, req.quantity, e, (time.perf_counter() - started) * 1000,
        )
        raise HTTPException(status_code=400, detail=str(e))
    await db.commit()
    logger.info(
        "orders.create_ok user_id=%s order_id=%s order_no=%s product_id=%s quantity=%s elapsed_ms=%.1f",
        current_user.id, order.id, order.order_no, req.product_id, req.quantity,
        (time.perf_counter() - started) * 1000,
    )
    return order


@router.post("/{order_no}/urge")
async def urge_order(
    order_no: str,
    req: OrderActionRef,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_service_identity),
):
    started = time.perf_counter()
    outcome = await urge_shipping(
        db, current_user, order_no=order_no, session_id=req.session_id,
        actor_type="ai",
    )
    if outcome.level == "deny":
        logger.warning("orders.urge_failed order_no=%s user_id=%s detail=%s", order_no, current_user.id, outcome.detail)
        raise HTTPException(status_code=outcome.http_status, detail=outcome.detail)
    logger.info("orders.urge_ok order_no=%s level=%s elapsed_ms=%.1f",
                order_no, outcome.level, (time.perf_counter() - started) * 1000)
    return {
        "action": "urge", "level": outcome.level, "reasons": outcome.reasons, **outcome.data,
    }


@router.post("/{order_no}/delay-coupon")
async def issue_delay_coupon_order(
    order_no: str,
    req: OrderActionRef,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_service_identity),
):
    started = time.perf_counter()
    outcome = await issue_delay_coupon(
        db, current_user, order_no=order_no, session_id=req.session_id,
        actor_type="ai",
    )
    if outcome.level == "deny":
        logger.warning("orders.delay_coupon_failed order_no=%s user_id=%s detail=%s",
                       order_no, current_user.id, outcome.detail)
        raise HTTPException(status_code=outcome.http_status, detail=outcome.detail)
    logger.info("orders.delay_coupon_ok order_no=%s level=%s elapsed_ms=%.1f",
                order_no, outcome.level, (time.perf_counter() - started) * 1000)
    return {
        "action": "delay_coupon", "level": outcome.level,
        "reasons": outcome.reasons, **outcome.data,
    }


@router.post("/{order_no}/address")
async def change_order_address(
    order_no: str,
    req: AddressChangeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_service_identity),
):
    # 地址不回显、不进日志，old/new 只入审计 payload
    started = time.perf_counter()
    outcome = await change_address(
        db, current_user, order_no=order_no, new_address=req.new_address,
        session_id=req.session_id, actor_type="ai",
    )
    if outcome.level == "deny":
        logger.warning("orders.address_failed order_no=%s user_id=%s detail=%s",
                       order_no, current_user.id, outcome.detail)
        raise HTTPException(status_code=outcome.http_status, detail=outcome.detail)
    logger.info("orders.address_ok order_no=%s level=%s elapsed_ms=%.1f",
                order_no, outcome.level, (time.perf_counter() - started) * 1000)
    return {
        "action": "change_address", "level": outcome.level,
        "reasons": outcome.reasons, **outcome.data,
    }


@router.post("/{order_no}/cancel")
async def cancel_order(
    order_no: str,
    req: CancelRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_service_identity),
):
    started = time.perf_counter()
    outcome = await apply_refund_with_policy(
        db, current_user, order_no=order_no, reason=req.reason,
        actor_type="ai", action="cancel_order", session_id=req.session_id,
    )
    if outcome.level == "deny":
        logger.warning("orders.cancel_failed order_no=%s user_id=%s missed=%s status=%s elapsed_ms=%.1f",
                       order_no, current_user.id, outcome.rules_missed, outcome.http_status,
                       (time.perf_counter() - started) * 1000)
        raise HTTPException(status_code=outcome.http_status, detail=outcome.detail)
    logger.info("orders.cancel_ok order_no=%s refund_id=%s level=%s elapsed_ms=%.1f",
                order_no, outcome.refund.id, outcome.level, (time.perf_counter() - started) * 1000)
    return {
        "action": "cancel_order", "level": outcome.level, "reasons": outcome.reasons,
        "refund_id": outcome.refund.id, "status": outcome.refund.status,
        "amount": float(outcome.refund.amount), "order_no": outcome.order.order_no,
    }


@router.put("/{order_id}/status", response_model=OrderOut)
async def update_order_status(
    order_id: int,
    req: OrderStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_leader_or_admin),
):
    started = time.perf_counter()
    order = await query.get_order_by_id(db, order_id)
    if order is None:
        logger.warning("orders.update_status_failed order_id=%s actor_id=%s reason=not_found", order_id, current_user.id)
        raise HTTPException(status_code=404, detail="订单不存在")

    previous_status = order.status
    allowed_next = ALLOWED_STATUS_TRANSITIONS.get(order.status)
    if allowed_next is None or req.status != allowed_next:
        logger.warning(
            "orders.update_status_failed order_id=%s order_no=%s actor_id=%s from_status=%s to_status=%s reason=illegal_transition",
            order.id, order.order_no, current_user.id, previous_status, req.status,
        )
        raise HTTPException(
            status_code=400,
            detail=f"订单当前状态 {order.status} 不允许流转到 {req.status}",
        )
    order.status = req.status
    if req.status == "shipped":
        order.shipped_at = datetime.now(timezone.utc)
    await db.flush()
    await db.commit()
    logger.info(
        "orders.update_status_ok order_id=%s order_no=%s actor_id=%s from_status=%s to_status=%s elapsed_ms=%.1f",
        order.id, order.order_no, current_user.id, previous_status, req.status,
        (time.perf_counter() - started) * 1000,
    )
    return order
