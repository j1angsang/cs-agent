from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import get_db
from core.deps import get_current_user
from models.coupon import UserCoupon

router = APIRouter(prefix="/coupons", tags=["优惠券"])


@router.get("/my")
async def my_coupons(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    rows = (await db.execute(
        select(UserCoupon)
        .where(UserCoupon.user_id == current_user.id)
        .order_by(UserCoupon.issued_at.desc())
    )).scalars().all()
    return [
        {
            "id": c.id,
            "amount": float(c.amount),
            "status": c.status,
            "source": c.source,
            "issued_at": c.issued_at,
            "expires_at": c.expires_at,
        }
        for c in rows
    ]
