"""知识库封装：pgvector 存文本、按语义检索，供 init_rag 灌库与 rag_hook 检索。"""
import hashlib
import logging
import os
import time

from dotenv import load_dotenv
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from config.db import create_sync_engine
from models.knowledge_chunk import KnowledgeChunk
from rag.rerank import get_min_score, get_top_n, is_rerank_enabled, rerank_docs

load_dotenv()
logger = logging.getLogger(__name__)
KNOWLEDGE_DIR = "knowledge"

# 同步 engine：retrieve() 在 async graph 内同步调用，保持既有线程模型。
_engine = create_sync_engine()
_SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def _preview(value, limit: int = 80) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# 换 embedding 模型或大改知识库时改版本，或置 KNOWLEDGE_REBUILD=1。
KNOWLEDGE_VERSION = os.getenv("KNOWLEDGE_VERSION", "v2")
COLLECTION_NAME = f"knowledge_{KNOWLEDGE_VERSION}"


class DashScopeEmbeddingFunction:
    # embedding 是同步 HTTP，必须显式超时，否则挂住会占死调用线程。
    TIMEOUT_SECONDS = float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "10"))
    # 单请求上限 20 条，超出 API 报 BadRequest；知识库超过必须分批，否则 init_rag 起不来。
    MAX_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "20"))

    def __init__(self, api_key: str, model: str = ""):
        self._api_key = api_key
        self._model = model or os.getenv("EMBEDDING_MODEL", "").strip()
        if not self._model:
            raise RuntimeError("EMBEDDING_MODEL 未配置：请在 .env 指定与 knowledge_chunks.embedding 维度一致的模型")

    def name(self) -> str:
        return "multi_modal_embedding"

    def _embed(self, items: list[dict]) -> list[list[float]]:
        import dashscope
        resp = dashscope.MultiModalEmbedding.call(
            model=self._model,
            input=items,
            api_key=self._api_key,
            timeout=self.TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"embedding 调用失败: {resp.code} {resp.message}")
        vectors: list = [[] for _ in range(len(items))]
        for idx, item in enumerate(resp.output["embeddings"]):
            vectors[idx] = item["embedding"]
        return vectors

    def __call__(self, input: list[str]) -> list[list[float]]:
        items = [{"text": t} for t in input]
        vectors: list[list[float]] = []
        for i in range(0, len(items), self.MAX_BATCH_SIZE):
            vectors.extend(self._embed(items[i : i + self.MAX_BATCH_SIZE]))
        return vectors

    def embed_query(self, input: list[str]) -> list[list[float]]:
        return self.__call__(input)


_EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
# 模型名不允许兜底：兜底维度与库列 vector(EMBEDDING_DIM) 不一致，写库必失败，缺失即 fail-fast。
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "").strip()
if _EMBEDDING_API_KEY:
    if not EMBEDDING_MODEL:
        raise RuntimeError(
            "EMBEDDING_MODEL 未配置：pgvector 列是 vector(EMBEDDING_DIM)，"
            "必须显式指定与库列维度一致的 embedding 模型"
        )
    default_ef = DashScopeEmbeddingFunction(api_key=_EMBEDDING_API_KEY, model=EMBEDDING_MODEL)
    EMBEDDING_MODE = "API 多模态 embedding"
else:
    raise RuntimeError("EMBEDDING_API_KEY 未配置：pgvector 需要与 .env EMBEDDING_DIM 一致的向量")


def chunk_text(text: str, size: int = 200) -> list[str]:
    import re
    sentences = re.split(r"(?<=[。！？\n])", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    chunks, current = [], ""
    for s in sentences:
        if len(current) + len(s) <= size:
            current += s
        else:
            if current:
                chunks.append(current)
            current = s
    if current:
        chunks.append(current)
    return chunks


class PgVectorStore:
    def add_texts(self, texts: list[str], batch_id: str = "default") -> None:
        ids = [f"doc_{batch_id}_{i}" for i in range(len(texts))]
        with _SessionLocal() as db:
            existing = set(
                db.execute(
                    select(KnowledgeChunk.id).where(KnowledgeChunk.id.in_(ids))
                ).scalars().all()
            )
        new_texts = [(cid, text) for cid, text in zip(ids, texts) if cid not in existing]
        if not new_texts:
            return
        vectors = default_ef([text for _, text in new_texts])
        rows = [
            {"id": cid, "content": text, "embedding": vector}
            for (cid, text), vector in zip(new_texts, vectors)
        ]
        with _SessionLocal.begin() as db:
            db.execute(
                pg_insert(KnowledgeChunk)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["id"])
            )

    def search_raw(self, query: str, top_k: int = 3) -> tuple[list[str], list[float], int]:
        started = time.perf_counter()
        threshold_log = str(get_min_score()) if is_rerank_enabled() else "none"
        with _SessionLocal() as db:
            total = int(db.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0)
            if total == 0:
                logger.info(
                    "rag.search query=%s collection_total=0 hits=0 top_score=N/A "
                    "truncated=False truncate_reason=collection_empty score_threshold=%s duration_ms=%.1f",
                    _preview(query), threshold_log, (time.perf_counter() - started) * 1000,
                )
                return [], [], 0

            n_results = min(top_k, total)
            qv = default_ef([query])[0]
            distance = KnowledgeChunk.embedding.cosine_distance(qv)
            rows = db.execute(
                select(KnowledgeChunk.id, KnowledgeChunk.content, distance)
                .order_by(distance)
                .limit(n_results)
            ).all()

        ids = [row[0] for row in rows]
        docs = [row[1] for row in rows]
        scores = [1 - float(row[2]) / 2 for row in rows]
        top_score = f"{max(scores):.4f}" if scores else "N/A"
        truncated = total > len(docs)
        logger.info(
            "rag.search query=%s collection_total=%d top_k=%d hits=%d top_score=%s "
            "truncated=%s truncate_reason=%s score_threshold=%s duration_ms=%.1f",
            _preview(query), total, top_k, len(docs), top_score,
            truncated, "top_k_cap" if truncated else "none", threshold_log,
            (time.perf_counter() - started) * 1000,
        )
        for cid, doc, score in zip(ids, docs, scores):
            logger.info("rag.hit id=%s score=%.4f preview=%s", cid, score, _preview(doc))
        return docs, scores, total

    def search(self, query: str, top_k: int = 3) -> list[str]:
        docs, _, _ = self.search_raw(query, top_k)
        return docs

    def count(self) -> int:
        with _SessionLocal() as db:
            return int(db.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0)

    def rebuild(self) -> None:
        with _SessionLocal.begin() as db:
            db.execute(delete(KnowledgeChunk))


store = PgVectorStore()


def validate_embedding_dimension() -> None:
    from sqlalchemy import text as _sql_text

    with _SessionLocal() as db:
        col_dim = db.execute(_sql_text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'knowledge_chunks'::regclass AND attname = 'embedding'"
        )).scalar()
    if col_dim is None:
        raise RuntimeError("维度校验失败：knowledge_chunks.embedding 列不存在（迁移未执行？）")
    model_dim = len(default_ef(["embedding 维度校验探针"])[0])
    if int(col_dim) != int(model_dim):
        raise RuntimeError(
            f"embedding 维度不一致：模型 {EMBEDDING_MODEL} 输出 {model_dim} 维，"
            f"而 knowledge_chunks.embedding 是 vector({int(col_dim)})，"
            "EMBEDDING_MODEL/EMBEDDING_DIM 必须与库列一致"
        )
    logger.info("rag.embedding_dim_ok model=%s dim=%d", EMBEDDING_MODEL, int(model_dim))


def load_knowledge_docs() -> list[str]:
    docs = []
    for fname in sorted(os.listdir(KNOWLEDGE_DIR)):
        if fname.endswith(".txt"):
            with open(os.path.join(KNOWLEDGE_DIR, fname), encoding="utf-8") as f:
                docs.extend(line.strip() for line in f if line.strip())
    return docs


def _knowledge_batch_id() -> str:
    raw = "".join(load_knowledge_docs())
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]


def init_rag() -> None:
    validate_embedding_dimension()
    if os.getenv("KNOWLEDGE_REBUILD", "0") == "1":
        store.rebuild()
        logger.info("已按 KNOWLEDGE_REBUILD=1 重建 knowledge_chunks")

    batch_id = _knowledge_batch_id()
    all_chunks = []
    for doc in load_knowledge_docs():
        all_chunks.extend(chunk_text(doc))
    try:
        store.add_texts(all_chunks, batch_id=batch_id)
        logger.info(
            "RAG 就绪 collection=%s batch=%s chunks=%d embedding_mode=%s",
            COLLECTION_NAME, batch_id, len(all_chunks), EMBEDDING_MODE,
        )
    except Exception as e:
        logger.exception("RAG 初始化失败：%s", e)
        raise


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return int(default)
    return int(raw)


# 无命中必须返回非空引导文本，空串会让 rag_hook 返回 None，模型会硬答超范围题。
NO_HIT_REPLY = "知识库中没有与用户问题相关的内容。请如实告诉用户你无法从知识库确认，不要编造答案。"


def retrieve_hits(query: str, top_k: int | None = None) -> list[dict]:
    if top_k is None:
        top_k = _env_int("RAG_TOP_K", 3)
    started = time.perf_counter()

    rerank_on = is_rerank_enabled()
    fetch_k = max(top_k, get_top_n()) if rerank_on else top_k
    docs, scores, _total = store.search_raw(query, fetch_k)
    if not docs:
        return []

    hits = [
        {"text": doc, "vector_score": score, "rerank_score": None}
        for doc, score in zip(docs, scores)
    ]

    gate = "off"
    if rerank_on:
        reranked = rerank_docs(query, docs)
        if reranked is None:
            logger.warning(
                "rag.retrieve_hits rerank 失败退回原向量排序 query=%s candidates=%d",
                _preview(query), len(docs),
            )
            hits = hits[:top_k]
        else:
            min_score = get_min_score()
            gate = f"rerank>={min_score}"
            hits = [
                {
                    "text": r["text"],
                    "vector_score": scores[r["index"]],
                    "rerank_score": r["rerank_score"],
                }
                for r in reranked
                if r["rerank_score"] >= min_score
            ][:top_k]

    logger.info(
        "rag.retrieve_hits query=%s candidates=%d kept=%d gate=%s rerank=%s duration_ms=%.1f",
        _preview(query), len(docs), len(hits), gate, "on" if rerank_on else "off",
        (time.perf_counter() - started) * 1000,
    )
    return hits


def retrieve(query: str, top_k: int | None = None) -> str:
    if top_k is None:
        top_k = _env_int("RAG_TOP_K", 3)
    started = time.perf_counter()
    try:
        hits = retrieve_hits(query, top_k)
    except Exception:
        logger.exception("rag.retrieve 检索失败，降级 query=%s top_k=%d", _preview(query), top_k)
        return "知识库检索服务暂时不可用；请明确告诉用户暂时无法从知识库确认，不要编造答案。"
    text = "\n".join(f"[{i}] {h['text']}" for i, h in enumerate(hits, 1))
    if not text:
        text = NO_HIT_REPLY
    logger.info(
        "rag.retrieve query=%s hits=%d returned_chars=%d duration_ms=%.1f preview=%s",
        _preview(query), len(hits), len(text),
        (time.perf_counter() - started) * 1000, _preview(text),
    )
    return text
