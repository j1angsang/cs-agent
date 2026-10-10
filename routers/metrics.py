import logging

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from prometheus_client import REGISTRY, generate_latest, CONTENT_TYPE_LATEST
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import engine, get_db
from core.deps import require_roles
from core.metrics import DB_POOL_INUSE, NEEDS_HUMAN_SESSIONS
from crud import query

router = APIRouter(tags=["指标"])
logger = logging.getLogger(__name__)


@router.get("/metrics")
async def metrics(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
):
    NEEDS_HUMAN_SESSIONS.set(await query.count_needs_human_sessions(db))
    DB_POOL_INUSE.set(engine.pool.checkedout())
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
