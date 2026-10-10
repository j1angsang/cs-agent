"""refund_audit_logs：退款审核动作审计表，与状态变更同事务各插一行。"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refund_audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("refund_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["refund_id"], ["refunds.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_refund_audit_logs_refund_id"), "refund_audit_logs", ["refund_id"])
    op.create_index(op.f("ix_refund_audit_logs_actor_id"), "refund_audit_logs", ["actor_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_refund_audit_logs_actor_id"), table_name="refund_audit_logs")
    op.drop_index(op.f("ix_refund_audit_logs_refund_id"), table_name="refund_audit_logs")
    op.drop_table("refund_audit_logs")
