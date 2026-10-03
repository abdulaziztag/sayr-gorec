"""Полный прогон разбора недели на фейках: без сети, без базы."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from gorets.catalog import Place
from gorets.config import Settings
from gorets.digest.claude import BatchItem, BatchStatus, Completion, Usage
from gorets.digest.run import DONE, DigestDeps, DigestError, run_digest
from gorets.digest.weeks import parse_week

TZ = ZoneInfo("Asia/Tashkent")
WEEK = parse_week("2026-W40", TZ)
CATALOG = [Place("beldersay", "Бельдерсай", "Beldersoy", "peak", "Ташкентская")]


class FakeRepo:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.digests: dict[str, dict[str, Any]] = {}
        self.runs: list[dict[str, Any]] = []

    def messages_between(self, start, end):
        return [r for r in self.rows if start <= r.date < end]

    def list_chats(self):
        return [SimpleNamespace(chat_id=1, username="gorets_uzb", title="ГОРЕЦ")]

    def start_run(self, kind, chat_id=None):
        self.runs.append({"kind": kind, "status": "running"})
        return len(self.runs)

    def finish_run(self, run_id, *, status, error=None, details=None, **kw):
        self.runs[run_id - 1].update(status=status, error=error, details=details)

    def get_digest(self, week):
        data = self.digests.get(week)
        return SimpleNamespace(**data) if data else None

    def save_digest(self, week, **fields):
        self.digests.setdefault(week, {"week": week}).update(fields)
        return SimpleNamespace(**self.digests[week])


class FakeGateway:
    def __init__(
        self,
        *,
        chunk_answer: dict | None = None,
        fail_ids: set[str] = frozenset(),
        polls_until_end: int = 1,
        synthesis: dict | None = None,
    ) -> None:
        self.chunk_answer = chunk_answer or {
            "places": [
                {
                    "name": "Бельдерсай",
                    "slug": "beldersay",
                    "mentions": 2,
                    "summary": "снег",
                    "conditions": [],
                }
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
            "trips": [],
            "questions": [],
            "sayr_mentions": [],
        }
        self.fail_ids = fail_ids
        self.polls_until_end = polls_until_end
        self.synthesis = synthesis or {
            "headline": "Снежная неделя.",
            "places": [
                {
                    "name": "Бельдерсай",
                    "slug": "beldersay",
                    "mentions": 4,
                    "summary": "снег",
                    "conditions": [],
                },
                {
                    "name": "Выдуманное",
                    "slug": "no-such-slug",
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
                    "mentions": 2,
                }
            ],
            "trips": [],
            "questions": [],
            "sayr": {"summary": "не упоминали", "items": []},
        }
        self.submitted: list[list[dict]] = []
        self.completions: list[dict] = []
        self.polls = 0

    def submit_batch(self, requests):
        self.submitted.append(requests)
        return f"batch_{len(self.submitted)}"

    def batch_status(self, batch_id):
        self.polls += 1
        status = "ended" if self.polls >= self.polls_until_end else "in_progress"
        return BatchStatus(id=batch_id, status=status)

    def batch_results(self, batch_id):
        items = []
        for r in self.submitted[-1]:
            cid = r["custom_id"]
            if cid in self.fail_ids:
                items.append(BatchItem(cid, False, error="errored: boom"))
            else:
                items.append(
                    BatchItem(
                        cid,
                        True,
                        text=json.dumps(self.chunk_answer, ensure_ascii=False),
                        usage=Usage(input_tokens=1000, output_tokens=100),
                    )
                )
        return items

    def complete(self, params):
        self.completions.append(params)
        return Completion(
            text=json.dumps(self.synthesis, ensure_ascii=False),
            usage=Usage(input_tokens=2000, output_tokens=500),
            stop_reason="end_turn",
            model=params["model"],
        )


def row(i, text="Снег на Бельдерсае", *, day=0, topic=1, author="a1"):
    return SimpleNamespace(
        chat_id=1,
        msg_id=i,
        date=(datetime(2026, 9, 28, 10, 0, tzinfo=TZ) + timedelta(days=day)).astimezone(UTC),
        topic_id=topic,
        topic_title="General",
        reply_to_msg_id=None,
        text=text,
        author_hash=author,
        media_type=None,
        file_name=None,
        lat=None,
        lng=None,
    )


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = {
        "reports_dir": tmp_path / "reports",
        "owner": "@owner",
        "chunk_token_budget": 200,
        "batch_poll_seconds": 10,
        "batch_wait_hours": 1,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def make_deps(repo, gateway, *, sent: list | None = None, clock_state=None) -> DigestDeps:
    sent = sent if sent is not None else []

    async def sender(text: str, path: Path) -> str:
        sent.append((text, path))
        return "parts"

    state = clock_state or {"t": 0.0}

    def sleep(s):
        state["t"] += s

    return DigestDeps(
        repo=repo,
        gateway=gateway,
        catalog_fetcher=lambda: CATALOG,
        sender=sender,
        sleep=sleep,
        clock=lambda: state["t"],
        now=lambda: datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
    )


def test_full_run_produces_report_and_delivers(tmp_path: Path) -> None:
    rows = [
        row(i, "Снег на Бельдерсае по колено " * 5, day=i % 7, author=f"a{i % 4}")
        for i in range(1, 31)
    ]
    repo = FakeRepo([*rows, row(99, "вне недели", day=10)])
    gateway = FakeGateway(polls_until_end=2)
    sent: list = []
    settings = make_settings(tmp_path)
    outcome = asyncio.run(run_digest(settings, make_deps(repo, gateway, sent=sent), week=WEEK))

    assert outcome.status == DONE
    assert outcome.chunks == len(gateway.submitted[0]) > 1
    assert outcome.delivered == "parts"
    assert outcome.cost_usd > 0
    # Запрос к модели: без авторов, с каталогом и схемой.
    request = gateway.submitted[0][0]["params"]
    assert request["model"] == settings.chunk_model
    assert "a1" not in json.dumps(request)
    assert "beldersay" in request["system"][1]["text"]
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert gateway.completions[0]["model"] == settings.synthesis_model
    # Отчёт и JSON на диске и в «базе».
    md_path = tmp_path / "reports" / "2026-W40.md"
    assert outcome.report_path == md_path and md_path.exists()
    report = json.loads((tmp_path / "reports" / "2026-W40.json").read_text())
    assert report["week"] == "2026-W40"
    assert report["stats"]["messages"] == 30
    assert report["stats"]["authors"] == 4
    assert report["places"][0]["url"] == "https://sayr.info/p/beldersay"
    assert report["places"][1]["slug"] is None  # выдуманный slug не прошёл
    assert report["usage"]["output_tokens"] == 100 * outcome.chunks + 500
    digest = repo.digests["2026-W40"]
    assert digest["status"] == DONE
    assert digest["delivered_at"] is not None
    assert digest["report_json"]["stats"]["chunks"] == outcome.chunks
    assert "Бельдерсай" in sent[0][0]
    assert repo.runs[-1]["status"] == "ok"


def test_dry_run_calls_nothing(tmp_path: Path) -> None:
    repo = FakeRepo([row(1), row(2)])
    gateway = FakeGateway()
    outcome = asyncio.run(
        run_digest(make_settings(tmp_path), make_deps(repo, gateway), week=WEEK, dry_run=True)
    )
    assert outcome.status == "dry_run"
    assert outcome.estimate["chunks"] == 1
    assert outcome.estimate["total_cost_usd"] > 0
    assert gateway.submitted == [] and gateway.completions == []
    assert "2026-W40" not in repo.digests
    assert repo.runs[-1]["details"]["dry_run"] is True


def test_batch_timeout_fails_loudly_and_resumes_next_time(tmp_path: Path) -> None:
    repo = FakeRepo([row(1), row(2)])
    gateway = FakeGateway(polls_until_end=1000)
    settings = make_settings(tmp_path, batch_wait_hours=0.01)
    with pytest.raises(DigestError, match="не завершился"):
        asyncio.run(run_digest(settings, make_deps(repo, gateway), week=WEEK))
    assert repo.digests["2026-W40"]["status"] == "batch_timeout"
    assert repo.digests["2026-W40"]["batch_id"] == "batch_1"
    assert repo.runs[-1]["status"] == "error"

    # Повторный запуск забирает тот же батч, не отправляя новый.
    gateway.polls_until_end = gateway.polls + 1
    outcome = asyncio.run(run_digest(settings, make_deps(repo, gateway), week=WEEK))
    assert outcome.status == DONE
    assert len(gateway.submitted) == 1


def test_failed_chunks_are_noted_and_all_failed_is_error(tmp_path: Path) -> None:
    rows = [row(i, "текст " * 50, day=i % 7, topic=i % 3 + 1) for i in range(1, 20)]
    repo = FakeRepo(rows)
    gateway = FakeGateway(fail_ids={"2026-W40-001"})
    outcome = asyncio.run(run_digest(make_settings(tmp_path), make_deps(repo, gateway), week=WEEK))
    assert outcome.status == DONE
    assert any("2026-W40-001" in n for n in outcome.notes)

    all_failed = FakeGateway(fail_ids={f"2026-W40-{i:03d}" for i in range(1, 50)})
    with pytest.raises(DigestError, match="Ни один кусок"):
        asyncio.run(
            run_digest(make_settings(tmp_path), make_deps(FakeRepo(rows), all_failed), week=WEEK)
        )


def test_week_without_messages_skips_models(tmp_path: Path) -> None:
    repo = FakeRepo([])
    gateway = FakeGateway()
    sent: list = []
    outcome = asyncio.run(
        run_digest(make_settings(tmp_path), make_deps(repo, gateway, sent=sent), week=WEEK)
    )
    assert outcome.status == DONE
    assert outcome.chunks == 0 and outcome.cost_usd == 0
    assert gateway.submitted == [] and gateway.completions == []
    assert "не было" in sent[0][0]


def test_missing_owner_is_an_error_after_saving(tmp_path: Path) -> None:
    repo = FakeRepo([row(1)])
    settings = make_settings(tmp_path, owner=None)
    with pytest.raises(DigestError, match="GORETS_OWNER"):
        asyncio.run(run_digest(settings, make_deps(repo, FakeGateway()), week=WEEK))
    assert repo.digests["2026-W40"]["status"] == DONE
    assert (tmp_path / "reports" / "2026-W40.md").exists()


def test_no_send_flag(tmp_path: Path) -> None:
    repo = FakeRepo([row(1)])
    sent: list = []
    outcome = asyncio.run(
        run_digest(
            make_settings(tmp_path),
            make_deps(repo, FakeGateway(), sent=sent),
            week=WEEK,
            send=False,
        )
    )
    assert outcome.status == DONE and outcome.delivered is None and sent == []
