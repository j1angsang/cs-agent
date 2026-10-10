import os

from dotenv import load_dotenv
from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base

load_dotenv()

EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "") or 0)
if EMBEDDING_DIM <= 0:
    raise RuntimeError(
        "EMBEDDING_DIM 未配置或非法：请先用 rag.rag.default_ef 测得维度并写入 .env"
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            # m=16 / ef_construction=64 是 HNSW 索引参数，知识库量级小够用
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )
