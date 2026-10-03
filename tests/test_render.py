from gorets.digest.render import render_markdown, render_telegram
from gorets.telegram.deliver import split_message

REPORT = {
    "week": "2026-W40",
    "period": {"start_date": "28.09.2026", "end_date": "04.10.2026"},
    "headline": "Неделя снега.",
    "places": [
        {
            "name": "Бельдерсай",
            "slug": "beldersay",
            "url": "https://sayr.info/p/beldersay",
            "mentions": 12,
            "summary": "Все идут туда.",
            "conditions": [{"kind": "snow", "text": "выше 2500 снег", "date": "30.09"}],
        },
        {"name": "Безымянная", "slug": None, "mentions": 2, "summary": "", "conditions": []},
    ],
    "new_places": [
        {
            "name": "Урочище Х",
            "hints": "за Чимганом",
            "region": "Ташкентская",
            "lat": 41.1234,
            "lng": 70.5678,
            "mentions": 3,
        }
    ],
    "trips": [
        {
            "place": "Бельдерсай",
            "slug": "beldersay",
            "url": "https://sayr.info/p/beldersay",
            "dates": "3–4 октября",
            "summary": "ночёвка, средняя сложность",
        }
    ],
    "questions": [
        {
            "theme": "Транспорт",
            "examples": "как доехать до Чимгана",
            "count": 5,
            "app_gap": "нет раздела «как добраться»",
        }
    ],
    "sayr": {
        "summary": "Хвалили треки.",
        "items": [{"summary": "трек помог", "sentiment": "positive"}],
    },
    "stats": {
        "messages": 300,
        "messages_with_text": 250,
        "authors": 40,
        "topics": 6,
        "chunks": 4,
        "tracks": 2,
        "points": 1,
        "by_day": {"2026-09-28": 50, "2026-09-29": 60},
        "top_topics": [{"title": "Выходы", "messages": 100, "authors": 20}],
    },
    "models": {"chunk": "claude-haiku-4-5-20251001", "synthesis": "claude-sonnet-5-5"},
    "usage": {"input_tokens": 50000, "output_tokens": 8000, "cost_usd": 0.1234},
    "notes": ["кусок 2026-W40-003 не разобран: таймаут"],
}


def test_markdown_has_all_sections_and_links() -> None:
    md = render_markdown(REPORT)
    assert md.startswith("# ГОРЕЦ, неделя 2026-W40 (28.09.2026 — 04.10.2026)")
    for title in (
        "## 1. Места в ходу",
        "## 2. Мест нет в каталоге",
        "## 3. Выходы и сборы",
        "## 4. Частые вопросы",
        "## 5. Упоминания Sayr",
        "## 6. Цифры недели",
    ):
        assert title in md
    assert "[Бельдерсай](https://sayr.info/p/beldersay)" in md
    assert "снег, 30.09: выше 2500 снег" in md
    assert "Координаты: 41.123, 70.568" in md
    assert "чего не хватает в Sayr: нет раздела" in md
    assert "трек помог (хорошо)" in md
    assert "участников: 40" in md
    assert "≈ $0.12" in md
    assert "не разобран" in md


def test_telegram_text_is_plain_and_fits_after_split() -> None:
    text = render_telegram(REPORT)
    assert "##" not in text and "**" not in text and "](" not in text
    assert "1. МЕСТА В ХОДУ" in text
    assert "Бельдерсай (https://sayr.info/p/beldersay)" in text
    assert all(len(p) <= 4096 for p in split_message(text))


def test_empty_report_renders_placeholders() -> None:
    md = render_markdown({"week": "2026-W41", "period": {}, "stats": {}})
    assert md.count("ничего не нашлось") == 4
    assert "Sayr на этой неделе не упоминали" in md
