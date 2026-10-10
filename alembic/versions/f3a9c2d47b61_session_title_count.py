"""chat_sessions 增加 title / message_count，并用 SQL 回填存量消息条数。"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a9c2d47b61"
down_revision: str | None = "381cf112ae72"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("title", sa.String(80), nullable=True))
    op.add_column(
        "chat_sessions",
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        "UPDATE chat_sessions SET message_count = "
        "(SELECT count(*) FROM messages WHERE messages.session_id = chat_sessions.id)"
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "message_count")
    op.drop_column("chat_sessions", "title")
