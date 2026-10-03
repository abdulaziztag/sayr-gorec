"""Недели по Ташкенту: с понедельника 00:00 по воскресенье включительно."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{1,2})$")


@dataclass(frozen=True)
class Week:
    label: str
    start: datetime  # понедельник 00:00 местного времени
    end: datetime  # следующий понедельник 00:00, не включая

    @property
    def last_day(self) -> date:
        return (self.end - timedelta(days=1)).date()


def week_from_monday(monday: date, tz: ZoneInfo) -> Week:
    start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    iso = monday.isocalendar()
    return Week(label=f"{iso.year}-W{iso.week:02d}", start=start, end=start + timedelta(days=7))


def parse_week(label: str, tz: ZoneInfo) -> Week:
    m = _WEEK_RE.match(label.strip().upper())
    if not m:
        raise ValueError(f"Неделя задаётся как ГГГГ-Wнн, например 2026-W40, а не {label!r}")
    year, number = int(m.group(1)), int(m.group(2))
    try:
        monday = date.fromisocalendar(year, number, 1)
    except ValueError as exc:
        raise ValueError(f"Нет такой недели: {label}") from exc
    return week_from_monday(monday, tz)


def week_of(moment: datetime, tz: ZoneInfo) -> Week:
    local = moment.astimezone(tz)
    monday = local.date() - timedelta(days=local.weekday())
    return week_from_monday(monday, tz)


def previous_week(now: datetime, tz: ZoneInfo) -> Week:
    """Неделя, закончившаяся перед той, в которой находится `now`."""
    return week_of(now.astimezone(tz) - timedelta(days=7), tz)
