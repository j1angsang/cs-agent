import asyncio
import json
import logging
import os
import time
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agents.graph import astream_agent, has_pending_interrupt, resume_agent, run_agent
from config.db import AsyncSessionLocal, get_db
from core.deps import (
    get_current_user,
    can_view_any_user,
    require_leader_or_admin,
    require_roles,
    require_service_identity,
)
from core.metrics import RATE_LIMIT_REJECTED_TOTAL, STREAM_DEGRADE_TOTAL
from core.rate_limit import create_rate_limiter
from crud import query
from models.chat import ChatSession
from models.message import Message
from models.user import ROLE_ADMIN, ROLE_CUSTOMER_SERVICE, ROLE_LEADER, User
from schemas import (
    AgentNoteRequest,
    ChatRequest,
    ChatSessionOut,
    MessageOut,
    NeedsHumanRequest,
    SessionResumeRequest,
)
from services.approvals import execute_pending_approval, find_pending_approval_payload, payload_json
from services.session_actions import add_agent_note
from services.memory import (
    checkpoint_divergence_check,
    get_checkpointer,
    load_memories,
    make_thread_id,
    run_memory_extraction,
)
from tools.order_tools import TOOL_FAILURE_COUNTS

router = APIRouter(prefix="/chat", tags=["聊天"])
logger = logging.getLogger(__name__)


def _preview(value, limit: int = 120) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


USE_SUPERVISOR = os.getenv("USE_SUPERVISOR", "true").strip().lower() == "true"

_stream_degrade_count = 0

SSE_HEARTBEAT_SECONDS = float(os.getenv("SSE_HEARTBEAT_SECONDS", "15"))


async def _sse_heartbeat(source, interval_s: float):
    """超时用 shield 只丢"等待"：直接取消 __anext__ 会把 CancelledError 误当成客户端断连。"""
    ait = source.__aiter__()
    next_task: asyncio.Future | None = None
    try:
        while True:
            if next_task is None:
                next_task = asyncio.ensure_future(ait.__anext__())
            try:
                chunk = await asyncio.wait_for(asyncio.shield(next_task), timeout=interval_s)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            except StopAsyncIteration:
                break
            next_task = None
            yield chunk
    finally:
        if next_task is not None and not next_task.done():
            next_task.cancel()

# 单进程内存版；多 worker 部署必须换 Redis，否则各进程配额独立
RATE_LIMIT_MAX = int(os.getenv("CHAT_RATE_LIMIT_MAX", "10"))
RATE_LIMIT_WINDOW = int(os.getenv("CHAT_RATE_LIMIT_WINDOW", "60"))
_chat_rate_limiter = create_rate_limiter(RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)


async def _check_rate_limit(user_id: int) -> None:
    retry_after = await _chat_rate_limiter.check(f"chat:user:{user_id}")
    if retry_after is not None:
        logger.warning("chat.rate_limited user_id=%s retry_after=%s", user_id, retry_after)
        RATE_LIMIT_REJECTED_TOTAL.labels(scope="chat").inc()
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后再试",
            headers={"Retry-After": str(retry_after)},
        )


def reset_chat_rate_limiter() -> None:
    asyncio.run(_chat_rate_limiter.reset())


TRIGGER_WORDS = ("人工", "真人", "投诉", "客服")
STRONG_WORDS = ("报警", "315", "工商", "举报")


async def _handoff_reason(db: AsyncSession, session: ChatSession) -> str:
    payload = await find_pending_approval_payload(db, session)
    if payload is not None:
        return payload_json(payload)
    if session.needs_human_reason:
        return session.needs_human_reason
    return "session_flagged_needs_human"

FALLBACK_REPLY = "抱歉，我没有理解您的问题。"
# 老数据可能是旧文案或被截断，按片段匹配，别改成整句相等
_FALLBACK_REPLY_MARKERS = ("抱歉，我没有理解", "没有理解您的问题")
_NEEDS_HUMAN_WINDOW = 50


async def _detect_needs_human(db: AsyncSession, session: ChatSession, current_message: str) -> bool:
    if session.needs_human:
        return True

    stmt = (
        select(Message)
        .where(Message.session_id == session.id)
        .order_by(Message.id.desc())
        .limit(_NEEDS_HUMAN_WINDOW)
    )
    # 从人工处理锚点之后算起，否则历史触发词会让处理后的新消息立刻又转人工
    if session.handoff_handled_message_id is not None:
        stmt = stmt.where(Message.id > session.handoff_handled_message_id)
    result = await db.execute(stmt)
    recent_messages = list(reversed(result.scalars().all()))

    # 当前用户消息已在上方落库，recent_messages 已含它，不能再计一次
    user_contents = [m.content for m in recent_messages if m.role == "user"]

    trigger_count = sum(
        1 for c in user_contents if any(w in c for w in TRIGGER_WORDS)
    )
    strong_hit = any(
        any(w in c for w in STRONG_WORDS) for c in user_contents
    )
    no_understand_count = sum(
        1 for m in recent_messages
        if m.role == "assistant" and any(marker in m.content for marker in _FALLBACK_REPLY_MARKERS)
    )
    tool_fail_hit = TOOL_FAILURE_COUNTS.get(session.id, 0) >= 3

    if trigger_count >= 2 or strong_hit or no_understand_count >= 2 or tool_fail_hit:
        session.needs_human = True
        await db.flush()
        logger.warning(
            "chat.transfer_to_human session_id=%s user_id=%s trigger_count=%d "
            "strong_hit=%s no_understand_count=%d tool_fail_hit=%s",
            session.id, session.user_id, trigger_count,
            strong_hit, no_understand_count, tool_fail_hit,
        )
        return True
    return False


async def _load_session(
    db: AsyncSession,
    session_id: int | None,
    user_id: int,
    *,
    user: User | None = None,
) -> tuple[ChatSession, list[dict]]:
    if session_id is not None:
        result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
        session = result.scalar_one_or_none()
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if session.user_id != user_id and not (user and can_view_any_user(user)):
            raise HTTPException(status_code=403, detail="无权访问该会话")
        rows = await db.execute(
            select(Message).where(Message.session_id == session_id).order_by(Message.id.desc()).limit(20)
        )
        history = [
            {"role": m.role, "content": m.content}
            for m in reversed(list(rows.scalars()))
        ]
        return session, history

    session = ChatSession(user_id=user_id)
    db.add(session)
    await db.flush()
    return session, []


async def _save_messages(db: AsyncSession, session: ChatSession, history: list[dict]) -> None:
    for item in history:
        db.add(Message(session_id=session.id, role=item["role"], content=item["content"]))
    session.message_count += len(history)
    if history and session.title is None:
        for item in history:
            if item["role"] == "user":
                content = item["content"]
                text = content if isinstance(content, str) else " ".join(str(p) for p in content)
                text = text.strip()
                if text:
                    session.title = text[:30]
                break
    await db.flush()


def _user_payload(current_user: User, session_id: int) -> dict:
    return {"id": current_user.id, "role": current_user.role, "session_id": session_id}


HUMAN_TAKEN_OVER_REPLY = "已转人工，客服将尽快接入。"
HANDOFF_END_NOTICE = "人工服务已结束，已切换为智能客服，如需继续咨询请直接留言。"


async def _save_interrupted_assistant_message(session_id: int, answer: str) -> None:
    """断连/流中断时用独立会话落库，interrupted=True 供与 checkpoint 对账。"""
    try:
        async with AsyncSessionLocal() as db:
            session = (
                await db.execute(select(ChatSession).where(ChatSession.id == session_id))
            ).scalar_one_or_none()
            if session is None:
                logger.warning("chat.session disconnect_save_miss session_id=%s", session_id)
                return
            db.add(Message(session_id=session_id, role="assistant", content=answer, interrupted=True))
            session.message_count += 1
            session.updated_at = datetime.now()
            await db.commit()
        logger.warning(
            "chat.session disconnect_saved session_id=%s answer_chars=%d interrupted=True",
            session_id, len(answer),
        )
    except Exception:
        logger.exception("chat.session disconnect_save_failed session_id=%s", session_id)


async def _safe_run_agent(user_message: str, history: list[dict], user_payload: dict,
                          thread_id: str | None = None, long_term_memory: str | None = None,
                          needs_human_reason: str | None = None) -> str:
    try:
        if USE_SUPERVISOR:
            from agents.supervisor import run_supervisor
            supervisor_kwargs = {"thread_id": thread_id}
            if long_term_memory:
                supervisor_kwargs["long_term_memory"] = long_term_memory
            return await run_supervisor(user_message, history, user_payload, **supervisor_kwargs)
        return await run_agent(user_message, history, user_payload,
                               thread_id=thread_id, long_term_memory=long_term_memory,
                               needs_human_reason=needs_human_reason)
    except Exception:
        logger.exception(
            "chat.agent_failed session_id=%s user_id=%s question=%s",
            user_payload.get("session_id"), user_payload.get("id"), _preview(user_message),
        )
        return FALLBACK_REPLY


@router.post("")
async def chat(
    req: ChatRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _check_rate_limit(current_user.id)
    session, history = await _load_session(db, req.session_id, current_user.id, user=current_user)
    already_waiting = session.needs_human
    logger.info(
        "chat.session start stream=False user_id=%s session_id=%s new_session=%s history=%d waiting_handoff=%s",
        current_user.id, session.id, req.session_id is None, len(history), already_waiting,
    )

    await _save_messages(db, session, [{"role": "user", "content": req.message}])

    if already_waiting:
        session.updated_at = datetime.now()
        await db.commit()
        logger.info("chat.session waiting_human_static session_id=%s notice=once", session.id)
        return {"session_id": session.id, "answer": "", "waiting_handoff": True, "needs_human": True}

    await _detect_needs_human(db, session, req.message)
    # 先提交再跑 Agent：写工具走独立 DB 会话来读会话，未提交的会话它们看不到
    await db.commit()
    handoff_invoked = False
    if session.needs_human and get_checkpointer() is None and not USE_SUPERVISOR:
        answer = HUMAN_TAKEN_OVER_REPLY
        logger.info("chat.session transferred stream=False session_id=%s", session.id)
    else:
        memory_user_id = session.user_id
        thread_id = make_thread_id(session.id)
        memory_text = await load_memories(memory_user_id, query=req.message)
        if session.needs_human and not USE_SUPERVISOR:
            handoff_invoked = True
        answer = await _safe_run_agent(req.message, history, _user_payload(current_user, session.id),
                                       thread_id=thread_id, long_term_memory=memory_text,
                                       needs_human_reason=await _handoff_reason(db, session) if handoff_invoked else None)
        if handoff_invoked:
            answer = HUMAN_TAKEN_OVER_REPLY

    await _save_messages(db, session, [{"role": "assistant", "content": answer}])
    session.updated_at = datetime.now()
    await db.flush()
    await _detect_needs_human(db, session, req.message)
    await db.commit()
    if not session.needs_human:
        background_tasks.add_task(
            run_memory_extraction, session.user_id, req.message, answer, session.id
        )
    logger.info(
        "chat.session finish stream=False session_id=%s answer_chars=%d needs_human=%s",
        session.id, len(answer), session.needs_human,
    )
    return {"session_id": session.id, "answer": answer}


@router.post("/stream")
async def chat_stream(
    req: ChatRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _check_rate_limit(current_user.id)
    session, history = await _load_session(db, req.session_id, current_user.id, user=current_user)
    already_waiting = session.needs_human
    logger.info(
        "chat.session start stream=True user_id=%s session_id=%s new_session=%s history=%d waiting_handoff=%s",
        current_user.id, session.id, req.session_id is None, len(history), already_waiting,
    )

    async def event_generator():
        # answer/usage_out 必须在首个 yield 前初始化，断连可能发生在任意 yield 点
        answer = ""
        usage_out: dict = {}
        try:
            await _save_messages(db, session, [{"role": "user", "content": req.message}])
            await db.commit()

            yield f"data: {json.dumps({'session_id': session.id}, ensure_ascii=False)}\n\n"

            handoff_invoked = False
            if already_waiting:
                session.updated_at = datetime.now()
                await db.commit()
                logger.info(
                    "chat.session finish stream=True session_id=%s answer_chars=0 needs_human=True "
                    "waiting_handoff=True notice=once",
                    session.id,
                )
                yield f"data: {json.dumps({'waiting_handoff': True}, ensure_ascii=False)}\n\n"
            else:
                await _detect_needs_human(db, session, req.message)
                answer = ""
            if not already_waiting and session.needs_human and (get_checkpointer() is None or USE_SUPERVISOR):
                answer = HUMAN_TAKEN_OVER_REPLY
                logger.info("chat.session transferred stream=True session_id=%s", session.id)
                for ch in answer:
                    yield f"data: {json.dumps({'chunk': ch}, ensure_ascii=False)}\n\n"
            elif not already_waiting:
                memory_text = await load_memories(session.user_id, query=req.message)
                if USE_SUPERVISOR:
                    from agents.supervisor import astream_supervisor
                    stream_fn = astream_supervisor
                    stream_kwargs = {
                        "thread_id": make_thread_id(session.id),
                        "long_term_memory": memory_text,
                    }
                else:
                    if session.needs_human:
                        handoff_invoked = True
                    stream_fn = astream_agent
                    stream_kwargs = {
                        "thread_id": make_thread_id(session.id),
                        "long_term_memory": memory_text,
                        "usage_out": usage_out,
                        "needs_human_reason": await _handoff_reason(db, session) if handoff_invoked else None,
                    }
                got_chunk = False
                started = time.perf_counter()
                stream_agen = stream_fn(req.message, history, _user_payload(current_user, session.id), **stream_kwargs)
                try:
                    async for ch in stream_agen:
                        if isinstance(ch, dict) and "tool" in ch:
                            yield f"data: {json.dumps(ch, ensure_ascii=False)}\n\n"
                            continue
                        if not got_chunk:
                            got_chunk = True
                            logger.info(
                                "chat.stream ttft_ms=%.1f session_id=%s model=%s",
                                (time.perf_counter() - started) * 1000, session.id,
                                "supervisor" if USE_SUPERVISOR else "single_agent",
                            )
                        answer += ch
                        yield f"data: {json.dumps({'chunk': ch}, ensure_ascii=False)}\n\n"
                except Exception:
                    if got_chunk:
                        # 已吐部分 token 就交给外层 error 分支，那里会落 interrupted
                        raise
                    global _stream_degrade_count
                    _stream_degrade_count += 1
                    STREAM_DEGRADE_TOTAL.inc()
                    degrade_ttft_ms = (time.perf_counter() - started) * 1000
                    logger.exception(
                        "chat.stream degraded=true ttft_ms=%.1f degrade_count=%d session_id=%s "
                        "reason=stream_failed_before_first_token",
                        degrade_ttft_ms, _stream_degrade_count, session.id,
                    )
                    yield f"data: {json.dumps({'degraded': True, 'stream_mode': 'fallback', 'ttft_ms': round(degrade_ttft_ms, 1)}, ensure_ascii=False)}\n\n"
                    answer = await _safe_run_agent(
                        req.message, history, _user_payload(current_user, session.id),
                        thread_id=make_thread_id(session.id), long_term_memory=memory_text,
                    )
                    for ch in answer:
                        yield f"data: {json.dumps({'chunk': ch}, ensure_ascii=False)}\n\n"
                        await asyncio.sleep(0.01)
                finally:
                    # 必须显式 aclose，否则模型连接等 GC 才关，供应商在收尾前仍在生成烧钱
                    try:
                        await stream_agen.aclose()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("chat.stream close_failed session_id=%s error=%s", session.id, exc)

            if not already_waiting and not answer:
                answer = HUMAN_TAKEN_OVER_REPLY if handoff_invoked else FALLBACK_REPLY
                for ch in answer:
                    yield f"data: {json.dumps({'chunk': ch}, ensure_ascii=False)}\n\n"

            if not already_waiting:
                await _save_messages(db, session, [{"role": "assistant", "content": answer}])
                session.updated_at = datetime.now()
                await db.flush()
                await _detect_needs_human(db, session, req.message)
                await db.commit()
                if not session.needs_human:
                    background_tasks.add_task(
                        run_memory_extraction, session.user_id, req.message, answer, session.id
                    )
                logger.info(
                    "chat.session finish stream=True session_id=%s answer_chars=%d needs_human=%s "
                    "waiting_handoff=%s tokens_input=%s tokens_output=%s tokens_total=%s",
                    session.id, len(answer), session.needs_human, already_waiting,
                    usage_out.get("input_tokens", "N/A"), usage_out.get("output_tokens", "N/A"),
                    usage_out.get("total_tokens", "N/A"),
                )
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            logger.warning(
                "chat.session disconnected stream=True session_id=%s answer_chars=%d",
                session.id, len(answer),
            )
            if answer:
                await _save_interrupted_assistant_message(session.id, answer)
            raise
        except Exception:
            logger.exception("SSE 聊天异常 session_id=%s user_id=%s", session.id, current_user.id)
            if answer:
                await _save_interrupted_assistant_message(session.id, answer)
            yield f"data: {json.dumps({'error': '系统繁忙，请稍后再试'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    # X-Accel-Buffering: no 禁 nginx 缓冲，否则逐 token 真流式会退化
    return StreamingResponse(
        _sse_heartbeat(event_generator(), SSE_HEARTBEAT_SECONDS),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions", response_model=list[ChatSessionOut])
async def list_sessions(
    needs_human: bool | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = await query.list_chat_sessions_with_last_message(
        db,
        user_id=None if can_view_any_user(current_user) else current_user.id,
        needs_human=needs_human,
        handler_uid=current_user.id if (needs_human and can_view_any_user(current_user)) else None,
        limit=min(max(limit, 1), 200),
        offset=max(offset, 0),
    )
    return [
        ChatSessionOut(
            id=s.id,
            user_id=s.user_id,
            needs_human=s.needs_human,
            created_at=s.created_at,
            updated_at=s.updated_at,
            last_message=last_content,
            last_role=last_role,
            title=s.title,
            message_count=s.message_count,
            is_owner=s.user_id == current_user.id,
            username=username,
            human_handler_id=s.human_handler_id,
            handler_username=handler_username,
        )
        for s, last_content, last_role, username, handler_username in rows
    ]


@router.post("/sessions", response_model=ChatSessionOut)
async def create_session(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    session = ChatSession(user_id=current_user.id)
    db.add(session)
    await db.commit()
    logger.info("chat.session created session_id=%s user_id=%s", session.id, current_user.id)
    return ChatSessionOut(
        id=session.id,
        user_id=session.user_id,
        needs_human=session.needs_human,
        created_at=session.created_at,
        updated_at=session.updated_at,
        last_message=None,
        last_role=None,
        title=None,
        message_count=0,
        is_owner=True,
    )


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 越权统一 404 防探测，特权角色也不例外；别和 GET messages 的 403 混淆
    session = (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()
    if session is None or session.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    await db.execute(delete(Message).where(Message.session_id == session.id))
    await db.delete(session)
    await db.commit()
    checkpointer = get_checkpointer()
    if checkpointer is not None:
        thread_id = make_thread_id(session_id)
        try:
            await checkpointer.adelete_thread(thread_id)
            logger.info("memory.checkpoint_deleted thread_id=%s", thread_id)
        except Exception as exc:
            logger.warning("memory.checkpoint_delete_failed thread_id=%s error=%s", thread_id, exc)
    logger.info("chat.session deleted session_id=%s user_id=%s", session_id, current_user.id)
    return {"ok": True, "id": session_id}


@router.post("/sessions/{session_id}/needs-human")
async def flag_session_needs_human(
    session_id: int,
    req: NeedsHumanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_service_identity),
):
    session = (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()
    if session is None or session.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    session.needs_human = True
    session.needs_human_reason = req.reason[:500]
    await db.commit()
    logger.info(
        "chat.session flagged_needs_human session_id=%s user_id=%s priority=%s reason=%s",
        session_id, current_user.id, req.priority, _preview(req.reason),
    )
    return {"ok": True, "session_id": session_id, "needs_human": True}


@router.post("/sessions/{session_id}/resume")
async def resume_session(
    session_id: int,
    req: SessionResumeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(ROLE_CUSTOMER_SERVICE, ROLE_LEADER, ROLE_ADMIN)),
):
    started = time.perf_counter()
    session = (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != current_user.id and not can_view_any_user(current_user):
        raise HTTPException(status_code=403, detail="无权处理该会话")
    if not session.needs_human:
        raise HTTPException(status_code=409, detail="该会话已不在人工处理状态，可能已结束，列表已刷新")

    if session.human_handler_id is None:
        cas = await db.execute(
            update(ChatSession)
            .where(ChatSession.id == session.id, ChatSession.human_handler_id.is_(None))
            .values(human_handler_id=current_user.id)
        )
        await db.commit()
        if cas.rowcount:
            session.human_handler_id = current_user.id
        else:
            await db.refresh(session)
    if (
        session.human_handler_id is not None
        and session.human_handler_id != current_user.id
        and current_user.role not in (ROLE_LEADER, ROLE_ADMIN)
    ):
        holder = (
            await db.execute(select(User.username).where(User.id == session.human_handler_id))
        ).scalar_one_or_none()
        raise HTTPException(
            status_code=409,
            detail=f"该会话已由客服 {holder or session.human_handler_id} 接待中，请勿重复回复",
        )

    if req.decision:
        try:
            reply = await execute_pending_approval(
                db, session, current_user,
                approve=(req.decision == "approve"), comment=req.comment,
                approval_ref=req.approval_ref,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    else:
        reply = req.reply

    answer = reply
    delivery = "direct_write"
    msg = Message(session_id=session_id, role="assistant", content=answer)
    db.add(msg)
    session.message_count += 1
    session.updated_at = datetime.now()
    await db.commit()
    logger.info(
        "chat.session human_reply session_id=%s actor_id=%s decision=%s delivery=%s "
        "reply_chars=%d handler_id=%s elapsed_ms=%.1f",
        session_id, current_user.id, req.decision or "reply", delivery, len(answer),
        session.human_handler_id, (time.perf_counter() - started) * 1000,
    )
    return {
        "session_id": session_id,
        "answer": answer,
        "needs_human": True,
        "delivery": delivery,
        "handler_id": session.human_handler_id,
    }


@router.post("/sessions/{session_id}/end-handoff")
async def end_handoff(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    started = time.perf_counter()
    session = (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    is_owner = session.user_id == current_user.id
    if not is_owner and not can_view_any_user(current_user):
        raise HTTPException(status_code=403, detail="无权结束该会话的人工服务")

    if not session.needs_human:
        return {"session_id": session_id, "needs_human": False, "graph_closed": False, "already_ended": True}

    graph_closed = False
    if get_checkpointer() is not None:
        try:
            if await has_pending_interrupt(make_thread_id(session_id)):
                await resume_agent(make_thread_id(session_id), {"action": "close"})
                graph_closed = True
        except Exception:
            logger.exception("chat.end_handoff_close_failed session_id=%s", session_id)

    session.needs_human = False
    session.needs_human_reason = None
    session.human_handler_id = None
    msg = Message(session_id=session_id, role="assistant", content=HANDOFF_END_NOTICE)
    db.add(msg)
    session.message_count += 1
    session.updated_at = datetime.now()
    await db.flush()
    session.handoff_handled_message_id = msg.id
    await db.commit()
    logger.info(
        "chat.session handoff_ended session_id=%s actor_id=%s actor_type=%s graph_closed=%s elapsed_ms=%.1f",
        session_id, current_user.id,
        "user" if is_owner and not can_view_any_user(current_user) else "staff",
        graph_closed, (time.perf_counter() - started) * 1000,
    )
    return {"session_id": session_id, "needs_human": False, "graph_closed": graph_closed}


@router.get("/sessions/{session_id}/pending-approval")
async def get_pending_approval(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(ROLE_CUSTOMER_SERVICE, ROLE_LEADER, ROLE_ADMIN)),
):
    session = (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    payload = await find_pending_approval_payload(db, session)
    return payload or {}


@router.post("/sessions/{session_id}/agent-note")
@router.put("/sessions/{session_id}/agent-note")
async def set_agent_note(
    session_id: int,
    req: AgentNoteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_service_identity),
):
    outcome = await add_agent_note(
        db, current_user, session_id=session_id, note=req.note, actor_type="ai",
    )
    if outcome.level == "deny":
        raise HTTPException(status_code=outcome.http_status, detail=outcome.detail)
    return {"action": "agent_note", "level": outcome.level, "reasons": outcome.reasons,
            "session_id": session_id}


@router.get("/sessions/{session_id}/checkpoint-check")
async def checkpoint_check(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_leader_or_admin),
):
    session = (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    rows = await db.execute(
        select(Message.content)
        .where(Message.session_id == session_id, Message.role == "assistant")
        .order_by(Message.id.desc())
        .limit(1)
    )
    last_assistant = rows.scalar_one_or_none()
    report = await checkpoint_divergence_check(session_id, last_assistant)
    return report


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def get_session_messages(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    session = (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != current_user.id and not can_view_any_user(current_user):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    rows = await db.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.id)
    )
    return list(rows.scalars().all())
