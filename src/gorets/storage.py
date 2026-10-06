"""Работа с базой: запись сообщений без дублей, сроки хранения, удаление по просьбе, журнал."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, and_, delete, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from gorets.db import make_session_factory
from gorets.models import Chat, Digest, ExtractBatch, Extraction, Message, Run, SyncState

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
    "fingerprint",
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

    def list_chats(self) -> list[Chat]:
        with self.session() as s:
            return list(s.execute(select(Chat).order_by(Chat.chat_id)).scalars())

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
        # У многострочного INSERT ключи берутся из первой строки: выравниваем
        # все строки по общему набору ключей, чтобы ничего не потерялось молча.
        keys = sorted({k for row in rows for k in row})
        aligned = [{k: row.get(k) for k in keys} for row in rows]
        stmt = insert(Message).values(aligned)
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

    # --- фиды и извлечения --------------------------------------------------------

    def get_chat(self, chat_id: int) -> Chat | None:
        with self.session() as s:
            return s.get(Chat, chat_id)

    @staticmethod
    def _topic_filter(topics: Sequence[str | int]):
        if not topics:
            return None
        ids = [t for t in topics if isinstance(t, int)]
        titles = [t.strip().lower() for t in topics if isinstance(t, str)]
        clauses = []
        if ids:
            clauses.append(Message.topic_id.in_(ids))
        for title in titles:
            # По началу названия, как в FeedConfig.topic_matches.
            escaped = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(func.lower(func.trim(Message.topic_title)).like(escaped + "%"))
        return or_(*clauses)

    def feed_messages(
        self,
        chat_id: int,
        topics: Sequence[str | int] = (),
        *,
        after_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        q: str | None = None,
        limit: int = 100,
        dedupe: bool = False,
    ) -> list[Message]:
        """Сообщения фида по возрастанию id: курсор потребителя — последний msg_id."""
        stmt = select(Message).where(Message.chat_id == chat_id)
        if dedupe:
            stmt = stmt.where(self._not_duplicate())
        topic_filter = self._topic_filter(topics)
        if topic_filter is not None:
            stmt = stmt.where(topic_filter)
        if after_id is not None:
            stmt = stmt.where(Message.msg_id > after_id)
        if since is not None:
            stmt = stmt.where(Message.date >= since)
        if until is not None:
            stmt = stmt.where(Message.date < until)
        if q:
            stmt = stmt.where(Message.text.ilike(f"%{q}%"))
        stmt = stmt.order_by(Message.msg_id).limit(limit)
        with self.session() as s:
            return list(s.execute(stmt).scalars())

    @staticmethod
    def _not_duplicate():
        """Нет более раннего сообщения с тем же отпечатком (в любом чате)."""
        earlier = aliased(Message)
        return ~exists().where(
            and_(
                earlier.fingerprint == Message.fingerprint,
                Message.fingerprint.is_not(None),
                or_(
                    earlier.date < Message.date,
                    and_(earlier.date == Message.date, earlier.msg_id < Message.msg_id),
                ),
            )
        )

    def search_messages(
        self,
        query: str,
        *,
        chat_id: int | None = None,
        topics: Sequence[str | int] = (),
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
    ) -> list[Message]:
        """Полнотекстовый поиск с русской морфологией, новые сначала."""
        tsquery = func.websearch_to_tsquery("russian", query)
        stmt = select(Message).where(Message.search.op("@@")(tsquery))
        if chat_id is not None:
            stmt = stmt.where(Message.chat_id == chat_id)
        topic_filter = self._topic_filter(topics)
        if topic_filter is not None:
            stmt = stmt.where(topic_filter)
        if since is not None:
            stmt = stmt.where(Message.date >= since)
        if until is not None:
            stmt = stmt.where(Message.date < until)
        stmt = stmt.order_by(Message.date.desc(), Message.msg_id.desc()).limit(limit)
        with self.session() as s:
            return list(s.execute(stmt).scalars())

    def duplicates_of(self, fingerprint: str) -> list[Message]:
        with self.session() as s:
            return list(
                s.execute(
                    select(Message)
                    .where(Message.fingerprint == fingerprint)
                    .order_by(Message.date, Message.msg_id)
                ).scalars()
            )

    def messages_without_extraction(
        self,
        extractor: str,
        chat_id: int,
        topics: Sequence[str | int] = (),
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[Message]:
        """Сообщения фида с текстом, по которым извлекатель ещё не проходил."""
        done = exists().where(
            and_(
                Extraction.extractor == extractor,
                Extraction.chat_id == Message.chat_id,
                Extraction.msg_id == Message.msg_id,
            )
        )
        stmt = (
            select(Message)
            .where(Message.chat_id == chat_id, Message.text != "", ~done)
            .order_by(Message.msg_id)
            .limit(limit)
        )
        topic_filter = self._topic_filter(topics)
        if topic_filter is not None:
            stmt = stmt.where(topic_filter)
        if since is not None:
            stmt = stmt.where(Message.date >= since)
        with self.session() as s:
            return list(s.execute(stmt).scalars())

    def save_extractions(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        stmt = insert(Extraction).values(list(rows))
        stmt = stmt.on_conflict_do_update(
            constraint="uq_extractions_message",
            set_={
                name: getattr(stmt.excluded, name)
                for name in ("status", "data", "error", "model", "input_tokens", "output_tokens")
            },
        )
        stmt = stmt.returning(Extraction.id)
        with self.session() as s, s.begin():
            return len(s.execute(stmt).all())

    def list_extractions(
        self,
        extractor: str,
        *,
        after_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        status: str | None = "ok",
        limit: int = 100,
    ) -> list[Extraction]:
        stmt = select(Extraction).where(Extraction.extractor == extractor)
        if status:
            stmt = stmt.where(Extraction.status == status)
        if after_id is not None:
            stmt = stmt.where(Extraction.id > after_id)
        if since is not None:
            stmt = stmt.where(Extraction.message_date >= since)
        if until is not None:
            stmt = stmt.where(Extraction.message_date < until)
        stmt = stmt.order_by(Extraction.id).limit(limit)
        with self.session() as s:
            return list(s.execute(stmt).scalars())

    def extraction_counts(self) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.execute(
                select(Extraction.extractor, Extraction.status, func.count())
                .group_by(Extraction.extractor, Extraction.status)
                .order_by(Extraction.extractor)
            ).all()
        return [{"extractor": e, "status": st, "count": int(n)} for e, st, n in rows]

    def add_extract_batch(self, extractor: str, batch_id: str, items: dict[str, Any]) -> None:
        with self.session() as s, s.begin():
            s.add(ExtractBatch(extractor=extractor, batch_id=batch_id, items=items))

    def pending_extract_batches(self, extractor: str | None = None) -> list[ExtractBatch]:
        stmt = select(ExtractBatch).where(ExtractBatch.status == "pending")
        if extractor:
            stmt = stmt.where(ExtractBatch.extractor == extractor)
        with self.session() as s:
            return list(s.execute(stmt.order_by(ExtractBatch.id)).scalars())

    def finish_extract_batch(self, batch_id: str, status: str = "done") -> None:
        with self.session() as s, s.begin():
            s.execute(
                update(ExtractBatch)
                .where(ExtractBatch.batch_id == batch_id)
                .values(status=status, finished_at=func.now())
            )

    # --- места, вопросы, события, авторы -----------------------------------------

    def place_mentions(
        self, slug: str, *, since: datetime | None = None, limit: int = 100
    ) -> list[Extraction]:
        """Извлечения, где место привязано к slug: поле place_slug или список places[]."""
        stmt = (
            select(Extraction)
            .where(
                Extraction.status == "ok",
                or_(
                    Extraction.data["place_slug"].as_string() == slug,
                    Extraction.data["places"].contains([{"slug": slug}]),
                ),
            )
            .order_by(Extraction.message_date.desc(), Extraction.id.desc())
            .limit(limit)
        )
        if since is not None:
            stmt = stmt.where(Extraction.message_date >= since)
        with self.session() as s:
            return list(s.execute(stmt).scalars())

    def top_places(self, *, since: datetime | None = None, limit: int = 30) -> list[dict[str, Any]]:
        """Сколько сообщений упоминают каждое место каталога (по всем извлекателям)."""
        sql = text(
            """
            WITH hits AS (
                SELECT chat_id, msg_id, data->>'place_slug' AS slug
                FROM extractions
                WHERE status = 'ok' AND data->>'place_slug' IS NOT NULL
                  AND (CAST(:since AS timestamptz) IS NULL
                       OR message_date >= CAST(:since AS timestamptz))
                UNION
                SELECT e.chat_id, e.msg_id, p->>'slug' AS slug
                FROM extractions e, jsonb_array_elements(e.data->'places') AS p
                WHERE e.status = 'ok' AND jsonb_typeof(e.data->'places') = 'array'
                  AND p->>'slug' IS NOT NULL
                  AND (CAST(:since AS timestamptz) IS NULL
                       OR e.message_date >= CAST(:since AS timestamptz))
            )
            SELECT slug, count(*) AS mentions
            FROM hits GROUP BY slug ORDER BY mentions DESC, slug LIMIT :limit
            """
        )
        with self.session() as s:
            rows = s.execute(sql, {"since": since, "limit": limit}).all()
        return [{"slug": slug, "mentions": int(n)} for slug, n in rows]

    def question_themes(
        self, *, since: datetime | None = None, examples: int = 3
    ) -> list[dict[str, Any]]:
        """Темы вопросов со счётчиками и примерами — бэклог приложения."""
        stmt = (
            select(Extraction)
            .where(Extraction.extractor == "user_question", Extraction.status == "ok")
            .order_by(Extraction.message_date.desc())
        )
        if since is not None:
            stmt = stmt.where(Extraction.message_date >= since)
        themes: dict[str, dict[str, Any]] = {}
        with self.session() as s:
            for e in s.execute(stmt).scalars():
                data = e.data or {}
                theme = str(data.get("theme") or "other")
                entry = themes.setdefault(
                    theme, {"theme": theme, "count": 0, "examples": [], "places": {}}
                )
                entry["count"] += 1
                if len(entry["examples"]) < examples and data.get("question"):
                    entry["examples"].append(
                        {"question": data["question"], "chat_id": e.chat_id, "msg_id": e.msg_id}
                    )
                slug = data.get("place_slug")
                if slug:
                    entry["places"][slug] = entry["places"].get(slug, 0) + 1
        result = sorted(themes.values(), key=lambda t: (-t["count"], t["theme"]))
        for entry in result:
            entry["places"] = sorted(
                ({"slug": k, "count": v} for k, v in entry["places"].items()),
                key=lambda x: -x["count"],
            )[:5]
        return result

    def author_stats(
        self,
        chat_id: int,
        topics: Sequence[str | int] = (),
        *,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Самые активные авторы (по хешам): сообщений и полученных ответов."""
        replies = aliased(Message)
        stmt = (
            select(
                Message.author_hash,
                func.count(func.distinct(Message.msg_id)).label("messages"),
                func.count(replies.msg_id).label("replies_received"),
            )
            .outerjoin(
                replies,
                and_(replies.chat_id == Message.chat_id, replies.reply_to_msg_id == Message.msg_id),
            )
            .where(Message.chat_id == chat_id, Message.author_hash.is_not(None))
            .group_by(Message.author_hash)
            .order_by(
                func.count(replies.msg_id).desc(), func.count(func.distinct(Message.msg_id)).desc()
            )
            .limit(limit)
        )
        topic_filter = self._topic_filter(topics)
        if topic_filter is not None:
            stmt = stmt.where(topic_filter)
        if since is not None:
            stmt = stmt.where(Message.date >= since)
        with self.session() as s:
            rows = s.execute(stmt).all()
        return [{"author": a, "messages": int(m), "replies_received": int(r)} for a, m, r in rows]

    def extractions_for_events(
        self, extractors: Sequence[str], *, since: datetime | None = None
    ) -> list[Extraction]:
        stmt = (
            select(Extraction)
            .where(Extraction.extractor.in_(list(extractors)), Extraction.status == "ok")
            .order_by(Extraction.message_date)
        )
        if since is not None:
            stmt = stmt.where(Extraction.message_date >= since)
        with self.session() as s:
            return list(s.execute(stmt).scalars())

    def last_runs_by_kind(self) -> dict[str, Run]:
        with self.session() as s:
            runs = s.execute(select(Run).order_by(Run.started_at.desc()).limit(200)).scalars()
            latest: dict[str, Run] = {}
            for run in runs:
                latest.setdefault(run.kind, run)
            return latest

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
