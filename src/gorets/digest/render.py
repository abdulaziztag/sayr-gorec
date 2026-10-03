"""Отчёт из JSON: Markdown для файла и базы, простой текст для Telegram.

Оба варианта строятся из одного JSON, поэтому у админки Sayr и у владельца
в Telegram одни и те же факты.
"""

from __future__ import annotations

from typing import Any

CONDITION_LABELS = {
    "snow": "снег",
    "water": "вода",
    "road": "дорога",
    "closure": "закрытие",
    "danger": "опасность",
    "weather": "погода",
    "other": "прочее",
}
SENTIMENT_LABELS = {"positive": "хорошо", "neutral": "нейтрально", "negative": "плохо"}


def _period(report: dict[str, Any]) -> str:
    period = report.get("period", {})
    return f"{period.get('start_date', '')} — {period.get('end_date', '')}"


def _place_link(place: dict[str, Any], markdown: bool) -> str:
    name = place.get("name") or place.get("slug") or "?"
    url = place.get("url")
    if url and markdown:
        return f"[{name}]({url})"
    if url:
        return f"{name} ({url})"
    return str(name)


def _conditions(place: dict[str, Any]) -> list[str]:
    lines = []
    for c in place.get("conditions") or []:
        label = CONDITION_LABELS.get(str(c.get("kind")), "прочее")
        date = f", {c['date']}" if c.get("date") else ""
        lines.append(f"{label}{date}: {c.get('text', '')}")
    return lines


def _sections(report: dict[str, Any], markdown: bool) -> list[tuple[str, list[str]]]:
    b = "**" if markdown else ""
    bullet = "- " if markdown else "• "
    sub = "  - " if markdown else "   – "
    nothing = ["ничего не нашлось"]

    places = []
    for place in report.get("places") or []:
        line = (
            f"{bullet}{b}{_place_link(place, markdown)}{b} — упоминаний: {place.get('mentions', 0)}"
        )
        if place.get("summary"):
            line += f". {place['summary']}"
        places.append(line)
        places.extend(f"{sub}{c}" for c in _conditions(place))

    new_places = []
    for place in report.get("new_places") or []:
        line = f"{bullet}{b}{place.get('name', '?')}{b} — упоминаний: {place.get('mentions', 0)}"
        if place.get("hints"):
            line += f". {place['hints']}"
        if place.get("region"):
            line += f" Регион: {place['region']}."
        if place.get("lat") is not None and place.get("lng") is not None:
            line += f" Координаты: {float(place['lat']):.3f}, {float(place['lng']):.3f}."
        new_places.append(line)

    trips = []
    for trip in report.get("trips") or []:
        name = _place_link(trip, markdown) if trip.get("url") else str(trip.get("place", "?"))
        dates = f" ({trip['dates']})" if trip.get("dates") else ""
        line = f"{bullet}{b}{name}{b}{dates}"
        if trip.get("summary"):
            line += f": {trip['summary']}"
        trips.append(line)

    questions = []
    for q in report.get("questions") or []:
        line = f"{bullet}{b}{q.get('theme', '?')}{b} — {q.get('count', 0)} раз(а)"
        if q.get("examples"):
            line += f". {q['examples']}"
        questions.append(line)
        if q.get("app_gap"):
            questions.append(f"{sub}чего не хватает в Sayr: {q['app_gap']}")

    sayr = report.get("sayr") or {}
    sayr_lines = []
    if sayr.get("summary"):
        sayr_lines.append(sayr["summary"])
    for item in sayr.get("items") or []:
        tone = SENTIMENT_LABELS.get(str(item.get("sentiment")), "")
        sayr_lines.append(f"{bullet}{item.get('summary', '')}" + (f" ({tone})" if tone else ""))

    stats = report.get("stats") or {}
    usage = report.get("usage") or {}
    models = report.get("models") or {}
    numbers = [
        f"{bullet}сообщений: {stats.get('messages', 0)}, с текстом: "
        f"{stats.get('messages_with_text', 0)}, участников: {stats.get('authors', 0)}, "
        f"веток: {stats.get('topics', 0)}",
        f"{bullet}треков (GPX/KML): {stats.get('tracks', 0)}, "
        f"точек и мест: {stats.get('points', 0)}",
    ]
    for topic in (stats.get("top_topics") or [])[:5]:
        numbers.append(
            f"{sub}{topic.get('title') or 'без ветки'}: {topic.get('messages', 0)} сообщений, "
            f"{topic.get('authors', 0)} участников"
        )
    if stats.get("by_day"):
        days = ", ".join(f"{day[5:]}: {n}" for day, n in stats["by_day"].items())
        numbers.append(f"{bullet}по дням: {days}")
    if usage:
        numbers.append(
            f"{bullet}разбор: кусков {stats.get('chunks', 0)}, токенов "
            f"{usage.get('input_tokens', 0)} на входе и {usage.get('output_tokens', 0)} на выходе, "
            f"≈ ${float(usage.get('cost_usd', 0)):.2f} ({models.get('chunk', '?')} + "
            f"{models.get('synthesis', '?')})"
        )
    if report.get("notes"):
        numbers.extend(f"{bullet}{note}" for note in report["notes"])

    return [
        ("1. Места в ходу", places or nothing),
        ("2. Мест нет в каталоге — кандидаты в черновики", new_places or nothing),
        ("3. Выходы и сборы", trips or nothing),
        ("4. Частые вопросы — чего не хватает в приложении", questions or nothing),
        ("5. Упоминания Sayr", sayr_lines or ["Sayr на этой неделе не упоминали"]),
        ("6. Цифры недели", numbers),
    ]


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# ГОРЕЦ, неделя {report.get('week', '')} ({_period(report)})", ""]
    if report.get("headline"):
        lines += [report["headline"], ""]
    for title, body in _sections(report, markdown=True):
        lines += [f"## {title}", "", *body, ""]
    return "\n".join(lines).rstrip() + "\n"


def render_telegram(report: dict[str, Any]) -> str:
    lines = [f"ГОРЕЦ, неделя {report.get('week', '')} ({_period(report)})", ""]
    if report.get("headline"):
        lines += [report["headline"], ""]
    for title, body in _sections(report, markdown=False):
        lines += [title.upper(), *body, ""]
    return "\n".join(lines).rstrip()
