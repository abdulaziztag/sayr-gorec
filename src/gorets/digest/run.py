"""Разбор недели от начала до конца.

Две ступени: куски недели → Claude Message Batches (вдвое дешевле), затем
сведение всех JSON одним обычным вызовом Messages API. Между ними ничего не
висит: батч опрашивается с паузами, а состояние пишется в таблицу digests,
так что не успевший батч можно забрать при следующем запуске без повторной
оплаты.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gorets.catalog import Place, catalog_index, catalog_text
from gorets.config import Settings
from gorets.digest import prompts
from gorets.digest.aggregate import compute_stats, merge_chunk_results, sanitize_chunk_result
from gorets.digest.chunking import (
    Chunk,
    ChunkMessage,
    build_chunks,
    chunks_fingerprint,
    estimate_tokens,
)
from gorets.digest.claude import ClaudeError, ClaudeGateway, Usage, extract_json, wait_for_batch
from gorets.digest.pricing import cost_usd
from gorets.digest.render import render_markdown, render_telegram
from gorets.digest.schemas import CHUNK_SCHEMA, SYNTHESIS_SCHEMA
from gorets.digest.weeks import Week

log = logging.getLogger(__name__)

# Статусы строки digests.
PENDING = "pending"
BATCH_SUBMITTED = "batch_submitted"
BATCH_TIMEOUT = "batch_timeout"
DONE = "done"
ERROR = "error"
RESUMABLE = {BATCH_SUBMITTED, BATCH_TIMEOUT}
# Готовую неделю тоже можно пересобрать без повторного батча: результаты
# кусков лежат в базе, платится только сведение.
REUSABLE = RESUMABLE | {DONE, "synthesizing"}

Sender = Callable[[str, Path], Awaitable[str]]


class DigestError(RuntimeError):
    pass


@dataclass
class DigestDeps:
    repo: Any
    gateway: ClaudeGateway | None
    catalog_fetcher: Callable[[], list[Place]]
    sender: Sender | None = None
    sleep: Callable[[float], None] = __import__("time").sleep
    clock: Callable[[], float] = __import__("time").monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass
class DigestPlan:
    week: Week
    messages: list[ChunkMessage]
    chunks: list[Chunk]
    stats: dict[str, Any]
    catalog: list[Place]
    chat_names: dict[int, str]
    estimate: dict[str, Any]


@dataclass
class DigestOutcome:
    week: str
    status: str
    chunks: int = 0
    report_path: Path | None = None
    delivered: str | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    estimate: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def to_chunk_messages(rows: list[Any], tz: ZoneInfo) -> list[ChunkMessage]:
    return [
        ChunkMessage(
            chat_id=r.chat_id,
            msg_id=r.msg_id,
            date=r.date.astimezone(tz),
            topic_id=r.topic_id,
            topic_title=r.topic_title,
            reply_to=r.reply_to_msg_id,
            text=r.text or "",
            author_hash=r.author_hash,
            media_type=r.media_type,
            file_name=r.file_name,
            lat=float(r.lat) if r.lat is not None else None,
            lng=float(r.lng) if r.lng is not None else None,
        )
        for r in rows
    ]


def period_label(week: Week) -> str:
    return f"{week.start.strftime('%d.%m.%Y')} — {week.last_day.strftime('%d.%m.%Y')}"


def chunk_request(
    chunk: Chunk, *, settings: Settings, system: list[dict[str, Any]], week: Week
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": settings.chunk_model,
        "max_tokens": settings.chunk_max_tokens,
        "system": system,
        "messages": [
            {
                "role": "user",
                "content": prompts.chunk_user_message(chunk.text, week.label, period_label(week)),
            }
        ],
    }
    if settings.structured_output:
        params["output_config"] = {"format": {"type": "json_schema", "schema": CHUNK_SCHEMA}}
    return {"custom_id": chunk.id, "params": params}


def chunk_system(catalog: list[Place]) -> list[dict[str, Any]]:
    # Инструкция и каталог одинаковы для всех кусков — кэшируем, чтобы платить
    # за них один раз на батч, а не за каждый кусок.
    return [
        {"type": "text", "text": prompts.CHUNK_SYSTEM},
        {
            "type": "text",
            "text": "Каталог мест Sayr (slug — ключ для ответа):\n" + catalog_text(catalog),
            "cache_control": {"type": "ephemeral"},
        },
    ]


def estimate_cost(settings: Settings, chunks: list[Chunk], system_tokens: int) -> dict[str, Any]:
    """Оценка цены без обращения к API: грубые токены на входе и типичный ответ."""
    chunk_in = sum(c.est_tokens for c in chunks) + system_tokens * max(len(chunks), 1)
    chunk_out = settings.chunk_output_estimate * len(chunks)
    synth_in = chunk_out + 3000
    synth_out = 6000
    chunks_cost = cost_usd(
        settings.chunk_model, input_tokens=chunk_in, output_tokens=chunk_out, batch=True
    )
    synth_cost = cost_usd(settings.synthesis_model, input_tokens=synth_in, output_tokens=synth_out)
    return {
        "chunks": len(chunks),
        "chunk_input_tokens": chunk_in,
        "chunk_output_tokens": chunk_out,
        "synthesis_input_tokens": synth_in,
        "synthesis_output_tokens": synth_out,
        "chunks_cost_usd": round(chunks_cost, 4),
        "synthesis_cost_usd": round(synth_cost, 4),
        "total_cost_usd": round(chunks_cost + synth_cost, 4),
    }


def plan_digest(settings: Settings, deps: DigestDeps, week: Week) -> DigestPlan:
    tz = ZoneInfo(settings.timezone)
    rows = deps.repo.messages_between(week.start, week.end)
    chat_names = {
        c.chat_id: ("@" + c.username if c.username else c.title or str(c.chat_id))
        for c in deps.repo.list_chats()
    }
    messages = to_chunk_messages(rows, tz)
    chunks = build_chunks(
        messages,
        week_label=week.label,
        chat_names=chat_names,
        budget_tokens=settings.chunk_token_budget,
        chars_per_token=settings.chars_per_token,
    )
    catalog = deps.catalog_fetcher() if messages else []
    system_tokens = estimate_tokens(
        prompts.CHUNK_SYSTEM + catalog_text(catalog), settings.chars_per_token
    )
    stats = compute_stats(messages, chunks, chat_names)
    estimate = estimate_cost(settings, chunks, system_tokens)
    return DigestPlan(week, messages, chunks, stats, catalog, chat_names, estimate)


def plan_fingerprint(settings: Settings, chunks: list[Chunk]) -> str:
    """Отпечаток нарезки вместе с моделью и инструкцией: изменилось что-то — батч новый."""
    import hashlib

    digest = hashlib.sha256(chunks_fingerprint(chunks).encode())
    digest.update(settings.chunk_model.encode())
    digest.update(prompts.CHUNK_SYSTEM.encode())
    digest.update(str(settings.structured_output).encode())
    return digest.hexdigest()


def assemble_report(
    *,
    week: Week,
    synthesis: dict[str, Any],
    stats: dict[str, Any],
    catalog: list[Place],
    settings: Settings,
    usage: Usage,
    cost: float,
    now: datetime,
    notes: list[str],
) -> dict[str, Any]:
    index = catalog_index(catalog)

    def with_url(item: dict[str, Any]) -> dict[str, Any]:
        slug = item.get("slug")
        if slug and slug in index:
            item["url"] = settings.place_url_template.format(slug=slug)
            item.setdefault("name", index[slug].name)
        elif slug:
            # Модель назвала slug, которого нет в каталоге — не верим.
            item["slug"] = None
        return item

    return {
        "schema_version": 1,
        "week": week.label,
        "period": {
            "start": week.start.isoformat(),
            "end": week.end.isoformat(),
            "start_date": week.start.strftime("%d.%m.%Y"),
            "end_date": week.last_day.strftime("%d.%m.%Y"),
            "timezone": settings.timezone,
        },
        "generated_at": now.isoformat(),
        "headline": str(synthesis.get("headline") or ""),
        "places": [with_url(p) for p in synthesis.get("places") or [] if isinstance(p, dict)],
        "new_places": [p for p in synthesis.get("new_places") or [] if isinstance(p, dict)],
        "trips": [with_url(t) for t in synthesis.get("trips") or [] if isinstance(t, dict)],
        "questions": [q for q in synthesis.get("questions") or [] if isinstance(q, dict)],
        "sayr": synthesis.get("sayr") or {"summary": "", "items": []},
        "stats": stats,
        "models": {"chunk": settings.chunk_model, "synthesis": settings.synthesis_model},
        "usage": {
            "input_tokens": usage.total_input,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cost_usd": round(cost, 4),
        },
        "notes": notes,
    }


def _collect_chunk_results(
    items: list[Any], chunks: list[Chunk], settings: Settings
) -> tuple[dict[str, dict[str, Any]], Usage, float, list[str]]:
    by_id = {c.id: c for c in chunks}
    results: dict[str, dict[str, Any]] = {}
    usage = Usage()
    notes: list[str] = []
    for item in items:
        if item.custom_id not in by_id:
            continue
        if not item.ok or not item.text:
            notes.append(f"кусок {item.custom_id} не разобран: {item.error or 'пустой ответ'}")
            continue
        if item.usage:
            usage.add(item.usage)
        try:
            results[item.custom_id] = sanitize_chunk_result(extract_json(item.text))
        except ClaudeError as exc:
            notes.append(f"кусок {item.custom_id} не разобран: {exc}")
    missing = [c.id for c in chunks if c.id not in results and not any(c.id in n for n in notes)]
    notes.extend(f"кусок {m} не вернулся из батча" for m in missing)
    cost = cost_usd(
        settings.chunk_model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        batch=True,
    )
    return results, usage, cost, notes


async def resend_digest(settings: Settings, deps: DigestDeps, *, week: Week) -> str:
    """Отправить уже собранный отчёт ещё раз (например, после сбоя доставки)."""
    digest = deps.repo.get_digest(week.label)
    if digest is None or not digest.report_json:
        raise DigestError(
            f"Отчёта за {week.label} в базе нет — соберите его: gorets digest --week …"
        )
    if deps.sender is None or not settings.owner:
        raise DigestError("Отправить некому: задайте GORETS_OWNER и сессию Telegram")
    md_path = Path(settings.reports_dir) / f"{week.label}.md"
    if not md_path.exists():
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(
            digest.report_md or render_markdown(digest.report_json), encoding="utf-8"
        )
    how = await deps.sender(render_telegram(digest.report_json), md_path)
    deps.repo.save_digest(week.label, delivered_at=deps.now())
    return how


async def run_digest(
    settings: Settings,
    deps: DigestDeps,
    *,
    week: Week,
    dry_run: bool = False,
    send: bool = True,
) -> DigestOutcome:
    repo = deps.repo
    run_id = repo.start_run("digest")
    try:
        outcome = await _run_digest(settings, deps, week=week, dry_run=dry_run, send=send)
    except Exception as exc:
        repo.finish_run(run_id, status="error", error=str(exc), details={"week": week.label})
        raise
    repo.finish_run(
        run_id,
        status="ok",
        details={
            "week": week.label,
            "status": outcome.status,
            "chunks": outcome.chunks,
            "cost_usd": outcome.cost_usd,
            "delivered": outcome.delivered,
            "dry_run": dry_run,
        },
    )
    return outcome


async def _run_digest(
    settings: Settings, deps: DigestDeps, *, week: Week, dry_run: bool, send: bool
) -> DigestOutcome:
    repo = deps.repo
    plan = plan_digest(settings, deps, week)
    outcome = DigestOutcome(week=week.label, status=PENDING, chunks=len(plan.chunks))
    outcome.estimate = plan.estimate
    log.info(
        "Неделя %s: сообщений %s, участников %s, кусков %s, оценка ≈ $%.2f",
        week.label,
        plan.stats["messages"],
        plan.stats["authors"],
        len(plan.chunks),
        plan.estimate["total_cost_usd"],
    )
    if dry_run:
        outcome.status = "dry_run"
        return outcome

    base_fields = {
        "period_start": week.start,
        "period_end": week.end,
        "chunk_model": settings.chunk_model,
        "synthesis_model": settings.synthesis_model,
        "messages_count": plan.stats["messages"],
        "authors_count": plan.stats["authors"],
        "error": None,
    }
    notes: list[str] = []
    usage = Usage()
    cost = 0.0
    chunk_results: dict[str, dict[str, Any]] = {}

    if plan.chunks:
        if deps.gateway is None:
            raise DigestError("Нет клиента Claude: задайте ANTHROPIC_API_KEY")
        fingerprint = plan_fingerprint(settings, plan.chunks)
        existing = repo.get_digest(week.label)
        batch_id = None
        stored_results = None
        if existing is not None and existing.chunks_fingerprint == fingerprint:
            if existing.status in REUSABLE and existing.chunk_results:
                stored_results = dict(existing.chunk_results)
                log.info("Неделя %s: результаты кусков берём из базы", week.label)
            elif existing.status in RESUMABLE and existing.batch_id:
                batch_id = existing.batch_id
                log.info("Неделя %s: забираем ранее отправленный батч %s", week.label, batch_id)
        if stored_results is not None:
            chunk_results = stored_results
            notes.append("результаты кусков взяты из прошлого разбора, батч не отправлялся")
        elif batch_id is None:
            system = chunk_system(plan.catalog)
            requests = [
                chunk_request(c, settings=settings, system=system, week=week) for c in plan.chunks
            ]
            batch_id = deps.gateway.submit_batch(requests)
            log.info(
                "Неделя %s: отправлен батч %s из %s кусков", week.label, batch_id, len(requests)
            )
        if stored_results is None:
            chunk_results, usage, cost, notes = _await_batch(
                settings, deps, week, plan, batch_id, fingerprint, base_fields, notes
            )
        else:
            repo.save_digest(
                week.label, status="synthesizing", chunks_fingerprint=fingerprint, **base_fields
            )

    merged = merge_chunk_results(list(chunk_results.values()))
    if plan.chunks:
        synthesis, synth_usage = _synthesize(settings, deps, week, plan, merged)
        usage.add(synth_usage)
        cost += cost_usd(
            settings.synthesis_model,
            input_tokens=synth_usage.input_tokens,
            output_tokens=synth_usage.output_tokens,
            cache_read_tokens=synth_usage.cache_read_tokens,
            cache_write_tokens=synth_usage.cache_write_tokens,
        )
    else:
        notes.append("за неделю не было сообщений с текстом — модель не вызывалась")
        synthesis = {"headline": "За неделю сообщений с текстом не было."}

    report = assemble_report(
        week=week,
        synthesis=synthesis,
        stats=plan.stats,
        catalog=plan.catalog,
        settings=settings,
        usage=usage,
        cost=cost,
        now=deps.now(),
        notes=notes,
    )
    markdown = render_markdown(report)
    telegram_text = render_telegram(report)

    reports_dir = Path(settings.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"{week.label}.md"
    md_path.write_text(markdown, encoding="utf-8")
    (reports_dir / f"{week.label}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    repo.save_digest(
        week.label,
        status=DONE,
        report_md=markdown,
        report_json=report,
        input_tokens=usage.total_input,
        output_tokens=usage.output_tokens,
        cost_usd=Decimal(str(round(cost, 4))),
        chunk_results=chunk_results or None,
        **base_fields,
    )
    outcome.status = DONE
    outcome.report_path = md_path
    outcome.usage = usage
    outcome.cost_usd = round(cost, 4)
    outcome.notes = notes

    if send:
        if deps.sender is None or not settings.owner:
            raise DigestError(
                "Отчёт собран и сохранён, но отправить некому: "
                "задайте GORETS_OWNER и сессию Telegram"
            )
        try:
            outcome.delivered = await deps.sender(telegram_text, md_path)
        except Exception as exc:
            raise DigestError(f"Отчёт собран и сохранён, но отправить не удалось: {exc}") from exc
        repo.save_digest(week.label, delivered_at=deps.now())
    return outcome


def _synthesize(
    settings: Settings,
    deps: DigestDeps,
    week: Week,
    plan: DigestPlan,
    merged: dict[str, Any],
) -> tuple[dict[str, Any], Usage]:
    assert deps.gateway is not None
    params: dict[str, Any] = {
        "model": settings.synthesis_model,
        "max_tokens": settings.synthesis_max_tokens,
        "system": [
            {"type": "text", "text": prompts.SYNTHESIS_SYSTEM},
            {
                "type": "text",
                "text": "Каталог мест Sayr (slug — ключ):\n" + catalog_text(plan.catalog),
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": prompts.synthesis_user_message(
                    week_label=week.label,
                    period=period_label(week),
                    stats=plan.stats,
                    merged=merged,
                ),
            }
        ],
    }
    if settings.structured_output:
        params["output_config"] = {"format": {"type": "json_schema", "schema": SYNTHESIS_SCHEMA}}
    completion = deps.gateway.complete(params)
    if completion.stop_reason == "max_tokens":
        raise DigestError("Сведение обрезано по max_tokens — увеличьте GORETS_SYNTHESIS_MAX_TOKENS")
    try:
        synthesis = extract_json(completion.text)
    except ClaudeError as exc:
        deps.repo.save_digest(week.label, status=ERROR, error=str(exc))
        raise DigestError(f"Сведение не разобрано: {exc}") from exc
    return synthesis, completion.usage


def _await_batch(
    settings: Settings,
    deps: DigestDeps,
    week: Week,
    plan: DigestPlan,
    batch_id: str,
    fingerprint: str,
    base_fields: dict[str, Any],
    notes: list[str],
) -> tuple[dict[str, dict[str, Any]], Usage, float, list[str]]:
    """Дождаться батча и разобрать ответы; не успел — ошибка с запомненным id."""
    repo = deps.repo
    assert deps.gateway is not None
    repo.save_digest(
        week.label,
        status=BATCH_SUBMITTED,
        batch_id=batch_id,
        chunks_fingerprint=fingerprint,
        **base_fields,
    )
    status = wait_for_batch(
        deps.gateway,
        batch_id,
        max_wait_seconds=settings.batch_wait_hours * 3600,
        poll_seconds=settings.batch_poll_seconds,
        sleep=deps.sleep,
        clock=deps.clock,
    )
    if status is None:
        repo.save_digest(
            week.label,
            status=BATCH_TIMEOUT,
            error=f"батч {batch_id} не завершился за {settings.batch_wait_hours} ч",
        )
        raise DigestError(
            f"Батч {batch_id} не завершился за {settings.batch_wait_hours} ч; "
            "повторный запуск `gorets digest --week …` заберёт его результаты"
        )
    items = deps.gateway.batch_results(batch_id)
    chunk_results, usage, cost, batch_notes = _collect_chunk_results(items, plan.chunks, settings)
    if not chunk_results:
        repo.save_digest(week.label, status=ERROR, error="ни один кусок не разобран")
        raise DigestError("Ни один кусок не разобран: " + "; ".join(batch_notes))
    repo.save_digest(week.label, status="synthesizing", chunk_results=chunk_results)
    return chunk_results, usage, cost, [*notes, *batch_notes]
