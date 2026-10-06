"""MCP-сервер: те же данные, что в HTTP API, но для агентов — Claude Code,
проекта агрегатора и любого MCP-клиента.

Запуск: `gorets mcp` (stdio). Нужна дополнительная зависимость:
`uv sync --extra mcp`. Ходит в базу напрямую, токен API не нужен — доступ
определяется тем, кто может запустить процесс.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from gorets.config import Settings
from gorets.events import EVENT_EXTRACTORS, build_events
from gorets.feeds import ProjectConfig
from gorets.serialize import extraction_to_dict, message_link, message_to_dict
from gorets.storage import Repository


class McpNotInstalled(RuntimeError):
    pass


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=__import__("datetime").UTC)


def build_tools(settings: Settings, repo: Repository, project: ProjectConfig) -> dict[str, Any]:
    """Инструменты как обычные функции — их регистрирует сервер и проверяют тесты."""
    usernames: dict[int, str | None] = {}

    def username_of(chat_id: int) -> str | None:
        if chat_id not in usernames:
            chat = repo.get_chat(chat_id)
            usernames[chat_id] = chat.username if chat else None
        return usernames[chat_id]

    def feed_chat(name: str) -> int:
        feed = project.feed(name)
        chat_id = repo.resolve_chat_id(feed.chat)
        if chat_id is None:
            raise ValueError(f"Чат фида {name!r} ещё не собран")
        return chat_id

    def list_feeds() -> list[dict[str, Any]]:
        """Фиды (чат плюс ветки), которые описаны в gorets.toml."""
        return [
            {"name": f.name, "chat": f.chat, "topics": list(f.topics), "description": f.description}
            for f in project.feeds.values()
        ]

    def feed_messages(
        feed: str, after: int | None = None, since: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Сообщения фида по возрастанию msg_id; after — курсор, since — ISO-дата."""
        chat_id = feed_chat(feed)
        rows = repo.feed_messages(
            chat_id,
            project.feed(feed).topics,
            after_id=after,
            since=_parse_dt(since),
            limit=min(limit, 200) + 1,
        )
        has_more = len(rows) > min(limit, 200)
        rows = rows[: min(limit, 200)]
        return {
            "items": [message_to_dict(m, username_of(chat_id)) for m in rows],
            "next_cursor": rows[-1].msg_id if rows else after,
            "has_more": has_more,
        }

    def search_messages(
        query: str, feed: str | None = None, since: str | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Полнотекстовый поиск по сообщениям (русская морфология, синтаксис websearch)."""
        chat_id = feed_chat(feed) if feed else None
        topics = project.feed(feed).topics if feed else ()
        rows = repo.search_messages(
            query, chat_id=chat_id, topics=topics, since=_parse_dt(since), limit=min(limit, 200)
        )
        return [message_to_dict(m, username_of(m.chat_id)) for m in rows]

    def list_extractors() -> list[dict[str, Any]]:
        """Извлекатели: имя, фиды, описание, схема результата."""
        return [
            {
                "name": e.name,
                "feeds": list(e.feeds),
                "description": e.description,
                "schema": e.schema,
            }
            for e in project.extractors.values()
        ]

    def extractions(
        extractor: str, after: int | None = None, since: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Результаты извлекателя (status=ok); after — курсор по id, since — по дате сообщения."""
        project.extractor(extractor)
        rows = repo.list_extractions(
            extractor, after_id=after, since=_parse_dt(since), limit=min(limit, 200) + 1
        )
        has_more = len(rows) > min(limit, 200)
        rows = rows[: min(limit, 200)]
        return {
            "items": [extraction_to_dict(e, username_of(e.chat_id)) for e in rows],
            "next_cursor": rows[-1].id if rows else after,
            "has_more": has_more,
        }

    def events(
        date_from: str | None = None, date_to: str | None = None, kind: str | None = None
    ) -> list[dict[str, Any]]:
        """Календарь выходов: туры из афиш и попутчики; kind — tour или companions."""
        rows = repo.extractions_for_events(list(EVENT_EXTRACTORS))
        built = build_events(
            rows,
            link=lambda c, m: message_link(username_of(c), c, m),
            date_from=date.fromisoformat(date_from) if date_from else None,
            date_to=date.fromisoformat(date_to) if date_to else None,
            kind=kind,
        )
        return [ev.to_dict() for ev in built]

    def places_top(since: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        """Места каталога Sayr по числу упоминаний."""
        return repo.top_places(since=_parse_dt(since), limit=min(limit, 200))

    def place_mentions(
        slug: str, since: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Всё извлечённое о месте каталога по slug: упоминания, состояние, туры, вопросы."""
        rows = repo.place_mentions(slug, since=_parse_dt(since), limit=min(limit, 200))
        return [extraction_to_dict(e, username_of(e.chat_id)) for e in rows]

    def question_themes(since: str | None = None) -> list[dict[str, Any]]:
        """О чём спрашивают участники, по темам со счётчиками и примерами."""
        items = repo.question_themes(since=_parse_dt(since))
        for theme in items:
            for ex in theme["examples"]:
                ex["link"] = message_link(username_of(ex["chat_id"]), ex["chat_id"], ex["msg_id"])
        return items

    def digest(week: str | None = None, as_markdown: bool = False) -> Any:
        """Итог недели (ГГГГ-Wнн); без week — последний готовый."""
        if week:
            d = repo.get_digest(week.upper())
        else:
            done = [x for x in repo.list_digests(10) if x.status == "done"]
            d = done[0] if done else None
        if d is None or d.status != "done":
            raise ValueError("Готового итога за эту неделю нет")
        return d.report_md if as_markdown else d.report_json

    return {
        "list_feeds": list_feeds,
        "feed_messages": feed_messages,
        "search_messages": search_messages,
        "list_extractors": list_extractors,
        "extractions": extractions,
        "events": events,
        "places_top": places_top,
        "place_mentions": place_mentions,
        "question_themes": question_themes,
        "digest": digest,
    }


def create_server(settings: Settings, repo: Repository, project: ProjectConfig) -> Any:
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:
        raise McpNotInstalled(
            "MCP SDK не установлен: uv sync --extra mcp (или --no-dev --extra mcp на сервере)"
        ) from exc
    server = MCPServer(
        "sayr-gorets",
        instructions=(
            "Данные туристических форумов «ГОРЕЦ» и «Горняшка»: сообщения по фидам, "
            "полнотекстовый поиск, извлечения (туры из афиш, попутчики, состояние троп, "
            "упоминания мест, вопросы), календарь выходов, итоги недель. Авторы — только хеши."
        ),
    )
    for func in build_tools(settings, repo, project).values():
        server.tool()(func)
    return server
