"""新增列一律可空或带 server_default，upgrade/downgrade 成对。"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("urgent", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("orders", sa.Column("urgent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("address", sa.String(length=200), nullable=True))

    op.add_column("chat_sessions", sa.Column("needs_human_reason", sa.Text(), nullable=True))
    op.add_column("chat_sessions", sa.Column("agent_note", sa.Text(), nullable=True))

    op.add_column(
        "refund_audit_logs",
        sa.Column("actor_type", sa.String(length=20), nullable=True, server_default="human_reviewer"),
    )
    op.add_column("refund_audit_logs", sa.Column("session_id", sa.Integer(), nullable=True))
    op.create_index("ix_refund_audit_logs_session_id", "refund_audit_logs", ["session_id"])
    op.add_column("refund_audit_logs", sa.Column("rules_hit", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("refund_audit_logs", sa.Column("thresholds", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.create_table(
        "action_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("rules_hit", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_action_audit_logs_actor_id", "action_audit_logs", ["actor_id"])
    op.create_index("ix_action_audit_logs_action", "action_audit_logs", ["action"])
    op.create_index("ix_action_audit_logs_session_id", "action_audit_logs", ["session_id"])
    op.create_index("ix_action_audit_logs_result", "action_audit_logs", ["result"])

    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticket_no", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tickets_ticket_no", "tickets", ["ticket_no"], unique=True)
    op.create_index("ix_tickets_session_id", "tickets", ["session_id"])
    op.create_index("ix_tickets_user_id", "tickets", ["user_id"])
    # 幂等键：同会话同类型未关闭工单最多一张。
    op.create_index(
        "ix_tickets_session_type_open", "tickets", ["session_id", "type"],
        unique=True, postgresql_where=sa.text("status = 'open'"),
    )

    op.create_table(
        "user_coupons",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="valid"),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_user_coupons_user_id", "user_coupons", ["user_id"])
    op.create_index("ix_user_coupons_order_id", "user_coupons", ["order_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_user_coupons_order_id", table_name="user_coupons")
    op.drop_index("ix_user_coupons_user_id", table_name="user_coupons")
    op.drop_table("user_coupons")

    op.drop_index("ix_tickets_session_type_open", table_name="tickets")
    op.drop_index("ix_tickets_user_id", table_name="tickets")
    op.drop_index("ix_tickets_session_id", table_name="tickets")
    op.drop_index("ix_tickets_ticket_no", table_name="tickets")
    op.drop_table("tickets")

    op.drop_index("ix_action_audit_logs_result", table_name="action_audit_logs")
    op.drop_index("ix_action_audit_logs_session_id", table_name="action_audit_logs")
    op.drop_index("ix_action_audit_logs_action", table_name="action_audit_logs")
    op.drop_index("ix_action_audit_logs_actor_id", table_name="action_audit_logs")
    op.drop_table("action_audit_logs")

    op.drop_column("refund_audit_logs", "thresholds")
    op.drop_column("refund_audit_logs", "rules_hit")
    op.drop_index("ix_refund_audit_logs_session_id", table_name="refund_audit_logs")
    op.drop_column("refund_audit_logs", "session_id")
    op.drop_column("refund_audit_logs", "actor_type")

    op.drop_column("chat_sessions", "agent_note")
    op.drop_column("chat_sessions", "needs_human_reason")

    op.drop_column("orders", "address")
    op.drop_column("orders", "shipped_at")
    op.drop_column("orders", "urgent_at")
    op.drop_column("orders", "urgent")
