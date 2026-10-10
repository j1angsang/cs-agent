import logging
import os
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

# 中文 Windows 控制台默认 GBK，非 GBK 字符打印会崩，统一转 UTF-8
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config.logging_conf import RequestLoggingMiddleware, setup_logging

# 日志基建必须早于 config.db / routers / agents import，否则首条日志会因 root logger 还没 handler 而丢
setup_logging()

from config.settings import validate_settings
from core.security_headers import SecurityHeadersMiddleware

validate_settings()

from config.db import AsyncSessionLocal, engine
from crud import query
from routers import auth, coupons, orders, products, refunds, chat, health, metrics, stats, tickets
from rag.rag import init_rag
from services.memory import close_memory_runtime, get_checkpointer, init_memory_runtime, make_thread_id

logger = logging.getLogger(__name__)

app = FastAPI(title="多Agent 智能客服系统")

# allow_origins=["*"] 与 allow_credentials=True 在浏览器规范里不合法，生产必须配 CORS_ORIGINS
CORS_ORIGINS = [x.strip() for x in os.getenv("CORS_ORIGINS", "*").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 用纯 ASGI 中间件做访问日志，不包装响应，否则 SSE 会被缓冲
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth.router)
app.include_router(products.router)
app.include_router(orders.router)
app.include_router(refunds.router)
app.include_router(coupons.router)
app.include_router(chat.router)
app.include_router(tickets.router)
app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(stats.router)

app.mount("/static", StaticFiles(directory="static", html=True), name="static")


@app.on_event("startup")
async def on_startup():
    """任一关键依赖起不来就退出，不带病启动。"""
    try:
        logger.info("startup.db 开始检查数据库连接")
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("startup.db ok")
    except Exception:
        logger.exception("startup.db_failed 数据库连接失败，进程退出")
        raise

    try:
        logger.info("startup.rag 开始初始化 pgvector/embedding")
        init_rag()
        logger.info("startup.rag ok")
    except Exception:
        logger.exception("startup.rag_failed 向量库/embedding 初始化失败，进程退出")
        raise

    # 失败即退出，不静默降级
    try:
        logger.info("startup.memory 开始装配记忆运行时（AsyncPostgresSaver）")
        await init_memory_runtime()
        logger.info("startup.memory ok")
    except Exception:
        logger.exception("startup.memory_failed 记忆运行时初始化失败，进程退出")
        raise

    if os.getenv("CLEANUP_SESSIONS_ENABLED", "false").strip().lower() == "true":
        try:
            days = int(os.getenv("CLEANUP_SESSIONS_AFTER_DAYS", "90"))
            deleted = await run_expired_session_cleanup(days)
            logger.info("startup.cleanup ok days=%d deleted_sessions=%d", days, deleted)
        except Exception:
            # 清理失败不阻断启动，旧数据多留一轮比服务起不来轻
            logger.exception("startup.cleanup_failed 过期会话清理失败（不阻断启动）")
    else:
        logger.info("startup.cleanup disabled, CLEANUP_SESSIONS_ENABLED=false")

    logger.info("客服系统启动完成：商品/订单/退款 CRUD + JWT + RAG + SSE")


async def run_expired_session_cleanup(after_days: int) -> int:
    """删 N 天前的会话：先删 checkpoint 再删业务表，checkpoint 失败只留残留不回滚。"""
    from datetime import datetime, timedelta

    cutoff = datetime.now() - timedelta(days=after_days)
    deleted = 0
    checkpointer = get_checkpointer()
    async with AsyncSessionLocal() as db:
        session_ids = await query.list_session_ids_older_than(db, cutoff)
        for sid in session_ids:
            if checkpointer is not None:
                try:
                    await checkpointer.adelete_thread(make_thread_id(sid))
                except Exception as exc:
                    logger.warning("cleanup.checkpoint_delete_failed session_id=%s error=%s", sid, exc)
        deleted = await query.delete_sessions_by_ids(db, session_ids)
        await db.commit()
    return deleted


@app.on_event("shutdown")
async def on_shutdown():
    await close_memory_runtime()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
