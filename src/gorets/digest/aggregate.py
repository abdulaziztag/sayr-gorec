"""Сведение результатов по кускам и цифры недели.

Сколько было людей и сообщений, считаем сами, не моделью. Результаты кусков
сливаем по slug или нормализованному названию, чтобы модели сведения
доставался уже компактный список.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from gorets.digest.chunking import Chunk, ChunkMessage
from gorets.digest.schemas import EMPTY_CHUNK_RESULT

_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def normalize_name(name: str) -> str:
    lowered = _NON_WORD_RE.sub(" ", name.lower().replace("ё", "е"))
    return " ".join(lowered.split())


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _as_int(value: Any, default: int = 1) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return default


def sanitize_chunk_result(data: dict[str, Any]) -> dict[str, Any]:
    """Привести ответ модели к ожидаемой форме, не падая на пропущенных полях."""
    result = {key: _as_list(data.get(key)) for key in EMPTY_CHUNK_RESULT}
    for place in result["places"]:
        place.setdefault("name", "")
        place.setdefault("slug", None)
        place["mentions"] = _as_int(place.get("mentions"))
        place.setdefault("summary", "")
        place["conditions"] = _as_list(place.get("conditions"))
    return result


def merge_chunk_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    places: dict[str, dict[str, Any]] = {}
    new_places: dict[str, dict[str, Any]] = {}
    trips: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    sayr: list[dict[str, Any]] = []
    seen_trips: set[tuple[str, str]] = set()

    for raw in results:
        data = sanitize_chunk_result(raw)
        for place in data["places"]:
            name = str(place.get("name") or "").strip()
            slug = place.get("slug") or None
            if not name and not slug:
                continue
            key = f"slug:{slug}" if slug else f"name:{normalize_name(name)}"
            entry = places.setdefault(
                key,
                {
                    "name": name or slug,
                    "slug": slug,
                    "mentions": 0,
                    "summaries": [],
                    "conditions": [],
                },
            )
            entry["mentions"] += place["mentions"]
            if place.get("summary"):
                entry["summaries"].append(str(place["summary"]))
            entry["conditions"].extend(
                {
                    "kind": str(c.get("kind") or "other"),
                    "text": str(c.get("text") or ""),
                    "date": c.get("date"),
                }
                for c in place["conditions"]
                if c.get("text")
            )
        for place in data["new_places"]:
            name = str(place.get("name") or "").strip()
            if not name:
                continue
            key = normalize_name(name)
            entry = new_places.setdefault(
                key,
                {
                    "name": name,
                    "hints": [],
                    "region": None,
                    "lat": None,
                    "lng": None,
                    "mentions": 0,
                },
            )
            entry["mentions"] += 1
            if place.get("hints"):
                entry["hints"].append(str(place["hints"]))
            entry["region"] = entry["region"] or place.get("region")
            if entry["lat"] is None and place.get("lat") is not None:
                entry["lat"], entry["lng"] = place.get("lat"), place.get("lng")
        for trip in data["trips"]:
            key = (normalize_name(str(trip.get("place") or "")), str(trip.get("dates") or ""))
            if key in seen_trips:
                continue
            seen_trips.add(key)
            trips.append(trip)
        questions.extend(data["questions"])
        sayr.extend(data["sayr_mentions"])

    merged_places = sorted(places.values(), key=lambda p: (-p["mentions"], p["name"]))
    for entry in merged_places:
        entry["summary"] = " ".join(dict.fromkeys(entry.pop("summaries")))
    merged_new = sorted(new_places.values(), key=lambda p: (-p["mentions"], p["name"]))
    for entry in merged_new:
        entry["hints"] = "; ".join(dict.fromkeys(entry["hints"]))
    return {
        "places": merged_places,
        "new_places": merged_new,
        "trips": trips,
        "questions": questions,
        "sayr_mentions": sayr,
    }


def compute_stats(
    messages: list[ChunkMessage], chunks: list[Chunk], chat_names: dict[int, str]
) -> dict[str, Any]:
    authors = {m.author_hash for m in messages if m.author_hash}
    by_topic: Counter[tuple[int, int | None, str | None]] = Counter()
    by_day: Counter[str] = Counter()
    media: Counter[str] = Counter()
    topic_authors: dict[tuple[int, int | None], set[str]] = defaultdict(set)
    for m in messages:
        by_topic[(m.chat_id, m.topic_id, m.topic_title)] += 1
        by_day[m.date.strftime("%Y-%m-%d")] += 1
        if m.media_type:
            media[m.media_type] += 1
        if m.author_hash:
            topic_authors[(m.chat_id, m.topic_id)].add(m.author_hash)
    topics = [
        {
            "chat": chat_names.get(chat_id, str(chat_id)),
            "topic_id": topic_id,
            "title": title,
            "messages": count,
            "authors": len(topic_authors[(chat_id, topic_id)]),
        }
        for (chat_id, topic_id, title), count in by_topic.most_common()
    ]
    return {
        "messages": len(messages),
        "messages_with_text": sum(1 for m in messages if m.text.strip()),
        "authors": len(authors),
        "topics": len(by_topic),
        "chunks": len(chunks),
        "by_day": dict(sorted(by_day.items())),
        "media": dict(media.most_common()),
        "tracks": media.get("gpx", 0) + media.get("kml", 0),
        "points": media.get("location", 0) + media.get("venue", 0),
        "top_topics": topics[:10],
    }
