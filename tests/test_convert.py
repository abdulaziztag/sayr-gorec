"""Сообщение Telethon → строка базы."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from telethon.tl import types as tl

from gorets.anonymize import author_hash
from gorets.cleaning import PHONE, USER, TextCleaner
from gorets.telegram.convert import (
    ChatInfo,
    classify_media,
    mask_mention_names,
    message_to_row,
)

CHAT = ChatInfo(chat_id=1234567890, username="gorets_uzb", title="ГОРЕЦ", is_forum=True)
SECRET = "s3cret"
TOPICS = {1: "General", 500: "Выходы и сборы", 777: "Чимган"}


def hasher(kind: str, peer_id: int) -> str:
    return author_hash(SECRET, peer_id, kind)


cleaner = TextCleaner(keep=["gorets_uzb"])


def make_message(msg_id: int = 10, **kwargs) -> tl.Message:
    defaults = {
        "peer_id": tl.PeerChannel(CHAT.chat_id),
        "date": datetime(2026, 9, 29, 10, 0, tzinfo=UTC),
        "message": "Привет",
        "from_id": tl.PeerUser(42),
    }
    defaults.update(kwargs)
    return tl.Message(id=msg_id, **defaults)


def convert(msg, sender=None, **kw):
    return message_to_row(
        msg, chat=CHAT, sender=sender, topics=TOPICS, hasher=hasher, cleaner=cleaner, **kw
    )


def test_plain_message_in_general_topic() -> None:
    row = convert(make_message(message="Кто идёт на Бельдерсай? Пишите +998 90 123 45 67"))
    assert row["chat_id"] == CHAT.chat_id
    assert row["msg_id"] == 10
    assert row["date"] == datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    assert row["topic_id"] == 1
    assert row["topic_title"] == "General"
    assert row["reply_to_msg_id"] is None
    assert row["author_hash"] == author_hash(SECRET, 42)
    assert row["text"] == f"Кто идёт на Бельдерсай? Пишите {PHONE}"
    assert row["media_type"] is None
    assert row["edited_at"] is None


def test_message_in_topic_without_reply() -> None:
    msg = make_message(reply_to=tl.MessageReplyHeader(reply_to_msg_id=500, forum_topic=True))
    row = convert(msg)
    assert row["topic_id"] == 500
    assert row["topic_title"] == "Выходы и сборы"
    assert row["reply_to_msg_id"] is None


def test_real_reply_inside_topic() -> None:
    msg = make_message(
        reply_to=tl.MessageReplyHeader(reply_to_msg_id=5100, reply_to_top_id=500, forum_topic=True)
    )
    row = convert(msg)
    assert row["topic_id"] == 500
    assert row["reply_to_msg_id"] == 5100


def test_reply_in_general_topic() -> None:
    msg = make_message(reply_to=tl.MessageReplyHeader(reply_to_msg_id=9))
    row = convert(msg)
    assert row["topic_id"] == 1
    assert row["reply_to_msg_id"] == 9


def test_unknown_topic_has_no_title() -> None:
    msg = make_message(reply_to=tl.MessageReplyHeader(reply_to_msg_id=999, forum_topic=True))
    row = convert(msg)
    assert row["topic_id"] == 999
    assert row["topic_title"] is None


def test_non_forum_chat_has_no_topic() -> None:
    chat = ChatInfo(chat_id=5, username=None, title="Канал", is_forum=False)
    msg = make_message(reply_to=tl.MessageReplyHeader(reply_to_msg_id=3))
    row = message_to_row(msg, chat=chat, sender=None, topics={}, hasher=hasher, cleaner=cleaner)
    assert row["topic_id"] is None
    assert row["reply_to_msg_id"] == 3


def test_service_messages_and_bots_are_skipped() -> None:
    service = tl.MessageService(
        id=1,
        peer_id=tl.PeerChannel(CHAT.chat_id),
        action=tl.MessageActionPinMessage(),
        date=datetime(2026, 9, 29, tzinfo=UTC),
    )
    assert convert(service) is None
    bot = tl.User(id=99, bot=True, first_name="Combot")
    assert convert(make_message(from_id=tl.PeerUser(99)), sender=bot) is None
    human = tl.User(id=42, first_name="Иван")
    assert convert(make_message(), sender=human) is not None


def test_anonymous_admin_posts_as_channel() -> None:
    row = convert(make_message(from_id=tl.PeerChannel(CHAT.chat_id)))
    assert row["author_hash"] == author_hash(SECRET, CHAT.chat_id, "channel")
    assert row["author_hash"] != author_hash(SECRET, CHAT.chat_id, "user")


def test_message_without_author() -> None:
    assert convert(make_message(from_id=None))["author_hash"] is None


def _document(*attributes, mime="application/octet-stream") -> tl.MessageMediaDocument:
    doc = tl.Document(
        id=1,
        access_hash=2,
        file_reference=b"",
        date=None,
        mime_type=mime,
        size=10,
        dc_id=2,
        attributes=list(attributes),
    )
    return tl.MessageMediaDocument(document=doc)


def test_media_types() -> None:
    assert classify_media(None).media_type is None
    assert classify_media(tl.MessageMediaPhoto()).media_type == "photo"
    gpx = classify_media(_document(tl.DocumentAttributeFilename("Beldersay_track.GPX")))
    assert (gpx.media_type, gpx.file_name) == ("gpx", "Beldersay_track.GPX")
    kml = classify_media(_document(tl.DocumentAttributeFilename("route.kmz")))
    assert kml.media_type == "kml"
    pdf = classify_media(_document(tl.DocumentAttributeFilename("list.pdf")))
    assert (pdf.media_type, pdf.file_name) == ("document", "list.pdf")
    voice = classify_media(_document(tl.DocumentAttributeAudio(duration=3, voice=True)))
    assert voice.media_type == "voice"
    music = classify_media(_document(tl.DocumentAttributeAudio(duration=3)))
    assert music.media_type == "other"
    video = classify_media(
        _document(tl.DocumentAttributeVideo(duration=3, w=1, h=1), mime="video/mp4")
    )
    assert video.media_type == "video"
    gif = classify_media(_document(tl.DocumentAttributeAnimated()))
    assert gif.media_type == "video"
    sticker = classify_media(
        _document(
            tl.DocumentAttributeVideo(duration=3, w=1, h=1),
            tl.DocumentAttributeSticker(alt="", stickerset=tl.InputStickerSetEmpty()),
        )
    )
    assert sticker.media_type == "sticker"
    poll_media = tl.MessageMediaPoll(
        poll=tl.Poll(
            id=1,
            hash=0,
            question=tl.TextWithEntities(text="Когда идём?", entities=[]),
            answers=[
                tl.PollAnswer(text=tl.TextWithEntities(text="Суббота", entities=[]), option=b"0"),
                tl.PollAnswer(
                    text=tl.TextWithEntities(text="Воскресенье", entities=[]), option=b"1"
                ),
            ],
        ),
        results=tl.PollResults(),
    )
    poll = classify_media(poll_media)
    assert poll.media_type == "poll"
    assert poll.extra_text == "Опрос: Когда идём?\n— Суббота\n— Воскресенье"
    assert classify_media(tl.MessageMediaDice(value=3, emoticon="🎲")).media_type == "other"
    assert classify_media(tl.MessageMediaWebPage(webpage=tl.WebPageEmpty(id=1))).media_type is None


def test_location_rounded_and_live_location_not_stored() -> None:
    geo = tl.MessageMediaGeo(geo=tl.GeoPoint(long=69.2797372, lat=41.3110816, access_hash=0))
    row = convert(make_message(media=geo, message=""))
    assert row["media_type"] == "location"
    assert row["lat"] == Decimal("41.311")
    assert row["lng"] == Decimal("69.280")
    live = tl.MessageMediaGeoLive(
        geo=tl.GeoPoint(long=69.2797372, lat=41.3110816, access_hash=0), period=3600
    )
    row = convert(make_message(media=live, message=""))
    assert row["media_type"] == "location"
    assert row["lat"] is None
    assert row["lng"] is None


def test_venue_adds_title_to_text() -> None:
    venue = tl.MessageMediaVenue(
        geo=tl.GeoPoint(long=70.0, lat=41.5, access_hash=0),
        title="Чимган, нижняя станция",
        address="Бостанлыкский район",
        provider="foursquare",
        venue_id="1",
        venue_type="",
    )
    row = convert(make_message(media=venue, message="Встречаемся тут"))
    assert row["media_type"] == "venue"
    assert row["text"] == "Встречаемся тут\n📍 Чимган, нижняя станция, Бостанлыкский район"
    assert row["lat"] == Decimal("41.500")


def test_forward_from_channel_keeps_title_but_from_user_does_not() -> None:
    from_channel = make_message(
        fwd_from=tl.MessageFwdHeader(date=None, from_id=tl.PeerChannel(555))
    )
    assert convert(from_channel, fwd_title="Погода в горах")["forwarded_from"] == ("Погода в горах")
    assert convert(from_channel)["forwarded_from"] == "channel:555"
    from_user = make_message(fwd_from=tl.MessageFwdHeader(date=None, from_id=tl.PeerUser(7)))
    assert convert(from_user)["forwarded_from"] is None
    hidden = make_message(fwd_from=tl.MessageFwdHeader(date=None, from_name="Иван"))
    assert convert(hidden)["forwarded_from"] is None


def test_edit_date_is_kept() -> None:
    edited = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    assert convert(make_message(edit_date=edited))["edited_at"] == edited


def test_mention_name_entity_is_masked() -> None:
    # Смещения в UTF-16: эмодзи перед именем занимает две единицы.
    text = "🙂 Иван Петров, идёшь?"
    entities = [tl.MessageEntityMentionName(offset=3, length=11, user_id=42)]
    assert mask_mention_names(text, entities) == f"🙂 {USER}, идёшь?"
    row = convert(make_message(message=text, entities=entities))
    assert row["text"] == f"🙂 {USER}, идёшь?"
