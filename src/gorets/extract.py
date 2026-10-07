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

from gorets.catalog import CatalogError, PlaceLinker, fetch_catalog
from gorets.config import Settings
from gorets.digest.claude import BatchItem, ClaudeError, ClaudeGateway, extract_json, wait_for_batch
from gorets.digest.pricing import cost_usd
from gorets.feeds import ExtractorConfig, ProjectConfig
from gorets.models import Message
from gorets.storage import Repository

log = logging.getLogger(__name__)

EXTRACT_SYSTEM = """\
Ты извлекаешь структурированные данные из сообщений Telegram-форума туристов \
Узбекистана. На входе несколько сообщений, каждое начинается строкой \
«### <msg_id>». Ответ — JSON по схеме: в items ровно по одному элементу на \
каждое сообщение, с его msg_id; relevant = true и заполненный data, если \
сообщение по теме инструкции; relevant = false и data = null, если нет. \
Сообщения независимы: не переноси факты из одного в другое. Не выдумывай: \
чего нет в тексте, то null или пустой список. Язык полей — русский, как в \
сообщении.

Инструкция:
"""

# Сколько сообщений кладём в один запрос: инструкция оплачивается один раз
# на группу, а не на каждое сообщение, — это основная статья расходов.
GROUP_SIZE = 20
GROUP_CHARS = 12000


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
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "msg_id": {"type": "integer"},
                        "relevant": {"type": "boolean"},
                        "data": {"anyOf": [schema, {"type": "null"}]},
                    },
                    "required": ["msg_id", "relevant", "data"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def message_prompt(m: Message, tz: ZoneInfo) -> str:
    header = [f"### {m.msg_id}", f"Дата: {m.date.astimezone(tz).strftime('%Y-%m-%d %H:%M')}"]
    if m.topic_title:
        header.append(f"Ветка: {m.topic_title}")
    if m.media_type:
        header.append(f"Вложение: {m.media_type}" + (f" {m.file_name}" if m.file_name else ""))
    if m.forwarded_from:
        header.append(f"Переслано из: {m.forwarded_from}")
    return "\n".join(header) + "\n" + m.text.strip()


def group_messages(messages: list[Message]) -> list[list[Message]]:
    """Нарезать сообщения на группы по числу и по объёму текста."""
    groups: list[list[Message]] = []
    current: list[Message] = []
    chars = 0
    for m in messages:
        size = len(m.text) + 120
        if current and (len(current) >= GROUP_SIZE or chars + size > GROUP_CHARS):
            groups.append(current)
            current, chars = [], 0
        current.append(m)
        chars += size
    if current:
        groups.append(current)
    return groups


def build_request(
    extractor: ExtractorConfig,
    group: list[Message],
    *,
    settings: Settings,
    tz: ZoneInfo,
    index: int,
) -> dict[str, Any]:
    body = "\n\n".join(message_prompt(m, tz) for m in group)
    params: dict[str, Any] = {
        "model": extractor.model or settings.extract_model,
        "max_tokens": extractor.max_tokens * max(1, len(group) // 4),
        "system": [{"type": "text", "text": EXTRACT_SYSTEM + extractor.prompt}],
        "messages": [
            {
                "role": "user",
                "content": f"Сообщений: {len(group)}.\n\n{body}\n\nВерни items по каждому.",
            }
        ],
    }
    if settings.structured_output:
        params["output_config"] = {
            "format": {"type": "json_schema", "schema": wrapped_schema(extractor.schema)}
        }
    # Разрешены только буквы, цифры, «_» и «-»; двоеточие API не принимает.
    return {"custom_id": f"g{index}_{group[0].chat_id}_{group[0].msg_id}", "params": params}


def item_meta(m: Message) -> dict[str, Any]:
    return {
        "chat_id": m.chat_id,
        "msg_id": m.msg_id,
        "date": m.date.isoformat(),
        "topic_id": m.topic_id,
        "topic_title": m.topic_title,
    }


def _meta_list(info: Any) -> list[dict[str, Any]]:
    # Старые батчи хранили одно сообщение на запрос, новые — список.
    if isinstance(info, dict) and "messages" in info:
        return list(info["messages"])
    return [info] if isinstance(info, dict) else list(info)


def results_to_rows(
    extractor: ExtractorConfig,
    items: list[BatchItem],
    meta: dict[str, Any],
    *,
    model: str,
) -> tuple[list[dict[str, Any]], int, int]:
    rows: list[dict[str, Any]] = []
    tokens_in = tokens_out = 0
    for item in items:
        if item.custom_id not in meta:
            continue
        messages = _meta_list(meta[item.custom_id])
        if not messages:
            continue
        usage_in = item.usage.total_input if item.usage else 0
        usage_out = item.usage.output_tokens if item.usage else 0
        tokens_in += usage_in
        tokens_out += usage_out
        share_in, share_out = usage_in // len(messages), usage_out // len(messages)

        answers: dict[int, dict[str, Any]] = {}
        failure: str | None = None
        if not item.ok or not item.text:
            failure = item.error or "пустой ответ"
        else:
            try:
                parsed = extract_json(item.text)
                raw_items = parsed.get("items")
                if raw_items is None and "relevant" in parsed:
                    # Ответ старого формата на одно сообщение.
                    raw_items = [{**parsed, "msg_id": int(messages[0]["msg_id"])}]
                for entry in raw_items or []:
                    if isinstance(entry, dict) and isinstance(entry.get("msg_id"), int):
                        answers[entry["msg_id"]] = entry
            except ClaudeError as exc:
                failure = str(exc)

        for info in messages:
            base = {
                "extractor": extractor.name,
                "chat_id": int(info["chat_id"]),
                "msg_id": int(info["msg_id"]),
                "message_date": datetime.fromisoformat(info["date"]),
                "topic_id": info.get("topic_id"),
                "topic_title": info.get("topic_title"),
                "model": model,
                "input_tokens": share_in,
                "output_tokens": share_out,
            }
            if failure:
                rows.append({**base, "status": "error", "data": None, "error": failure})
                continue
            answer = answers.get(int(info["msg_id"]))
            if answer is None:
                rows.append(
                    {
                        **base,
                        "status": "error",
                        "data": None,
                        "error": "модель пропустила сообщение",
                    }
                )
                continue
            data = answer.get("data")
            if answer.get("relevant") and isinstance(data, dict):
                rows.append({**base, "status": "ok", "data": data, "error": None})
            else:
                rows.append({**base, "status": "skipped", "data": None, "error": None})
    return rows, tokens_in, tokens_out


def link_places_in(data: dict[str, Any], paths: tuple[str, ...], linker: PlaceLinker) -> None:
    """Проставить slug каталога по путям вида "place" и "places[].name"."""
    for path in paths:
        if "[]." in path:
            list_key, item_key = path.split("[].", 1)
            items = data.get(list_key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        item["slug"] = linker.link(item.get(item_key))
        else:
            value = data.get(path)
            data[f"{path}_slug"] = linker.link(value) if isinstance(value, str) else None


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
    linker: PlaceLinker | None = None,
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
    if linker is not None and extractor.link_places:
        for row in rows:
            if isinstance(row.get("data"), dict):
                link_places_in(row["data"], extractor.link_places, linker)
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
    catalog_fetcher: Any = None,
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

    linker: PlaceLinker | None = None
    if not dry_run and any(e.link_places for e in selected):
        fetcher = catalog_fetcher or (
            lambda: fetch_catalog(settings.catalog_url, timeout=settings.catalog_timeout)
        )
        try:
            linker = PlaceLinker(fetcher())
        except CatalogError as exc:
            # Без каталога извлечение всё равно полезно: slug останется пустым.
            log.warning("Каталог Sayr недоступен, места не привязаны: %s", exc)

    for extractor in selected:
        entry: dict[str, Any] = {"extractor": extractor.name, "feeds": list(extractor.feeds)}
        entry.update(
            {"candidates": 0, "saved": 0, "ok": 0, "skipped": 0, "error": 0, "cost_usd": 0.0}
        )
        notes: list[str] = []

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
                    linker=linker,
                )
                if result is None:
                    report.pending.append(pending.batch_id)
                else:
                    report.cost_usd += result["cost_usd"]
                    entry.setdefault("resumed", []).append({"batch_id": pending.batch_id, **result})

        candidates: list[Message] = []
        for feed_name in extractor.feeds:
            feed = project.feed(feed_name)
            chat_id = repo.resolve_chat_id(feed.chat)
            if chat_id is None:
                notes.append(f"чат {feed.chat} ещё не собран")
                continue
            candidates.extend(
                repo.messages_without_extraction(
                    extractor.name, chat_id, feed.topics, since=since, limit=limit or 500
                )
            )
        if limit:
            candidates = candidates[:limit]
        entry["candidates"] = len(candidates)
        if notes:
            entry["note"] = "; ".join(notes)
        groups = group_messages(candidates)
        entry["requests"] = len(groups)
        if dry_run or not candidates:
            if candidates:
                model = extractor.model or settings.extract_model
                system_tokens = len(EXTRACT_SYSTEM + extractor.prompt) / settings.chars_per_token
                est_in = sum(
                    (len(m.text) + 120) / settings.chars_per_token for m in candidates
                ) + system_tokens * len(groups)
                entry["estimate_usd"] = round(
                    cost_usd(
                        model,
                        input_tokens=int(est_in),
                        output_tokens=120 * len(candidates),
                        batch=True,
                    ),
                    4,
                )
            report.extractors.append(entry)
            continue

        requests = [
            build_request(extractor, group, settings=settings, tz=tz, index=i)
            for i, group in enumerate(groups)
        ]
        meta = {
            r["custom_id"]: {"messages": [item_meta(m) for m in group]}
            for r, group in zip(requests, groups, strict=True)
        }
        batch_id = gateway.submit_batch(requests)
        repo.add_extract_batch(extractor.name, batch_id, meta)
        log.info(
            "Извлекатель %s: отправлен батч %s — %s сообщений в %s запросах",
            extractor.name,
            batch_id,
            len(candidates),
            len(requests),
        )
        result = _collect_batch(
            repo,
            gateway,
            extractor,
            batch_id,
            meta,
            settings=settings,
            sleep=sleep,
            clock=clock,
            linker=linker,
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
