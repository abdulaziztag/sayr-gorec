from datetime import datetime
from zoneinfo import ZoneInfo

from gorets.digest.aggregate import compute_stats, merge_chunk_results, normalize_name
from gorets.digest.chunking import ChunkMessage

TZ = ZoneInfo("Asia/Tashkent")


def test_normalize_name() -> None:
    assert normalize_name("Бельдерсай!") == normalize_name("бельдерсай") == "бельдерсай"
    assert normalize_name("Большой  Чимган") == "большой чимган"
    assert normalize_name("Ёлочка") == normalize_name("елочка")


def test_merge_places_by_slug_and_name() -> None:
    merged = merge_chunk_results(
        [
            {
                "places": [
                    {
                        "name": "Бельдерсай",
                        "slug": "beldersay",
                        "mentions": 3,
                        "summary": "снег",
                        "conditions": [{"kind": "snow", "text": "по колено", "date": "29.09"}],
                    },
                    {
                        "name": "Чимган",
                        "slug": None,
                        "mentions": 1,
                        "summary": "",
                        "conditions": [],
                    },
                ],
                "new_places": [
                    {
                        "name": "Урочище Х",
                        "hints": "за Чимганом",
                        "region": None,
                        "lat": None,
                        "lng": None,
                    }
                ],
                "trips": [
                    {"place": "Бельдерсай", "slug": "beldersay", "dates": "сб", "summary": "лёгкий"}
                ],
                "questions": [{"topic": "транспорт", "question": "как доехать", "answered": True}],
                "sayr_mentions": [],
            },
            {
                "places": [
                    {
                        "name": "Бельдерсай",
                        "slug": "beldersay",
                        "mentions": 2,
                        "summary": "лёд",
                        "conditions": [],
                    },
                    {
                        "name": "чимган",
                        "slug": None,
                        "mentions": 4,
                        "summary": "",
                        "conditions": [],
                    },
                ],
                "new_places": [
                    {
                        "name": "урочище х",
                        "hints": "озеро",
                        "region": "Ташкентская",
                        "lat": 41.1,
                        "lng": 70.1,
                    }
                ],
                "trips": [
                    {"place": "бельдерсай", "slug": "beldersay", "dates": "сб", "summary": "дубль"}
                ],
                "questions": [],
                "sayr_mentions": [{"summary": "хвалят треки", "sentiment": "positive"}],
            },
            {"places": "мусор"},
        ]
    )
    assert [p["name"] for p in merged["places"]] == ["Бельдерсай", "Чимган"]
    assert merged["places"][0]["mentions"] == 5
    assert merged["places"][0]["summary"] == "снег лёд"
    assert merged["places"][0]["conditions"][0]["kind"] == "snow"
    assert merged["places"][1]["mentions"] == 5
    assert len(merged["new_places"]) == 1
    assert merged["new_places"][0]["mentions"] == 2
    assert merged["new_places"][0]["hints"] == "за Чимганом; озеро"
    assert merged["new_places"][0]["region"] == "Ташкентская"
    assert merged["new_places"][0]["lat"] == 41.1
    assert len(merged["trips"]) == 1
    assert len(merged["questions"]) == 1
    assert len(merged["sayr_mentions"]) == 1


def test_compute_stats_counts_people_and_topics() -> None:
    def m(i, topic, author, text="т", media=None):
        return ChunkMessage(
            chat_id=1,
            msg_id=i,
            date=datetime(2026, 9, 28 + i % 3, 10, tzinfo=TZ),
            topic_id=topic,
            topic_title=f"Ветка {topic}",
            reply_to=None,
            text=text,
            author_hash=author,
            media_type=media,
        )

    messages = [
        m(1, 1, "a"),
        m(2, 1, "b"),
        m(3, 2, "a", "", "photo"),
        m(4, 2, "c", "", "gpx"),
        m(5, 2, None, "x", "location"),
    ]
    stats = compute_stats(messages, [], {1: "@chat"})
    assert stats["messages"] == 5
    assert stats["messages_with_text"] == 3
    assert stats["authors"] == 3
    assert stats["topics"] == 2
    assert stats["tracks"] == 1 and stats["points"] == 1
    assert stats["media"] == {"photo": 1, "gpx": 1, "location": 1}
    assert stats["top_topics"][0] == {
        "chat": "@chat",
        "topic_id": 2,
        "title": "Ветка 2",
        "messages": 3,
        "authors": 2,
    }
    assert sum(stats["by_day"].values()) == 5
