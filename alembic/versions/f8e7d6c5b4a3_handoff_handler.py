"""chat_sessions.human_handler_id：首位回复的客服认领会话，人工接管为持续状态。"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8e7d6c5b4a3"
down_revision: str | None = "d4c5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("human_handler_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "human_handler_id")
