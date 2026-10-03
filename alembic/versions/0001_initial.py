"""Начальная схема: чаты, сообщения, состояние догрузки, журнал прогонов, итоги недель.

Ревизия: 0001
Предыдущая:
Создана: 2026-10-03 12:12:33.762611+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("is_forum", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("chat_id"),
    )
    op.create_table(
        "digests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("week", sa.String(length=10), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("chunk_model", sa.String(length=64), nullable=True),
        sa.Column("synthesis_model", sa.String(length=64), nullable=True),
        sa.Column("batch_id", sa.String(length=64), nullable=True),
        sa.Column("chunks_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("chunk_results", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("report_md", sa.Text(), nullable=True),
        sa.Column("report_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "cost_usd", sa.Numeric(precision=10, scale=4), server_default="0", nullable=False
        ),
        sa.Column("messages_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("authors_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("week"),
    )
    op.create_table(
        "messages",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("msg_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("topic_id", sa.BigInteger(), nullable=True),
        sa.Column("topic_title", sa.Text(), nullable=True),
        sa.Column("reply_to_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("author_hash", sa.String(length=64), nullable=True),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("lat", sa.Numeric(precision=7, scale=3), nullable=True),
        sa.Column("lng", sa.Numeric(precision=7, scale=3), nullable=True),
        sa.Column("forwarded_from", sa.Text(), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("chat_id", "msg_id"),
    )
    op.create_index("ix_messages_author_hash", "messages", ["author_hash"], unique=False)
    op.create_index(
        "ix_messages_chat_topic_date", "messages", ["chat_id", "topic_id", "date"], unique=False
    )
    op.create_index("ix_messages_date", "messages", ["date"], unique=False)
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("new_messages", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_runs_kind_started", "runs", ["kind", "started_at"], unique=False)
    op.create_table(
        "sync_state",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("backfill_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backfill_last_id", sa.BigInteger(), nullable=True),
        sa.Column("backfill_done", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("chat_id"),
    )


def downgrade() -> None:
    op.drop_table("sync_state")
    op.drop_index("ix_runs_kind_started", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_messages_date", table_name="messages")
    op.drop_index("ix_messages_chat_topic_date", table_name="messages")
    op.drop_index("ix_messages_author_hash", table_name="messages")
    op.drop_table("messages")
    op.drop_table("digests")
    op.drop_table("chats")
