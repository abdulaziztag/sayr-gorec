from datetime import UTC, date, datetime
from types import SimpleNamespace

from gorets.events import build_events, to_ical


def ext(extractor, msg_id, data, chat_id=1, day=1):
    return SimpleNamespace(
        extractor=extractor,
        chat_id=chat_id,
        msg_id=msg_id,
        message_date=datetime(2026, 10, day, 9, tzinfo=UTC),
        data=data,
    )


TOUR = {
    "title": "Бельдерсай за день",
    "place": "Бельдерсай",
    "place_slug": "beldersay",
    "date_start": "2026-10-12",
    "date_end": None,
    "price": 300000,
    "currency": "сум",
    "organizer": "Lider Hikers",
    "contact": "+998901234567",
    "summary": "лёгкий выход",
}


def test_events_dedupe_and_filters() -> None:
    rows = [
        ext("afisha_tour", 1, TOUR),
        ext("afisha_tour", 7, {**TOUR, "title": "та же афиша в другом чате"}, chat_id=2, day=2),
        ext(
            "companion_request",
            3,
            {
                "place": "Чимган",
                "place_slug": None,
                "date_start": "2026-10-19",
                "date_end": "2026-10-20",
                "summary": "ищу компанию",
            },
        ),
        ext(
            "companion_request",
            4,
            {"place": "Чимган", "date_start": None, "summary": "когда-нибудь"},
        ),
        ext("trail_condition", 5, {"place": "Чимган"}),
    ]
    events = build_events(rows, link=lambda c, m: f"https://t.me/c/{c}/{m}")
    assert [e.kind for e in events] == ["tour", "companions", "companions"]
    tour = events[0]
    assert tour.sources == ["https://t.me/c/1/1", "https://t.me/c/2/7"]
    assert tour.date_start == date(2026, 10, 12) and tour.price == 300000
    assert events[2].date_start is None
    only_tours = build_events(rows, link=lambda c, m: "", kind="tour")
    assert len(only_tours) == 1
    window = build_events(
        rows, link=lambda c, m: "", date_from=date(2026, 10, 15), date_to=date(2026, 10, 31)
    )
    assert [e.kind for e in window] == ["companions"] and window[0].date_start == date(2026, 10, 19)
    as_dict = tour.to_dict()
    assert as_dict["date_start"] == "2026-10-12" and as_dict["posted_at"].startswith("2026-10-01")


def test_ical_output() -> None:
    events = build_events([ext("afisha_tour", 1, TOUR)], link=lambda c, m: "https://t.me/x/1")
    ics = to_ical(events)
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert "DTSTART;VALUE=DATE:20261012" in ics
    assert "DTEND;VALUE=DATE:20261013" in ics
    assert "SUMMARY:Бельдерсай за день" in ics
    assert "UID:1-1@sayr-gorets" in ics
    assert "Контакт: +998901234567" in ics
    assert ics.endswith("END:VCALENDAR\r\n")
