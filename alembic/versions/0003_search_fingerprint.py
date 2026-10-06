"""Полнотекстовый поиск и отпечаток текста у сообщений.

Ревизия: 0003
Предыдущая: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("fingerprint", sa.String(length=32), nullable=True))
    op.add_column(
        "messages",
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('russian', coalesce(text, ''))", persisted=True),
            nullable=True,
        ),
    )
    op.create_index("ix_messages_fingerprint", "messages", ["fingerprint"])
    op.create_index("ix_messages_search", "messages", ["search"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_messages_search", table_name="messages")
    op.drop_index("ix_messages_fingerprint", table_name="messages")
    op.drop_column("messages", "search")
    op.drop_column("messages", "fingerprint")
