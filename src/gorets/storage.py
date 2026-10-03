"""Работа с базой: запись сообщений без дублей, сроки хранения, удаление по просьбе, журнал."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from gorets.db import make_session_factory
from gorets.models import Chat, Digest, Message, Run, SyncState

# Поля сообщения, которые обновляются при повторной записи. Время сбора не
# трогаем: оно отвечает на вопрос «когда это впервые попало к нам».
_MESSAGE_UPDATABLE = (
    "date",
    "topic_id",
    "topic_title",
    "reply_to_msg_id",
    "author_hash",
    "text",
    "media_type",
    "file_name",
    "lat",
    "lng",
    "forwarded_from",
    "edited_at",
)


class Repository:
    """Все обращения к базе проходят здесь — остальной код о SQL не знает."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessions = make_session_factory(engine)

    def session(self) -> Session:
        return self._sessions()

    # --- чаты ------------------------------------------------------------------

    def upsert_chat(
        self, chat_id: int, *, username: str | None, title: str | None, is_forum: bool
    ) -> None:
        stmt = insert(Chat).values(
            chat_id=chat_id, username=username, title=title, is_forum=is_forum
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Chat.chat_id],
            set_={
                "username": stmt.excluded.username,
                "title": stmt.excluded.title,
                "is_forum": stmt.excluded.is_forum,
                "updated_at": func.now(),
            },
        )
        with self.session() as s, s.begin():
            s.execute(stmt)

    def resolve_chat_id(self, chat: str) -> int | None:
        """Числовой id чата по username или строке с числом (поддерживается форма -100…)."""
        stripped = chat.strip().lstrip("@")
        if stripped.lstrip("-").isdigit():
            return normalize_chat_id(int(stripped))
        with self.session() as s:
            row = s.execute(
                select(Chat.chat_id).where(func.lower(Chat.username) == stripped.lower())
            ).first()
        return row[0] if row else None

    # --- сообщения ---------------------------------------------------------------

    def upsert_messages(self, rows: Sequence[dict[str, Any]]) -> int:
        """Записать сообщения; вернуть, сколько из них новых.

        Повторный прогон ничего не дублирует: ключ — (chat_id, msg_id).
        Число новых считается по `xmax = 0`: у только что вставленной строки
        Postgres ещё не выставил xmax, у обновлённой — выставил.
        """
        if not rows:
            return 0
        stmt = insert(Message).values(list(rows))
        stmt = stmt.on_conflict_do_update(
            index_elements=[Message.chat_id, Message.msg_id],
            set_={name: getattr(stmt.excluded, name) for name in _MESSAGE_UPDATABLE},
        ).returning(text("(xmax = 0) AS inserted"))
        with self.session() as s, s.begin():
            result = s.execute(stmt)
            return sum(1 for (inserted,) in result if inserted)

    def last_msg_id(self, chat_id: int) -> int:
        with self.session() as s:
            value = s.execute(
                select(func.max(Message.msg_id)).where(Message.chat_id == chat_id)
            ).scalar()
        return int(value or 0)

    def count_messages(self, chat_id: int | None = None) -> int:
        stmt = select(func.count()).select_from(Message)
        if chat_id is not None:
            stmt = stmt.where(Message.chat_id == chat_id)
        with self.session() as s:
            return int(s.execute(stmt).scalar() or 0)

    def messages_between(self, start: datetime, end: datetime) -> list[Message]:
        """Сообщения за период [start, end) по всем чатам, по порядку дат."""
        with self.session() as s:
            rows = s.execute(
                select(Message)
                .where(Message.date >= start, Message.date < end)
                .order_by(Message.chat_id, Message.date, Message.msg_id)
            ).scalars()
            return list(rows)

    def delete_older_than(self, days: int, now: datetime | None = None) -> int:
        """Чистка по сроку хранения: вернуть число удалённых."""
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=days)
        with self.session() as s, s.begin():
            result = s.execute(delete(Message).where(Message.date < cutoff))
            return int(result.rowcount or 0)

    def forget_author(self, author_hash: str) -> int:
        with self.session() as s, s.begin():
            result = s.execute(delete(Message).where(Message.author_hash == author_hash))
            return int(result.rowcount or 0)

    def forget_message(self, chat_id: int, msg_id: int) -> int:
        with self.session() as s, s.begin():
            result = s.execute(
                delete(Message).where(Message.chat_id == chat_id, Message.msg_id == msg_id)
            )
            return int(result.rowcount or 0)

    # --- состояние догрузки ------------------------------------------------------

    def get_sync_state(self, chat_id: int) -> SyncState | None:
        with self.session() as s:
            return s.get(SyncState, chat_id)

    def set_sync_state(
        self,
        chat_id: int,
        *,
        since: datetime | None,
        last_id: int | None,
        done: bool,
    ) -> None:
        stmt = insert(SyncState).values(
            chat_id=chat_id, backfill_since=since, backfill_last_id=last_id, backfill_done=done
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[SyncState.chat_id],
            set_={
                "backfill_since": stmt.excluded.backfill_since,
                "backfill_last_id": stmt.excluded.backfill_last_id,
                "backfill_done": stmt.excluded.backfill_done,
                "updated_at": func.now(),
            },
        )
        with self.session() as s, s.begin():
            s.execute(stmt)

    # --- журнал прогонов ---------------------------------------------------------

    def start_run(self, kind: str, chat_id: int | None = None) -> int:
        with self.session() as s, s.begin():
            run = Run(kind=kind, chat_id=chat_id, status="running")
            s.add(run)
            s.flush()
            return run.id

    def update_run(
        self,
        run_id: int,
        *,
        new_messages: int | None = None,
        last_msg_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {}
        if new_messages is not None:
            values["new_messages"] = new_messages
        if last_msg_id is not None:
            values["last_msg_id"] = last_msg_id
        if details is not None:
            values["details"] = details
        if not values:
            return
        with self.session() as s, s.begin():
            s.execute(update(Run).where(Run.id == run_id).values(**values))

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        error: str | None = None,
        new_messages: int | None = None,
        last_msg_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status, "finished_at": func.now(), "error": error}
        if new_messages is not None:
            values["new_messages"] = new_messages
        if last_msg_id is not None:
            values["last_msg_id"] = last_msg_id
        if details is not None:
            values["details"] = details
        with self.session() as s, s.begin():
            s.execute(update(Run).where(Run.id == run_id).values(**values))

    def recent_runs(self, limit: int = 10) -> list[Run]:
        with self.session() as s:
            return list(
                s.execute(select(Run).order_by(Run.started_at.desc()).limit(limit)).scalars()
            )

    # --- итоги недель -------------------------------------------------------------

    def get_digest(self, week: str) -> Digest | None:
        with self.session() as s:
            return s.execute(select(Digest).where(Digest.week == week)).scalar_one_or_none()

    def save_digest(self, week: str, **fields: Any) -> Digest:
        """Создать или обновить итог недели (одна строка на неделю)."""
        with self.session() as s, s.begin():
            digest = s.execute(select(Digest).where(Digest.week == week)).scalar_one_or_none()
            if digest is None:
                digest = Digest(week=week, **fields)
                s.add(digest)
            else:
                for name, value in fields.items():
                    setattr(digest, name, value)
            s.flush()
            s.refresh(digest)
            return digest

    def list_digests(self, limit: int = 10) -> list[Digest]:
        with self.session() as s:
            return list(
                s.execute(select(Digest).order_by(Digest.week.desc()).limit(limit)).scalars()
            )

    # --- статистика ---------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self.session() as s:
            total = s.execute(select(func.count()).select_from(Message)).scalar() or 0
            span = s.execute(select(func.min(Message.date), func.max(Message.date))).one()
            per_chat = s.execute(
                select(
                    Message.chat_id,
                    Chat.username,
                    func.count(),
                    func.max(Message.msg_id),
                )
                .outerjoin(Chat, Chat.chat_id == Message.chat_id)
                .group_by(Message.chat_id, Chat.username)
                .order_by(Message.chat_id)
            ).all()
            authors = (
                s.execute(select(func.count(func.distinct(Message.author_hash)))).scalar() or 0
            )
            digests = s.execute(select(func.count()).select_from(Digest)).scalar() or 0
        return {
            "messages": int(total),
            "authors": int(authors),
            "first_date": span[0],
            "last_date": span[1],
            "chats": [
                {"chat_id": cid, "username": username, "messages": int(n), "last_msg_id": last}
                for cid, username, n, last in per_chat
            ],
            "digests": int(digests),
        }


def normalize_chat_id(value: int) -> int:
    """«Голый» id чата: -1001234567890 → 1234567890, -123 → 123, 123 → 123."""
    value = int(value)
    if value < 0:
        value = -value
        if str(value).startswith("100") and value > 10**12:
            value = value - 10**12
    return value


def as_decimal_coord(value: float | None) -> Decimal | None:
    """Координата, округлённая до трёх знаков (около 100 м) — точнее для разбора не нужно."""
    if value is None:
        return None
    return Decimal(str(round(float(value), 3)))


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
