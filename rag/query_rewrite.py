import logging
import os
import time

logger = logging.getLogger(__name__)

INDICATOR_WORDS = ("这个", "那个", "它", "该", "此", "上面", "刚才", "那", "它们")

# ≥12 字且无指代词视为信息量足够，直接检索不改写。
SHORT_QUERY_CHARS = 12

MAX_QUERY_CHARS = 200

REWRITE_ENABLED_ENV = "QUERY_REWRITE_ENABLED"
REWRITE_MODEL_ENV = "QUERY_REWRITE_MODEL"
REWRITE_TIMEOUT_ENV = "QUERY_REWRITE_TIMEOUT_SECONDS"
REWRITE_HISTORY_ENV = "QUERY_REWRITE_HISTORY_TURNS"

DEFAULT_TIMEOUT = 2.0
DEFAULT_HISTORY_TURNS = 2

_PROMPT = """你是检索查询改写器。根据最近的对话历史，把用户的最新问题改写成一条独立、完整、适合知识库检索的查询。

硬性要求：
1. 只补全指代与省略（如"这个/那个/它"指代的东西、省略的主语），不得改变用户意图；
2. 不得引入用户没说过的实体、品牌、数字或新问题；
3. 输出语言与用户问题保持一致；
4. 如果上下文不足以确定指代，或问题本来就完整，原样输出用户问题；
5. 只输出一行查询文本，不要解释、不要引号、不要 markdown。

对话历史（可能为空）：
{history}

用户最新问题：{question}

改写后的查询："""


def is_rewrite_enabled() -> bool:
    return os.getenv(REWRITE_ENABLED_ENV, "0").strip() == "1"


def get_history_turns() -> int:
    raw = os.getenv(REWRITE_HISTORY_ENV, "").strip()
    return int(raw) if raw else DEFAULT_HISTORY_TURNS


def _get_timeout() -> float:
    raw = os.getenv(REWRITE_TIMEOUT_ENV, "").strip()
    return float(raw) if raw else DEFAULT_TIMEOUT


def should_rewrite(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    if any(w in text for w in INDICATOR_WORDS):
        return True
    return len(text) < SHORT_QUERY_CHARS


def extract_history_pairs(messages, last_human_idx: int, turns: int) -> list[tuple[str, str]]:
    from langchain_core.messages import AIMessage, HumanMessage

    pairs: list[tuple[str, str]] = []
    pending_user: str | None = None
    for msg in messages[:last_human_idx]:
        if isinstance(msg, HumanMessage):
            pending_user = str(msg.content)
        elif isinstance(msg, AIMessage) and pending_user is not None:
            pairs.append((pending_user, str(msg.content)))
            pending_user = None
    return pairs[-turns:] if turns > 0 else []


def _build_llm():
    from langchain_openai import ChatOpenAI

    model = os.getenv(REWRITE_MODEL_ENV, "").strip() \
        or os.getenv("LLM_MODEL", "").strip()
    base_url = os.getenv("QUERY_REWRITE_BASE_URL", "").strip() \
        or os.getenv("LLM_BASE_URL", "").strip()
    api_key = os.getenv("QUERY_REWRITE_API_KEY", "").strip() \
        or os.getenv("LLM_API_KEY", "").strip()
    if not model or not api_key:
        raise RuntimeError("QUERY_REWRITE 回落配置缺失：LLM_MODEL/LLM_API_KEY 未配置")
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url or None,
        temperature=0,
        timeout=_get_timeout(),
        max_retries=0,
        max_tokens=100,
    )


def rewrite_query(user_text: str, history_pairs: list[tuple[str, str]] | None = None) -> str:
    original = (user_text or "").strip()
    if not is_rewrite_enabled():
        return original
    if not should_rewrite(original):
        logger.info(
            "rag.rewrite skip original=%s changed=false fallback=0 reason=no_indicator_and_long_enough",
            original[:80],
        )
        return original

    started = time.perf_counter()
    try:
        history_text = ""
        for i, (u, a) in enumerate((history_pairs or [])[-get_history_turns():], 1):
            history_text += f"轮{i} 用户：{u[:100]}\n轮{i} 客服：{a[:100]}\n"
        if not history_text:
            history_text = "（无）"

        prompt = _PROMPT.format(history=history_text, question=original)
        # 改写模型 token 绝不能漏进用户可见 SSE，必须打 INTERNAL_MODEL_TAG。
        from rag._internal import INTERNAL_MODEL_TAG

        resp = _build_llm().with_config(tags=[INTERNAL_MODEL_TAG]).invoke([("user", prompt)])

        rewritten = str(resp.content).strip()
        for wrap in ('"', "'", "`"):
            if rewritten.startswith(wrap) and rewritten.endswith(wrap) and len(rewritten) >= 2:
                rewritten = rewritten[1:-1].strip()
        rewritten = rewritten.splitlines()[0].strip() if rewritten else ""

        elapsed_ms = (time.perf_counter() - started) * 1000
        usage = getattr(resp, "usage_metadata", None) or {}
        tokens = int(usage.get("total_tokens", 0) or 0) if isinstance(usage, dict) else 0

        if not rewritten or len(rewritten) > MAX_QUERY_CHARS:
            logger.warning(
                "rag.rewrite original=%s rewritten=%s changed=false elapsed_ms=%.1f tokens=%d "
                "fallback=1 reason=empty_or_too_long",
                original[:80], rewritten[:80], elapsed_ms, tokens,
            )
            return original

        logger.info(
            "rag.rewrite original=%s rewritten=%s changed=%s elapsed_ms=%.1f tokens=%d fallback=0",
            original[:80], rewritten[:80], rewritten != original, elapsed_ms, tokens,
        )
        return rewritten or original
    except Exception as e:
        logger.warning(
            "rag.rewrite original=%s changed=false elapsed_ms=%.1f tokens=0 fallback=1 error=%s",
            original[:80], (time.perf_counter() - started) * 1000, e,
        )
        return original
