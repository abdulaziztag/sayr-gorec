"""Календарь выходов: туры из афиш и объявления попутчиков как события.

Строится из извлечений, дубли (одна афиша в двух чатах) схлопываются по
месту и дате. Отдаётся как JSON и как iCal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from gorets.digest.aggregate import normalize_name

EVENT_EXTRACTORS = {"afisha_tour": "tour", "companion_request": "companions"}


@dataclass
class Event:
    kind: str  # tour | companions
    title: str
    place: str
    place_slug: str | None
    date_start: date | None
    date_end: date | None
    price: float | None
    currency: str | None
    organizer: str | None
    contact: str | None
    summary: str
    link: str
    chat_id: int
    msg_id: int
    posted_at: datetime
    sources: list[str]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["date_start"] = self.date_start.isoformat() if self.date_start else None
        d["date_end"] = self.date_end.isoformat() if self.date_end else None
        d["posted_at"] = self.posted_at.isoformat()
        return d


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def build_events(
    extractions: list[Any],
    *,
    link: Any,
    date_from: date | None = None,
    date_to: date | None = None,
    kind: str | None = None,
) -> list[Event]:
    """`link(chat_id, msg_id)` → ссылка на оригинал."""
    events: dict[tuple[str, str, date | None], Event] = {}
    for e in extractions:
        event_kind = EVENT_EXTRACTORS.get(e.extractor)
        if event_kind is None or (kind and event_kind != kind):
            continue
        data = e.data or {}
        place = str(data.get("place") or "")
        start = _parse_date(data.get("date_start"))
        end = _parse_date(data.get("date_end"))
        if date_from and (start or e.message_date.date()) < date_from:
            continue
        if date_to and start and start > date_to:
            continue
        key = (event_kind, data.get("place_slug") or normalize_name(place), start)
        source = link(e.chat_id, e.msg_id)
        if key in events:
            events[key].sources.append(source)
            continue
        events[key] = Event(
            kind=event_kind,
            title=str(data.get("title") or data.get("summary") or place or "выход"),
            place=place,
            place_slug=data.get("place_slug"),
            date_start=start,
            date_end=end,
            price=data.get("price") if isinstance(data.get("price"), int | float) else None,
            currency=data.get("currency"),
            organizer=data.get("organizer"),
            contact=data.get("contact"),
            summary=str(data.get("summary") or ""),
            link=source,
            chat_id=e.chat_id,
            msg_id=e.msg_id,
            posted_at=e.message_date,
            sources=[source],
        )
    return sorted(events.values(), key=lambda ev: (ev.date_start or date.max, ev.posted_at))


def _ical_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def to_ical(events: list[Event], *, name: str = "ГОРЕЦ: выходы") -> str:
    """Календарь iCalendar; события без даты пропускаются."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//sayr-gorets//RU",
        f"X-WR-CALNAME:{_ical_escape(name)}",
    ]
    for ev in events:
        if ev.date_start is None:
            continue
        end = ev.date_end or ev.date_start
        end_exclusive = date.fromordinal(end.toordinal() + 1)
        description = ev.summary
        if ev.price:
            description += f"\nЦена: {ev.price:g} {ev.currency or ''}".rstrip()
        if ev.organizer:
            description += f"\nОрганизатор: {ev.organizer}"
        if ev.contact:
            description += f"\nКонтакт: {ev.contact}"
        description += f"\n{ev.link}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{ev.chat_id}-{ev.msg_id}@sayr-gorets",
            f"DTSTAMP:{ev.posted_at.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;VALUE=DATE:{ev.date_start.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{end_exclusive.strftime('%Y%m%d')}",
            f"SUMMARY:{_ical_escape(ev.title)}",
            f"LOCATION:{_ical_escape(ev.place)}",
            f"DESCRIPTION:{_ical_escape(description)}",
            f"URL:{ev.link}",
            f"CATEGORIES:{ev.kind}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
