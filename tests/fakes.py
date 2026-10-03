"""Фейки Telegram для тестов без сети."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from telethon.tl import types as tl
from telethon.tl.functions.messages import GetForumTopicsByIDRequest, GetForumTopicsRequest

CHAT_ID = 1234567890


def forum_topic(topic_id: int, title: str) -> tl.ForumTopic:
    return tl.ForumTopic(
        id=topic_id,
        date=datetime(2025, 1, 1, tzinfo=UTC),
        peer=tl.PeerChannel(CHAT_ID),
        title=title,
        icon_color=0,
        top_message=topic_id,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=tl.PeerUser(1),
        notify_settings=tl.PeerNotifySettings(),
    )


class FakeTelegramClient:
    """Отдаёт заранее подготовленные сообщения, запоминает всё отправленное."""

    def __init__(
        self,
        messages: list[Any],
        *,
        topics: dict[int, str] | None = None,
        forum: bool = True,
        flood_at: int | None = None,
        flood_seconds: int = 10,
    ) -> None:
        self.messages = sorted(messages, key=lambda m: m.id)
        self.topics = topics or {}
        self.forum = forum
        self.flood_at = flood_at
        self.flood_seconds = flood_seconds
        self.sent: list[tuple[Any, str]] = []
        self.files: list[tuple[Any, str, str]] = []
        self.calls: list[Any] = []
        self.flood_sleep_threshold = 60

    async def get_entity(self, key: Any) -> tl.Channel:
        return tl.Channel(
            id=CHAT_ID,
            title="ГОРЕЦ",
            photo=tl.ChatPhotoEmpty(),
            date=None,
            username="gorets_uzb",
            megagroup=True,
            forum=self.forum,
        )

    async def __call__(self, request: Any) -> Any:
        self.calls.append(request)
        if isinstance(request, GetForumTopicsRequest):
            topics = [forum_topic(i, t) for i, t in self.topics.items()]
            offset = request.offset_topic
            page = [t for t in topics if t.id > offset][: request.limit]
            return tl.messages.ForumTopics(
                count=len(topics), topics=page, messages=[], chats=[], users=[], pts=0
            )
        if isinstance(request, GetForumTopicsByIDRequest):
            page = [forum_topic(i, self.topics[i]) for i in request.topics if i in self.topics]
            return tl.messages.ForumTopics(
                count=len(page), topics=page, messages=[], chats=[], users=[], pts=0
            )
        raise AssertionError(f"неожиданный запрос {request!r}")

    def iter_messages(self, entity, limit=None, *, min_id=0, offset_date=None, reverse=False, **kw):
        assert reverse, "сборщик читает по возрастанию"
        selected = [m for m in self.messages if m.id > min_id]
        if offset_date is not None:
            selected = [m for m in selected if m.date > offset_date]
        if limit is not None:
            selected = selected[:limit]
        return self._iterate(selected)

    async def _iterate(self, selected):
        from telethon.errors import FloodWaitError

        for m in selected:
            if self.flood_at is not None and m.id == self.flood_at:
                self.flood_at = None
                raise FloodWaitError(request=None, capture=self.flood_seconds)
            yield m

    async def get_input_entity(self, target):
        if isinstance(target, int) and not getattr(self, "dialogs_loaded", False):
            raise ValueError("нет в кэше")
        return target

    async def get_dialogs(self, limit=None):
        self.dialogs_loaded = True

    async def send_message(self, entity, message, **kw):
        self.sent.append((entity, message))

    async def send_file(self, entity, file, *, caption=None, **kw):
        self.files.append((entity, file, caption))
