"""JSON-схемы ответов модели.

Схема кусков — то, что извлекается из каждого куска недели; схема сведения —
содержательная часть отчёта. Итоговый JSON отчёта (см. `run.assemble_report`)
спроектирован так, чтобы потом лечь в админку Sayr без переделки.
"""

from __future__ import annotations

from typing import Any

CONDITION_KINDS = ["snow", "water", "road", "closure", "danger", "weather", "other"]
SENTIMENTS = ["positive", "neutral", "negative"]


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


CONDITION = _obj(
    {
        "kind": {"type": "string", "enum": CONDITION_KINDS},
        "text": {"type": "string"},
        "date": _nullable({"type": "string"}),
    }
)

PLACE = _obj(
    {
        "name": {"type": "string"},
        "slug": _nullable({"type": "string"}),
        "mentions": {"type": "integer"},
        "summary": {"type": "string"},
        "conditions": {"type": "array", "items": CONDITION},
    }
)

NEW_PLACE = _obj(
    {
        "name": {"type": "string"},
        "hints": {"type": "string"},
        "region": _nullable({"type": "string"}),
        "lat": _nullable({"type": "number"}),
        "lng": _nullable({"type": "number"}),
    }
)

TRIP = _obj(
    {
        "place": {"type": "string"},
        "slug": _nullable({"type": "string"}),
        "dates": _nullable({"type": "string"}),
        "summary": {"type": "string"},
    }
)

QUESTION = _obj(
    {
        "topic": {"type": "string"},
        "question": {"type": "string"},
        "answered": {"type": "boolean"},
    }
)

SAYR_MENTION = _obj(
    {
        "summary": {"type": "string"},
        "sentiment": {"type": "string", "enum": SENTIMENTS},
    }
)

CHUNK_SCHEMA = _obj(
    {
        "places": {"type": "array", "items": PLACE},
        "new_places": {"type": "array", "items": NEW_PLACE},
        "trips": {"type": "array", "items": TRIP},
        "questions": {"type": "array", "items": QUESTION},
        "sayr_mentions": {"type": "array", "items": SAYR_MENTION},
    }
)

REPORT_NEW_PLACE = _obj({**NEW_PLACE["properties"], "mentions": {"type": "integer"}})

REPORT_QUESTION = _obj(
    {
        "theme": {"type": "string"},
        "examples": {"type": "string"},
        "count": {"type": "integer"},
        "app_gap": {"type": "string"},
    }
)

SYNTHESIS_SCHEMA = _obj(
    {
        "headline": {"type": "string"},
        "places": {"type": "array", "items": PLACE},
        "new_places": {"type": "array", "items": REPORT_NEW_PLACE},
        "trips": {"type": "array", "items": TRIP},
        "questions": {"type": "array", "items": REPORT_QUESTION},
        "sayr": _obj(
            {
                "summary": {"type": "string"},
                "items": {"type": "array", "items": SAYR_MENTION},
            }
        ),
    }
)

EMPTY_CHUNK_RESULT: dict[str, Any] = {
    "places": [],
    "new_places": [],
    "trips": [],
    "questions": [],
    "sayr_mentions": [],
}
