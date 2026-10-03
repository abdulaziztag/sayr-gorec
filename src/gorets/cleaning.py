"""Очистка текста перед записью в базу.

Хранить личные данные участников форума незачем: для разбора недели важны
места, тропы и вопросы, а не телефоны и ники. Поэтому ещё при записи номера
телефонов, e-mail, @упоминания людей и ссылки t.me на людей заменяются
заглушками. Удалить по просьбе потом можно только то, что хранится, а что
не сохранили — и просить не придётся.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

PHONE = "[телефон]"
EMAIL = "[почта]"
USER = "[пользователь]"

# Коды операторов Узбекистана: мобильные и городские (первые две цифры 9-значного номера).
UZ_MOBILE = {"20", "33", "50", "55", "77", "88", "90", "91", "93", "94", "95", "97", "98", "99"}
UZ_LANDLINE = {
    "61",
    "62",
    "65",
    "66",
    "67",
    "69",
    "70",
    "71",
    "72",
    "73",
    "74",
    "75",
    "76",
    "78",
    "79",
}

# Пути t.me, которые ведут не к человеку: приглашения, стикеры, прокси и т.п.
_TG_SERVICE_PATHS = {
    "joinchat",
    "c",
    "s",
    "addstickers",
    "addemoji",
    "share",
    "proxy",
    "socks",
    "login",
    "setlanguage",
    "bg",
    "addtheme",
    "iv",
    "confirm",
    "boost",
    "giftcode",
    "m",
    "nft",
    "addlist",
    "contact",
    "invoice",
    "list",
    "folder",
    "web",
}

_EMAIL_RE = re.compile(r"[\w.+-]+@(?:[\w-]+\.)+[A-Za-z]{2,}")

_TG_LINK_RE = re.compile(
    r"(?<![\w/@])(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?P<path>[^\s<>()\"'«»,;!]+)",
    re.IGNORECASE,
)
_TG_URI_RE = re.compile(
    r"tg://(?:user\?id=\d+|resolve\?domain=(?P<domain>[A-Za-z0-9_]+))[^\s<>()\"'«»,;!]*",
    re.IGNORECASE,
)
_MENTION_RE = re.compile(r"(?<![\w@])@(?P<name>[A-Za-z][A-Za-z0-9_]{3,31})(?!\w)")

# Кандидат на номер: цепочка цифр с пробелами, дефисами, точками и скобками.
# Начинается не внутри слова и заканчивается цифрой, после которой не буква.
_PHONE_CANDIDATE_RE = re.compile(r"(?<![\w+])\+?\(?\d(?:[\d\s\-.()]*\d)?(?!\w)")

_SEP = r"[\s\-.]*"
# +998 90 123 45 67 в любых разбивках (с плюсом и без, со скобками).
_UZ_INTL_RE = re.compile(
    rf"^\+?\(?998\)?{_SEP}\(?\d{{2}}\)?{_SEP}\d{{3}}{_SEP}\d{{2}}{_SEP}\d{{2}}"
)
# +7 999 123-45-67 и 8 (999) 123-45-67: Россия и Казахстан, 11 цифр.
_RU_RE = re.compile(rf"^\+?[78]{_SEP}\(?\d{{3}}\)?{_SEP}\d{{3}}{_SEP}\d{{2}}{_SEP}\d{{2}}")
# Местный 9-значный: 90 123 45 67, (90) 123-45-67, 901234567, 90 1234567.
_UZ_LOCAL_RE = re.compile(rf"^\(?(?P<prefix>\d{{2}})\)?{_SEP}\d{{3}}{_SEP}\d{{2}}{_SEP}\d{{2}}")
# Прочие международные: плюс, код страны и ещё 7–12 цифр.
_INTL_RE = re.compile(r"^\+\d{1,3}(?:[\s\-.()]*\d){7,12}")

_SEPARATORS = set(" -.()\t")


def _phone_end(candidate: str) -> int | None:
    """Длина номера в начале кандидата или None, если это не номер.

    Кандидат может захватить соседние числа («… 45 67 2 человека»), поэтому
    определяется не «похож ли весь кусок на номер», а «где номер заканчивается».
    """
    m = _UZ_INTL_RE.match(candidate)
    if m:
        return m.end()
    m = _RU_RE.match(candidate)
    if m:
        return m.end()
    m = _UZ_LOCAL_RE.match(candidate)
    if m:
        matched = m.group(0)
        prefix = m.group("prefix")
        has_sep = any(ch in _SEPARATORS for ch in matched)
        followed_by_digit = candidate[m.end() : m.end() + 1].isdigit()
        if not followed_by_digit:
            if has_sep and prefix in (UZ_MOBILE | UZ_LANDLINE):
                return m.end()
            if not has_sep and prefix in UZ_MOBILE:
                return m.end()
    m = _INTL_RE.match(candidate)
    if m:
        return m.end()
    return None


def mask_phones(text: str) -> str:
    """Заменить номера телефонов заглушкой, не трогая даты, цены, координаты и время."""
    out: list[str] = []
    pos = 0
    while True:
        m = _PHONE_CANDIDATE_RE.search(text, pos)
        if not m:
            break
        candidate = m.group(0)
        end = _phone_end(candidate)
        if end is None:
            # Не номер: оставить как есть и продолжить после кандидата.
            out.append(text[pos : m.end()])
            pos = m.end()
            continue
        out.append(text[pos : m.start()])
        out.append(PHONE)
        pos = m.start() + end
    out.append(text[pos:])
    return "".join(out)


def mask_emails(text: str) -> str:
    return _EMAIL_RE.sub(EMAIL, text)


def _is_person_name(name: str, keep: set[str]) -> bool:
    lowered = name.lower()
    # Боты — не люди, собираемые чаты и явный список тоже оставляем.
    return lowered not in keep and not lowered.endswith("bot")


def mask_tg_links(text: str, keep: set[str]) -> str:
    """Заменить ссылки t.me на людей; ссылки на сообщения, приглашения и сервисные пути оставить."""

    def repl(m: re.Match[str]) -> str:
        raw_path = m.group("path")
        path = raw_path.rstrip(".,:;!?)»")
        trailing = raw_path[len(path) :]
        core = path.split("?", 1)[0]
        segments = core.split("/")
        first = segments[0].lstrip("@")
        if first.startswith("+"):
            # t.me/+998901234567 — ссылка на номер; t.me/+AbCd — приглашение в чат.
            return (PHONE + trailing) if first[1:].isdigit() else m.group(0)
        if first.lower() in _TG_SERVICE_PATHS:
            return m.group(0)
        if len(segments) > 1 and segments[1]:
            # t.me/chat/12345 — ссылка на сообщение, она полезна и не личная.
            return m.group(0)
        if not first or not _is_person_name(first, keep):
            return m.group(0)
        return USER + trailing

    text = _TG_LINK_RE.sub(repl, text)

    def repl_uri(m: re.Match[str]) -> str:
        domain = m.group("domain")
        if domain and not _is_person_name(domain, keep):
            return m.group(0)
        return USER

    return _TG_URI_RE.sub(repl_uri, text)


def mask_mentions(text: str, keep: set[str]) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m.group("name")
        return USER if _is_person_name(name, keep) else m.group(0)

    return _MENTION_RE.sub(repl, text)


def clean_text(text: str | None, keep: Iterable[str] = ()) -> str:
    """Полная очистка: почта, ссылки на людей, @упоминания, телефоны."""
    if not text:
        return ""
    keep_set = {k.lstrip("@").lower() for k in keep}
    text = mask_emails(text)
    text = mask_tg_links(text, keep_set)
    text = mask_mentions(text, keep_set)
    text = mask_phones(text)
    return text.strip()


class TextCleaner:
    """Очистка с запомненным списком имён, которые не считаем людьми."""

    def __init__(self, keep: Iterable[str] = ()) -> None:
        self.keep = {k.lstrip("@").lower() for k in keep}

    def __call__(self, text: str | None) -> str:
        return clean_text(text, self.keep)
