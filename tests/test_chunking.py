from datetime import datetime
from zoneinfo import ZoneInfo

from gorets.digest.chunking import (
    ChunkMessage,
    build_chunks,
    chunk_summary,
    chunks_fingerprint,
    estimate_tokens,
    message_line,
)

TZ = ZoneInfo("Asia/Tashkent")
CHAT = {1: "@gorets_uzb"}


def msg(msg_id, text="сообщение", *, topic=1, title="General", reply=None, **kw) -> ChunkMessage:
    return ChunkMessage(
        chat_id=1,
        msg_id=msg_id,
        date=datetime(2026, 9, 29, 10, msg_id % 60, tzinfo=TZ),
        topic_id=topic,
        topic_title=title,
        reply_to=reply,
        text=text,
        author_hash=f"a{msg_id % 5}",
        **kw,
    )


def test_message_line_formats() -> None:
    m = msg(10, "Снег на перевале", topic=5, title="Чимган")
    assert message_line(m, None) == "#10 29.09 10:10: Снег на перевале"
    reply = msg(11, "Сколько?", reply=10)
    assert message_line(reply, m) == "#11 29.09 10:11 (ответ на #10: «Снег на перевале»): Сколько?"
    assert message_line(msg(12, "?", reply=9), None) == "#12 29.09 10:12 (ответ на #9): ?"
    track = msg(13, "трек", media_type="gpx", file_name="x.gpx")
    assert message_line(track, None).endswith(": трек [файл: x.gpx]")
    point = msg(14, "", media_type="location", lat=41.3111, lng=69.2797)
    assert message_line(point, None) == "#14 29.09 10:14: [точка 41.311, 69.280]"
    photo = msg(15, "вид", media_type="photo")
    assert message_line(photo, None).endswith(": вид [фото]")


def test_empty_photo_messages_are_not_sent_to_model() -> None:
    chunks = build_chunks(
        [msg(1, "", media_type="photo"), msg(2, "", media_type="sticker"), msg(3, "текст")],
        week_label="2026-W40",
        chat_names=CHAT,
        budget_tokens=1000,
        chars_per_token=2.5,
    )
    assert len(chunks) == 1
    assert chunks[0].message_count == 1
    assert "#3" in chunks[0].text and "#1" not in chunks[0].text


def test_small_topics_are_packed_together_and_big_ones_split() -> None:
    messages = [msg(i, "короткий текст", topic=1, title="General") for i in range(1, 6)]
    messages += [msg(100 + i, "ещё короткий", topic=2, title="Выходы") for i in range(1, 4)]
    messages += [msg(200 + i, "о" * 400, topic=3, title="Большая ветка") for i in range(1, 11)]
    chunks = build_chunks(
        messages, week_label="2026-W40", chat_names=CHAT, budget_tokens=500, chars_per_token=2.5
    )
    assert all(c.est_tokens <= 500 for c in chunks)
    # Две маленькие ветки уместились в один кусок.
    small = [c for c in chunks if any(t[0] == 1 for t in c.topics)]
    assert len(small) == 1
    assert {t[0] for t in small[0].topics} == {1, 2}
    # Большая ветка порезана на части, по порядку, без потерь.
    big = [c for c in chunks if any(t[0] == 3 for t in c.topics)]
    assert len(big) > 1
    ids = [i for c in big for i in c.message_ids]
    assert ids == sorted(ids) == [201 + i for i in range(10)]
    assert "часть 1 из" in big[0].text
    assert [c.id for c in chunks] == [f"2026-W40-{i:03d}" for i in range(1, len(chunks) + 1)]
    summary = chunk_summary(chunks)
    assert summary["messages"] == 18
    assert summary["chunks"] == len(chunks)


def test_oversized_single_message_is_truncated_not_lost() -> None:
    chunks = build_chunks(
        [msg(1, "х" * 20000)],
        week_label="2026-W40",
        chat_names=CHAT,
        budget_tokens=100,
        chars_per_token=2.5,
    )
    assert len(chunks) == 1
    assert chunks[0].message_count == 1
    assert len(chunks[0].text) < 5000


def test_fingerprint_is_stable_and_sensitive() -> None:
    a = build_chunks(
        [msg(1, "a")], week_label="W", chat_names=CHAT, budget_tokens=100, chars_per_token=2
    )
    b = build_chunks(
        [msg(1, "a")], week_label="W", chat_names=CHAT, budget_tokens=100, chars_per_token=2
    )
    c = build_chunks(
        [msg(1, "b")], week_label="W", chat_names=CHAT, budget_tokens=100, chars_per_token=2
    )
    assert chunks_fingerprint(a) == chunks_fingerprint(b) != chunks_fingerprint(c)


def test_estimate_tokens() -> None:
    assert estimate_tokens("", 2.5) == 1
    assert estimate_tokens("а" * 25, 2.5) == 10
