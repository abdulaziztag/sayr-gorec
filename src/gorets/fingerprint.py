"""Отпечаток текста для поиска дублей: одна афиша в двух чатах — одна запись."""

from __future__ import annotations

import hashlib
import re

_URL_RE = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)
MIN_CHARS = 40


def normalize_for_fingerprint(text: str) -> str:
    lowered = _URL_RE.sub(" ", text.lower().replace("ё", "е"))
    return _NON_WORD_RE.sub(" ", lowered).strip()


def text_fingerprint(text: str | None) -> str | None:
    """sha256 нормализованного текста; короткие тексты («спасибо») не отпечатываем."""
    if not text:
        return None
    normalized = normalize_for_fingerprint(text)
    if len(normalized) < MIN_CHARS:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
