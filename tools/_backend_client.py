import logging
import os
import time

import httpx
from dotenv import load_dotenv

from core.metrics import TOOL_FAILURE_TOTAL
from tools import _service_auth as service_auth

load_dotenv()

logger = logging.getLogger(__name__)

TOOL_HTTP_TIMEOUT = float(os.getenv("TOOL_HTTP_TIMEOUT", "10"))
TOOL_RETRIES = int(os.getenv("TOOL_RETRIES", "2"))
TOOL_RETRY_DELAYS = (0.5, 1.0)

# 会话失败计数供 routers/chat.py 判断是否接入人工；有界，超出淘汰最早的 key，防长跑进程内存无限增长
TOOL_FAILURE_COUNTS: dict[int, int] = {}
TOOL_FAILURE_MAX_SESSIONS = 1000


def _preview(value, limit: int = 160) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _record_tool_failure(caller: dict | None) -> None:
    if caller and caller.get("session_id"):
        TOOL_FAILURE_TOTAL.inc()
        key = int(caller["session_id"])
        if key not in TOOL_FAILURE_COUNTS and len(TOOL_FAILURE_COUNTS) >= TOOL_FAILURE_MAX_SESSIONS:
            oldest = next(iter(TOOL_FAILURE_COUNTS))
            TOOL_FAILURE_COUNTS.pop(oldest, None)
        TOOL_FAILURE_COUNTS[key] = TOOL_FAILURE_COUNTS.get(key, 0) + 1


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    if code is not None:
        try:
            return int(code)
        except (TypeError, ValueError):
            return None
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if code is not None:
        try:
            return int(code)
        except (TypeError, ValueError):
            return None
    return None


def _is_retryable(exc: BaseException) -> bool:
    code = _status_code(exc)
    if code is not None:
        return 500 <= code < 600
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, TimeoutError, ConnectionError, OSError)):
        return True
    msg = str(exc).lower()
    return any(
        key in msg
        for key in ("timeout", "timed out", "connection", "connect", "500", "502", "503", "504", "temporarily", "unavailable")
    )


def _retry_reason(exc: BaseException) -> str:
    code = _status_code(exc)
    if code is not None:
        return f"http_{code}"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if isinstance(exc, (httpx.TransportError, ConnectionError, OSError)):
        return "connection"
    return type(exc).__name__


def _retry_decision(exc: BaseException, attempt: int, attempts: int) -> tuple[bool, float | None]:
    if not (_is_retryable(exc) and attempt < attempts - 1):
        return False, None
    delay = TOOL_RETRY_DELAYS[min(attempt, len(TOOL_RETRY_DELAYS) - 1)]
    return True, delay


def _final_error_result(exc: BaseException, *, caller: dict | None,
                        tool_name: str | None, attempts: int, started: float) -> dict:
    retryable = _is_retryable(exc)
    code = _status_code(exc)
    elapsed_ms = (time.perf_counter() - started) * 1000
    _record_tool_failure(caller)
    if code is not None:
        logger.error(
            "tool_retry_final tool=%s attempts=%d retryable=%s status=%d elapsed_ms=%.1f error=%s",
            tool_name or "-", attempts, retryable, code, elapsed_ms, exc,
        )
    else:
        logger.exception(
            "tool_retry_final tool=%s attempts=%d retryable=%s elapsed_ms=%.1f error=%s",
            tool_name or "-", attempts, retryable, elapsed_ms, exc,
        )
    if code == 403:
        return {"error": "无权查询该订单/退款"}
    if code == 404:
        return {"error": "未找到对应的订单/退款/商品"}
    return {"error": "系统繁忙，请稍后再试"}


def post(path: str, caller: dict | None = None, *, json_body: dict,
         idempotency_key: str, base_url: str,
         tool_name: str | None = None, tool_args: dict | None = None) -> dict:
    started = time.perf_counter()
    # 仅带幂等键才允许重试，attempts = 重试上限 + 1
    attempts = 2 if idempotency_key else 1
    headers = service_auth.build_headers(caller, write=True)
    headers["Idempotency-Key"] = idempotency_key
    for attempt in range(attempts):
        try:
            with httpx.Client(timeout=TOOL_HTTP_TIMEOUT) as client:
                resp = client.post(f"{base_url}{path}", json=json_body, headers=headers)
                resp.raise_for_status()
                elapsed_ms = (time.perf_counter() - started) * 1000
                logger.info(
                    "工具调用完成 tool=%s status=%d path=%s elapsed_ms=%.1f result=%s",
                    tool_name or "-", resp.status_code, path, elapsed_ms, _preview(resp.text),
                )
                return resp.json()
        except Exception as e:
            code = _status_code(e)
            retry, delay = _retry_decision(e, attempt, attempts)
            if retry:
                logger.warning(
                    "tool_retry tool=%s attempt=%d/%d reason=%s delay_s=%.1f error=%s",
                    tool_name or "-", attempt + 1, attempts - 1, _retry_reason(e), delay, e,
                )
                time.sleep(delay)
                continue
            if code is not None and code < 500:
                # 4xx 是业务结果，detail 是既有面向用户话术，直接透传
                detail = None
                response = getattr(e, "response", None)
                if response is not None:
                    try:
                        detail = response.json().get("detail")
                    except Exception:
                        detail = None
                logger.info(
                    "工具调用完成 tool=%s status=%d path=%s elapsed_ms=%.1f",
                    tool_name or "-", code, path, (time.perf_counter() - started) * 1000,
                )
                if code == 403:
                    return {"error": detail or "无权执行该操作"}
                if code == 404:
                    return {"error": detail or "未找到对应的资源"}
                return {"error": detail or "系统繁忙，请稍后再试"}
            result = _final_error_result(
                e, caller=caller, tool_name=tool_name, attempts=attempts, started=started,
            )
            logger.exception(
                "工具调用后端异常 path=%s tool=%s args=%s elapsed_ms=%.1f",
                path, tool_name or "-", tool_args or {}, (time.perf_counter() - started) * 1000,
            )
            return result
    _record_tool_failure(caller)
    logger.error("tool_retry_final tool=%s attempts=%d 未知失败", tool_name or "-", attempts)
    return {"error": "系统繁忙，请稍后再试"}


def get(path: str, caller: dict | None = None, *, base_url: str,
        tool_name: str | None = None, tool_args: dict | None = None) -> dict:
    started = time.perf_counter()
    attempts = TOOL_RETRIES + 1
    for attempt in range(attempts):
        try:
            with httpx.Client(timeout=TOOL_HTTP_TIMEOUT) as client:
                resp = client.get(f"{base_url}{path}", headers=service_auth.build_headers(caller))
                resp.raise_for_status()
                elapsed_ms = (time.perf_counter() - started) * 1000
                try:
                    result_preview = _preview(resp.text)
                except Exception:
                    result_preview = "<response preview unavailable>"
                logger.info(
                    "工具调用成功 tool=%s args=%s status=%s path=%s elapsed_ms=%.1f result=%s",
                    tool_name or "-", tool_args or {}, resp.status_code, path, elapsed_ms,
                    result_preview,
                )
                return resp.json()
        except Exception as e:
            retry, delay = _retry_decision(e, attempt, attempts)
            if retry:
                logger.warning(
                    "tool_retry tool=%s attempt=%d/%d reason=%s delay_s=%.1f error=%s",
                    tool_name or "-", attempt + 1, TOOL_RETRIES, _retry_reason(e), delay, e,
                )
                time.sleep(delay)
                continue
            result = _final_error_result(
                e, caller=caller, tool_name=tool_name, attempts=attempts, started=started,
            )
            if not isinstance(e, httpx.HTTPStatusError):
                logger.exception(
                    "工具调用后端异常 path=%s tool=%s args=%s elapsed_ms=%.1f",
                    path, tool_name or "-", tool_args or {}, (time.perf_counter() - started) * 1000,
                )
            return result
    _record_tool_failure(caller)
    logger.error("tool_retry_final tool=%s attempts=%d 未知失败", tool_name or "-", attempts)
    return {"error": "系统繁忙，请稍后再试"}
