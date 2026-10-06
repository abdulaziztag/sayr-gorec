"""Каталог мест Sayr: публичный API без ключа.

Нужен, чтобы модель узнавала места по slug и чтобы отличать известные места
от кандидатов в черновики. Берём русские и узбекские названия.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

Fetcher = Callable[[str], Any]


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class Place:
    slug: str
    name: str
    name_uz: str | None = None
    category: str | None = None
    region: str | None = None
    lat: float | None = None
    lng: float | None = None
    elevation_m: float | None = None

    @property
    def url(self) -> str:
        return f"https://sayr.info/p/{self.slug}"


def fetch_json(url: str, timeout: float = 30.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "sayr-gorets/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in ("items", "places", "results", "data"):
            if isinstance(payload.get(key), list):
                return [p for p in payload[key] if isinstance(p, dict)]
    raise CatalogError("Непонятный ответ каталога: ожидался список мест")


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_places(payload: Any, payload_uz: Any = None) -> list[Place]:
    uz_names: dict[str, str] = {}
    if payload_uz is not None:
        for item in _items(payload_uz):
            if item.get("slug") and item.get("name"):
                uz_names[str(item["slug"])] = str(item["name"])
    places: list[Place] = []
    for item in _items(payload):
        slug = item.get("slug")
        if not slug:
            continue
        places.append(
            Place(
                slug=str(slug),
                name=str(item.get("name") or slug),
                name_uz=uz_names.get(str(slug)),
                category=item.get("category"),
                region=item.get("region_name") or item.get("region"),
                lat=_float(item.get("lat")),
                lng=_float(item.get("lng")),
                elevation_m=_float(item.get("elevation_m")),
            )
        )
    return places


def _with_query(url: str, **params: Any) -> str:
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query.update({k: str(v) for k, v in params.items()})
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def fetch_catalog(
    base_url: str,
    *,
    timeout: float = 30.0,
    fetcher: Fetcher | None = None,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Place]:
    """Каталог целиком (русские названия) плюс узбекские названия, если они доступны."""
    fetch = fetcher or (lambda url: fetch_json(url, timeout))
    url_ru = _with_query(base_url, limit=1000)
    url_uz = _with_query(base_url, limit=1000, lang="uz")
    last_error: Exception | None = None
    payload = None
    for attempt in range(1, attempts + 1):
        try:
            payload = fetch(url_ru)
            break
        except (urllib.error.URLError, OSError, ValueError, CatalogError) as exc:
            last_error = exc
            log.warning("Каталог Sayr не ответил (попытка %s из %s): %s", attempt, attempts, exc)
            if attempt < attempts:
                sleep(5 * attempt)
    if payload is None:
        raise CatalogError(f"Не удалось получить каталог Sayr: {last_error}")
    payload_uz = None
    try:
        payload_uz = fetch(url_uz)
    except (urllib.error.URLError, OSError, ValueError, CatalogError) as exc:
        log.warning("Узбекские названия каталога недоступны, обходимся русскими: %s", exc)
    places = parse_places(payload, payload_uz)
    if not places:
        raise CatalogError("Каталог Sayr пуст — разбор без него был бы бесполезен")
    return places


def catalog_text(places: list[Place]) -> str:
    """Компактный список для модели: slug | название | по-узбекски | категория | регион."""
    lines = ["slug | название | название (uz) | категория | регион"]
    for p in sorted(places, key=lambda x: x.slug):
        lines.append(
            " | ".join(
                [p.slug, p.name, p.name_uz or "-", str(p.category or "-"), str(p.region or "-")]
            )
        )
    return "\n".join(lines)


def catalog_index(places: list[Place]) -> dict[str, Place]:
    return {p.slug: p for p in places}


# Слова-обёртки, которые люди добавляют к названию, а каталог — нет.
_GENERIC_WORDS = {
    "гора",
    "горы",
    "пик",
    "вершина",
    "перевал",
    "пер",
    "озеро",
    "оз",
    "ущелье",
    "сай",
    "река",
    "речка",
    "водопад",
    "кишлак",
    "село",
    "посёлок",
    "поселок",
    "база",
    "лагерь",
    "плато",
    "ледник",
    "каньон",
    "долина",
    "урочище",
    "тропа",
    "маршрут",
    "поход",
    "на",
    "в",
    "к",
    "до",
    "от",
    "через",
    "и",
    "big",
    "mount",
    "peak",
    "lake",
    "tog",
    "togi",
    "ko'l",
    "koli",
    "dovon",
    "soy",
    "sharshara",
    "qishloq",
    "cho'qqi",
    "choqqi",
    "большой",
    "малый",
    "большая",
    "малая",
    "верхний",
    "нижний",
    "верхняя",
    "нижняя",
}


def _core(name: str) -> str:
    from gorets.digest.aggregate import normalize_name

    words = [w for w in normalize_name(name).split() if w not in _GENERIC_WORDS]
    return " ".join(words)


class PlaceLinker:
    """Название места в тексте → slug каталога, без обращения к модели.

    Сначала точное совпадение нормализованного названия (русского или
    узбекского), затем совпадение «ядра» без слов-обёрток («гора», «озеро»),
    затем вхождение достаточно длинного ядра одного в другое.
    """

    def __init__(self, places: list[Place]) -> None:
        from gorets.digest.aggregate import normalize_name

        self.exact: dict[str, str] = {}
        self.core: dict[str, str] = {}
        for place in places:
            for name in (place.name, place.name_uz):
                if not name:
                    continue
                self.exact.setdefault(normalize_name(name), place.slug)
                core = _core(name)
                if len(core) >= 3:
                    self.core.setdefault(core, place.slug)

    def link(self, name: str | None) -> str | None:
        if not name:
            return None
        from gorets.digest.aggregate import normalize_name

        normalized = normalize_name(name)
        if normalized in self.exact:
            return self.exact[normalized]
        core = _core(name)
        if core in self.core:
            return self.core[core]
        if len(core) >= 5:
            for known, slug in self.core.items():
                if len(known) >= 5 and (known in core or core in known):
                    return slug
        return None
