"""Вебхуки: новое сообщение фида уходит потребителю сразу, с подписью."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Gorets-Signature"


def sign(secret: str | None, body: bytes) -> str:
    digest = hmac.new((secret or "").encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, header_value: str | None) -> bool:
    """Проверка на стороне потребителя: сравнить подпись из заголовка с телом запроса."""
    if not header_value:
        return False
    return hmac.compare_digest(sign(secret, body), header_value)


def post_webhook(
    url: str,
    payload: dict[str, Any],
    *,
    secret: str | None,
    event: str,
    feed: str,
    attempts: int = 3,
    timeout: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """POST JSON с подписью; три попытки с паузой. Неудача — предупреждение, не ошибка."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "sayr-gorets/0.1",
        "X-Gorets-Event": event,
        "X-Gorets-Feed": feed,
        SIGNATURE_HEADER: sign(secret, body),
    }
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if 200 <= response.status < 300:
                    return True
                log.warning("Вебхук %s ответил %s", url, response.status)
        except urllib.error.HTTPError as exc:
            log.warning("Вебхук %s ответил %s (попытка %s)", url, exc.code, attempt)
            if 400 <= exc.code < 500 and exc.code != 429:
                return False  # наша ошибка, повтор не поможет
        except (urllib.error.URLError, OSError) as exc:
            log.warning("Вебхук %s недоступен (попытка %s): %s", url, attempt, exc)
        if attempt < attempts:
            sleep(2 * attempt)
    return False
