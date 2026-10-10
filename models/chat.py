from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, false, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    needs_human: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    title: Mapped[str | None] = mapped_column(String(80), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    needs_human_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 转人工判定只统计此 id 之后的消息，防旧消息里的触发词让下一条普通消息重复转人工
    handoff_handled_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    human_handler_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    agent_note: Mapped[str | None] = mapped_column(Text, nullable=True)
