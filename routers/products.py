import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

import schemas
from config.db import get_db
from crud import query

router = APIRouter(prefix="/products", tags=["商品"])
logger = logging.getLogger(__name__)


@router.get("", response_model=List[schemas.ProductOut])
async def list_products(
        keyword: str | None = None,
        db: AsyncSession = Depends(get_db),
):
    # 匿名可访问是故意的：商品浏览公开，购物车/下单/订单仍须登录
    return await query.list_products(db, keyword)


@router.get("/{product_id}", response_model=schemas.ProductOut)
async def get_product(
        product_id: int,
        db: AsyncSession = Depends(get_db),
):
    product = await query.get_product(db, product_id)
    if product is None:
        logger.warning("products.get_not_found product_id=%s", product_id)
        raise HTTPException(status_code=404, detail="商品不存在")
    return product
