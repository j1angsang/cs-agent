import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.action_audit import ActionAuditLog
from models.chat import ChatSession
from models.order import Order
from models.refund import Refund
from models.refund_audit import RefundAuditLog
from models.user import ROLE_ADMIN, ROLE_LEADER, User
from services.refunds import review_refund_in_transaction

logger = logging.getLogger(__name__)

# 人工批准后可执行的动作 -> 服务执行器；签名 fn(db, session, reviewer, *, row, approve, comment) -> str。
EXECUTORS: dict[str, object] = {}


def _register_executors() -> None:
    from services.order_actions import (
        execute_change_address_approval,
        execute_delay_coupon_approval,
        execute_urge_approval,
    )
    from services.session_actions import execute_agent_note_approval
    from services.tickets import execute_ticket_approval

    EXECUTORS.update({
        "urge": execute_urge_approval,
        "agent_note": execute_agent_note_approval,
        "ticket": execute_ticket_approval,
        "delay_coupon": execute_delay_coupon_approval,
        "change_address": execute_change_address_approval,
    })


_register_executors()


async def _latest_session_pending_refund(
    db: AsyncSession, session_id: int
) -> tuple[RefundAuditLog, Refund, Order] | None:
    row = (await db.execute(
        select(RefundAuditLog, Refund, Order)
        .join(Refund, Refund.id == RefundAuditLog.refund_id)
        .join(Order, Order.id == Refund.order_id)
        .where(RefundAuditLog.session_id == session_id,
               RefundAuditLog.action == "submit",
               Refund.status == "pending")
        .order_by(RefundAuditLog.id.desc())
        .limit(1)
    )).first()
    return row


async def find_pending_approval_payload(db: AsyncSession, session: ChatSession) -> dict | None:
    row = (await db.execute(
        select(ActionAuditLog)
        .where(ActionAuditLog.session_id == session.id,
               ActionAuditLog.result == "pending_approval")
        .order_by(ActionAuditLog.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    if row is not None:
        return {
            "type": "action_approval",
            "approval_ref": f"action:{row.id}",
            "session_id": session.id,
            "action": row.action,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "rules_hit": (row.rules_hit or {}).get("hit", []),
            "rules_missed": (row.rules_hit or {}).get("missed", []),
            "payload": row.payload,
            "suggested": (row.payload or {}).get("suggested", "按命中规则执行该动作"),
            "reason": f"动作 {row.action} 等待人工确认",
        }

    refund_row = await _latest_session_pending_refund(db, session.id)
    if refund_row is not None:
        audit, refund, order = refund_row
        rules = audit.rules_hit or {}
        return {
            "type": "action_approval",
            "approval_ref": f"refund:{refund.id}",
            "session_id": session.id,
            "action": "refund",
            "order_no": order.order_no,
            "refund_id": refund.id,
            "amount": float(refund.amount),
            "rules_hit": rules.get("hit", []),
            "rules_missed": rules.get("missed", []),
            "suggested": "审核退款单：通过则订单置为已退款，驳回则恢复原状态",
            "reason": f"退款单 {refund.id}（订单 {order.order_no}，{float(refund.amount)} 元）等待人工审核",
        }
    return None


async def execute_pending_approval(
    db: AsyncSession,
    session: ChatSession,
    reviewer: User,
    *,
    approve: bool,
    comment: str | None,
    approval_ref: str | None,
) -> str:
    # 执行发生在 resume 的路由请求里，interrupt 恢复重跑不会重复执行。
    kind, _, ref = (approval_ref or "").partition(":")
    if kind == "refund":
        return await _execute_refund_decision(db, session, reviewer, approve=approve,
                                              comment=comment, refund_id=int(ref) if ref else None)
    if kind == "action" and ref:
        row = (await db.execute(
            select(ActionAuditLog).where(ActionAuditLog.id == int(ref))
        )).scalar_one_or_none()
        if row is None or row.result != "pending_approval" or row.session_id != session.id:
            raise ValueError("待审批记录不存在或已处理")
        executor = EXECUTORS.get(row.action)
        if executor is None:
            raise ValueError(f"动作 {row.action} 暂不支持自动执行")
        return await executor(db, session, reviewer, row=row, approve=approve, comment=comment)

    payload = await find_pending_approval_payload(db, session)
    if payload is None:
        raise ValueError("该会话没有待人工处理的审批")
    return await execute_pending_approval(
        db, session, reviewer, approve=approve, comment=comment,
        approval_ref=payload["approval_ref"],
    )


async def _execute_refund_decision(
    db: AsyncSession, session: ChatSession, reviewer: User,
    *, approve: bool, comment: str | None, refund_id: int | None,
) -> str:
    if reviewer.role not in (ROLE_LEADER, ROLE_ADMIN):
        raise PermissionError("退款审核仅主管/管理员可操作")
    refund = None
    if refund_id is not None:
        refund = (await db.execute(select(Refund).where(Refund.id == refund_id))).scalar_one_or_none()
        if refund is not None and refund.user_id != session.user_id:
            refund = None
    else:
        row = await _latest_session_pending_refund(db, session.id)
        refund = row[1] if row else None
    if refund is None or refund.status != "pending":
        raise ValueError("待审核退款单不存在或已处理")

    from crud import query as crud_query

    order = await crud_query.get_order_by_id(db, refund.order_id)
    if order is None:
        raise ValueError("关联订单不存在")
    refund = await review_refund_in_transaction(
        db, refund, order, reviewer, approve=approve, comment=comment,
    )
    if approve:
        return f"退款单 {refund.id} 已通过审核，订单 {order.order_no} 已完成退款。"
    return f"退款单 {refund.id} 已驳回，订单 {order.order_no} 恢复原状态。"


def payload_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)
