"""Сообщение Telethon → строка таблицы `messages`.

Чистая функция: сетевых вызовов нет, всё нужное (отправитель, названия
веток, название канала-источника пересылки) передаётся снаружи. Так её легко
проверить на синтетических объектах Telethon.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl import types as tl

from gorets.cleaning import USER
from gorets.storage import as_decimal_coord

# В форуме сообщения без ветки лежат в «General» — у него всегда id 1.
GENERAL_TOPIC_ID = 1
GENERAL_TOPIC_TITLE = "General"

TRACK_EXTENSIONS = {".gpx": "gpx", ".kml": "kml", ".kmz": "kml"}

Hasher = Callable[[str, int], str]
Cleaner = Callable[[str | None], str]


@dataclass(frozen=True)
class ChatInfo:
    chat_id: int
    username: str | None
    title: str | None
    is_forum: bool


@dataclass(frozen=True)
class MediaInfo:
    media_type: str | None = None
    file_name: str | None = None
    lat: float | None = None
    lng: float | None = None
    # Текст, которого нет в самом сообщении, но который важен для разбора:
    # название места у venue, вопрос и варианты у опроса.
    extra_text: str | None = None


def peer_kind_and_id(peer: Any) -> tuple[str, int] | None:
    if isinstance(peer, tl.PeerUser):
        return "user", peer.user_id
    if isinstance(peer, tl.PeerChannel):
        return "channel", peer.channel_id
    if isinstance(peer, tl.PeerChat):
        return "chat", peer.chat_id
    return None


def topic_and_reply(msg: Any, is_forum: bool) -> tuple[int | None, int | None]:
    """(id ветки, id сообщения, на которое отвечают).

    В форуме ветка — это технически «ответ» на её корневое сообщение, поэтому
    `reply_to_msg_id` без `reply_to_top_id` означает «написано в ветку», а не
    «ответ кому-то». Настоящий ответ — когда заполнены оба поля.
    """
    header = msg.reply_to
    if not isinstance(header, tl.MessageReplyHeader):
        return (GENERAL_TOPIC_ID if is_forum else None), None
    if is_forum and header.forum_topic:
        if header.reply_to_top_id:
            return header.reply_to_top_id, header.reply_to_msg_id
        return header.reply_to_msg_id, None
    return (GENERAL_TOPIC_ID if is_forum else None), header.reply_to_msg_id


def _text_of(value: Any) -> str:
    # Вопрос и варианты опроса в новых слоях API — объекты TextWithEntities.
    if value is None:
        return ""
    return str(getattr(value, "text", value))


def track_type(file_name: str | None) -> str | None:
    if not file_name:
        return None
    lowered = file_name.lower()
    for ext, kind in TRACK_EXTENSIONS.items():
        if lowered.endswith(ext):
            return kind
    return None


def classify_media(media: Any) -> MediaInfo:
    """Тип вложения и то немногое, что из него храним."""
    if media is None or isinstance(media, tl.MessageMediaEmpty | tl.MessageMediaWebPage):
        return MediaInfo()
    if isinstance(media, tl.MessageMediaPhoto):
        return MediaInfo("photo")
    if isinstance(media, tl.MessageMediaDocument):
        doc = media.document
        file_name = None
        kind = "document"
        attributes = list(getattr(doc, "attributes", None) or [])
        for attr in attributes:
            if isinstance(attr, tl.DocumentAttributeFilename):
                file_name = attr.file_name
        if any(isinstance(a, tl.DocumentAttributeSticker) for a in attributes):
            kind = "sticker"
        elif media.voice or any(
            isinstance(a, tl.DocumentAttributeAudio) and a.voice for a in attributes
        ):
            kind = "voice"
        elif (
            media.video
            or media.round
            or any(
                isinstance(a, tl.DocumentAttributeVideo | tl.DocumentAttributeAnimated)
                for a in attributes
            )
        ):
            kind = "video"
        elif any(isinstance(a, tl.DocumentAttributeAudio) for a in attributes):
            kind = "other"
        if kind == "document":
            kind = track_type(file_name) or "document"
        return MediaInfo(kind, file_name)
    if isinstance(media, tl.MessageMediaGeoLive):
        # Живую геопозицию не храним: тип остаётся, координаты — нет.
        return MediaInfo("location")
    if isinstance(media, tl.MessageMediaGeo):
        geo = media.geo
        if isinstance(geo, tl.GeoPoint):
            return MediaInfo("location", lat=geo.lat, lng=geo.long)
        return MediaInfo("location")
    if isinstance(media, tl.MessageMediaVenue):
        geo = media.geo
        lat = geo.lat if isinstance(geo, tl.GeoPoint) else None
        lng = geo.long if isinstance(geo, tl.GeoPoint) else None
        parts = [p for p in (media.title, media.address) if p]
        extra = ("📍 " + ", ".join(parts)) if parts else None
        return MediaInfo("venue", lat=lat, lng=lng, extra_text=extra)
    if isinstance(media, tl.MessageMediaPoll):
        poll = media.poll
        question = _text_of(getattr(poll, "question", None)).strip()
        answers = [_text_of(getattr(a, "text", None)).strip() for a in getattr(poll, "answers", [])]
        lines = [f"Опрос: {question}"] if question else ["Опрос"]
        lines += [f"— {a}" for a in answers if a]
        return MediaInfo("poll", extra_text="\n".join(lines))
    return MediaInfo("other")


def mask_mention_names(text: str, entities: list[Any] | None) -> str:
    """Упоминания людей без username (MessageEntityMentionName) — заглушкой.

    Смещения сущностей Telegram считает в UTF-16, поэтому текст временно
    переводится в суррогатные пары.
    """
    if not text or not entities:
        return text
    spans = sorted(
        (
            (e.offset, e.length)
            for e in entities
            if isinstance(e, tl.MessageEntityMentionName | tl.InputMessageEntityMentionName)
        ),
        reverse=True,
    )
    if not spans:
        return text
    surrogated = add_surrogate(text)
    for offset, length in spans:
        surrogated = surrogated[:offset] + USER + surrogated[offset + length :]
    return del_surrogate(surrogated)


def forwarded_channel_title(msg: Any, fwd_title: str | None = None) -> str | None:
    """Название канала, из которого переслано сообщение; пересылки от людей не храним."""
    fwd = getattr(msg, "fwd_from", None)
    if fwd is None or not isinstance(fwd.from_id, tl.PeerChannel):
        return None
    if fwd_title:
        return fwd_title
    forward = getattr(msg, "forward", None)
    chat = getattr(forward, "chat", None) if forward is not None else None
    title = getattr(chat, "title", None)
    return title or f"channel:{fwd.from_id.channel_id}"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def message_to_row(
    msg: Any,
    *,
    chat: ChatInfo,
    sender: Any,
    topics: Mapping[int, str | None],
    hasher: Hasher,
    cleaner: Cleaner,
    fwd_title: str | None = None,
) -> dict[str, Any] | None:
    """Строка для upsert или None, если сообщение не храним (служебное, от бота, пустое)."""
    if not isinstance(msg, tl.Message) or getattr(msg, "action", None) is not None:
        return None
    if isinstance(sender, tl.User) and sender.bot:
        return None

    author = peer_kind_and_id(msg.from_id)
    author_hash = hasher(author[0], author[1]) if author else None

    topic_id, reply_to = topic_and_reply(msg, chat.is_forum)
    topic_title = None
    if topic_id is not None:
        topic_title = topics.get(topic_id)
        if topic_title is None and topic_id == GENERAL_TOPIC_ID:
            topic_title = GENERAL_TOPIC_TITLE

    media = classify_media(msg.media)
    raw_text = mask_mention_names(msg.message or "", msg.entities)
    text = cleaner(raw_text)
    if media.extra_text:
        text = f"{text}\n{cleaner(media.extra_text)}".strip()

    return {
        "chat_id": chat.chat_id,
        "msg_id": msg.id,
        "date": _as_utc(msg.date),
        "topic_id": topic_id,
        "topic_title": topic_title,
        "reply_to_msg_id": reply_to,
        "author_hash": author_hash,
        "text": text,
        "media_type": media.media_type,
        "file_name": media.file_name,
        "lat": as_decimal_coord(media.lat),
        "lng": as_decimal_coord(media.lng),
        "forwarded_from": forwarded_channel_title(msg, fwd_title),
        "edited_at": _as_utc(msg.edit_date),
    }
