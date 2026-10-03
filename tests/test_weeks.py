from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from gorets.digest.weeks import parse_week, previous_week, week_of

TZ = ZoneInfo("Asia/Tashkent")


def test_parse_week_boundaries_in_tashkent() -> None:
    week = parse_week("2026-W40", TZ)
    assert week.label == "2026-W40"
    assert week.start == datetime(2026, 9, 28, 0, 0, tzinfo=TZ)
    assert week.end == datetime(2026, 10, 5, 0, 0, tzinfo=TZ)
    assert week.last_day.isoformat() == "2026-10-04"
    # Понедельник 00:00 по Ташкенту — это воскресенье 19:00 UTC.
    assert week.start.astimezone(UTC) == datetime(2026, 9, 27, 19, 0, tzinfo=UTC)


def test_parse_week_accepts_lowercase_and_rejects_garbage() -> None:
    assert parse_week("2026-w1", TZ).label == "2026-W01"
    with pytest.raises(ValueError):
        parse_week("2026-40", TZ)
    with pytest.raises(ValueError):
        parse_week("2026-W60", TZ)


def test_week_of_and_previous_week() -> None:
    # Понедельник 5 октября 2026, 09:00 по Ташкенту: прошлая неделя — W40.
    now = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)
    assert week_of(now, TZ).label == "2026-W41"
    assert previous_week(now, TZ).label == "2026-W40"
    # Воскресенье поздно вечером по UTC — по Ташкенту уже понедельник.
    late = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)
    assert week_of(late, TZ).label == "2026-W41"
    assert previous_week(late, TZ).label == "2026-W40"


def test_year_boundary() -> None:
    assert parse_week("2026-W01", TZ).start.date().isoformat() == "2025-12-29"
    assert previous_week(datetime(2027, 1, 4, 9, 0, tzinfo=TZ), TZ).label == "2026-W53"
