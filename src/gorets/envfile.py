"""Аккуратная запись в `.env`: дописать ключ, если его нет, и ничего больше не трогать."""

from __future__ import annotations

import os
import re
from pathlib import Path


def has_key(path: Path, key: str) -> bool:
    if not path.exists():
        return False
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    return any(pattern.match(line) for line in path.read_text(encoding="utf-8").splitlines())


def append_key(path: Path, key: str, value: str) -> None:
    """Дописать `KEY=value` в конец файла; новый файл создаётся с правами 600."""
    is_new = not path.exists()
    existing = path.read_text(encoding="utf-8") if not is_new else ""
    with path.open("a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(f"{key}={value}\n")
    if is_new:
        os.chmod(path, 0o600)
