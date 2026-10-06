"""Слушатель в реальном времени: новые и отредактированные сообщения сразу в базу
и в вебхуки фидов.

Ежедневный сбор остаётся: он догоняет всё, что слушатель пропустил, пока
не работал. Оба пишут одним upsert, поэтому не мешают друг другу.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from telethon import events

from gorets.anonymize import author_hash
from gorets.cleaning import TextCleaner
from gorets.config import Settings
from gorets.feeds import FeedConfig, ProjectConfig
from gorets.serialize import row_to_dict
from gorets.storage import Repository
from gorets.telegram.collect import TopicResolver, resolve_chat
from gorets.telegram.convert import ChatInfo, message_to_row
from gorets.webhooks import post_webhook

log = logging.getLogger(__name__)

Poster = Callable[..., bool]


@dataclass
class WatchedChat:
    entity: Any
    info: ChatInfo
    topics: TopicResolver


class Watcher:
    """Преобразует событие Telethon в строку базы и рассылает вебхуки."""

    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        project: ProjectConfig,
        *,
        poster: Poster = post_webhook,
    ) -> None:
        secret = settings.author_hmac_secret
        if not secret:
            raise RuntimeError("Не задан GORETS_AUTHOR_HMAC_SECRET")
        self.settings = settings
        self.repo = repo
        self.project = project
        self.poster = poster
        self.cleaner = TextCleaner(settings.mention_keep_set)
        self.hasher = lambda kind, peer_id: author_hash(secret, peer_id, kind)
        self.chats: dict[int, WatchedChat] = {}

    def feeds_for(self, chat: ChatInfo, row: dict[str, Any]) -> list[FeedConfig]:
        return [
            f
            for f in self.project.feeds.values()
            if f.webhook_url
            and f.matches(chat.chat_id, chat.username, row["topic_id"], row["topic_title"])
        ]

    async def handle(self, msg: Any, watched: WatchedChat, *, event: str = "message") -> list[str]:
        """Записать сообщение и разослать вебхуки; вернуть имена фидов, куда ушло."""
        chat = watched.info
        topic_id = None
        header = getattr(msg, "reply_to", None)
        if header is not None and getattr(header, "forum_topic", False):
            topic_id = header.reply_to_top_id or header.reply_to_msg_id
        await watched.topics.ensure(topic_id)
        row = message_to_row(
            msg,
            chat=chat,
            sender=getattr(msg, "sender", None),
            topics=watched.topics.titles,
            hasher=self.hasher,
            cleaner=self.cleaner,
            keep_contacts=lambda tid, title: self.project.keeps_contacts(
                chat.chat_id, chat.username, tid, title
            ),
        )
        if row is None:
            return []
        self.repo.upsert_messages([row])
        payload = row_to_dict(row, chat.username)
        delivered: list[str] = []
        for feed in self.feeds_for(chat, row):
            ok = self.poster(
                feed.webhook_url,
                {"event": event, "feed": feed.name, "message": payload},
                secret=feed.webhook_secret,
                event=event,
                feed=feed.name,
            )
            if ok:
                delivered.append(feed.name)
        return delivered


async def run_watch(
    settings: Settings,
    repo: Repository,
    client: Any,
    project: ProjectConfig,
    *,
    poster: Poster = post_webhook,
) -> None:
    """Подписаться на чаты из настроек и слушать, пока не отключат."""
    watcher = Watcher(settings, repo, project, poster=poster)
    entities = []
    for ref in settings.chat_list:
        entity, info = await resolve_chat(client, ref)
        repo.upsert_chat(
            info.chat_id, username=info.username, title=info.title, is_forum=info.is_forum
        )
        topics = TopicResolver(client, entity, info.is_forum)
        await topics.load_all()
        watcher.chats[info.chat_id] = WatchedChat(entity, info, topics)
        entities.append(entity)
    log.info("Слушаем %s чат(ов): %s", len(entities), ", ".join(settings.chat_list))

    def watched_for(event_obj: Any) -> WatchedChat | None:
        from gorets.storage import normalize_chat_id

        chat_id = getattr(event_obj, "chat_id", None)
        return watcher.chats.get(normalize_chat_id(chat_id)) if chat_id else None

    @client.on(events.NewMessage(chats=entities))
    async def on_new(event_obj: Any) -> None:
        watched = watched_for(event_obj)
        if watched is not None:
            try:
                await watcher.handle(event_obj.message, watched, event="message")
            except Exception:
                log.exception("Не удалось обработать сообщение %s", event_obj.message.id)

    @client.on(events.MessageEdited(chats=entities))
    async def on_edit(event_obj: Any) -> None:
        watched = watched_for(event_obj)
        if watched is not None:
            try:
                await watcher.handle(event_obj.message, watched, event="message_edited")
            except Exception:
                log.exception("Не удалось обработать правку %s", event_obj.message.id)

    await client.run_until_disconnected()
