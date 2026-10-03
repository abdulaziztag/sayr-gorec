"""Цены моделей для оценки и учёта стоимости разбора.

Цены — за миллион токенов, доллары, по прайсу Anthropic. Батч вдвое дешевле,
чтение кэша — десятая часть входной цены, запись в кэш — на четверть дороже.
Таблицу легко поправить, когда прайс изменится; неизвестная модель считается
по цене Sonnet с предупреждением в логе.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# (вход, выход) за 1M токенов
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
}
FALLBACK_PRICE = PRICES["claude-sonnet-5-5"]
BATCH_DISCOUNT = 0.5
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25


def price_for(model: str) -> tuple[float, float]:
    """Цена по самому длинному совпадающему префиксу id модели."""
    best = ""
    for prefix in PRICES:
        if model.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    if best:
        return PRICES[best]
    log.warning("Неизвестная модель %s: цена считается как у Sonnet", model)
    return FALLBACK_PRICE


def cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    batch: bool = False,
) -> float:
    price_in, price_out = price_for(model)
    total = (
        input_tokens * price_in
        + cache_read_tokens * price_in * CACHE_READ_FACTOR
        + cache_write_tokens * price_in * CACHE_WRITE_FACTOR
        + output_tokens * price_out
    ) / 1_000_000
    if batch:
        total *= BATCH_DISCOUNT
    return round(total, 6)
