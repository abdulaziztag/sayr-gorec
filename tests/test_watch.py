"""Слушатель, вебхуки и оповещения — без сети."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telethon.tl import types as tl

from gorets.alert import alert_text, send_alert
from gorets.config import Settings
from gorets.feeds import load_project_config
from gorets.storage import Repository
from gorets.telegram.collect import TopicResolver
from gorets.telegram.convert import ChatInfo
from gorets.telegram.watch import WatchedChat, Watcher
from gorets.webhooks import SIGNATURE_HEADER, post_webhook, sign, verify_signature
from tests.fakes import CHAT_ID, FakeTelegramClient


def test_signature_roundtrip() -> None:
    body = b'{"a": 1}'
    header = sign("secret", body)
    assert header.startswith("sha256=")
    assert verify_signature("secret", body, header)
    assert not verify_signature("other", body, header)
    assert not verify_signature("secret", b'{"a": 2}', header)
    assert not verify_signature("secret", body, None)


def test_post_webhook_retries_and_gives_up_on_client_error(monkeypatch) -> None:
    import urllib.error

    calls: list[dict] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout):
        calls.append(
            {"url": request.full_url, "headers": dict(request.headers), "body": request.data}
        )
        if len(calls) == 1:
            raise urllib.error.URLError("сеть")
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ok = post_webhook(
        "http://127.0.0.1:9/hook",
        {"x": 1},
        secret="s",
        event="message",
        feed="afisha",
        sleep=lambda _s: None,
    )
    assert ok and len(calls) == 2
    headers = {k.lower(): v for k, v in calls[-1]["headers"].items()}
    assert headers["x-gorets-feed"] == "afisha"
    assert verify_signature("s", calls[-1]["body"], headers[SIGNATURE_HEADER.lower()])

    def forbidden(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "no", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    assert not post_webhook(
        "http://127.0.0.1:9/hook", {}, secret=None, event="message", feed="f", sleep=lambda _s: None
    )


def make_watcher(repo: Repository, tmp_path: Path, posted: list) -> tuple[Watcher, WatchedChat]:
    config = tmp_path / "gorets.toml"
    config.write_text(
        '[feeds.afisha]\nchat = "gorets_uzb"\ntopics = ["АФИША"]\nkeep_contacts = true\n'
        'webhook_url = "http://127.0.0.1:9/afisha"\nwebhook_secret = "s3"\n'
        '[feeds.all]\nchat = "gorets_uzb"\nwebhook_url = "http://127.0.0.1:9/all"\n'
        '[feeds.silent]\nchat = "gorets_uzb"\n'
    )
    settings = Settings(_env_file=None, author_hmac_secret="secret", chats="gorets_uzb")

    def poster(url, payload, *, secret, event, feed):
        posted.append((url, payload, secret, event, feed))
        return "afisha" in url

    watcher = Watcher(settings, repo, load_project_config(config), poster=poster)
    client = FakeTelegramClient([], topics={500: "АФИША ПОХОДОВ"})
    topics = TopicResolver(client, None, True)
    asyncio.run(topics.load_all())
    info = ChatInfo(chat_id=CHAT_ID, username="gorets_uzb", title="ГОРЕЦ", is_forum=True)
    return watcher, WatchedChat(entity=None, info=info, topics=topics)


def message(msg_id: int, text: str, topic: int | None = None) -> tl.Message:
    return tl.Message(
        id=msg_id,
        peer_id=tl.PeerChannel(CHAT_ID),
        date=datetime.now(UTC),
        message=text,
        from_id=tl.PeerUser(5),
        reply_to=tl.MessageReplyHeader(reply_to_msg_id=topic, forum_topic=True) if topic else None,
    )


def test_watcher_stores_and_posts_to_matching_feeds(repo: Repository, tmp_path: Path) -> None:
    posted: list = []
    watcher, watched = make_watcher(repo, tmp_path, posted)
    delivered = asyncio.run(
        watcher.handle(message(1, "Тур на Чимган, звоните +998901234567", topic=500), watched)
    )
    assert delivered == ["afisha"]  # «all» отвечает ошибкой — в список доставленных не попал
    assert repo.count_messages(CHAT_ID) == 1
    urls = sorted(p[0] for p in posted)
    assert urls == ["http://127.0.0.1:9/afisha", "http://127.0.0.1:9/all"]
    afisha = next(p for p in posted if p[4] == "afisha")
    assert afisha[2] == "s3" and afisha[3] == "message"
    assert afisha[1]["message"]["text"] == "Тур на Чимган, звоните +998901234567"
    assert afisha[1]["message"]["link"] == "https://t.me/gorets_uzb/1"
    assert afisha[1]["message"]["topic_title"] == "АФИША ПОХОДОВ"
    json.dumps(afisha[1])  # сериализуемо

    posted.clear()
    asyncio.run(watcher.handle(message(2, "Привет всем, мой номер +998901234567"), watched))
    assert [p[4] for p in posted] == ["all"]  # General: только фид на весь чат
    assert "998" not in posted[0][1]["message"]["text"]  # контакты вне афиши стёрты

    posted.clear()
    asyncio.run(watcher.handle(message(2, "Правка"), watched, event="message_edited"))
    assert posted[0][3] == "message_edited"
    assert repo.count_messages(CHAT_ID) == 2

    service = tl.MessageService(
        id=3,
        peer_id=tl.PeerChannel(CHAT_ID),
        date=datetime.now(UTC),
        action=tl.MessageActionPinMessage(),
    )
    posted.clear()
    assert asyncio.run(watcher.handle(service, watched)) == []
    assert posted == []


def test_alert_text_and_send(repo: Repository) -> None:
    run_id = repo.start_run("collect")
    repo.finish_run(run_id, status="error", error="Telegram просит подождать 7200 с")
    text = alert_text("sayr-gorets-collect.service", repo)
    assert "sayr-gorets-collect.service" in text
    assert "подождать 7200" in text
    assert "journalctl -u sayr-gorets-collect.service" in text
    old = alert_text("x", repo, now=datetime.now(UTC) + timedelta(days=1))
    assert "подождать" not in old  # старые ошибки не показываем
    client = FakeTelegramClient([])
    asyncio.run(send_alert(client, "@owner", text))
    assert client.sent == [("@owner", text)]
