"""Клиент Telethon: только чтение, строка сессии из `.env`."""

from __future__ import annotations

from telethon import TelegramClient
from telethon.sessions import StringSession

from gorets.config import Settings


class TelegramNotConfigured(RuntimeError):
    pass


def make_client(
    settings: Settings, session: str | None = None, *, updates: bool = False
) -> TelegramClient:
    """Клиент с сессией из настроек (или переданной явно, как при входе).

    `receive_updates=False`: сборщик ничего не слушает в реальном времени,
    а лишние запросы к Telegram ни к чему. `flood_sleep_threshold` — сколько
    секунд FloodWait Telethon пережидает сам, не поднимая ошибку.
    """
    if not settings.tg_api_id or not settings.tg_api_hash:
        raise TelegramNotConfigured("Не заданы GORETS_TG_API_ID и GORETS_TG_API_HASH")
    return TelegramClient(
        StringSession(session),
        settings.tg_api_id,
        settings.tg_api_hash,
        receive_updates=updates,
        flood_sleep_threshold=settings.flood_wait_max,
        # Чтобы Telegram не считал клиент подозрительным, представляемся понятно.
        device_model="sayr-gorets",
        app_version="0.1",
    )


async def connect_authorized(settings: Settings, *, updates: bool = False) -> TelegramClient:
    """Подключиться по сохранённой сессии; без авторизации — ошибка с подсказкой."""
    if not settings.tg_session:
        raise TelegramNotConfigured("Нет GORETS_TG_SESSION: выполните `gorets login`")
    client = make_client(settings, settings.tg_session, updates=updates)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise TelegramNotConfigured(
            "Сессия Telegram недействительна: выполните `gorets login` заново"
        )
    return client


async def login_interactive(settings: Settings) -> tuple[str, str]:
    """Интерактивный вход (телефон, код, пароль 2FA) → (строка сессии, кто вошёл)."""
    client = make_client(settings, None)
    # Telethon сам спрашивает телефон, код и пароль через input()/getpass().
    await client.start()
    try:
        me = await client.get_me()
        who = "@" + me.username if getattr(me, "username", None) else str(me.id)
        session = client.session.save()
    finally:
        await client.disconnect()
    return session, who
