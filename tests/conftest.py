"""Общие фикстуры.

Тесты базы идут на Postgres, если задан GORETS_TEST_DATABASE_URL, иначе
пропускаются. Схема накатывается миграциями Alembic — так проверяется и то,
что миграции соответствуют моделям.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text

from gorets.db import make_engine
from gorets.storage import Repository

TEST_DB_URL = os.environ.get("GORETS_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parent.parent


def _upgrade_schema(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.cmd_opts = type("Opts", (), {"x": [f"url={url}"]})()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def pg_engine():
    if not TEST_DB_URL:
        pytest.skip("GORETS_TEST_DATABASE_URL не задан — тесты базы пропущены")
    _upgrade_schema(TEST_DB_URL)
    engine = make_engine(TEST_DB_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def repo(pg_engine) -> Repository:
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE messages, chats, sync_state, runs, digests, "
                "extractions, extract_batches RESTART IDENTITY"
            )
        )
    return Repository(pg_engine)
