"""Извлечения по сообщениям фидов и незабранные батчи извлекателей.

Ревизия: 0002
Предыдущая: 0001
Создана: 2026-10-06 05:48:40.614204+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extract_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("extractor", sa.String(length=64), nullable=False),
        sa.Column("batch_id", sa.String(length=64), nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id"),
    )
    op.create_table(
        "extractions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("extractor", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("msg_id", sa.BigInteger(), nullable=False),
        sa.Column("message_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("topic_id", sa.BigInteger(), nullable=True),
        sa.Column("topic_title", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("extractor", "chat_id", "msg_id", name="uq_extractions_message"),
    )
    op.create_index(
        "ix_extractions_extractor_date", "extractions", ["extractor", "message_date"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_extractions_extractor_date", table_name="extractions")
    op.drop_table("extractions")
    op.drop_table("extract_batches")
