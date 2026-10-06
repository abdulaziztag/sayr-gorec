"""Сбор и догрузка на фейковом Telegram и настоящем Postgres."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from telethon.tl import types as tl

from gorets.config import Settings
from gorets.storage import Repository
from gorets.telegram.collect import CollectError, run_backfill, run_collect
from tests.fakes import CHAT_ID, FakeTelegramClient


def msg(msg_id: int, text: str = "текст", *, days_ago: int = 0, topic: int | None = None, **kw):
    reply_to = tl.MessageReplyHeader(reply_to_msg_id=topic, forum_topic=True) if topic else None
    return tl.Message(
        id=msg_id,
        peer_id=tl.PeerChannel(CHAT_ID),
        date=datetime.now(UTC) - timedelta(days=days_ago),
        message=text,
        from_id=tl.PeerUser(msg_id % 3 + 1),
        reply_to=reply_to,
        **kw,
    )


def settings(**overrides) -> Settings:
    base = {
        "author_hmac_secret": "secret",
        "chats": "gorets_uzb",
        "initial_since": "2020-01-01",
        "request_pause": 0,
        "upsert_batch": 2,
        "retention_days": 90,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_collect_stores_new_messages_and_is_idempotent(repo: Repository) -> None:
    client = FakeTelegramClient(
        [msg(1), msg(2, topic=500), msg(3, days_ago=120), msg(4, topic=600)],
        topics={500: "Выходы"},
    )
    report = asyncio.run(run_collect(settings(), repo, client))
    assert report.new_total == 4
    # Старое сообщение попало в базу, но его тут же удалила чистка по сроку.
    assert report.deleted_old == 1
    assert repo.count_messages(CHAT_ID) == 3
    with repo.session() as s:
        from gorets.models import Message

        m2 = s.get(Message, (CHAT_ID, 2))
        assert m2.topic_title == "Выходы"
        m4 = s.get(Message, (CHAT_ID, 4))
        assert m4.topic_id == 600 and m4.topic_title is None

    # Повтор ничего не добавляет: читаем только id больше последнего.
    report = asyncio.run(run_collect(settings(), repo, client))
    assert report.new_total == 0
    runs = repo.recent_runs()
    assert runs[0].kind == "collect" and runs[0].status == "ok"
    assert runs[0].details["seen"] == 0
    assert repo.resolve_chat_id("gorets_uzb") == CHAT_ID


def test_new_chat_is_scanned_from_initial_date_only(repo: Repository) -> None:
    from datetime import date

    client = FakeTelegramClient(
        [msg(1, days_ago=400), msg(2, days_ago=20), msg(3, days_ago=1)], forum=False
    )
    cutoff = (datetime.now(UTC) - timedelta(days=30)).date()
    report = asyncio.run(
        run_collect(settings(initial_since=cutoff, retention_days=3650), repo, client)
    )
    assert report.new_total == 2
    assert repo.last_msg_id(CHAT_ID) == 3
    with repo.session() as s:
        from gorets.models import Message

        assert s.get(Message, (CHAT_ID, 1)) is None
        assert s.get(Message, (CHAT_ID, 2)).topic_id is None  # канал без веток
    # Чат уже в базе: дата больше не используется, читаем по id.
    client.messages.append(msg(4, days_ago=0))
    report = asyncio.run(
        run_collect(settings(initial_since=date(2030, 1, 1), retention_days=3650), repo, client)
    )
    assert report.new_total == 1


def test_interrupted_initial_scan_resumes_by_id(repo: Repository) -> None:
    client = FakeTelegramClient(
        [msg(i, days_ago=10) for i in range(1, 6)], forum=False, flood_at=4, flood_seconds=99999
    )
    cutoff = (datetime.now(UTC) - timedelta(days=30)).date()
    with pytest.raises(CollectError):
        asyncio.run(run_collect(settings(initial_since=cutoff), repo, client))
    # До FloodWait успели прочитать три сообщения, они сохранены.
    assert repo.count_messages(CHAT_ID) == 3
    report = asyncio.run(run_collect(settings(initial_since=cutoff), repo, client))
    assert report.new_total == 2
    assert repo.count_messages(CHAT_ID) == 5


def test_collect_continues_after_new_messages_appear(repo: Repository) -> None:
    client = FakeTelegramClient([msg(1), msg(2)])
    asyncio.run(run_collect(settings(), repo, client))
    client.messages.append(msg(3, "новое"))
    report = asyncio.run(run_collect(settings(), repo, client))
    assert report.new_total == 1
    assert repo.last_msg_id(CHAT_ID) == 3


def test_collect_fails_loudly_on_long_flood_wait_but_keeps_progress(repo: Repository) -> None:
    client = FakeTelegramClient([msg(1), msg(2), msg(3), msg(4)], flood_at=3, flood_seconds=7200)
    with pytest.raises(CollectError, match="подождать 7200"):
        asyncio.run(run_collect(settings(flood_wait_max=3600), repo, client))
    assert repo.count_messages(CHAT_ID) == 2
    assert repo.recent_runs()[0].status == "error"
    # Следующий прогон продолжает с места остановки.
    report = asyncio.run(run_collect(settings(), repo, client))
    assert report.new_total == 2


def test_collect_requires_hmac_secret(repo: Repository) -> None:
    client = FakeTelegramClient([msg(1)])
    with pytest.raises(CollectError, match="GORETS_AUTHOR_HMAC_SECRET"):
        asyncio.run(run_collect(settings(author_hmac_secret=None), repo, client))


def test_backfill_in_portions_resumes_where_it_stopped(repo: Repository) -> None:
    since = datetime.now(UTC) - timedelta(days=10)
    client = FakeTelegramClient(
        [msg(1, days_ago=30), msg(2, days_ago=5), msg(3, days_ago=4), msg(4, days_ago=3)]
    )
    report = asyncio.run(run_backfill(settings(), repo, client, since=since, limit=2))
    assert report.chats[0]["new"] == 2
    assert report.chats[0]["done"] is False
    state = repo.get_sync_state(CHAT_ID)
    assert state.backfill_last_id == 3 and state.backfill_done is False

    report = asyncio.run(run_backfill(settings(), repo, client, since=since, limit=2))
    assert report.chats[0]["new"] == 1
    assert report.chats[0]["done"] is True
    assert repo.get_sync_state(CHAT_ID).backfill_done is True
    assert repo.count_messages(CHAT_ID) == 3  # сообщение старше since не читали

    # Завершённую догрузку повторно не делаем.
    report = asyncio.run(run_backfill(settings(), repo, client, since=since, limit=2))
    assert report.chats[0]["seen"] == 0


def test_backfill_and_collect_do_not_duplicate(repo: Repository) -> None:
    since = datetime.now(UTC) - timedelta(days=10)
    client = FakeTelegramClient([msg(1, days_ago=5), msg(2, days_ago=1)])
    asyncio.run(run_collect(settings(), repo, client))
    report = asyncio.run(run_backfill(settings(), repo, client, since=since))
    assert report.chats[0]["new"] == 0
    assert repo.count_messages(CHAT_ID) == 2


def test_collect_keeps_contacts_in_feed_with_keep_contacts(repo: Repository, tmp_path) -> None:
    config = tmp_path / "gorets.toml"
    config.write_text(
        '[feeds.afisha]\nchat = "gorets_uzb"\ntopics = ["Афиши"]\nkeep_contacts = true\n'
    )
    client = FakeTelegramClient(
        [msg(1, "Звоните +998901234567"), msg(2, "Тур, звоните +998901234567", topic=500)],
        topics={500: "Афиши"},
    )
    asyncio.run(run_collect(settings(config_file=config), repo, client))
    with repo.session() as s:
        from gorets.models import Message

        assert "998" not in s.get(Message, (CHAT_ID, 1)).text
        assert "+998901234567" in s.get(Message, (CHAT_ID, 2)).text
