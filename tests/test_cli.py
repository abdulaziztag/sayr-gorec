"""Команды через `main`: разбор аргументов, коды выхода, работа с базой."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gorets import cli
from gorets.anonymize import author_hash
from gorets.catalog import Place
from gorets.storage import Repository
from tests.test_storage_pg import CHAT, SECRET, _row

TEST_DB_URL = os.environ.get("GORETS_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Repository) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"GORETS_DATABASE_URL={TEST_DB_URL}\nGORETS_AUTHOR_HMAC_SECRET={SECRET}\n"
        f"GORETS_REPORTS_DIR={tmp_path / 'reports'}\nGORETS_OWNER=@owner\n"
        f"GORETS_CONFIG={ROOT / 'gorets.toml'}\n"
    )
    # Чтобы настоящие переменные окружения не перебили тестовый .env.
    for key in list(os.environ):
        if key.startswith("GORETS_") and key != "GORETS_TEST_DATABASE_URL":
            monkeypatch.delenv(key)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return env_file


def test_forget_author_and_message(env: Path, repo: Repository, capsys) -> None:
    repo.upsert_chat(CHAT, username="gorets_uzb", title="ГОРЕЦ", is_forum=True)
    repo.upsert_messages([_row(1, author=7), _row(2, author=7), _row(3, author=8)])
    assert cli.main(["--env-file", str(env), "forget-author", "7"]) == 0
    assert "Удалено сообщений автора: 2" in capsys.readouterr().out
    assert repo.count_messages(CHAT) == 1
    assert repo.forget_author(author_hash(SECRET, 8)) == 1

    repo.upsert_messages([_row(5)])
    assert cli.main(["--env-file", str(env), "forget-message", "gorets_uzb", "5"]) == 0
    assert "Удалено сообщений: 1" in capsys.readouterr().out
    assert cli.main(["--env-file", str(env), "forget-message", "nope", "5"]) == 1


def test_stats_command(env: Path, repo: Repository, capsys) -> None:
    repo.upsert_chat(CHAT, username="gorets_uzb", title="ГОРЕЦ", is_forum=True)
    repo.upsert_messages([_row(1), _row(2)])
    run_id = repo.start_run("collect", CHAT)
    repo.finish_run(run_id, status="ok", new_messages=2, last_msg_id=2)
    assert cli.main(["--env-file", str(env), "stats"]) == 0
    out = capsys.readouterr().out
    assert "Сообщений в базе: 2" in out
    assert "gorets_uzb: 2 сообщений, последний id 2" in out
    assert "collect   ok" in out


def test_digest_dry_run_without_api_key(env: Path, repo: Repository, monkeypatch, capsys) -> None:
    repo.upsert_chat(CHAT, username="gorets_uzb", title="ГОРЕЦ", is_forum=True)
    now = datetime.now(UTC)
    repo.upsert_messages([_row(i, text="Снег на Чимгане " * 10, days_ago=1) for i in range(1, 6)])
    monkeypatch.setattr(cli, "fetch_catalog", lambda url, timeout: [Place("chimgan", "Чимган")])
    week = cli.previous_week(now, cli.ZoneInfo("Asia/Tashkent"))
    # Сообщения «вчера» могут быть и в текущей неделе — берём ту, где они есть.
    label = week.label if repo.messages_between(week.start, week.end) else None
    args = ["--env-file", str(env), "digest", "--dry-run"] + (["--week", label] if label else [])
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "API не вызывался" in out
    assert repo.recent_runs()[0].kind == "digest"


def test_digest_without_key_fails_with_code_1(env: Path, repo: Repository, monkeypatch) -> None:
    inside_w40 = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    repo.upsert_messages([_row(1, text="Снег", date=inside_w40)])
    monkeypatch.setattr(cli, "fetch_catalog", lambda url, timeout: [Place("chimgan", "Чимган")])
    assert cli.main(["--env-file", str(env), "digest", "--week", "2026-W40", "--no-send"]) == 1
    assert "ANTHROPIC_API_KEY" in (repo.recent_runs()[0].error or "")


def test_unknown_week_is_an_error(env: Path, capsys) -> None:
    assert cli.main(["--env-file", str(env), "digest", "--week", "2026-W99", "--dry-run"]) == 1


def test_collect_without_session_fails_cleanly(env: Path, capsys) -> None:
    assert cli.main(["--env-file", str(env), "collect"]) == 1


def test_extract_dry_run_cli(env: Path, repo: Repository, capsys) -> None:
    repo.upsert_chat(CHAT, username="gorets_uzb", title="ГОРЕЦ", is_forum=True)
    repo.upsert_messages([_row(1, text="Тур", topic_id=500, topic_title="АФИША ПОХОДОВ")])
    assert cli.main(["--env-file", str(env), "extract", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "afisha_tour (afisha): кандидатов 1" in out
    assert repo.recent_runs()[0].kind == "extract"


def test_parser_has_all_commands() -> None:
    parser = cli.build_parser()
    commands = parser._subparsers._group_actions[0].choices
    assert set(commands) == {
        "login",
        "collect",
        "backfill",
        "import-export",
        "digest",
        "extract",
        "api",
        "forget-author",
        "forget-message",
        "stats",
    }
