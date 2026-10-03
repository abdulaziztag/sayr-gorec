"""Доставка отчёта владельцу личным сообщением тем же аккаунтом."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Нарезать текст на части не длиннее `limit`, по абзацам, затем по строкам.

    Если частей больше одной, каждая получает пометку «(i/n)» — она тоже
    укладывается в лимит.
    """
    text = text.strip()
    if not text:
        return []
    marker_reserve = 12  # «(99/99)\n\n»
    body_limit = max(limit - marker_reserve, 1)

    def cut(block: str, size: int) -> list[str]:
        pieces: list[str] = []
        while len(block) > size:
            split_at = block.rfind("\n", 0, size)
            if split_at < size // 2:
                split_at = block.rfind(" ", 0, size)
            if split_at < size // 2:
                split_at = size
            pieces.append(block[:split_at].rstrip())
            block = block[split_at:].lstrip()
        if block:
            pieces.append(block)
        return pieces

    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= body_limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        if len(paragraph) <= body_limit:
            current = paragraph
        else:
            pieces = cut(paragraph, body_limit)
            parts.extend(pieces[:-1])
            current = pieces[-1]
    if current:
        parts.append(current)
    total = len(parts)
    return [f"({i}/{total})\n\n{part}" for i, part in enumerate(parts, start=1)]


def resolve_owner(owner: str | int) -> str | int:
    """«@username» или числовой id из настроек → то, что понимает Telethon."""
    if isinstance(owner, int):
        return owner
    value = owner.strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value


async def send_report(
    client: Any,
    owner: str | int,
    *,
    text: str,
    file_path: Path | None,
    mode: str = "auto",
    max_parts: int = 4,
    limit: int = TELEGRAM_LIMIT,
) -> str:
    """Отправить отчёт частями или файлом; вернуть, как именно отправили.

    `auto`: частями, если их не больше `max_parts`, иначе — файлом с коротким
    сопроводительным сообщением (первая часть текста).
    """
    target = resolve_owner(owner)
    parts = split_message(text, limit)
    use_file = mode == "file" or (mode == "auto" and len(parts) > max_parts)
    if use_file and file_path is not None:
        intro = parts[0] if parts else "Отчёт за неделю"
        if len(parts) > 1:
            # Пометку «(1/n)» у сопроводительного сообщения убираем: частей не будет.
            intro = intro.split("\n\n", 1)[1] if intro.startswith("(") else intro
        await client.send_file(
            target, str(file_path), caption=intro[:1024], parse_mode=None, force_document=True
        )
        return "file"
    for part in parts:
        await client.send_message(target, part, parse_mode=None, link_preview=False)
    return "parts"
