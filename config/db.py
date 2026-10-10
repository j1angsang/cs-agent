"""数据库配置；全仓的连接串只在这里构造，其他模块 import 本模块。"""
import asyncio
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import URL, create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# psycopg async 在 Windows 不支持 ProactorEventLoop，必须在建事件循环前切 selector
if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    def selector_loop_factory(use_subprocess: bool = False):
        return asyncio.SelectorEventLoop

    try:
        import uvicorn.loops.asyncio as _uvicorn_asyncio_loop

        _uvicorn_asyncio_loop.asyncio_loop_factory = selector_loop_factory
    except Exception:
        pass
else:
    def selector_loop_factory(use_subprocess: bool = False):
        return asyncio.SelectorEventLoop

load_dotenv()

DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
if not DB_PASSWORD:
    raise RuntimeError("DB_PASSWORD 未配置:请检查 .env")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "customer_service_db")

DATABASE_URL = URL.create(
    "postgresql+psycopg",
    username=DB_USER,
    password=DB_PASSWORD,
    host=DB_HOST,
    port=DB_PORT,
    database=DB_NAME,
)
DATABASE_URL_STR = DATABASE_URL.render_as_string(hide_password=False)
SYNC_DATABASE_URL = DATABASE_URL_STR

# LangGraph DSN 的 scheme 必须是纯 postgresql://，不能带 +psycopg
LANGGRAPH_DB_URL = URL.create(
    "postgresql",
    username=DB_USER,
    password=DB_PASSWORD,
    host=DB_HOST,
    port=DB_PORT,
    database=DB_NAME,
).render_as_string(hide_password=False)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


def create_sync_engine():
    return create_engine(SYNC_DATABASE_URL, pool_pre_ping=True)


async def get_db():
    async with AsyncSessionLocal() as db:
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise
