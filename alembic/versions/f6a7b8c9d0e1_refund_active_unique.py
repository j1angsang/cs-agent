"""refunds 部分唯一索引：同一订单最多一笔进行中退款，冲突在应用层翻译成 409。"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_refunds_order_id_active",
        "refunds",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'approved')"),
    )


def downgrade() -> None:
    op.drop_index("ix_refunds_order_id_active", table_name="refunds")
