"""Импорт выгрузки Telegram Desktop (Machine-readable JSON, без медиа).

Файл на сотни мегабайт разбирается потоком через ijson. Id сообщений в
выгрузке совпадают с id в API, поэтому импорт и ежедневный сбор пишут в одну
таблицу с одним ключом и друг друга не дублируют. Тот же HMAC авторов, та же
очистка текста, что и при сборе.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import ijson

from gorets.anonymize import author_hash
from gorets.cleaning import EMAIL, PHONE, USER, TextCleaner
from gorets.config import Settings
from gorets.storage import Repository, as_decimal_coord, chunked
from gorets.telegram.convert import GENERAL_TOPIC_ID, GENERAL_TOPIC_TITLE, track_type

log = logging.getLogger(__name__)

_PEER_RE = re.compile(r"^(user|channel|chat)(\d+)$")


@dataclass
class ExportMeta:
    name: str | None = None
    type: str | None = None
    chat_id: int | None = None


@dataclass
class ImportResult:
    chat_id: int
    seen: int = 0
    stored: int = 0
    new: int = 0
    skipped: int = 0
    topics: dict[int, str] = field(default_factory=dict)


def read_export_meta(path: Path) -> ExportMeta:
    """Шапка выгрузки (name, type, id) без чтения списка сообщений."""
    meta = ExportMeta()
    with path.open("rb") as fh:
        for prefix, event, value in ijson.parse(fh):
            if prefix == "messages" and event == "start_array":
                break
            if event in ("string", "number") and prefix in ("name", "type", "id"):
                if prefix == "id":
                    meta.chat_id = int(value)
                else:
                    setattr(meta, prefix, str(value))
    return meta


def iter_export_messages(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("rb") as fh:
        yield from ijson.items(fh, "messages.item")


def parse_peer(value: Any) -> tuple[str, int] | None:
    if not isinstance(value, str):
        return None
    m = _PEER_RE.match(value)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def flatten_text(text: Any, keep: set[str]) -> str:
    """`text` выгрузки — строка или список строк и сущностей; личное меняем сразу."""
    if text is None:
        return ""
    if isinstance(text, str):
        return text
    parts: list[str] = []
    for part in text:
        if isinstance(part, str):
            parts.append(part)
            continue
        kind = part.get("type")
        value = str(part.get("text", ""))
        if kind == "mention":
            name = value.lstrip("@").lower()
            parts.append(value if (name in keep or name.endswith("bot")) else USER)
        elif kind == "mention_name":
            parts.append(USER)
        elif kind == "phone":
            parts.append(PHONE)
        elif kind == "email":
            parts.append(EMAIL)
        else:
            parts.append(value)
    return "".join(parts)


def _looks_like_bot(item: dict[str, Any]) -> bool:
    # В выгрузке нет признака «бот»; единственная зацепка — имя, как у ботов Telegram.
    name = str(item.get("from") or "")
    return name.lower().endswith("bot")


def _parse_date(item: dict[str, Any], key: str, tz: ZoneInfo) -> datetime | None:
    unix = item.get(f"{key}_unixtime")
    if unix not in (None, ""):
        return datetime.fromtimestamp(int(unix), tz=UTC)
    raw = item.get(key)
    if not raw:
        return None
    # Без unixtime остаётся местное время машины, с которой делали выгрузку.
    return datetime.fromisoformat(str(raw)).replace(tzinfo=tz).astimezone(UTC)


def _media(item: dict[str, Any]) -> tuple[str | None, str | None, Any, Any, str | None]:
    """(тип, имя файла, lat, lng, дополнительный текст)."""
    if "poll" in item:
        poll = item["poll"] or {}
        lines = [f"Опрос: {poll.get('question', '')}".rstrip()]
        lines += [f"— {a.get('text', '')}" for a in poll.get("answers", []) if a.get("text")]
        return "poll", None, None, None, "\n".join(lines)
    if "location_information" in item:
        loc = item["location_information"] or {}
        if item.get("live_location_period_seconds"):
            return "location", None, None, None, None
        lat, lng = loc.get("latitude"), loc.get("longitude")
        if item.get("place_name"):
            parts = [p for p in (item.get("place_name"), item.get("address")) if p]
            return "venue", None, lat, lng, "📍 " + ", ".join(parts)
        return "location", None, lat, lng, None
    if "contact_information" in item:
        return "other", None, None, None, None
    if "photo" in item:
        return "photo", None, None, None, None
    media_type = item.get("media_type")
    file_name = item.get("file_name")
    if not file_name and isinstance(item.get("file"), str) and "/" in item["file"]:
        file_name = os.path.basename(item["file"])
    if media_type == "sticker":
        return "sticker", None, None, None, None
    if media_type == "voice_message":
        return "voice", None, None, None, None
    if media_type in ("video_file", "video_message", "animation"):
        return "video", file_name, None, None, None
    if media_type == "audio_file":
        return "other", file_name, None, None, None
    if "file" in item or media_type:
        return track_type(file_name) or "document", file_name, None, None, None
    if any(k in item for k in ("game_title", "invoice_title", "dice")):
        return "other", None, None, None, None
    return None, None, None, None, None


class ExportConverter:
    """Превращает элементы выгрузки в строки базы, попутно вычисляя ветки форума.

    В выгрузке нет id ветки, только «ответ на». Ветка — это дерево ответов,
    растущее из служебного сообщения `topic_created`, поэтому ветка сообщения
    = ветка того, на что оно отвечает; сообщения без ответа лежат в General.
    """

    def __init__(
        self,
        *,
        chat_id: int,
        hasher: Callable[[str, int], str],
        cleaner: Callable[[str | None], str],
        keep: set[str],
        tz: ZoneInfo,
        is_forum: bool = True,
    ) -> None:
        self.chat_id = chat_id
        self.hasher = hasher
        self.cleaner = cleaner
        self.keep = keep
        self.tz = tz
        self.is_forum = is_forum
        self.topics: dict[int, str] = {GENERAL_TOPIC_ID: GENERAL_TOPIC_TITLE} if is_forum else {}
        self._topic_of: dict[int, int | None] = {}

    def _resolve_topic(self, msg_id: int, reply_to: int | None) -> tuple[int | None, int | None]:
        if not self.is_forum:
            return None, reply_to
        if reply_to is None:
            topic: int | None = GENERAL_TOPIC_ID
            real_reply = None
        elif reply_to in self.topics:
            topic, real_reply = reply_to, None
        elif reply_to in self._topic_of:
            topic, real_reply = self._topic_of[reply_to], reply_to
        else:
            # Ответ на сообщение, которого в выгрузке нет (удалено): ветка неизвестна.
            topic, real_reply = None, reply_to
        self._topic_of[msg_id] = topic
        return topic, real_reply

    def convert(self, item: dict[str, Any]) -> dict[str, Any] | None:
        msg_id = int(item["id"])
        if item.get("type") == "service":
            action = item.get("action")
            if action == "topic_created":
                self.topics[msg_id] = str(item.get("title") or "")
                self._topic_of[msg_id] = msg_id
            elif action == "topic_edit" and item.get("new_title"):
                # Переименование ветки приходит как ответ на её корень.
                root = item.get("reply_to_message_id")
                if root in self.topics:
                    self.topics[int(root)] = str(item["new_title"])
            return None
        if item.get("type") != "message" or _looks_like_bot(item):
            return None

        reply_to = item.get("reply_to_message_id")
        reply_to = int(reply_to) if reply_to is not None else None
        topic_id, real_reply = self._resolve_topic(msg_id, reply_to)

        author = parse_peer(item.get("from_id"))
        media_type, file_name, lat, lng, extra = _media(item)
        text = self.cleaner(flatten_text(item.get("text"), self.keep))
        if extra:
            text = f"{text}\n{self.cleaner(extra)}".strip()

        forwarded_from = None
        fwd_peer = parse_peer(item.get("forwarded_from_id"))
        if fwd_peer and fwd_peer[0] == "channel":
            forwarded_from = str(item.get("forwarded_from") or f"channel:{fwd_peer[1]}")

        return {
            "chat_id": self.chat_id,
            "msg_id": msg_id,
            "date": _parse_date(item, "date", self.tz),
            "topic_id": topic_id,
            "topic_title": self.topics.get(topic_id) if topic_id is not None else None,
            "reply_to_msg_id": real_reply,
            "author_hash": self.hasher(author[0], author[1]) if author else None,
            "text": text,
            "media_type": media_type,
            "file_name": file_name,
            "lat": as_decimal_coord(lat),
            "lng": as_decimal_coord(lng),
            "forwarded_from": forwarded_from,
            "edited_at": _parse_date(item, "edited", self.tz),
        }


def import_export(
    path: Path,
    repo: Repository,
    settings: Settings,
    *,
    chat_id: int | None = None,
    is_forum: bool = True,
    batch_size: int = 500,
    progress: Callable[[ImportResult], None] | None = None,
) -> ImportResult:
    secret = settings.author_hmac_secret
    if not secret:
        raise ValueError("Не задан GORETS_AUTHOR_HMAC_SECRET — без него авторов не обезличить")
    meta = read_export_meta(path)
    chat_id = chat_id or meta.chat_id
    if not chat_id:
        raise ValueError("В выгрузке нет id чата — укажите его параметром --chat-id")
    tz = ZoneInfo(settings.timezone)
    keep = settings.mention_keep_set
    converter = ExportConverter(
        chat_id=chat_id,
        hasher=lambda kind, peer_id: author_hash(secret, peer_id, kind),
        cleaner=TextCleaner(keep),
        keep=keep,
        tz=tz,
        is_forum=is_forum,
    )
    repo.upsert_chat(chat_id, username=None, title=meta.name, is_forum=is_forum)
    result = ImportResult(chat_id=chat_id)

    def rows() -> Iterator[dict[str, Any]]:
        for item in iter_export_messages(path):
            result.seen += 1
            row = converter.convert(item)
            if row is None or row["date"] is None:
                result.skipped += 1
                continue
            yield row

    for batch in chunked(rows(), batch_size):
        result.new += repo.upsert_messages(batch)
        result.stored += len(batch)
        if progress:
            progress(result)
    result.topics = dict(converter.topics)
    return result
