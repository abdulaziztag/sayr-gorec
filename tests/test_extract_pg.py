"""Фиды, извлекатели и API на Postgres с фейковым Claude."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gorets.api import create_app
from gorets.config import Settings
from gorets.digest.claude import BatchItem, BatchStatus, Usage
from gorets.extract import ExtractError, run_extract, wrapped_schema
from gorets.feeds import load_project_config
from gorets.storage import Repository
from tests.test_storage_pg import CHAT, _row

ROOT = Path(__file__).resolve().parent.parent
PROJECT = load_project_config(ROOT / "gorets.toml")
AFISHA = 500


def seed(repo: Repository) -> None:
    repo.upsert_chat(CHAT, username="gorets_uzb", title="ГОРЕЦ", is_forum=True)
    repo.upsert_messages(
        [
            _row(1, text="Общий вопрос", topic_id=1, topic_title="General"),
            _row(
                2,
                text="Тур на Бельдерсай 12.10, 300 000 сум, +998901234567",
                topic_id=AFISHA,
                topic_title="АФИША ПОХОДОВ",
            ),
            _row(
                3,
                text="А сколько идти?",
                topic_id=AFISHA,
                topic_title="АФИША ПОХОДОВ",
                reply_to_msg_id=2,
            ),
            _row(4, text="", topic_id=AFISHA, topic_title="АФИША ПОХОДОВ", media_type="photo"),
            _row(5, text="Выезд в Чимган 19.10", topic_id=AFISHA, topic_title="афиша походов "),
        ]
    )


class ExtractGateway:
    def __init__(self, *, ended: bool = True) -> None:
        self.ended = ended
        self.submitted: list[list[dict]] = []

    def submit_batch(self, requests):
        self.submitted.append(requests)
        return f"xb_{len(self.submitted)}"

    def batch_status(self, batch_id):
        return BatchStatus(id=batch_id, status="ended" if self.ended else "in_progress")

    def batch_results(self, batch_id):
        items = []
        for r in self.submitted[int(batch_id.split("_")[1]) - 1]:
            text = r["params"]["messages"][0]["content"]
            if "сколько" in text:
                answer = {"relevant": False, "data": None}
            else:
                answer = {
                    "relevant": True,
                    "data": {
                        "title": text.splitlines()[-1][:30],
                        "organizer": None,
                        "contact": None,
                        "place": "Бельдерсай",
                        "region": None,
                        "date_start": "2026-10-12",
                        "date_end": None,
                        "days": 1,
                        "price": 300000,
                        "currency": "сум",
                        "difficulty": None,
                        "includes": [],
                        "transport": None,
                        "summary": "тур",
                    },
                }
            items.append(
                BatchItem(
                    r["custom_id"],
                    True,
                    text=json.dumps(answer, ensure_ascii=False),
                    usage=Usage(input_tokens=500, output_tokens=80),
                )
            )
        return items

    def complete(self, params):
        raise AssertionError("извлекатели не используют обычные вызовы")


def settings(**overrides) -> Settings:
    base = {
        "author_hmac_secret": "s",
        "config_file": ROOT / "gorets.toml",
        "api_token": "t0k",
        "batch_wait_hours": 0.001,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def run(repo, gateway, **kw):
    return run_extract(
        settings(), repo, PROJECT, gateway, sleep=lambda s: None, clock=lambda: 0.0, **kw
    )


def test_feed_messages_with_cursor_and_filters(repo: Repository) -> None:
    seed(repo)
    feed = PROJECT.feeds["afisha"]
    rows = repo.feed_messages(CHAT, feed.topics, limit=10)
    assert [m.msg_id for m in rows] == [2, 3, 4, 5]
    assert [m.msg_id for m in repo.feed_messages(CHAT, feed.topics, after_id=3)] == [4, 5]
    assert [m.msg_id for m in repo.feed_messages(CHAT, feed.topics, q="чимган")] == [5]
    assert [m.msg_id for m in repo.feed_messages(CHAT, (), limit=10)] == [1, 2, 3, 4, 5]
    assert [m.msg_id for m in repo.feed_messages(CHAT, (AFISHA,), after_id=4)] == [5]
    pending = repo.messages_without_extraction("afisha_tour", CHAT, feed.topics)
    assert [m.msg_id for m in pending] == [2, 3, 5]  # фото без текста не берём


def test_run_extract_stores_results_and_skips_done(repo: Repository) -> None:
    seed(repo)
    gateway = ExtractGateway()
    report = run(repo, gateway, names=["afisha_tour"])
    entry = report.extractors[0]
    assert entry["candidates"] == 3 and entry["ok"] == 2 and entry["skipped"] == 1
    assert report.cost_usd > 0
    request = gateway.submitted[0][0]["params"]
    expected = wrapped_schema(PROJECT.extractors["afisha_tour"].schema)
    assert request["output_config"]["format"]["schema"] == expected
    assert "Инструкция" in request["system"][0]["text"]
    ok = repo.list_extractions("afisha_tour")
    assert [e.msg_id for e in ok] == [2, 5]
    assert ok[0].data["price"] == 300000
    assert ok[0].topic_title == "АФИША ПОХОДОВ"
    assert repo.list_extractions("afisha_tour", status="skipped")[0].msg_id == 3
    # Повтор ничего не отправляет: всё уже извлечено.
    report = run(repo, gateway, names=["afisha_tour"])
    assert report.extractors[0]["candidates"] == 0 and len(gateway.submitted) == 1
    # dry-run без ключа: только оценка.
    repo.upsert_messages([_row(6, text="Новый тур", topic_id=AFISHA, topic_title="АФИША ПОХОДОВ")])
    report = run(repo, None, dry_run=True)
    assert report.extractors[0]["candidates"] == 1 and report.extractors[0]["estimate_usd"] > 0
    # Извлекатель на несколько фидов: чат «Горняшки» ещё не собран — замечание, не ошибка.
    cond = next(e for e in report.extractors if e["extractor"] == "trail_condition")
    assert "hikinguz" in cond["note"] and cond["candidates"] == 0


def test_pending_batch_is_collected_next_time(repo: Repository) -> None:
    seed(repo)
    gateway = ExtractGateway(ended=False)
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    with pytest.raises(ExtractError, match="не завершились"):
        run_extract(
            settings(),
            repo,
            PROJECT,
            gateway,
            names=["afisha_tour"],
            sleep=sleep,
            clock=lambda: clock["t"],
        )
    assert [b.batch_id for b in repo.pending_extract_batches()] == ["xb_1"]
    assert repo.list_extractions("afisha_tour") == []

    gateway.ended = True
    report = run_extract(
        settings(),
        repo,
        PROJECT,
        gateway,
        names=["afisha_tour"],
        sleep=sleep,
        clock=lambda: clock["t"],
    )
    assert len(gateway.submitted) == 1
    assert report.extractors[0]["resumed"][0]["ok"] == 2
    assert repo.pending_extract_batches() == []
    assert len(repo.list_extractions("afisha_tour")) == 2


def test_extractions_outlive_messages(repo: Repository) -> None:
    seed(repo)
    run(repo, ExtractGateway(), names=["afisha_tour"])
    repo.delete_older_than(0, datetime.now(UTC) + timedelta(days=1))
    assert repo.count_messages(CHAT) == 0
    assert len(repo.list_extractions("afisha_tour")) == 2


def test_api_endpoints(repo: Repository) -> None:
    seed(repo)
    run(repo, ExtractGateway(), names=["afisha_tour"])
    repo.save_digest(
        "2026-W40",
        period_start=datetime(2026, 9, 28, tzinfo=UTC),
        period_end=datetime(2026, 10, 5, tzinfo=UTC),
        status="done",
        report_md="# Отчёт",
        report_json={"week": "2026-W40"},
    )
    client = TestClient(create_app(settings(), repo, PROJECT))
    assert client.get("/health").json()["ok"] is True
    assert client.get("/feeds").status_code == 401
    assert client.get("/feeds", headers={"Authorization": "Bearer wrong"}).status_code == 401
    auth = {"Authorization": "Bearer t0k"}
    names = [f["name"] for f in client.get("/feeds", headers=auth).json()["items"]]
    assert names[0] == "afisha" and "dispatch_hikinguz" in names

    page = client.get("/feeds/afisha/messages", params={"limit": 2}, headers=auth).json()
    assert [m["msg_id"] for m in page["items"]] == [2, 3]
    assert page["has_more"] is True and page["next_cursor"] == 3
    assert page["items"][0]["link"] == "https://t.me/gorets_uzb/2"
    assert "+998901234567" in page["items"][0]["text"]  # в фиде афиш контакты сохранены
    page = client.get("/feeds/afisha/messages", params={"after": 3}, headers=auth).json()
    assert [m["msg_id"] for m in page["items"]] == [4, 5] and page["has_more"] is False
    assert client.get("/feeds/nope/messages", headers=auth).status_code == 404

    ext = client.get("/extractions/afisha_tour", headers={"X-API-Token": "t0k"}).json()
    assert [e["msg_id"] for e in ext["items"]] == [2, 5]
    assert ext["items"][0]["data"]["place"] == "Бельдерсай"
    assert ext["items"][0]["link"].endswith("/2")
    everything = client.get("/extractions/afisha_tour", params={"status": "all"}, headers=auth)
    assert len(everything.json()["items"]) == 3
    assert client.get("/extractions/nope", headers=auth).status_code == 404
    info = client.get("/extractors", headers=auth).json()["items"][0]
    assert info["name"] == "afisha_tour" and info["counts"] == {"ok": 2, "skipped": 1}

    assert client.get("/messages/gorets_uzb/1", headers=auth).json()["text"] == "Общий вопрос"
    assert client.get("/messages/gorets_uzb/999", headers=auth).status_code == 404
    assert client.get("/digests", headers=auth).json()["items"][0]["week"] == "2026-W40"
    assert client.get("/digests/2026-w40", headers=auth).json() == {"week": "2026-W40"}
    md = client.get("/digests/2026-W40", params={"format": "md"}, headers=auth)
    assert md.text == "# Отчёт"
    assert client.get("/digests/2026-W41", headers=auth).status_code == 404


def test_api_requires_token() -> None:
    with pytest.raises(RuntimeError, match="GORETS_API_TOKEN"):
        create_app(settings(api_token=None), None, PROJECT)
