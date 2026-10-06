"""Фиды и извлекатели: описание того, что сервис отдаёт другим проектам.

Фид — именованный выбор сообщений: чат плюс ветки. Извлекатель — модель,
инструкция и JSON-схема, которые прогоняются по сообщениям фида; результат
хранится бессрочно на каждое сообщение. Описываются в `gorets.toml`, чтобы
новая ветка или новый вид извлечения не требовали правки кода.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class FeedConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FeedConfig:
    name: str
    chat: str  # username без @ или числовой id, как в GORETS_CHATS
    topics: tuple[str | int, ...] = ()  # начала названий или id веток; пусто — весь чат
    description: str = ""
    # В коммерческих ветках (афиши, объявления) телефоны и ники — часть
    # предложения, а не личные данные; их не стираем.
    keep_contacts: bool = False

    def chat_matches(self, chat_id: int, username: str | None) -> bool:
        ref = self.chat.lstrip("@").lower()
        if ref.lstrip("-").isdigit():
            from gorets.storage import normalize_chat_id

            return normalize_chat_id(int(ref)) == chat_id
        return bool(username) and username.lower() == ref

    def topic_matches(self, topic_id: int | None, topic_title: str | None) -> bool:
        if not self.topics:
            return True
        title = (topic_title or "").strip().lower()
        for wanted in self.topics:
            if isinstance(wanted, int):
                if topic_id == wanted:
                    return True
            elif title and title.startswith(wanted.strip().lower()):
                # По началу названия: длинные названия веток Telegram показывает
                # обрезанными, и точную форму знать необязательно.
                return True
        return False

    def matches(
        self, chat_id: int, username: str | None, topic_id: int | None, topic_title: str | None
    ) -> bool:
        return self.chat_matches(chat_id, username) and self.topic_matches(topic_id, topic_title)


@dataclass(frozen=True)
class ExtractorConfig:
    name: str
    feeds: tuple[str, ...]
    prompt: str
    schema: dict[str, Any]
    description: str = ""
    model: str | None = None  # None — GORETS_EXTRACT_MODEL
    max_tokens: int = 2048
    # Поля с названиями мест, которые после извлечения привязываются к каталогу
    # Sayr: "place" → data["place_slug"], "places[].name" → slug у каждого элемента.
    link_places: tuple[str, ...] = ()


@dataclass
class ProjectConfig:
    feeds: dict[str, FeedConfig] = field(default_factory=dict)
    extractors: dict[str, ExtractorConfig] = field(default_factory=dict)

    def feeds_for(self, chat_id: int, username: str | None) -> list[FeedConfig]:
        return [f for f in self.feeds.values() if f.chat_matches(chat_id, username)]

    def keeps_contacts(
        self, chat_id: int, username: str | None, topic_id: int | None, topic_title: str | None
    ) -> bool:
        return any(
            f.keep_contacts and f.matches(chat_id, username, topic_id, topic_title)
            for f in self.feeds.values()
        )

    def extractor(self, name: str) -> ExtractorConfig:
        try:
            return self.extractors[name]
        except KeyError:
            raise FeedConfigError(f"Извлекатель {name!r} не описан в gorets.toml") from None

    def feed(self, name: str) -> FeedConfig:
        try:
            return self.feeds[name]
        except KeyError:
            raise FeedConfigError(f"Фид {name!r} не описан в gorets.toml") from None


def _topics(raw: Any) -> tuple[str | int, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise FeedConfigError("topics должен быть списком названий или id веток")
    out: list[str | int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int | str):
            raise FeedConfigError(f"Непонятная ветка в topics: {item!r}")
        out.append(item)
    return tuple(out)


def _schema(name: str, raw: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    if "schema_file" in raw:
        path = Path(raw["schema_file"])
        if not path.is_absolute():
            path = base_dir / path
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FeedConfigError(f"Извлекатель {name}: схема {path} не читается: {exc}") from exc
    elif "schema" in raw:
        schema = raw["schema"]
        if isinstance(schema, str):
            try:
                schema = json.loads(schema)
            except json.JSONDecodeError as exc:
                raise FeedConfigError(f"Извлекатель {name}: schema — не JSON: {exc}") from exc
    else:
        raise FeedConfigError(f"Извлекатель {name}: нужна schema или schema_file")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise FeedConfigError(f"Извлекатель {name}: схема должна описывать объект")
    return schema


def parse_project_config(data: dict[str, Any], base_dir: Path) -> ProjectConfig:
    config = ProjectConfig()
    for name, raw in (data.get("feeds") or {}).items():
        if not isinstance(raw, dict) or not raw.get("chat"):
            raise FeedConfigError(f"Фид {name}: нужен chat")
        config.feeds[name] = FeedConfig(
            name=name,
            chat=str(raw["chat"]),
            topics=_topics(raw.get("topics")),
            description=str(raw.get("description", "")),
            keep_contacts=bool(raw.get("keep_contacts", False)),
        )
    for name, raw in (data.get("extractors") or {}).items():
        if not isinstance(raw, dict) or not raw.get("prompt"):
            raise FeedConfigError(f"Извлекатель {name}: нужны feed (или feeds) и prompt")
        feeds_raw = raw.get("feeds") or raw.get("feed")
        feeds = tuple(feeds_raw) if isinstance(feeds_raw, list) else (str(feeds_raw or ""),)
        if not feeds or not all(feeds):
            raise FeedConfigError(f"Извлекатель {name}: нужны feed (или feeds) и prompt")
        for feed_name in feeds:
            if feed_name not in config.feeds:
                raise FeedConfigError(f"Извлекатель {name}: фид {feed_name!r} не описан")
        config.extractors[name] = ExtractorConfig(
            name=name,
            feeds=tuple(str(f) for f in feeds),
            prompt=str(raw["prompt"]).strip(),
            schema=_schema(name, raw, base_dir),
            description=str(raw.get("description", "")),
            model=raw.get("model"),
            max_tokens=int(raw.get("max_tokens", 2048)),
            link_places=tuple(str(f) for f in (raw.get("link_places") or [])),
        )
    return config


def load_project_config(path: Path | None) -> ProjectConfig:
    """Прочитать gorets.toml; отсутствие файла — пустая конфигурация, не ошибка."""
    if path is None or not path.exists():
        return ProjectConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise FeedConfigError(f"{path}: {exc}") from exc
    return parse_project_config(data, path.resolve().parent)
