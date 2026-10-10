import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from config.db import AsyncSessionLocal
from config.llm import LLM_API_KEY
from rag.rag import store

router = APIRouter(tags=["健康检查"])
logger = logging.getLogger(__name__)


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    # 对外只回枚举状态，不回传异常原文，避免表名/主机名随 traceback 泄漏
    checks: dict[str, str] = {}

    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:
        logger.exception("health.ready db check failed")
        checks["db"] = "error:db"

    try:
        count = store.count()
        checks["vector"] = f"ok(count={count})"
    except Exception:
        logger.exception("health.ready vector check failed")
        checks["vector"] = "error:vector"

    if LLM_API_KEY and LLM_API_KEY.strip():
        checks["llm_key"] = "ok"
    else:
        checks["llm_key"] = "error:llm_key_missing"

    try:
        from routers.chat import _chat_rate_limiter

        backend = _chat_rate_limiter.rate_limit_backend
        if backend == "memory" and getattr(_chat_rate_limiter, "degraded_since", None) is not None:
            checks["rate_limit_backend"] = "ok(memory;degraded)"
        else:
            checks["rate_limit_backend"] = f"ok({backend})"
    except Exception:
        logger.exception("health.ready rate_limit_backend check failed")
        checks["rate_limit_backend"] = "error:rate_limit_backend"

    try:
        from config.db import engine

        checks["db_pool"] = f"ok(inuse={engine.pool.checkedout()})"
    except Exception:
        logger.exception("health.ready db_pool check failed")
        checks["db_pool"] = "error:db_pool"

    healthy = all(value.startswith("ok") for value in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "unhealthy", "checks": checks},
    )
