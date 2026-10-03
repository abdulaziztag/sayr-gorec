"""Обёртка над API Claude: батчи для кусков, обычный вызов для сведения.

Через протокол `ClaudeGateway` код разбора не зависит от SDK: в тестах
подставляется фейк, который возвращает заготовленные ответы.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


class ClaudeError(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class Completion:
    text: str
    usage: Usage
    stop_reason: str | None
    model: str


@dataclass
class BatchItem:
    custom_id: str
    ok: bool
    text: str | None = None
    usage: Usage | None = None
    error: str | None = None


@dataclass
class BatchStatus:
    id: str
    status: str  # in_progress | canceling | ended
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def ended(self) -> bool:
        return self.status == "ended"


class ClaudeGateway(Protocol):
    def submit_batch(self, requests: list[dict[str, Any]]) -> str: ...

    def batch_status(self, batch_id: str) -> BatchStatus: ...

    def batch_results(self, batch_id: str) -> list[BatchItem]: ...

    def complete(self, params: dict[str, Any]) -> Completion: ...


def _usage_from(usage: Any) -> Usage:
    return Usage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )


def _text_of(message: Any) -> str:
    return "".join(
        block.text
        for block in getattr(message, "content", [])
        if getattr(block, "type", "") == "text"
    )


class AnthropicGateway:
    """Настоящий клиент. Создаётся только когда есть ключ."""

    def __init__(self, api_key: str | None) -> None:
        import anthropic

        if not api_key:
            raise ClaudeError("Не задан ANTHROPIC_API_KEY")
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=4)

    def submit_batch(self, requests: list[dict[str, Any]]) -> str:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        batch = self.client.messages.batches.create(
            requests=[
                Request(
                    custom_id=r["custom_id"], params=MessageCreateParamsNonStreaming(**r["params"])
                )
                for r in requests
            ]
        )
        return batch.id

    def batch_status(self, batch_id: str) -> BatchStatus:
        batch = self.client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        return BatchStatus(
            id=batch.id,
            status=batch.processing_status,
            counts={
                "processing": counts.processing,
                "succeeded": counts.succeeded,
                "errored": counts.errored,
                "canceled": counts.canceled,
                "expired": counts.expired,
            },
        )

    def batch_results(self, batch_id: str) -> list[BatchItem]:
        items: list[BatchItem] = []
        for entry in self.client.messages.batches.results(batch_id):
            result = entry.result
            if result.type == "succeeded":
                message = result.message
                stop = getattr(message, "stop_reason", None)
                if stop == "refusal":
                    items.append(BatchItem(entry.custom_id, False, error="модель отказалась"))
                    continue
                items.append(
                    BatchItem(
                        entry.custom_id,
                        True,
                        text=_text_of(message),
                        usage=_usage_from(message.usage),
                        error="ответ обрезан по max_tokens" if stop == "max_tokens" else None,
                    )
                )
            else:
                error = getattr(result, "error", None)
                detail = getattr(error, "message", None) or getattr(error, "type", None) or ""
                items.append(BatchItem(entry.custom_id, False, error=f"{result.type}: {detail}"))
        return items

    def complete(self, params: dict[str, Any]) -> Completion:
        a = self._anthropic
        try:
            message = self.client.messages.create(**params)
        except a.RateLimitError as exc:
            raise ClaudeError(f"Лимит запросов Claude: {exc.message}") from exc
        except a.APIStatusError as exc:
            raise ClaudeError(f"Ошибка API Claude ({exc.status_code}): {exc.message}") from exc
        except a.APIConnectionError as exc:
            raise ClaudeError(f"Нет связи с API Claude: {exc}") from exc
        if message.stop_reason == "refusal":
            raise ClaudeError("Модель отказалась выполнять запрос (stop_reason=refusal)")
        return Completion(
            text=_text_of(message),
            usage=_usage_from(message.usage),
            stop_reason=message.stop_reason,
            model=message.model,
        )


def wait_for_batch(
    gateway: ClaudeGateway,
    batch_id: str,
    *,
    max_wait_seconds: float,
    poll_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> BatchStatus | None:
    """Опрашивать батч до завершения; None, если за отведённое время не успел.

    Соединение не висит: между опросами процесс спит.
    """
    deadline = clock() + max_wait_seconds
    while True:
        status = gateway.batch_status(batch_id)
        if status.ended:
            return status
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        log.info("Батч %s: %s, ждём ещё до %.0f мин", batch_id, status.counts, remaining / 60)
        sleep(min(poll_seconds, remaining))


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> dict[str, Any]:
    """Разобрать JSON из ответа модели, терпя обрамление ```json и лишний текст вокруг."""
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ClaudeError("В ответе модели нет JSON") from None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ClaudeError(f"Ответ модели — не JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ClaudeError("Ответ модели — не объект JSON")
    return data
