"""Сбор сообщений: ежедневный догон новых и порционная догрузка истории.

Один проход по чату — это `harvest`: читаем сообщения от точки старта по
возрастанию id, превращаем в строки, пишем пачками. Прогресс сохраняется
после каждой пачки, поэтому упавший прогон продолжится с места остановки.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from telethon.errors import FloodWaitError
from telethon.tl import types as tl
from telethon.tl.functions.messages import GetForumTopicsByIDRequest, GetForumTopicsRequest

from gorets.anonymize import author_hash
from gorets.cleaning import TextCleaner
from gorets.config import Settings
from gorets.storage import Repository, normalize_chat_id
from gorets.telegram.convert import GENERAL_TOPIC_ID, GENERAL_TOPIC_TITLE, ChatInfo, message_to_row

log = logging.getLogger(__name__)


class CollectError(RuntimeError):
    pass


@dataclass
class HarvestResult:
    seen: int = 0
    new: int = 0
    stored: int = 0
    last_id: int | None = None
    # True, если история кончилась (а не кончилась порция).
    exhausted: bool = True


@dataclass
class CollectReport:
    chats: list[dict[str, Any]] = field(default_factory=list)
    deleted_old: int = 0

    @property
    def new_total(self) -> int:
        return sum(c["new"] for c in self.chats)


class TopicResolver:
    """Названия веток форума: грузим список разом, неизвестные дотягиваем по id."""

    def __init__(self, client: Any, entity: Any, is_forum: bool) -> None:
        self.client = client
        self.entity = entity
        self.is_forum = is_forum
        self.titles: dict[int, str | None] = {GENERAL_TOPIC_ID: GENERAL_TOPIC_TITLE}

    async def load_all(self, page: int = 100) -> None:
        if not self.is_forum:
            return
        offset_date: datetime | None = None
        offset_id = 0
        offset_topic = 0
        while True:
            result = await self.client(
                GetForumTopicsRequest(
                    peer=self.entity,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=page,
                )
            )
            topics = [t for t in result.topics if isinstance(t, tl.ForumTopic)]
            for topic in topics:
                self.titles[topic.id] = topic.title
            if len(result.topics) < page or not topics:
                break
            last = topics[-1]
            offset_date, offset_id, offset_topic = last.date, last.top_message, last.id

    async def ensure(self, topic_id: int | None) -> None:
        """Ветка, созданная после загрузки списка: один запрос по id, результат запоминаем."""
        if topic_id is None or topic_id in self.titles or not self.is_forum:
            return
        title: str | None = None
        try:
            result = await self.client(
                GetForumTopicsByIDRequest(peer=self.entity, topics=[topic_id])
            )
            for topic in result.topics:
                if isinstance(topic, tl.ForumTopic) and topic.id == topic_id:
                    title = topic.title
        except Exception as exc:
            log.warning("Не удалось получить название ветки %s: %s", topic_id, exc)
        self.titles[topic_id] = title


async def resolve_chat(client: Any, ref: str) -> tuple[Any, ChatInfo]:
    """Сущность Telegram по username или id из настроек и её описание для базы."""
    key: str | int = ref.strip()
    if isinstance(key, str) and key.lstrip("-").isdigit():
        key = int(key)
    entity = await client.get_entity(key)
    chat_id = normalize_chat_id(entity.id)
    info = ChatInfo(
        chat_id=chat_id,
        username=getattr(entity, "username", None),
        title=getattr(entity, "title", None),
        is_forum=bool(getattr(entity, "forum", False)),
    )
    return entity, info


def _topic_id_of(msg: Any) -> int | None:
    header = getattr(msg, "reply_to", None)
    if isinstance(header, tl.MessageReplyHeader) and header.forum_topic:
        return header.reply_to_top_id or header.reply_to_msg_id
    return None


def _forward_title(msg: Any) -> str | None:
    forward = getattr(msg, "forward", None)
    chat = getattr(forward, "chat", None) if forward is not None else None
    return getattr(chat, "title", None)


async def harvest(
    client: Any,
    repo: Repository,
    settings: Settings,
    entity: Any,
    chat: ChatInfo,
    topics: TopicResolver,
    *,
    min_id: int = 0,
    offset_date: datetime | None = None,
    limit: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> HarvestResult:
    """Прочитать сообщения чата от точки старта по возрастанию и записать в базу."""
    secret = settings.author_hmac_secret
    if not secret:
        raise CollectError("Не задан GORETS_AUTHOR_HMAC_SECRET — без него авторов не обезличить")

    def hasher(kind: str, peer_id: int) -> str:
        return author_hash(secret, peer_id, kind)

    cleaner = TextCleaner(settings.mention_keep_set)
    result = HarvestResult()
    batch: list[dict[str, Any]] = []

    def flush() -> None:
        if not batch:
            return
        inserted = repo.upsert_messages(batch)
        result.new += inserted
        result.stored += len(batch)
        batch.clear()
        if on_progress and result.last_id is not None:
            on_progress(result.last_id, result.new)

    try:
        iterator = client.iter_messages(
            entity,
            limit=limit,
            min_id=min_id,
            offset_date=offset_date,
            reverse=True,
            wait_time=settings.request_pause,
        )
        async for msg in iterator:
            result.seen += 1
            result.last_id = msg.id
            if isinstance(msg, tl.Message):
                await topics.ensure(
                    msg.reply_to.reply_to_top_id or msg.reply_to.reply_to_msg_id
                    if isinstance(msg.reply_to, tl.MessageReplyHeader) and msg.reply_to.forum_topic
                    else None
                )
            row = message_to_row(
                msg,
                chat=chat,
                sender=getattr(msg, "sender", None),
                topics=topics.titles,
                hasher=hasher,
                cleaner=cleaner,
                fwd_title=_forward_title(msg),
            )
            if row is not None:
                batch.append(row)
            if len(batch) >= settings.upsert_batch:
                flush()
    except FloodWaitError as exc:
        # Короткие FloodWait Telethon пережидает сам (flood_sleep_threshold);
        # сюда попадают только те, что длиннее допустимого. Сохраняем, что успели.
        flush()
        raise CollectError(
            f"Telegram просит подождать {exc.seconds} с — это больше допустимого, "
            "продолжим в следующий раз"
        ) from exc
    flush()
    if limit is not None and result.seen >= limit:
        result.exhausted = False
    return result


async def run_collect(settings: Settings, repo: Repository, client: Any) -> CollectReport:
    """Ежедневный прогон: новые сообщения по каждому чату, затем чистка по сроку."""
    report = CollectReport()
    failures: list[str] = []
    for ref in settings.chat_list:
        run_id = repo.start_run("collect")
        try:
            entity, chat = await resolve_chat(client, ref)
            repo.upsert_chat(
                chat.chat_id, username=chat.username, title=chat.title, is_forum=chat.is_forum
            )
            repo.update_run(run_id, details={"chat_id": chat.chat_id, "chat": ref})
            topics = TopicResolver(client, entity, chat.is_forum)
            await topics.load_all()
            since_id = repo.last_msg_id(chat.chat_id)
            log.info("Чат %s: читаем сообщения с id > %s", ref, since_id)
            result = await harvest(
                client,
                repo,
                settings,
                entity,
                chat,
                topics,
                min_id=since_id,
                on_progress=lambda last, new, rid=run_id: repo.update_run(
                    rid, new_messages=new, last_msg_id=last
                ),
            )
            repo.finish_run(
                run_id,
                status="ok",
                new_messages=result.new,
                last_msg_id=result.last_id or since_id,
                details={"chat_id": chat.chat_id, "chat": ref, "seen": result.seen},
            )
            report.chats.append(
                {"chat": ref, "chat_id": chat.chat_id, "new": result.new, "seen": result.seen}
            )
            log.info("Чат %s: новых %s, просмотрено %s", ref, result.new, result.seen)
        except Exception as exc:
            log.exception("Чат %s: сбор не удался", ref)
            repo.finish_run(run_id, status="error", error=str(exc))
            failures.append(f"{ref}: {exc}")
            await asyncio.sleep(0)
    report.deleted_old = repo.delete_older_than(settings.retention_days, datetime.now(UTC))
    if report.deleted_old:
        log.info(
            "Удалено сообщений старше %s дней: %s", settings.retention_days, report.deleted_old
        )
    if failures:
        raise CollectError("; ".join(failures))
    return report


async def run_backfill(
    settings: Settings,
    repo: Repository,
    client: Any,
    *,
    since: datetime,
    limit: int | None = None,
) -> CollectReport:
    """Догрузка истории с даты порциями; каждая следующая порция — с места остановки."""
    report = CollectReport()
    failures: list[str] = []
    portion = limit or settings.backfill_batch
    for ref in settings.chat_list:
        run_id = repo.start_run("backfill")
        try:
            entity, chat = await resolve_chat(client, ref)
            repo.upsert_chat(
                chat.chat_id, username=chat.username, title=chat.title, is_forum=chat.is_forum
            )
            topics = TopicResolver(client, entity, chat.is_forum)
            await topics.load_all()
            state = repo.get_sync_state(chat.chat_id)
            resume = (
                state is not None
                and state.backfill_since == since
                and not state.backfill_done
                and state.backfill_last_id
            )
            if state is not None and state.backfill_since == since and state.backfill_done:
                log.info("Чат %s: догрузка с %s уже завершена", ref, since.date())
                repo.finish_run(
                    run_id, status="ok", details={"chat_id": chat.chat_id, "skipped": True}
                )
                report.chats.append({"chat": ref, "chat_id": chat.chat_id, "new": 0, "seen": 0})
                continue
            min_id = int(state.backfill_last_id) if resume else 0
            offset_date = None if resume else since
            log.info(
                "Чат %s: догрузка %s", ref, f"с id > {min_id}" if resume else f"с {since.date()}"
            )

            def progress(
                last: int, new: int, chat_id: int = chat.chat_id, rid: int = run_id
            ) -> None:
                repo.set_sync_state(chat_id, since=since, last_id=last, done=False)
                repo.update_run(rid, new_messages=new, last_msg_id=last)

            result = await harvest(
                client,
                repo,
                settings,
                entity,
                chat,
                topics,
                min_id=min_id,
                offset_date=offset_date,
                limit=portion,
                on_progress=progress,
            )
            repo.set_sync_state(
                chat.chat_id,
                since=since,
                last_id=result.last_id or min_id or None,
                done=result.exhausted,
            )
            repo.finish_run(
                run_id,
                status="ok",
                new_messages=result.new,
                last_msg_id=result.last_id,
                details={"chat_id": chat.chat_id, "seen": result.seen, "done": result.exhausted},
            )
            report.chats.append(
                {
                    "chat": ref,
                    "chat_id": chat.chat_id,
                    "new": result.new,
                    "seen": result.seen,
                    "done": result.exhausted,
                }
            )
            log.info(
                "Чат %s: новых %s, просмотрено %s, %s",
                ref,
                result.new,
                result.seen,
                "история дочитана" if result.exhausted else "порция кончилась, продолжение позже",
            )
        except Exception as exc:
            log.exception("Чат %s: догрузка не удалась", ref)
            repo.finish_run(run_id, status="error", error=str(exc))
            failures.append(f"{ref}: {exc}")
    if failures:
        raise CollectError("; ".join(failures))
    return report
