from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class RefundAuditLog(Base):
    __tablename__ = "refund_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    refund_id: Mapped[int] = mapped_column(Integer, ForeignKey("refunds.id"), nullable=False, index=True)
    actor_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_type: Mapped[str | None] = mapped_column(String(20), nullable=True, server_default="human_reviewer")
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    rules_hit: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    thresholds: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
