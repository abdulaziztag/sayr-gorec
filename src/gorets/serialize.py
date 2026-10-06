"""Сообщение и извлечение в виде словаря: для HTTP API, вебхуков и MCP."""

from __future__ import annotations

from typing import Any

from gorets.models import Extraction, Message


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
        "fingerprint": m.fingerprint,
        "link": message_link(username, m.chat_id, m.msg_id),
    }


def row_to_dict(row: dict[str, Any], username: str | None) -> dict[str, Any]:
    """То же для строки, которую сборщик только что записал (без ORM-объекта)."""
    return {
        "chat_id": row["chat_id"],
        "msg_id": row["msg_id"],
        "date": row["date"].isoformat() if row.get("date") else None,
        "topic_id": row.get("topic_id"),
        "topic_title": row.get("topic_title"),
        "reply_to_msg_id": row.get("reply_to_msg_id"),
        "author": row.get("author_hash"),
        "text": row.get("text", ""),
        "media_type": row.get("media_type"),
        "file_name": row.get("file_name"),
        "lat": float(row["lat"]) if row.get("lat") is not None else None,
        "lng": float(row["lng"]) if row.get("lng") is not None else None,
        "forwarded_from": row.get("forwarded_from"),
        "edited_at": row["edited_at"].isoformat() if row.get("edited_at") else None,
        "fingerprint": row.get("fingerprint"),
        "link": message_link(username, row["chat_id"], row["msg_id"]),
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
