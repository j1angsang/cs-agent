import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from config.db import LANGGRAPH_DB_URL

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() == "true"


def use_checkpointer() -> bool:
    return _env_flag("USE_CHECKPOINTER", "true")


def memory_enabled() -> bool:
    return _env_flag("MEMORY_ENABLED", "true")


def checkpoint_window() -> int:
    try:
        return max(2, int(os.getenv("CHECKPOINT_MESSAGE_WINDOW", "20")))
    except ValueError:
        return 20


def long_term_memory_limit() -> int:
    try:
        return max(1, int(os.getenv("LONG_TERM_MEMORY_LIMIT", "10")))
    except ValueError:
        return 10


def extract_max_items() -> int:
    try:
        return max(1, int(os.getenv("MEMORY_EXTRACT_MAX_ITEMS", "5")))
    except ValueError:
        return 5


def thread_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def make_thread_id(session_id: int) -> str:
    return f"sess-{session_id}"


# namespace 必带用户 id；代管会话时挂被服务的用户，绝不挂客服账号。
def memory_namespace(user_id: int) -> tuple[str, str]:
    return ("memories", str(user_id))


_saver: AsyncPostgresSaver | None = None
_saver_cm = None
_store = None
_store_cm = None


async def init_memory_runtime() -> None:
    global _saver, _saver_cm
    global _store, _store_cm
    if _saver is None and use_checkpointer():
        logger.info("memory.init 开始装配 AsyncPostgresSaver")
        _saver_cm = AsyncPostgresSaver.from_conn_string(LANGGRAPH_DB_URL)
        saver = await _saver_cm.__aenter__()
        await saver.setup()
        _saver = saver
        logger.info("memory.init AsyncPostgresSaver 就绪")
    else:
        logger.info("memory.init USE_CHECKPOINTER=false，不装配 checkpointer")
    if _store is None and memory_enabled():
        from langgraph.store.postgres.aio import AsyncPostgresStore

        logger.info("memory.init 开始装配 AsyncPostgresStore")
        _store_cm = AsyncPostgresStore.from_conn_string(LANGGRAPH_DB_URL)
        store = await _store_cm.__aenter__()
        await store.setup()
        _store = store
        logger.info("memory.init AsyncPostgresStore 就绪")
    else:
        logger.info("memory.init MEMORY_ENABLED=false，不装配长期记忆 Store")


async def close_memory_runtime() -> None:
    global _saver, _saver_cm, _store, _store_cm
    if _saver_cm is not None:
        try:
            await _saver_cm.__aexit__(None, None, None)
        except Exception:
            logger.exception("memory.close AsyncPostgresSaver 关闭失败")
        _saver_cm = None
        _saver = None
        logger.info("memory.close AsyncPostgresSaver 已关闭")
    if _store_cm is not None:
        try:
            await _store_cm.__aexit__(None, None, None)
        except Exception:
            logger.exception("memory.close AsyncPostgresStore 关闭失败")
        _store_cm = None
        _store = None
        logger.info("memory.close AsyncPostgresStore 已关闭")


def get_checkpointer() -> AsyncPostgresSaver | None:
    return _saver


def get_store():
    return _store


async def checkpoint_divergence_check(session_id: int, last_business_assistant: str | None) -> dict:
    saver = get_checkpointer()
    thread_id = make_thread_id(session_id)
    report: dict = {
        "thread_id": thread_id,
        "checkpoint_steps": None,
        "checkpoint_last_ai": None,
        "business_last_ai": last_business_assistant,
        "diverged": False,
        "reason": "",
    }
    if saver is None:
        report["reason"] = "checkpointer_disabled"
        return report
    try:
        state = await saver.aget_state(thread_config(thread_id))
        if state is not None:
            report["checkpoint_steps"] = len(list(await saver.aget_state_history(thread_config(thread_id))))
            msgs = (state.values or {}).get("messages", [])
            for m in reversed(msgs):
                if isinstance(m, AIMessage) and getattr(m, "content", ""):
                    report["checkpoint_last_ai"] = str(m.content)
                    break
        cp_last = report["checkpoint_last_ai"]
        biz_last = (last_business_assistant or "").strip()
        cp_norm = (cp_last or "").strip()
        if cp_last is None:
            report["reason"] = "checkpoint_empty" if not biz_last else "checkpoint_empty_but_business_has_reply"
            report["diverged"] = bool(biz_last) and False
        elif not biz_last:
            report["diverged"] = True
            report["reason"] = "checkpoint_has_ai_but_business_missing"
        elif not cp_norm.startswith(biz_last) and not biz_last.startswith(cp_norm):
            report["diverged"] = True
            report["reason"] = "content_mismatch"
        if report["diverged"]:
            logger.warning(
                "memory.divergence session_id=%d thread_id=%s reason=%s cp_len=%s biz_len=%s",
                session_id, thread_id, report["reason"], len(cp_norm), len(biz_last),
            )
        return report
    except Exception as exc:
        logger.warning("memory.divergence_check_failed session_id=%s error=%s", session_id, exc)
        report["reason"] = f"check_failed: {type(exc).__name__}"
        return report


async def load_memories(user_id: int, query: str | None = None) -> str:
    # asearch 没有 order_by，传 query 时按语义相似度排序取 top-k。
    store = get_store()
    if store is None:
        return ""
    try:
        limit = long_term_memory_limit()
        if query:
            items = await store.asearch(memory_namespace(user_id), query=query, limit=limit)
        else:
            items = await store.asearch(memory_namespace(user_id), limit=limit)
        entries = [
            it for it in items
            if isinstance(it.value, dict) and it.value.get("text")
        ][:limit]
        if not entries:
            return ""
        lines = [f"- {str(it.value['text']).strip()}" for it in entries]
        logger.info("memory.load user_id=%s entries=%d query=%s", user_id, len(lines), bool(query))
        return (
            "你可能记得关于这位用户的以下信息（供参考，不确定时不要编造；如有冲突，以靠前的较新条目为准）：\n"
            + "\n".join(lines)
        )
    except Exception as exc:
        logger.warning("memory.load_failed user_id=%s error=%s", user_id, exc)
        return ""


_EXTRACT_PROMPT = """你是记忆抽取器。只从【用户消息】中抽取用户明确说出的、关于用户自己的稳定事实或偏好，
每条一句话，不超过 100 字。不要从任何助手回答/知识库/工具结果中抽取。
没有值得长期记住的就输出 []。只输出 JSON 数组，不要解释。

用户消息：{message}"""


def _parse_memory_items(raw: str) -> list[str]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    max_len = 100
    items = []
    for it in data:
        if not isinstance(it, str):
            continue
        item = it.strip()
        if not item or len(item) > max_len:
            continue
        items.append(item)
    return items


def _is_doc_like(text: str) -> bool:
    if len(text) > 100:
        return True
    return "[1]" in text or "[2]" in text or "[3]" in text


async def extract_and_store(user_id: int, user_message: str, assistant_answer: str, session_id: int) -> None:
    # 只从用户消息抽取，不从助手回答/工具结果/RAG 里抽。
    store = get_store()
    if store is None or not user_message or not user_message.strip():
        return
    try:
        from agents.graph import get_memory_llm

        prompt = _EXTRACT_PROMPT.format(message=user_message)
        resp = await get_memory_llm().ainvoke(prompt)
        items = _parse_memory_items(str(getattr(resp, "content", resp) or ""))
        if not items:
            return
        items = [it for it in items if not _is_doc_like(it)][:extract_max_items()]
        if not items:
            return
        known: set[str] = set()
        offset, page = 0, 100
        while offset < 1000:
            batch = await store.asearch(memory_namespace(user_id), limit=page, offset=offset)
            if not batch:
                break
            known.update(
                str(it.value.get("text", "")).strip()
                for it in batch if isinstance(it.value, dict)
            )
            offset += len(batch)
            if len(batch) < page:
                break
        stored = 0
        for item in items:
            if item in known:
                continue
            await store.aput(
                memory_namespace(user_id),
                str(uuid.uuid4()),
                {
                    "text": item,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "session_id": session_id,
                },
            )
            stored += 1
        if stored:
            logger.info("memory.stored user_id=%s session_id=%s count=%d", user_id, session_id, stored)
    except Exception as exc:
        logger.warning("memory.extract_failed user_id=%s session_id=%s error=%s", user_id, session_id, exc)


MEMORY_EXTRACTION_TIMEOUT_SECONDS = float(os.getenv("MEMORY_EXTRACTION_TIMEOUT_SECONDS", "10"))


async def run_memory_extraction(user_id: int, user_message: str, assistant_answer: str, session_id: int) -> None:
    import asyncio
    import time

    from core.metrics import MEMORY_EXTRACTION_TIMEOUT_TOTAL, MEMORY_EXTRACTION_TOTAL

    started = time.perf_counter()
    try:
        await asyncio.wait_for(
            extract_and_store(user_id, user_message, assistant_answer, session_id),
            timeout=MEMORY_EXTRACTION_TIMEOUT_SECONDS,
        )
        MEMORY_EXTRACTION_TOTAL.inc()
        logger.info(
            "memory.extract_bg ok user_id=%s session_id=%s elapsed_ms=%.1f",
            user_id, session_id, (time.perf_counter() - started) * 1000,
        )
    except asyncio.TimeoutError:
        MEMORY_EXTRACTION_TIMEOUT_TOTAL.inc()
        logger.warning(
            "memory.extract_bg timeout session_id=%s timeout_s=%.1f",
            session_id, MEMORY_EXTRACTION_TIMEOUT_SECONDS,
        )
