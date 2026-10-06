"""HTTP API для других проектов Sayr: фиды с курсором, извлечения, итоги недель.

Только чтение. Доступ по токену из `.env` (`GORETS_API_TOKEN`), слушаем на
127.0.0.1: потребители живут на том же VPS.
"""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query

from gorets import __version__
from gorets.config import Settings
from gorets.events import EVENT_EXTRACTORS, build_events, to_ical
from gorets.feeds import ProjectConfig
from gorets.models import Message
from gorets.serialize import extraction_to_dict, message_link, message_to_dict
from gorets.storage import Repository

MAX_LIMIT = 500


def create_app(settings: Settings, repo: Repository, project: ProjectConfig) -> FastAPI:
    if not settings.api_token:
        raise RuntimeError("Не задан GORETS_API_TOKEN — без него API не запускается")
    token = settings.api_token
    app = FastAPI(title="sayr-gorets", version=__version__, docs_url=None, redoc_url=None)

    def require_token(
        authorization: Annotated[str | None, Header()] = None,
        x_api_token: Annotated[str | None, Header()] = None,
    ) -> None:
        provided = x_api_token
        if authorization and authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        if not provided or not secrets.compare_digest(provided, token):
            raise HTTPException(status_code=401, detail="Нужен токен API")

    router = APIRouter(dependencies=[Depends(require_token)])
    usernames: dict[int, str | None] = {}

    def username_of(chat_id: int) -> str | None:
        if chat_id not in usernames:
            chat = repo.get_chat(chat_id)
            usernames[chat_id] = chat.username if chat else None
        return usernames[chat_id]

    def feed_chat_id(name: str) -> int:
        if name not in project.feeds:
            raise HTTPException(status_code=404, detail=f"Фид {name!r} не описан")
        chat_id = repo.resolve_chat_id(project.feeds[name].chat)
        if chat_id is None:
            raise HTTPException(status_code=404, detail=f"Чат фида {name!r} ещё не собран")
        return chat_id

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Живость сервиса и свежесть данных: без токена, для мониторинга."""
        runs = repo.last_runs_by_kind()
        now = datetime.now(UTC)
        collect = runs.get("collect")
        fresh = bool(
            collect
            and collect.status == "ok"
            and collect.finished_at
            and now - collect.finished_at < timedelta(hours=36)
        )
        return {
            "ok": True,
            "collect_fresh": fresh,
            "version": __version__,
            "runs": {
                kind: {
                    "status": run.status,
                    "started_at": run.started_at.isoformat(),
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                    "error": run.error,
                }
                for kind, run in runs.items()
            },
        }

    @router.get("/feeds")
    def feeds() -> dict[str, Any]:
        return {
            "items": [
                {
                    "name": f.name,
                    "chat": f.chat,
                    "topics": list(f.topics),
                    "description": f.description,
                    "keep_contacts": f.keep_contacts,
                }
                for f in project.feeds.values()
            ]
        }

    @router.get("/feeds/{name}/messages")
    def feed_messages(
        name: str,
        after: int | None = Query(None, description="курсор: msg_id последнего полученного"),
        since: datetime | None = None,
        until: datetime | None = None,
        q: str | None = Query(None, min_length=2, description="подстрока в тексте"),
        limit: int = Query(100, ge=1, le=MAX_LIMIT),
        dedupe: bool = Query(
            False, description="пропускать повторы одного текста (и из других чатов)"
        ),
    ) -> dict[str, Any]:
        chat_id = feed_chat_id(name)
        rows = repo.feed_messages(
            chat_id,
            project.feeds[name].topics,
            after_id=after,
            since=since,
            until=until,
            q=q,
            limit=limit + 1,
            dedupe=dedupe,
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        username = username_of(chat_id)
        return {
            "items": [message_to_dict(m, username) for m in rows],
            "next_cursor": rows[-1].msg_id if rows else after,
            "has_more": has_more,
        }

    @router.get("/search")
    def search(
        q: str = Query(..., min_length=2, description='запрос: слова, "фраза", -минус'),
        feed: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = Query(50, ge=1, le=MAX_LIMIT),
    ) -> dict[str, Any]:
        """Полнотекстовый поиск по всем собранным сообщениям (русская морфология)."""
        chat_id = feed_chat_id(feed) if feed else None
        topics = project.feeds[feed].topics if feed else ()
        rows = repo.search_messages(
            q, chat_id=chat_id, topics=topics, since=since, until=until, limit=limit
        )
        return {"items": [message_to_dict(m, username_of(m.chat_id)) for m in rows]}

    @router.get("/messages/{chat}/{msg_id}/duplicates")
    def duplicates(chat: str, msg_id: int) -> dict[str, Any]:
        """Тот же текст в других чатах или ветках (по отпечатку)."""
        chat_id = repo.resolve_chat_id(chat)
        if chat_id is None:
            raise HTTPException(status_code=404, detail="Чат не найден")
        with repo.session() as s:
            m = s.get(Message, (chat_id, msg_id))
        if m is None:
            raise HTTPException(status_code=404, detail="Сообщения нет")
        if not m.fingerprint:
            return {"items": []}
        rows = [
            d
            for d in repo.duplicates_of(m.fingerprint)
            if (d.chat_id, d.msg_id) != (chat_id, msg_id)
        ]
        return {"items": [message_to_dict(d, username_of(d.chat_id)) for d in rows]}

    @router.get("/places/top")
    def places_top(
        since: datetime | None = None, limit: int = Query(30, ge=1, le=200)
    ) -> dict[str, Any]:
        """Места каталога по числу упоминаний — «места в ходу» в любой момент."""
        return {"items": repo.top_places(since=since, limit=limit)}

    @router.get("/places/{slug}/mentions")
    def place_mentions(
        slug: str, since: datetime | None = None, limit: int = Query(100, ge=1, le=MAX_LIMIT)
    ) -> dict[str, Any]:
        """Всё, что извлекли о месте: упоминания, состояние, туры, вопросы."""
        rows = repo.place_mentions(slug, since=since, limit=limit)
        return {"items": [extraction_to_dict(e, username_of(e.chat_id)) for e in rows]}

    @router.get("/questions/themes")
    def question_themes(since: datetime | None = None) -> dict[str, Any]:
        """О чём спрашивают, по темам со счётчиками — бэклог приложения."""
        items = repo.question_themes(since=since)
        for theme in items:
            for ex in theme["examples"]:
                ex["link"] = message_link(username_of(ex["chat_id"]), ex["chat_id"], ex["msg_id"])
        return {"items": items}

    @router.get("/stats/authors")
    def stats_authors(
        feed: str,
        since: datetime | None = None,
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        """Самые активные и самые «отвечаемые» авторы фида (только хеши)."""
        chat_id = feed_chat_id(feed)
        return {
            "items": repo.author_stats(
                chat_id, project.feeds[feed].topics, since=since, limit=limit
            )
        }

    def _events(
        date_from: date | None, date_to: date | None, kind: str | None, since: datetime | None
    ) -> list[Any]:
        rows = repo.extractions_for_events(list(EVENT_EXTRACTORS), since=since)
        return build_events(
            rows,
            link=lambda chat_id, msg_id: message_link(username_of(chat_id), chat_id, msg_id),
            date_from=date_from,
            date_to=date_to,
            kind=kind,
        )

    @router.get("/events")
    def events(
        date_from: date | None = Query(None, alias="from"),
        date_to: date | None = Query(None, alias="to"),
        kind: str | None = Query(None, pattern="^(tour|companions)$"),
        since: datetime | None = Query(None, description="по дате публикации"),
    ) -> dict[str, Any]:
        """Календарь выходов: туры из афиш и попутчики, дубли из разных чатов схлопнуты."""
        return {"items": [ev.to_dict() for ev in _events(date_from, date_to, kind, since)]}

    @router.get("/events.ics")
    def events_ics(
        date_from: date | None = Query(None, alias="from"),
        date_to: date | None = Query(None, alias="to"),
        kind: str | None = Query(None, pattern="^(tour|companions)$"),
    ) -> Any:
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(
            to_ical(_events(date_from, date_to, kind, None)), media_type="text/calendar"
        )

    @router.get("/extractors")
    def extractors() -> dict[str, Any]:
        counts = repo.extraction_counts()
        return {
            "items": [
                {
                    "name": e.name,
                    "feeds": list(e.feeds),
                    "description": e.description,
                    "model": e.model or settings.extract_model,
                    "schema": e.schema,
                    "counts": {c["status"]: c["count"] for c in counts if c["extractor"] == e.name},
                }
                for e in project.extractors.values()
            ]
        }

    @router.get("/extractions/{name}")
    def extractions(
        name: str,
        after: int | None = Query(None, description="курсор: id последней полученной записи"),
        since: datetime | None = Query(None, description="по дате сообщения"),
        until: datetime | None = None,
        status: str | None = Query("ok", description="ok | skipped | error | all"),
        limit: int = Query(100, ge=1, le=MAX_LIMIT),
    ) -> dict[str, Any]:
        if name not in project.extractors:
            raise HTTPException(status_code=404, detail=f"Извлекатель {name!r} не описан")
        rows = repo.list_extractions(
            name,
            after_id=after,
            since=since,
            until=until,
            status=None if status in (None, "all") else status,
            limit=limit + 1,
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {
            "items": [extraction_to_dict(e, username_of(e.chat_id)) for e in rows],
            "next_cursor": rows[-1].id if rows else after,
            "has_more": has_more,
        }

    @router.get("/messages/{chat}/{msg_id}")
    def one_message(chat: str, msg_id: int) -> dict[str, Any]:
        chat_id = repo.resolve_chat_id(chat)
        if chat_id is None:
            raise HTTPException(status_code=404, detail="Чат не найден")
        with repo.session() as s:
            m = s.get(Message, (chat_id, msg_id))
        if m is None:
            raise HTTPException(status_code=404, detail="Сообщения нет (не собрано или удалено)")
        return message_to_dict(m, username_of(chat_id))

    @router.get("/digests")
    def digests(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
        return {
            "items": [
                {
                    "week": d.week,
                    "status": d.status,
                    "messages": d.messages_count,
                    "authors": d.authors_count,
                    "cost_usd": float(d.cost_usd or 0),
                    "created_at": d.created_at.isoformat(),
                    "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
                }
                for d in repo.list_digests(limit)
            ]
        }

    @router.get("/digests/{week}")
    def digest(week: str, format: str = Query("json", pattern="^(json|md)$")) -> Any:
        d = repo.get_digest(week.upper())
        if d is None or d.status != "done":
            raise HTTPException(status_code=404, detail="Итога за эту неделю нет")
        if format == "md":
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse(d.report_md or "", media_type="text/markdown")
        return d.report_json

    app.include_router(router)
    return app
