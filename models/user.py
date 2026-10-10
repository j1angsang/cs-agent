from datetime import datetime
from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


ROLE_USER = "user"
ROLE_CUSTOMER_SERVICE = "customer_service"
ROLE_LEADER = "leader"
ROLE_ADMIN = "admin"

PRIVILEGED_ROLES = {ROLE_CUSTOMER_SERVICE, ROLE_LEADER, ROLE_ADMIN}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 用户名大小写不敏感，必须用 CITEXT，改回普通字符串会破坏旧库语义
    username: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_USER, server_default=ROLE_USER)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
