"""Схема базы `sayr_gorets`.

Сырые сообщения живут недолго (срок задаётся настройкой), итоги недель —
бессрочно. Авторов в базе нет: только HMAC от id.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Типы вложений. Медиа не скачиваем — только тип и имя файла.
MEDIA_TYPES = (
    "photo",
    "video",
    "document",
    "gpx",
    "kml",
    "voice",
    "sticker",
    "poll",
    "location",
    "venue",
    "other",
)


class Base(DeclarativeBase):
    pass


class Chat(Base):
    """Чаты, которые собираем: нужны, чтобы по username найти chat_id (forget-message)."""

    __tablename__ = "chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(Text)
    is_forum: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=func.now()
    )


class Message(Base):
    __tablename__ = "messages"

    # chat_id — «голый» id супергруппы (без префикса -100), одинаковый для API и выгрузки.
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    msg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    topic_id: Mapped[int | None] = mapped_column(BigInteger)
    topic_title: Mapped[str | None] = mapped_column(Text)
    reply_to_msg_id: Mapped[int | None] = mapped_column(BigInteger)
    author_hash: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    media_type: Mapped[str | None] = mapped_column(String(16))
    file_name: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[Decimal | None] = mapped_column(Numeric(7, 3))
    lng: Mapped[Decimal | None] = mapped_column(Numeric(7, 3))
    forwarded_from: Mapped[str | None] = mapped_column(Text)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=func.now()
    )
    # Отпечаток текста для дублей между чатами (см. fingerprint.py).
    fingerprint: Mapped[str | None] = mapped_column(String(32))
    # Полнотекстовый индекс считается базой из text: русская морфология.
    search = mapped_column(
        TSVECTOR, Computed("to_tsvector('russian', coalesce(text, ''))", persisted=True)
    )

    __table_args__ = (
        Index("ix_messages_date", "date"),
        Index("ix_messages_author_hash", "author_hash"),
        Index("ix_messages_chat_topic_date", "chat_id", "topic_id", "date"),
        Index("ix_messages_fingerprint", "fingerprint"),
        Index("ix_messages_search", "search", postgresql_using="gin"),
    )


class SyncState(Base):
    """Где остановилась догрузка истории (`backfill`) по каждому чату."""

    __tablename__ = "sync_state"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    backfill_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_last_id: Mapped[int | None] = mapped_column(BigInteger)
    backfill_done: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=func.now()
    )


class Run(Base):
    """Журнал прогонов: сбор, догрузка, импорт, разбор недели."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    new_messages: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_msg_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (Index("ix_runs_kind_started", "kind", "started_at"),)


class Digest(Base):
    """Итог недели: отчёт в Markdown, структурированный JSON, модели, токены, цена."""

    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    week: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    chunk_model: Mapped[str | None] = mapped_column(String(64))
    synthesis_model: Mapped[str | None] = mapped_column(String(64))
    batch_id: Mapped[str | None] = mapped_column(String(64))
    # Отпечаток нарезки: по нему понятно, можно ли забрать результаты старого батча.
    chunks_fingerprint: Mapped[str | None] = mapped_column(String(64))
    chunk_results: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    report_md: Mapped[str | None] = mapped_column(Text)
    report_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0, server_default="0")
    messages_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    authors_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=func.now(),
        onupdate=func.now(),
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Extraction(Base):
    """Результат извлекателя по одному сообщению; живёт дольше самого сообщения."""

    __tablename__ = "extractions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    extractor: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    msg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    topic_id: Mapped[int | None] = mapped_column(BigInteger)
    topic_title: Mapped[str | None] = mapped_column(Text)
    # ok — data заполнен; skipped — модель сочла сообщение не по теме; error — не разобрано.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("extractor", "chat_id", "msg_id", name="uq_extractions_message"),
        Index("ix_extractions_extractor_date", "extractor", "message_date"),
    )


class ExtractBatch(Base):
    """Отправленный, но ещё не забранный батч извлекателя — чтобы забрать его позже."""

    __tablename__ = "extract_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    extractor: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # custom_id → {chat_id, msg_id, date, topic_id, topic_title}
    items: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
