"""Нарезка недели на куски для модели: по веткам и бюджету токенов.

В модель уходит только текст без авторов: ветка, дата, текст, «ответ на»
коротко. Токены считаем грубо по длине текста — без обращения к API, чтобы
`--dry-run` и тесты работали без сети.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MEDIA_LABELS = {
    "photo": "[фото]",
    "video": "[видео]",
    "voice": "[голосовое]",
    "sticker": "[стикер]",
    "poll": "",
    "location": "[точка]",
    "venue": "[место]",
    "gpx": "[трек GPX]",
    "kml": "[трек KML]",
    "document": "[файл]",
    "other": "[вложение]",
}
# Сообщения без текста попадают в модель, только если вложение само по себе говорящее.
MEANINGFUL_MEDIA = {"gpx", "kml", "location", "venue", "document", "poll"}
REPLY_QUOTE_CHARS = 60
MAX_MESSAGE_CHARS = 4000


@dataclass(frozen=True)
class ChunkMessage:
    chat_id: int
    msg_id: int
    date: datetime  # местное время
    topic_id: int | None
    topic_title: str | None
    reply_to: int | None
    text: str
    author_hash: str | None = None
    media_type: str | None = None
    file_name: str | None = None
    lat: float | None = None
    lng: float | None = None


@dataclass
class Chunk:
    id: str
    chat_id: int
    topics: list[tuple[int | None, str]]
    text: str
    est_tokens: int
    message_count: int
    message_ids: list[int] = field(default_factory=list)


@dataclass
class _Unit:
    chat_id: int
    topic_id: int | None
    title: str
    part: int
    parts: int
    lines: list[str]
    tokens: int
    message_ids: list[int]


def estimate_tokens(text: str, chars_per_token: float) -> int:
    return max(1, math.ceil(len(text) / max(chars_per_token, 0.1)))


def topic_label(topic_id: int | None, title: str | None) -> str:
    if topic_id is None:
        return "без ветки"
    return f"«{title}» (id {topic_id})" if title else f"ветка id {topic_id}"


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def is_meaningful(m: ChunkMessage) -> bool:
    return bool(m.text.strip()) or (m.media_type in MEANINGFUL_MEDIA)


def message_line(m: ChunkMessage, replied: ChunkMessage | None) -> str:
    stamp = m.date.strftime("%d.%m %H:%M")
    reply = ""
    if m.reply_to:
        if replied is not None and replied.text.strip():
            reply = f" (ответ на #{m.reply_to}: «{shorten(replied.text, REPLY_QUOTE_CHARS)}»)"
        else:
            reply = f" (ответ на #{m.reply_to})"
    text = shorten(m.text, MAX_MESSAGE_CHARS) if m.text else ""
    extras: list[str] = []
    if m.media_type:
        if m.media_type in ("gpx", "kml", "document") and m.file_name:
            extras.append(f"[файл: {m.file_name}]")
        elif m.lat is not None and m.lng is not None:
            extras.append(f"[точка {m.lat:.3f}, {m.lng:.3f}]")
        elif MEDIA_LABELS.get(m.media_type):
            extras.append(MEDIA_LABELS[m.media_type])
    body = " ".join(p for p in (text, *extras) if p)
    return f"#{m.msg_id} {stamp}{reply}: {body}".rstrip()


def _unit_text(unit: _Unit, chat_names: dict[int, str]) -> str:
    chat = chat_names.get(unit.chat_id, str(unit.chat_id))
    suffix = f", часть {unit.part} из {unit.parts}" if unit.parts > 1 else ""
    header = f"=== Чат {chat}, ветка {unit.title}{suffix}, сообщений: {len(unit.lines)} ==="
    return "\n".join([header, *unit.lines])


def build_chunks(
    messages: list[ChunkMessage],
    *,
    week_label: str,
    chat_names: dict[int, str],
    budget_tokens: int,
    chars_per_token: float,
) -> list[Chunk]:
    """Собрать куски: ветка целиком, если влезает; длинные ветки — по частям;
    короткие ветки — по несколько в одном куске."""
    by_id = {(m.chat_id, m.msg_id): m for m in messages}
    groups: dict[tuple[int, int | None], list[ChunkMessage]] = defaultdict(list)
    for m in sorted(messages, key=lambda x: (x.chat_id, x.date, x.msg_id)):
        if is_meaningful(m):
            groups[(m.chat_id, m.topic_id)].append(m)

    units: list[_Unit] = []
    header_reserve = 40  # токены на заголовок части
    for (chat_id, topic_id), group in groups.items():
        title = topic_label(topic_id, group[0].topic_title)
        lines = [
            (message_line(m, by_id.get((m.chat_id, m.reply_to)) if m.reply_to else None), m.msg_id)
            for m in group
        ]
        pieces: list[list[tuple[str, int]]] = [[]]
        used = header_reserve
        for line, msg_id in lines:
            tokens = estimate_tokens(line, chars_per_token)
            if pieces[-1] and used + tokens > budget_tokens:
                pieces.append([])
                used = header_reserve
            pieces[-1].append((line, msg_id))
            used += tokens
        for index, piece in enumerate(pieces, start=1):
            text_lines = [line for line, _ in piece]
            unit = _Unit(
                chat_id=chat_id,
                topic_id=topic_id,
                title=title,
                part=index,
                parts=len(pieces),
                lines=text_lines,
                tokens=header_reserve
                + sum(estimate_tokens(line, chars_per_token) for line in text_lines),
                message_ids=[msg_id for _, msg_id in piece],
            )
            units.append(unit)

    # Упаковка: сначала большие, каждый — в первый кусок, куда влезает.
    # Части порезанной ветки живут в своих кусках: к ним ничего не подселяем,
    # чтобы «часть 2 из 3» читалась моделью как продолжение, а не как смесь.
    bins: list[list[_Unit]] = []
    sizes: list[int] = []
    closed: list[bool] = []
    for unit in sorted(units, key=lambda u: (-u.tokens, u.chat_id, u.topic_id or 0, u.part)):
        placed = False
        if unit.parts == 1:
            for i, size in enumerate(sizes):
                if not closed[i] and size + unit.tokens <= budget_tokens:
                    bins[i].append(unit)
                    sizes[i] += unit.tokens
                    placed = True
                    break
        if not placed:
            bins.append([unit])
            sizes.append(unit.tokens)
            closed.append(unit.parts > 1)

    chunks: list[Chunk] = []
    ordered = sorted(
        zip(bins, sizes, strict=True),
        key=lambda pair: (pair[0][0].chat_id, pair[0][0].topic_id or 0, pair[0][0].part),
    )
    for number, (units_in_bin, size) in enumerate(ordered, start=1):
        units_in_bin = sorted(units_in_bin, key=lambda u: (u.chat_id, u.topic_id or 0, u.part))
        text = "\n\n".join(_unit_text(u, chat_names) for u in units_in_bin)
        chunks.append(
            Chunk(
                id=f"{week_label}-{number:03d}",
                chat_id=units_in_bin[0].chat_id,
                topics=[(u.topic_id, u.title) for u in units_in_bin],
                text=text,
                est_tokens=size,
                message_count=sum(len(u.lines) for u in units_in_bin),
                message_ids=[i for u in units_in_bin for i in u.message_ids],
            )
        )
    return chunks


def chunks_fingerprint(chunks: list[Chunk]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk.id.encode())
        digest.update(b"\0")
        digest.update(chunk.text.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def chunk_summary(chunks: list[Chunk]) -> dict[str, Any]:
    return {
        "chunks": len(chunks),
        "messages": sum(c.message_count for c in chunks),
        "est_input_tokens": sum(c.est_tokens for c in chunks),
        "largest_chunk_tokens": max((c.est_tokens for c in chunks), default=0),
    }
