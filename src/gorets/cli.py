"""Команда `gorets`: вход, сбор, догрузка, импорт, разбор недели, удаление по просьбе, статистика.

Любая ошибка — ненулевой код выхода: служба systemd становится failed, и это
видно в `systemctl --failed`. Секреты (строка сессии, ключ API) в журнал не
попадают: сессия печатается только командой `login` и только в stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gorets import __version__
from gorets.anonymize import author_hash
from gorets.catalog import CatalogError, fetch_catalog
from gorets.config import Settings, load_settings
from gorets.db import make_engine
from gorets.digest.claude import AnthropicGateway, ClaudeError
from gorets.digest.run import DigestDeps, DigestError, resend_digest, run_digest
from gorets.digest.weeks import Week, parse_week, previous_week
from gorets.envfile import append_key, has_key
from gorets.extract import ExtractError, run_extract
from gorets.feeds import FeedConfigError, load_project_config
from gorets.importer import ImportResult, import_export
from gorets.storage import Repository
from gorets.telegram.client import TelegramNotConfigured, connect_authorized, login_interactive
from gorets.telegram.collect import CollectError, run_backfill, run_collect
from gorets.telegram.deliver import send_report

log = logging.getLogger("gorets")

KNOWN_ERRORS = (
    CollectError,
    ExtractError,
    FeedConfigError,
    DigestError,
    ClaudeError,
    CatalogError,
    TelegramNotConfigured,
    ValueError,
    OSError,
)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Telethon многословен на INFO, а нам важны только предупреждения.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def make_repo(settings: Settings) -> Repository:
    return Repository(make_engine(settings.database_url))


# --- команды -----------------------------------------------------------------------


def cmd_login(args: argparse.Namespace, settings: Settings) -> int:
    session, who = asyncio.run(login_interactive(settings))
    env_path = Path(args.env_file or ".env")
    print(f"Вход выполнен: {who}")
    if has_key(env_path, "GORETS_TG_SESSION"):
        print(f"В {env_path} уже есть GORETS_TG_SESSION — файл не тронут. Новая строка сессии:")
        print(session)
    else:
        append_key(env_path, "GORETS_TG_SESSION", session)
        print(f"Строка сессии дописана в {env_path} (GORETS_TG_SESSION).")
    return 0


async def _with_client(settings: Settings, coro_factory):
    client = await connect_authorized(settings)
    try:
        return await coro_factory(client)
    finally:
        await client.disconnect()


def cmd_collect(args: argparse.Namespace, settings: Settings) -> int:
    repo = make_repo(settings)
    report = asyncio.run(_with_client(settings, lambda c: run_collect(settings, repo, c)))
    for chat in report.chats:
        print(f"{chat['chat']}: новых {chat['new']}, просмотрено {chat['seen']}")
    print(f"Удалено старше {settings.retention_days} дней: {report.deleted_old}")
    return 0


def cmd_backfill(args: argparse.Namespace, settings: Settings) -> int:
    since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=ZoneInfo(settings.timezone))
    since = since.astimezone(UTC)
    repo = make_repo(settings)
    report = asyncio.run(
        _with_client(
            settings, lambda c: run_backfill(settings, repo, c, since=since, limit=args.limit)
        )
    )
    for chat in report.chats:
        state = "история дочитана" if chat.get("done") else "есть ещё — запустите снова"
        print(f"{chat['chat']}: новых {chat['new']}, просмотрено {chat['seen']}, {state}")
    return 0


def cmd_import_export(args: argparse.Namespace, settings: Settings) -> int:
    repo = make_repo(settings)
    path = Path(args.path)
    if not path.exists():
        raise ValueError(f"Файл не найден: {path}")

    def progress(result: ImportResult) -> None:
        if result.stored % 5000 < 500:
            log.info(
                "Импорт: просмотрено %s, записано %s, новых %s",
                result.seen,
                result.stored,
                result.new,
            )

    run_id = repo.start_run("import")
    try:
        result = import_export(
            path,
            repo,
            settings,
            chat_id=args.chat_id,
            is_forum=not args.no_forum,
            progress=progress,
        )
    except Exception as exc:
        repo.finish_run(run_id, status="error", error=str(exc))
        raise
    repo.finish_run(
        run_id,
        status="ok",
        new_messages=result.new,
        details={"chat_id": result.chat_id, "seen": result.seen, "skipped": result.skipped},
    )
    print(
        f"Чат {result.chat_id}: просмотрено {result.seen}, записано {result.stored}, "
        f"новых {result.new}, пропущено (служебные, боты) {result.skipped}, "
        f"веток {len(result.topics)}"
    )
    return 0


def resolve_week(args: argparse.Namespace, settings: Settings) -> Week:
    tz = ZoneInfo(settings.timezone)
    if args.week:
        return parse_week(args.week, tz)
    return previous_week(datetime.now(UTC), tz)


def build_digest_deps(settings: Settings, *, dry_run: bool) -> DigestDeps:
    repo = make_repo(settings)
    gateway = None
    if not dry_run and settings.anthropic_api_key:
        gateway = AnthropicGateway(settings.anthropic_api_key)

    async def sender(text: str, path: Path) -> str:
        return await _with_client(
            settings,
            lambda c: send_report(
                c,
                settings.owner or "",
                text=text,
                file_path=path,
                mode=settings.delivery,
                max_parts=settings.delivery_max_parts,
            ),
        )

    return DigestDeps(
        repo=repo,
        gateway=gateway,
        catalog_fetcher=lambda: fetch_catalog(
            settings.catalog_url, timeout=settings.catalog_timeout
        ),
        sender=sender,
    )


def cmd_digest(args: argparse.Namespace, settings: Settings) -> int:
    week = resolve_week(args, settings)
    deps = build_digest_deps(settings, dry_run=args.dry_run or args.resend)
    if args.resend:
        how = asyncio.run(resend_digest(settings, deps, week=week))
        print(f"Неделя {week.label}: отчёт отправлен заново ({how})")
        return 0
    outcome = asyncio.run(
        run_digest(settings, deps, week=week, dry_run=args.dry_run, send=not args.no_send)
    )
    if args.dry_run:
        est = outcome.estimate
        print(f"Неделя {week.label}: кусков {est['chunks']}")
        print(
            f"Куски ({settings.chunk_model}, батч): ≈{est['chunk_input_tokens']} токенов на входе, "
            f"≈{est['chunk_output_tokens']} на выходе, ≈${est['chunks_cost_usd']:.2f}"
        )
        print(
            f"Сведение ({settings.synthesis_model}): ≈{est['synthesis_input_tokens']} на входе, "
            f"≈{est['synthesis_output_tokens']} на выходе, ≈${est['synthesis_cost_usd']:.2f}"
        )
        print(f"Итого ≈ ${est['total_cost_usd']:.2f}. API не вызывался.")
        return 0
    print(
        f"Неделя {week.label}: отчёт {outcome.report_path}, кусков {outcome.chunks}, "
        f"токенов {outcome.usage.total_input}+{outcome.usage.output_tokens}, "
        f"≈${outcome.cost_usd:.2f}, доставка: {outcome.delivered or 'нет'}"
    )
    for note in outcome.notes:
        print(f"  замечание: {note}")
    return 0


def cmd_extract(args: argparse.Namespace, settings: Settings) -> int:
    repo = make_repo(settings)
    project = load_project_config(settings.config_file)
    gateway = None
    if not args.dry_run and settings.anthropic_api_key:
        gateway = AnthropicGateway(settings.anthropic_api_key)
    since = None
    if args.since:
        since = (
            datetime.strptime(args.since, "%Y-%m-%d")
            .replace(tzinfo=ZoneInfo(settings.timezone))
            .astimezone(UTC)
        )
    run_id = repo.start_run("extract")
    try:
        report = run_extract(
            settings,
            repo,
            project,
            gateway,
            names=args.extractor or None,
            since=since,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except ExtractError as exc:
        repo.finish_run(run_id, status="error", error=str(exc))
        raise
    except Exception as exc:
        repo.finish_run(run_id, status="error", error=str(exc))
        raise
    repo.finish_run(
        run_id,
        status="ok",
        details={
            "extractors": report.extractors,
            "cost_usd": report.cost_usd,
            "dry_run": args.dry_run,
        },
    )
    for entry in report.extractors:
        feeds = ", ".join(entry["feeds"])
        line = f"{entry['extractor']} ({feeds}): кандидатов {entry.get('candidates', 0)}"
        if "estimate_usd" in entry:
            line += f", оценка ≈${entry['estimate_usd']:.2f}"
        if "saved" in entry:
            line += (
                f", записано {entry['saved']}: по теме {entry['ok']}, не по теме "
                f"{entry['skipped']}, ошибок {entry['error']}, ≈${entry['cost_usd']:.3f}"
            )
        if entry.get("note"):
            line += f" — {entry['note']}"
        print(line)
    if not args.dry_run:
        print(f"Итого ≈ ${report.cost_usd:.3f}")
    return 0


def cmd_api(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    from gorets.api import create_app

    app = create_app(settings, make_repo(settings), load_project_config(settings.config_file))
    uvicorn.run(
        app,
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        log_level=settings.log_level.lower(),
    )
    return 0


def cmd_mcp(args: argparse.Namespace, settings: Settings) -> int:
    from gorets.mcp_server import McpNotInstalled, create_server

    try:
        server = create_server(
            settings, make_repo(settings), load_project_config(settings.config_file)
        )
    except McpNotInstalled as exc:
        raise ValueError(str(exc)) from exc
    server.run(transport="stdio")
    return 0


def cmd_watch(args: argparse.Namespace, settings: Settings) -> int:
    from gorets.telegram.watch import run_watch

    repo = make_repo(settings)
    project = load_project_config(settings.config_file)

    async def run(client: Any) -> None:
        await run_watch(settings, repo, client, project)

    asyncio.run(_with_client(settings, run, updates=True))
    return 0


def cmd_alert(args: argparse.Namespace, settings: Settings) -> int:
    from gorets.alert import alert_text, send_alert

    repo = None
    try:
        repo = make_repo(settings)
    except Exception:
        repo = None
    text = alert_text(args.unit, repo)
    if not settings.owner:
        log.error("GORETS_OWNER не задан, оповещение некому: %s", text)
        return 1
    asyncio.run(_with_client(settings, lambda c: send_alert(c, settings.owner or "", text)))
    print("Оповещение отправлено")
    return 0


def cmd_forget_author(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.author_hmac_secret:
        raise ValueError("Не задан GORETS_AUTHOR_HMAC_SECRET")
    repo = make_repo(settings)
    deleted = repo.forget_author(author_hash(settings.author_hmac_secret, args.tg_user_id))
    print(f"Удалено сообщений автора: {deleted}")
    return 0


def cmd_forget_message(args: argparse.Namespace, settings: Settings) -> int:
    repo = make_repo(settings)
    chat_id = repo.resolve_chat_id(args.chat)
    if chat_id is None:
        raise ValueError(f"Чат {args.chat!r} не найден в базе: укажите username или числовой id")
    deleted = repo.forget_message(chat_id, args.msg_id)
    print(f"Удалено сообщений: {deleted}")
    return 0


def cmd_stats(args: argparse.Namespace, settings: Settings) -> int:
    repo = make_repo(settings)
    stats = repo.stats()
    print(f"Сообщений в базе: {stats['messages']}, авторов (хешей): {stats['authors']}")
    if stats["first_date"]:
        print(f"Период: {stats['first_date']:%Y-%m-%d} — {stats['last_date']:%Y-%m-%d}")
    for chat in stats["chats"]:
        name = chat["username"] or chat["chat_id"]
        print(f"  {name}: {chat['messages']} сообщений, последний id {chat['last_msg_id']}")
    print(f"Итогов недель: {stats['digests']}")
    for digest in repo.list_digests(5):
        print(
            f"  {digest.week}: {digest.status}, сообщений {digest.messages_count}, "
            f"${digest.cost_usd}, доставлен: {digest.delivered_at or 'нет'}"
        )
    print("Последние прогоны:")
    for run in repo.recent_runs(8):
        finished = f"{run.finished_at:%Y-%m-%d %H:%M}" if run.finished_at else "…"
        line = f"  {run.started_at:%Y-%m-%d %H:%M} {run.kind:9} {run.status:7} до {finished}"
        if run.new_messages:
            line += f", новых {run.new_messages}"
        if run.error:
            line += f", ошибка: {run.error[:120]}"
        print(line)
    return 0


# --- разбор аргументов ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gorets",
        description="Сборщик сообщений форума «ГОРЕЦ» и недельный разбор ИИ для Sayr",
    )
    parser.add_argument("--env-file", help="файл настроек вместо .env")
    parser.add_argument("--version", action="version", version=f"gorets {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="интерактивный вход в Telegram, строка сессии → .env")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("collect", help="забрать новые сообщения и почистить старые")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("backfill", help="догрузить историю с даты, порциями")
    p.add_argument("--since", required=True, help="дата начала, ГГГГ-ММ-ДД")
    p.add_argument("--limit", type=int, help="сообщений за запуск (по умолчанию из настроек)")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("import-export", help="импорт выгрузки Telegram Desktop (result.json)")
    p.add_argument("path", help="путь к result.json")
    p.add_argument("--chat-id", type=int, help="id чата, если его нет в выгрузке")
    p.add_argument("--no-forum", action="store_true", help="чат без веток (не форум)")
    p.set_defaults(func=cmd_import_export)

    p = sub.add_parser("digest", help="разбор недели моделью и отчёт владельцу")
    p.add_argument("--week", help="неделя ГГГГ-Wнн (по умолчанию прошлая)")
    p.add_argument(
        "--dry-run", action="store_true", help="только объём, число кусков и оценка цены"
    )
    p.add_argument("--no-send", action="store_true", help="собрать отчёт, но не отправлять")
    p.add_argument("--resend", action="store_true", help="отправить уже собранный отчёт ещё раз")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("extract", help="прогнать извлекатели из gorets.toml по новым сообщениям")
    p.add_argument("--extractor", action="append", help="только этот извлекатель (можно несколько)")
    p.add_argument("--since", help="не старше даты ГГГГ-ММ-ДД")
    p.add_argument("--limit", type=int, help="не больше N сообщений на извлекатель за запуск")
    p.add_argument("--dry-run", action="store_true", help="сколько сообщений ждёт и оценка цены")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("api", help="HTTP API для других проектов (только чтение, по токену)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_api)

    p = sub.add_parser("mcp", help="MCP-сервер (stdio) для агентов; нужен `uv sync --extra mcp`")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("watch", help="слушать чаты в реальном времени и слать вебхуки фидов")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("alert", help="сообщить владельцу о сбое службы (systemd OnFailure)")
    p.add_argument("unit", help="имя unit-а systemd")
    p.set_defaults(func=cmd_alert)

    p = sub.add_parser("forget-author", help="удалить всё написанное автором (по id Telegram)")
    p.add_argument("tg_user_id", type=int)
    p.set_defaults(func=cmd_forget_author)

    p = sub.add_parser("forget-message", help="удалить одно сообщение")
    p.add_argument("chat", help="username или id чата")
    p.add_argument("msg_id", type=int)
    p.set_defaults(func=cmd_forget_message)

    p = sub.add_parser("stats", help="что в базе и как прошли последние прогоны")
    p.set_defaults(func=cmd_stats)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.env_file)
    setup_logging(settings.log_level)
    try:
        return int(args.func(args, settings))
    except KNOWN_ERRORS as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("Прервано")
        return 130


if __name__ == "__main__":
    sys.exit(main())
