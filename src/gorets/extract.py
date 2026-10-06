"""Извлекатели: прогон сообщений фидов через Claude по заданной схеме.

Одно сообщение — один запрос в Message Batches API; результат хранится в
`extractions` бессрочно и отдаётся через API другим проектам. Батч, не
успевший за отведённое время, запоминается и забирается при следующем
запуске.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from gorets.config import Settings
from gorets.digest.claude import BatchItem, ClaudeError, ClaudeGateway, extract_json, wait_for_batch
from gorets.digest.pricing import cost_usd
from gorets.feeds import ExtractorConfig, ProjectConfig
from gorets.models import Message
from gorets.storage import Repository

log = logging.getLogger(__name__)

EXTRACT_SYSTEM = """\
Ты извлекаешь структурированные данные из одного сообщения Telegram-форума \
туристов Узбекистана. Ниже — инструкция для этого вида извлечения. Ответ — \
JSON по схеме: relevant = true и заполненный data, если сообщение по теме; \
relevant = false и data = null, если нет. Не выдумывай: чего нет в тексте, \
то null или пустой список. Язык полей — русский, как в сообщении.

Инструкция:
"""


class ExtractError(RuntimeError):
    pass


@dataclass
class ExtractReport:
    extractors: list[dict[str, Any]] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


def wrapped_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean"},
            "data": {"anyOf": [schema, {"type": "null"}]},
        },
        "required": ["relevant", "data"],
        "additionalProperties": False,
    }


def message_prompt(m: Message, tz: ZoneInfo) -> str:
    header = [f"Дата: {m.date.astimezone(tz).strftime('%Y-%m-%d %H:%M')}"]
    if m.topic_title:
        header.append(f"Ветка: {m.topic_title}")
    if m.media_type:
        header.append(f"Вложение: {m.media_type}" + (f" {m.file_name}" if m.file_name else ""))
    if m.forwarded_from:
        header.append(f"Переслано из: {m.forwarded_from}")
    return "\n".join(header) + "\n\nСообщение:\n" + m.text


def build_request(
    extractor: ExtractorConfig, m: Message, *, settings: Settings, tz: ZoneInfo
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": extractor.model or settings.extract_model,
        "max_tokens": extractor.max_tokens,
        "system": [
            {
                "type": "text",
                "text": EXTRACT_SYSTEM + extractor.prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": message_prompt(m, tz)}],
    }
    if settings.structured_output:
        params["output_config"] = {
            "format": {"type": "json_schema", "schema": wrapped_schema(extractor.schema)}
        }
    return {"custom_id": f"{m.chat_id}:{m.msg_id}", "params": params}


def item_meta(m: Message) -> dict[str, Any]:
    return {
        "chat_id": m.chat_id,
        "msg_id": m.msg_id,
        "date": m.date.isoformat(),
        "topic_id": m.topic_id,
        "topic_title": m.topic_title,
    }


def results_to_rows(
    extractor: ExtractorConfig,
    items: list[BatchItem],
    meta: dict[str, dict[str, Any]],
    *,
    model: str,
) -> tuple[list[dict[str, Any]], int, int]:
    rows: list[dict[str, Any]] = []
    tokens_in = tokens_out = 0
    for item in items:
        info = meta.get(item.custom_id)
        if info is None:
            continue
        base = {
            "extractor": extractor.name,
            "chat_id": int(info["chat_id"]),
            "msg_id": int(info["msg_id"]),
            "message_date": datetime.fromisoformat(info["date"]),
            "topic_id": info.get("topic_id"),
            "topic_title": info.get("topic_title"),
            "model": model,
            "input_tokens": item.usage.total_input if item.usage else 0,
            "output_tokens": item.usage.output_tokens if item.usage else 0,
        }
        if item.usage:
            tokens_in += item.usage.total_input
            tokens_out += item.usage.output_tokens
        if not item.ok or not item.text:
            rows.append({**base, "status": "error", "data": None, "error": item.error or "пусто"})
            continue
        try:
            answer = extract_json(item.text)
        except ClaudeError as exc:
            rows.append({**base, "status": "error", "data": None, "error": str(exc)})
            continue
        data = answer.get("data")
        if answer.get("relevant") and isinstance(data, dict):
            rows.append({**base, "status": "ok", "data": data, "error": None})
        else:
            rows.append({**base, "status": "skipped", "data": None, "error": None})
    return rows, tokens_in, tokens_out


def _collect_batch(
    repo: Repository,
    gateway: ClaudeGateway,
    extractor: ExtractorConfig,
    batch_id: str,
    meta: dict[str, dict[str, Any]],
    *,
    settings: Settings,
    sleep: Any,
    clock: Any,
) -> dict[str, Any] | None:
    """Дождаться батча и записать результаты; None — не успел, остаётся pending."""
    status = wait_for_batch(
        gateway,
        batch_id,
        max_wait_seconds=settings.batch_wait_hours * 3600,
        poll_seconds=settings.batch_poll_seconds,
        sleep=sleep,
        clock=clock,
    )
    if status is None:
        return None
    model = extractor.model or settings.extract_model
    rows, tokens_in, tokens_out = results_to_rows(
        extractor, gateway.batch_results(batch_id), meta, model=model
    )
    saved = repo.save_extractions(rows)
    repo.finish_extract_batch(batch_id)
    cost = cost_usd(model, input_tokens=tokens_in, output_tokens=tokens_out, batch=True)
    counts = {
        "ok": sum(1 for r in rows if r["status"] == "ok"),
        "skipped": sum(1 for r in rows if r["status"] == "skipped"),
        "error": sum(1 for r in rows if r["status"] == "error"),
    }
    log.info(
        "Извлекатель %s: батч %s, записано %s, %s, ≈$%.3f",
        extractor.name,
        batch_id,
        saved,
        counts,
        cost,
    )
    return {"saved": saved, "cost_usd": cost, **counts}


def run_extract(
    settings: Settings,
    repo: Repository,
    project: ProjectConfig,
    gateway: ClaudeGateway | None,
    *,
    names: list[str] | None = None,
    since: datetime | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    sleep: Any = None,
    clock: Any = None,
) -> ExtractReport:
    import time

    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    tz = ZoneInfo(settings.timezone)
    report = ExtractReport()
    selected = [project.extractor(n) for n in names] if names else list(project.extractors.values())
    if not selected:
        log.info("Извлекатели не описаны в %s — делать нечего", settings.config_file)
        return report
    if not dry_run and gateway is None:
        raise ExtractError("Нет клиента Claude: задайте ANTHROPIC_API_KEY")

    for extractor in selected:
        feed = project.feed(extractor.feed)
        chat_id = repo.resolve_chat_id(feed.chat)
        entry: dict[str, Any] = {"extractor": extractor.name, "feed": feed.name}
        if chat_id is None:
            entry["note"] = f"чат {feed.chat} ещё не собран"
            report.extractors.append(entry)
            continue

        # Сначала — батчи, не забранные в прошлый раз.
        if not dry_run:
            for pending in repo.pending_extract_batches(extractor.name):
                result = _collect_batch(
                    repo,
                    gateway,
                    extractor,
                    pending.batch_id,
                    pending.items,
                    settings=settings,
                    sleep=sleep,
                    clock=clock,
                )
                if result is None:
                    report.pending.append(pending.batch_id)
                else:
                    report.cost_usd += result["cost_usd"]
                    entry.setdefault("resumed", []).append({"batch_id": pending.batch_id, **result})

        candidates = repo.messages_without_extraction(
            extractor.name, chat_id, feed.topics, since=since, limit=limit or 500
        )
        entry["candidates"] = len(candidates)
        if dry_run or not candidates:
            if candidates:
                model = extractor.model or settings.extract_model
                est_in = sum(len(m.text) / settings.chars_per_token + 600 for m in candidates)
                entry["estimate_usd"] = round(
                    cost_usd(
                        model,
                        input_tokens=int(est_in),
                        output_tokens=300 * len(candidates),
                        batch=True,
                    ),
                    4,
                )
            report.extractors.append(entry)
            continue

        requests = [build_request(extractor, m, settings=settings, tz=tz) for m in candidates]
        meta = {r["custom_id"]: item_meta(m) for r, m in zip(requests, candidates, strict=True)}
        batch_id = gateway.submit_batch(requests)
        repo.add_extract_batch(extractor.name, batch_id, meta)
        log.info(
            "Извлекатель %s: отправлен батч %s из %s сообщений",
            extractor.name,
            batch_id,
            len(requests),
        )
        result = _collect_batch(
            repo, gateway, extractor, batch_id, meta, settings=settings, sleep=sleep, clock=clock
        )
        if result is None:
            report.pending.append(batch_id)
            entry["batch_id"] = batch_id
        else:
            report.cost_usd += result["cost_usd"]
            entry.update(result)
        report.extractors.append(entry)

    if report.pending:
        raise ExtractError(
            f"Батчи не завершились за {settings.batch_wait_hours} ч: {', '.join(report.pending)}; "
            "следующий запуск `gorets extract` заберёт их"
        )
    return report


def now_utc() -> datetime:
    return datetime.now(UTC)
