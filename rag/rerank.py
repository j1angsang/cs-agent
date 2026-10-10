"""cross-encoder 重排客户端，模型 qwen3.7-text-rerank；失败或超时返回 None，调用方退回向量排序。"""
import logging
import os
import time

logger = logging.getLogger(__name__)

RERANK_ENABLED_ENV = "RERANK_ENABLED"
RERANK_MODEL_ENV = "RERANK_MODEL"
RERANK_TOP_N_ENV = "RERANK_TOP_N"
RERANK_MIN_SCORE_ENV = "RERANK_MIN_SCORE"
RERANK_TIMEOUT_ENV = "RERANK_TIMEOUT_SECONDS"

DEFAULT_MODEL = "qwen3.7-text-rerank"
DEFAULT_TOP_N = 20      # 库小，固定全量重排
DEFAULT_MIN_SCORE = 0.5
DEFAULT_TIMEOUT = 2.0

# 阈值 0.5 由标注集标定：正例最低 0.86、负例最高 0.36，换语料需重标。


def is_rerank_enabled() -> bool:
    return os.getenv(RERANK_ENABLED_ENV, "0").strip() == "1"


def get_model() -> str:
    return os.getenv(RERANK_MODEL_ENV, "").strip() or DEFAULT_MODEL


def get_top_n() -> int:
    raw = os.getenv(RERANK_TOP_N_ENV, "").strip()
    return int(raw) if raw else DEFAULT_TOP_N


def get_min_score() -> float:
    raw = os.getenv(RERANK_MIN_SCORE_ENV, "").strip()
    return float(raw) if raw else DEFAULT_MIN_SCORE


def get_timeout() -> float:
    raw = os.getenv(RERANK_TIMEOUT_ENV, "").strip()
    return float(raw) if raw else DEFAULT_TIMEOUT


def rerank_docs(query: str, documents: list[str]) -> list[dict] | None:
    if not documents:
        return []
    model = get_model()
    timeout = get_timeout()
    started = time.perf_counter()
    try:
        import dashscope

        dashscope.api_key = os.getenv("EMBEDDING_API_KEY", "")
        resp = dashscope.TextReRank.call(
            model=model,
            query=query,
            documents=list(documents),
            top_n=len(documents),
            instruct="Given a web search query, retrieve relevant passages that answer the query.",
            timeout=timeout,
        )
    except Exception as e:
        logger.warning(
            "rag.rerank query=%s candidates=%d model=%s elapsed_ms=%.1f total_tokens=0 fallback=1 error=%s",
            str(query)[:80], len(documents), model, (time.perf_counter() - started) * 1000, e,
        )
        return None

    elapsed_ms = (time.perf_counter() - started) * 1000
    if getattr(resp, "status_code", None) != 200:
        logger.warning(
            "rag.rerank query=%s candidates=%d model=%s elapsed_ms=%.1f total_tokens=0 fallback=1 "
            "code=%s message=%s",
            str(query)[:80], len(documents), model, elapsed_ms,
            getattr(resp, "code", "?"), getattr(resp, "message", "?"),
        )
        return None

    results = (getattr(resp, "output", None) or {}).get("results") or []
    hits: list[dict] = []
    for item in results:
        idx = int(item["index"])
        hits.append(
            {
                "index": idx,
                "text": documents[idx],
                "rerank_score": float(item["relevance_score"]),
            }
        )
    usage = getattr(resp, "usage", None)
    if isinstance(usage, dict):
        total_tokens = int(usage.get("total_tokens") or 0)
    else:
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
    logger.info(
        "rag.rerank query=%s candidates=%d elapsed_ms=%.1f model=%s total_tokens=%d fallback=0 "
        "top_score=%.4f",
        str(query)[:80], len(documents), elapsed_ms, model, total_tokens,
        hits[0]["rerank_score"] if hits else 0.0,
    )
    return hits
