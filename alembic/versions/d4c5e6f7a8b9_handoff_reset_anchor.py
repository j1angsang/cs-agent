"""转人工判定只看 handoff_handled_message_id 之后的消息，避免旧触发词反复转人工。"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4c5e6f7a8b9"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("handoff_handled_message_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "handoff_handled_message_id")
