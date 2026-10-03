"""Тесты базы: upsert без дублей, сроки хранения, удаление по просьбе, журнал, итоги."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from gorets.anonymize import author_hash
from gorets.storage import Repository, as_decimal_coord, normalize_chat_id

CHAT = 1234567890
SECRET = "test-secret"


def _row(msg_id: int, *, days_ago: int = 0, author: int = 1, text: str = "текст", **extra):
    base = {
        "chat_id": CHAT,
        "msg_id": msg_id,
        "date": datetime.now(UTC) - timedelta(days=days_ago),
        "topic_id": 1,
        "topic_title": "General",
        "reply_to_msg_id": None,
        "author_hash": author_hash(SECRET, author),
        "text": text,
        "media_type": None,
        "file_name": None,
        "lat": None,
        "lng": None,
        "forwarded_from": None,
        "edited_at": None,
    }
    base.update(extra)
    return base


def test_upsert_is_idempotent(repo: Repository) -> None:
    rows = [_row(1), _row(2), _row(3)]
    assert repo.upsert_messages(rows) == 3
    assert repo.upsert_messages(rows) == 0
    assert repo.count_messages(CHAT) == 3
    assert repo.last_msg_id(CHAT) == 3
    assert repo.last_msg_id(999) == 0


def test_upsert_updates_text_but_keeps_collected_at(repo: Repository) -> None:
    repo.upsert_messages([_row(1, text="старый")])
    with repo.session() as s:
        from gorets.models import Message

        before = s.get(Message, (CHAT, 1))
        collected = before.collected_at
    assert repo.upsert_messages([_row(1, text="новый", edited_at=datetime.now(UTC))]) == 0
    with repo.session() as s:
        after = s.get(Message, (CHAT, 1))
        assert after.text == "новый"
        assert after.edited_at is not None
        assert after.collected_at == collected


def test_coordinates_rounded_to_three_digits(repo: Repository) -> None:
    repo.upsert_messages(
        [
            _row(
                1,
                media_type="location",
                lat=as_decimal_coord(41.3110816),
                lng=as_decimal_coord(69.2797372),
            )
        ]
    )
    with repo.session() as s:
        from gorets.models import Message

        m = s.get(Message, (CHAT, 1))
        assert m.lat == Decimal("41.311")
        assert m.lng == Decimal("69.280")


def test_retention_deletes_only_old_messages(repo: Repository) -> None:
    repo.upsert_messages([_row(1, days_ago=100), _row(2, days_ago=91), _row(3, days_ago=89)])
    assert repo.delete_older_than(90) == 2
    assert repo.count_messages(CHAT) == 1
    assert repo.last_msg_id(CHAT) == 3


def test_forget_author_removes_everything_by_that_author(repo: Repository) -> None:
    repo.upsert_messages([_row(1, author=7), _row(2, author=7), _row(3, author=8)])
    assert repo.forget_author(author_hash(SECRET, 7)) == 2
    assert repo.forget_author(author_hash(SECRET, 7)) == 0
    assert repo.count_messages(CHAT) == 1


def test_forget_message(repo: Repository) -> None:
    repo.upsert_messages([_row(1), _row(2)])
    assert repo.forget_message(CHAT, 1) == 1
    assert repo.forget_message(CHAT, 1) == 0
    assert repo.count_messages(CHAT) == 1


def test_chat_resolution(repo: Repository) -> None:
    repo.upsert_chat(CHAT, username="gorets_uzb", title="ГОРЕЦ", is_forum=True)
    repo.upsert_chat(CHAT, username="gorets_uzb", title="ГОРЕЦ (новое)", is_forum=True)
    assert repo.resolve_chat_id("gorets_uzb") == CHAT
    assert repo.resolve_chat_id("@Gorets_UZB") == CHAT
    assert repo.resolve_chat_id(str(CHAT)) == CHAT
    assert repo.resolve_chat_id(f"-100{CHAT}") == CHAT
    assert repo.resolve_chat_id("nope") is None


def test_messages_between_period(repo: Repository) -> None:
    now = datetime.now(UTC)
    repo.upsert_messages([_row(1, days_ago=10), _row(2, days_ago=5), _row(3, days_ago=1)])
    found = repo.messages_between(now - timedelta(days=7), now - timedelta(days=2))
    assert [m.msg_id for m in found] == [2]


def test_runs_journal(repo: Repository) -> None:
    run_id = repo.start_run("collect", CHAT)
    repo.update_run(run_id, new_messages=10, last_msg_id=10)
    repo.finish_run(run_id, status="ok", new_messages=12, last_msg_id=12)
    failed = repo.start_run("digest")
    repo.finish_run(failed, status="error", error="батч не успел")
    runs = repo.recent_runs()
    assert [r.kind for r in runs] == ["digest", "collect"]
    assert runs[1].status == "ok"
    assert runs[1].new_messages == 12
    assert runs[1].last_msg_id == 12
    assert runs[1].finished_at is not None
    assert runs[0].error == "батч не успел"


def test_sync_state(repo: Repository) -> None:
    since = datetime(2025, 10, 1, tzinfo=UTC)
    assert repo.get_sync_state(CHAT) is None
    repo.set_sync_state(CHAT, since=since, last_id=100, done=False)
    state = repo.get_sync_state(CHAT)
    assert state.backfill_last_id == 100
    assert state.backfill_since == since
    assert state.backfill_done is False
    repo.set_sync_state(CHAT, since=since, last_id=200, done=True)
    assert repo.get_sync_state(CHAT).backfill_done is True


def test_digest_save_and_get(repo: Repository) -> None:
    start = datetime(2026, 9, 28, tzinfo=UTC)
    end = start + timedelta(days=7)
    assert repo.get_digest("2026-W40") is None
    repo.save_digest("2026-W40", period_start=start, period_end=end, status="pending")
    repo.save_digest(
        "2026-W40",
        status="done",
        report_md="# Отчёт",
        report_json={"week": "2026-W40"},
        cost_usd=Decimal("0.1234"),
        input_tokens=1000,
    )
    digest = repo.get_digest("2026-W40")
    assert digest.status == "done"
    assert digest.report_json == {"week": "2026-W40"}
    assert digest.cost_usd == Decimal("0.1234")
    assert digest.input_tokens == 1000
    assert len(repo.list_digests()) == 1


def test_stats(repo: Repository) -> None:
    repo.upsert_chat(CHAT, username="gorets_uzb", title="ГОРЕЦ", is_forum=True)
    repo.upsert_messages([_row(1, author=1), _row(2, author=2), _row(3, author=2)])
    stats = repo.stats()
    assert stats["messages"] == 3
    assert stats["authors"] == 2
    assert stats["chats"][0]["username"] == "gorets_uzb"
    assert stats["chats"][0]["last_msg_id"] == 3


def test_normalize_chat_id() -> None:
    assert normalize_chat_id(-1001234567890) == 1234567890
    assert normalize_chat_id(1234567890) == 1234567890
    assert normalize_chat_id(-123) == 123
