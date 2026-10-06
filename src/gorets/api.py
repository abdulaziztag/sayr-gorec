"""HTTP API для других проектов Sayr: фиды с курсором, извлечения, итоги недель.

Только чтение. Доступ по токену из `.env` (`GORETS_API_TOKEN`), слушаем на
127.0.0.1: потребители живут на том же VPS.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query

from gorets import __version__
from gorets.config import Settings
from gorets.feeds import ProjectConfig
from gorets.models import Extraction, Message
from gorets.storage import Repository

MAX_LIMIT = 500


def message_link(username: str | None, chat_id: int, msg_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{msg_id}"
    return f"https://t.me/c/{chat_id}/{msg_id}"


def message_to_dict(m: Message, username: str | None) -> dict[str, Any]:
    return {
        "chat_id": m.chat_id,
        "msg_id": m.msg_id,
        "date": m.date.isoformat(),
        "topic_id": m.topic_id,
        "topic_title": m.topic_title,
        "reply_to_msg_id": m.reply_to_msg_id,
        "author": m.author_hash,
        "text": m.text,
        "media_type": m.media_type,
        "file_name": m.file_name,
        "lat": float(m.lat) if m.lat is not None else None,
        "lng": float(m.lng) if m.lng is not None else None,
        "forwarded_from": m.forwarded_from,
        "edited_at": m.edited_at.isoformat() if m.edited_at else None,
        "link": message_link(username, m.chat_id, m.msg_id),
    }


def extraction_to_dict(e: Extraction, username: str | None) -> dict[str, Any]:
    return {
        "id": e.id,
        "extractor": e.extractor,
        "chat_id": e.chat_id,
        "msg_id": e.msg_id,
        "message_date": e.message_date.isoformat(),
        "topic_id": e.topic_id,
        "topic_title": e.topic_title,
        "status": e.status,
        "data": e.data,
        "error": e.error,
        "model": e.model,
        "created_at": e.created_at.isoformat(),
        "link": message_link(username, e.chat_id, e.msg_id),
    }


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
        return {"ok": True, "version": __version__}

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
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        username = username_of(chat_id)
        return {
            "items": [message_to_dict(m, username) for m in rows],
            "next_cursor": rows[-1].msg_id if rows else after,
            "has_more": has_more,
        }

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
