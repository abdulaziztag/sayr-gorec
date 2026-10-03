"""Импорт выгрузки Telegram Desktop: потоковый разбор, ветки, очистка, совместимость с API."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from gorets.anonymize import author_hash
from gorets.cleaning import EMAIL, PHONE, USER, TextCleaner
from gorets.config import Settings
from gorets.importer import (
    ExportConverter,
    flatten_text,
    import_export,
    iter_export_messages,
    read_export_meta,
)
from gorets.storage import Repository

FIXTURE = Path(__file__).parent / "fixtures" / "result.json"
SECRET = "secret"
KEEP = {"gorets_uzb"}


def converter(is_forum: bool = True) -> ExportConverter:
    return ExportConverter(
        chat_id=1234567890,
        hasher=lambda kind, pid: author_hash(SECRET, pid, kind),
        cleaner=TextCleaner(KEEP),
        keep=KEEP,
        tz=ZoneInfo("Asia/Tashkent"),
        is_forum=is_forum,
    )


def rows_by_id() -> dict[int, dict]:
    conv = converter()
    out = {}
    for item in iter_export_messages(FIXTURE):
        row = conv.convert(item)
        if row is not None:
            out[row["msg_id"]] = row
    return out


def test_meta_is_read_without_messages() -> None:
    meta = read_export_meta(FIXTURE)
    assert meta.chat_id == 1234567890
    assert meta.name == "ГОРЕЦ (тест)"
    assert meta.type == "public_supergroup"


def test_service_messages_and_bots_are_skipped() -> None:
    rows = rows_by_id()
    assert 1 not in rows  # создание канала
    assert 100 not in rows  # topic_created
    assert 114 not in rows  # закреп
    assert 115 not in rows  # Combot


def test_topics_are_inferred_from_reply_chains() -> None:
    rows = rows_by_id()
    assert rows[101]["topic_id"] == 100
    assert rows[101]["topic_title"] == "Выходы и сборы"
    assert rows[101]["reply_to_msg_id"] is None  # «в ветку», а не ответ
    assert rows[102]["topic_id"] == 100
    assert rows[102]["reply_to_msg_id"] == 101  # настоящий ответ в ветке
    assert rows[104]["topic_id"] == 1
    assert rows[104]["topic_title"] == "General"
    assert rows[118]["topic_id"] is None  # ответ на удалённое: ветка неизвестна
    assert rows[118]["reply_to_msg_id"] == 50


def test_text_entities_are_flattened_and_cleaned() -> None:
    rows = rows_by_id()
    assert rows[101]["text"] == (
        f"Идём на Бельдерсай в субботу, пишите {USER} или {PHONE}, почта {EMAIL}. "
        "Форум: @gorets_uzb"
    )
    assert rows[102]["text"] == f"Я с вами! Мой номер {PHONE}"


def test_flatten_text_variants() -> None:
    assert flatten_text(None, set()) == ""
    assert flatten_text("просто", set()) == "просто"
    assert flatten_text([{"type": "mention_name", "text": "Иван"}, "!"], set()) == f"{USER}!"
    assert flatten_text([{"type": "mention", "text": "@SayrBot"}], set()) == "@SayrBot"


def test_media_mapping() -> None:
    rows = rows_by_id()
    assert (rows[103]["media_type"], rows[103]["file_name"]) == ("gpx", "Beldersay_2025.gpx")
    assert rows[104]["media_type"] == "photo"
    assert rows[105]["media_type"] == "voice"
    assert rows[106]["media_type"] == "location"
    assert rows[106]["lat"] == Decimal("41.311")
    assert rows[106]["lng"] == Decimal("69.280")
    assert rows[107]["media_type"] == "venue"
    assert rows[107]["text"] == "📍 Чимган, нижняя станция, Бостанлыкский район"
    assert rows[108]["media_type"] == "poll"
    assert rows[108]["text"] == "Опрос: Когда идём?\n— Суббота\n— Воскресенье"
    assert rows[112]["media_type"] == "sticker"
    assert rows[113]["media_type"] == "video"
    assert rows[116]["media_type"] == "other"
    assert "998" not in rows[116]["text"]
    assert rows[117]["media_type"] == "location"
    assert rows[117]["lat"] is None  # живая геопозиция без координат


def test_forwards_authors_and_dates() -> None:
    rows = rows_by_id()
    assert rows[109]["forwarded_from"] == "Погода в горах"
    assert rows[110]["forwarded_from"] is None
    assert rows[101]["author_hash"] == author_hash(SECRET, 123456)
    assert rows[119]["author_hash"] == author_hash(SECRET, 1234567890, "channel")
    assert rows[101]["date"] == datetime(2025, 10, 1, 4, 15, tzinfo=UTC)
    assert rows[111]["edited_at"] == datetime(2025, 10, 1, 6, 0, tzinfo=UTC)
    # Без unixtime дата считается местным временем Ташкента (UTC+5).
    assert rows[120]["date"] == datetime(2025, 10, 1, 6, 25, tzinfo=UTC)


def test_non_forum_chat_has_no_topics() -> None:
    conv = converter(is_forum=False)
    rows = {}
    for item in iter_export_messages(FIXTURE):
        row = conv.convert(item)
        if row is not None:
            rows[row["msg_id"]] = row
    assert rows[101]["topic_id"] is None
    assert rows[101]["reply_to_msg_id"] == 100


def test_import_into_db_and_no_duplicates_with_api_rows(repo: Repository) -> None:
    settings = Settings(_env_file=None, author_hmac_secret=SECRET, chats="gorets_uzb")
    result = import_export(FIXTURE, repo, settings, batch_size=3)
    assert result.chat_id == 1234567890
    assert result.seen == 22
    assert result.stored == 18
    assert result.new == 18
    assert result.skipped == 4
    assert result.topics[100] == "Выходы и сборы"
    assert repo.count_messages(1234567890) == 18

    again = import_export(FIXTURE, repo, settings)
    assert again.new == 0
    assert repo.count_messages(1234567890) == 18

    # Сообщение с тем же id, пришедшее из API, обновляет строку, а не дублирует.
    from telethon.tl import types as tl

    from gorets.telegram.convert import ChatInfo, message_to_row

    api_msg = tl.Message(
        id=102,
        peer_id=tl.PeerChannel(1234567890),
        date=datetime(2025, 10, 1, 4, 20, tzinfo=UTC),
        message="Я с вами! Мой номер 93 765 43 21",
        from_id=tl.PeerUser(654321),
        reply_to=tl.MessageReplyHeader(reply_to_msg_id=101, reply_to_top_id=100, forum_topic=True),
    )
    row = message_to_row(
        api_msg,
        chat=ChatInfo(1234567890, "gorets_uzb", "ГОРЕЦ", True),
        sender=None,
        topics={100: "Выходы и сборы"},
        hasher=lambda kind, pid: author_hash(SECRET, pid, kind),
        cleaner=TextCleaner(KEEP),
    )
    assert repo.upsert_messages([row]) == 0
    assert repo.count_messages(1234567890) == 18


def test_import_requires_secret_and_chat_id(tmp_path: Path, repo: Repository) -> None:
    with pytest.raises(ValueError, match="HMAC"):
        import_export(FIXTURE, repo, Settings(_env_file=None, author_hmac_secret=None))
    no_id = tmp_path / "result.json"
    no_id.write_text('{"name": "x", "type": "public_supergroup", "messages": []}')
    with pytest.raises(ValueError, match="chat-id"):
        import_export(no_id, repo, Settings(_env_file=None, author_hmac_secret="s"))
    result = import_export(no_id, repo, Settings(_env_file=None, author_hmac_secret="s"), chat_id=5)
    assert result.seen == 0 and result.chat_id == 5
