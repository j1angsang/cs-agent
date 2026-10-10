from datetime import datetime
import random
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from models.chat import ChatSession
from models.message import Message
from models.order import Order
from models.product import Product
from models.refund import Refund
from models.refund_audit import RefundAuditLog
from models.user import User


class ProductNotFoundError(Exception):
    pass


class InsufficientStockError(Exception):
    pass


async def get_product(db: AsyncSession, product_id: int) -> Product | None:
    stmt = select(Product).where(Product.id == product_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_products(db: AsyncSession, keyword: str | None = None) -> list[Product]:
    stmt = select(Product)
    if keyword:
        stmt = stmt.where(Product.name.like(f"%{keyword}%"))
    result = await db.execute(stmt.order_by(Product.id))
    return list(result.scalars().all())


async def get_order(db: AsyncSession, order_no: str, *, for_update: bool = False) -> Order | None:
    stmt = select(Order).where(Order.order_no == order_no)
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_order_by_id(db: AsyncSession, order_id: int, *, for_update: bool = False) -> Order | None:
    # 退款创建靠此行锁把查状态与建单串行化，防并发重复退款。
    stmt = select(Order).where(Order.id == order_id)
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_user_orders(db: AsyncSession, user_id: int) -> list[Order]:
    stmt = select(Order).where(Order.user_id == user_id).order_by(Order.id.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_refund(db: AsyncSession, refund_id: int) -> Refund | None:
    stmt = select(Refund).where(Refund.id == refund_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_user_refunds(db: AsyncSession, user_id: int) -> list[Refund]:
    stmt = select(Refund).where(Refund.user_id == user_id).order_by(Refund.id.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _make_order_no() -> str:
    # 订单号：时间戳+3位随机数，同秒冲突由 unique 约束兜底，代码里重试一次。
    return f"{datetime.now():%Y%m%d%H%M%S}{random.randint(100, 999)}"


async def create_order(db: AsyncSession, user_id: int, product_id: int, quantity: int) -> Order:
    # FOR UPDATE 串行化校验库存与扣减，避免两个请求同时买最后一件导致超卖。
    if quantity <= 0:
        raise InsufficientStockError("购买数量必须大于 0")

    for attempt in range(2):
        try:
            # 每次重试重新查一次，防止上个 IntegrityError rollback 后对象过期。
            stmt = select(Product).where(Product.id == product_id).with_for_update()
            result = await db.execute(stmt)
            product = result.scalar_one_or_none()
            if product is None:
                raise ProductNotFoundError("商品不存在")
            if quantity > product.stock:
                raise InsufficientStockError("库存不足")

            order = Order(
                user_id=user_id,
                product_id=product_id,
                quantity=quantity,
                total_price=float(product.price) * quantity,
                order_no=_make_order_no(),
            )
            db.add(order)
            # 先 flush 拿 order.id，同时让数据库立即检查 order_no 唯一冲突。
            await db.flush()
            product.stock -= quantity
            return order
        except IntegrityError:
            await db.rollback()
            if attempt == 0:
                continue
            raise


async def list_chat_sessions_with_last_message(
    db: AsyncSession,
    *,
    user_id: int | None = None,
    needs_human: bool | None = None,
    handler_uid: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple]:
    rn = (
        func.row_number()
        .over(partition_by=Message.session_id, order_by=Message.id.desc())
        .label("rn")
    )
    last_msg = (
        select(
            Message.session_id.label("sid"),
            Message.content.label("content"),
            Message.role.label("role"),
            rn,
        ).subquery()
    )
    handler = aliased(User)
    stmt = (
        select(ChatSession, last_msg.c.content, last_msg.c.role, User.username, handler.username)
        .outerjoin(last_msg, and_(last_msg.c.sid == ChatSession.id, last_msg.c.rn == 1))
        .join(User, User.id == ChatSession.user_id)
        .outerjoin(handler, handler.id == ChatSession.human_handler_id)
    )
    if user_id is not None:
        stmt = stmt.where(ChatSession.user_id == user_id)
    if needs_human is not None:
        stmt = stmt.where(ChatSession.needs_human == needs_human)
    if needs_human and handler_uid is not None:
        # 待人工队列仅返回未认领和本人认领的会话，他人已认领不可见。
        stmt = stmt.where(
            or_(ChatSession.human_handler_id.is_(None), ChatSession.human_handler_id == handler_uid)
        )
    stmt = stmt.order_by(ChatSession.updated_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).all()
    return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]


async def list_pending_refunds_with_order(db: AsyncSession) -> list[tuple[Refund, Order, User]]:
    stmt = (
        select(Refund, Order, User)
        .join(Order, Order.id == Refund.order_id)
        .join(User, User.id == Refund.user_id)
        .where(Refund.status == "pending")
        .order_by(Refund.created_at.asc())
    )
    rows = (await db.execute(stmt)).all()
    return [(row[0], row[1], row[2]) for row in rows]


async def list_refund_timeline(db: AsyncSession, order_id: int) -> list[tuple[Refund, RefundAuditLog]]:
    stmt = (
        select(Refund, RefundAuditLog)
        .join(RefundAuditLog, RefundAuditLog.refund_id == Refund.id)
        .where(Refund.order_id == order_id)
        .order_by(RefundAuditLog.created_at.asc(), Refund.id.asc())
    )
    rows = (await db.execute(stmt)).all()
    return [(row[0], row[1]) for row in rows]


async def list_session_ids_older_than(db: AsyncSession, cutoff: datetime) -> list[int]:
    result = await db.execute(
        select(ChatSession.id).where(ChatSession.updated_at < cutoff)
    )
    return [int(row[0]) for row in result.all()]


async def delete_sessions_by_ids(db: AsyncSession, session_ids: list[int]) -> int:
    if not session_ids:
        return 0
    result = await db.execute(
        delete(Message).where(Message.session_id.in_(session_ids))
    )
    result2 = await db.execute(
        delete(ChatSession).where(ChatSession.id.in_(session_ids))
    )
    return int(result2.rowcount or 0)


async def count_needs_human_sessions(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count()).select_from(ChatSession).where(ChatSession.needs_human == True)  # noqa: E712
    )
    return int(result.scalar() or 0)
