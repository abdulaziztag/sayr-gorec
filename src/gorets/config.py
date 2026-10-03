"""Настройки проекта.

Читаются pydantic-settings из окружения и файла `.env` с префиксом `GORETS_`
(исключение — `ANTHROPIC_API_KEY`, его ждёт сам SDK Anthropic). Лишние ключи
в `.env` игнорируются: у сервера Sayr неизвестный ключ однажды ронял загрузку
настроек, повторять это не хочется.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GORETS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- База -----------------------------------------------------------------
    database_url: str = "postgresql+psycopg://sayr_gorets:sayr_gorets@localhost:5432/sayr_gorets"

    # --- Telegram (MTProto, обычный аккаунт) ----------------------------------
    tg_api_id: int | None = None
    tg_api_hash: str | None = None
    # Строка сессии Telethon. Получается командой `gorets login`.
    tg_session: str | None = None
    # Чаты для сбора через запятую: username или числовой id.
    chats: str = "gorets_uzb"
    # Кому слать отчёт: @username или числовой id владельца.
    owner: str | None = None
    # Пауза между запросами к Telegram, секунды.
    request_pause: float = 1.5
    # Сколько секунд FloodWait ещё терпим (ждём), прежде чем сдаться с ошибкой.
    flood_wait_max: int = 3600
    # Сколько сообщений за один запуск `backfill` (порция; продолжение — с места остановки).
    backfill_batch: int = 5000
    # Сколько сообщений копим в памяти перед записью в базу.
    upsert_batch: int = 200

    # --- Хранение --------------------------------------------------------------
    # Секрет для HMAC от id автора. Без него сбор не запускается.
    author_hmac_secret: str | None = None
    # Сырые сообщения старше этого срока удаляются в конце ежедневного прогона.
    retention_days: int = 90
    # @упоминания и ссылки t.me на эти имена не заменяются заглушкой
    # (чаты из `chats` добавляются автоматически). Через запятую.
    mention_keep: str = ""

    # --- Разбор недели ----------------------------------------------------------
    chunk_model: str = "claude-haiku-4-5-20251001"
    synthesis_model: str = "claude-sonnet-5-5"
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    # Бюджет входных токенов одного куска (без системной части).
    chunk_token_budget: int = 12000
    # Грубая оценка «символов на токен» для русского текста, без обращения к API.
    chars_per_token: float = 2.5
    # Ожидаемый размер ответа модели на один кусок — для оценки цены в --dry-run.
    chunk_output_estimate: int = 1500
    # Максимум токенов ответа на кусок и на сведение.
    chunk_max_tokens: int = 8192
    synthesis_max_tokens: int = 16000
    # Сколько часов ждём завершения батча и как часто опрашиваем.
    batch_wait_hours: float = 6.0
    batch_poll_seconds: int = 120
    # Использовать структурированный вывод (JSON по схеме). Если выключить —
    # модель просят вернуть JSON текстом, разбор мягкий.
    structured_output: bool = True
    # Каталог мест Sayr (публичный API без ключа).
    catalog_url: str = "https://sayr.info/api/v1/places"
    catalog_timeout: float = 30.0
    place_url_template: str = "https://sayr.info/p/{slug}"
    # Куда класть отчёты (reports/ГГГГ-Wнн.md). В systemd — единственный путь на запись.
    reports_dir: Path = Path("reports")
    # Часовой пояс для границ суток и недель.
    timezone: str = "Asia/Tashkent"
    # Как доставлять отчёт: auto — частями, если их не больше max_parts, иначе файлом.
    delivery: str = "auto"  # auto | parts | file
    delivery_max_parts: int = 4

    log_level: str = "INFO"

    @property
    def chat_list(self) -> list[str]:
        return [c.strip() for c in self.chats.split(",") if c.strip()]

    @property
    def mention_keep_set(self) -> set[str]:
        """Имена, которые не считаем людьми: сами собираемые чаты и явный список."""
        keep = {c.lstrip("@").lower() for c in self.chat_list if not c.lstrip("-").isdigit()}
        keep |= {c.strip().lstrip("@").lower() for c in self.mention_keep.split(",") if c.strip()}
        return keep


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Загрузить настройки; `env_file` позволяет указать другой файл вместо `.env`."""
    if env_file is not None:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    return Settings()
