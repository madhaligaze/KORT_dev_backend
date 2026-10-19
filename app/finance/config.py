"""Настройки учёта KORT - отдельный BaseSettings (префикс FINANCE_ - от раздела «Финансы»).

Переменные окружения (префикс FINANCE_):
    FINANCE_ENABLED           - "false" выключает раздел целиком (по умолчанию true)
    FINANCE_DEVICE_APPROVAL   - "true" включает подтверждение входа с новых
                                устройств для слабых паролей (по умолчанию
                                false: в разработке барьер мешает)
    FINANCE_BASE_CURRENCY     - валюта компании, в которой сходятся отчёты (KZT)
    FINANCE_IMPORT_MAX_ROWS   - потолок строк в одном импорте (20000)
    FINANCE_IMPORT_MAX_MB     - потолок размера файла (15)
    FINANCE_GRID_MAX_ROWS     - сколько операций отдаётся в табличный вид (10000)
    FINANCE_SERVICE_ACCOUNT_JSON - креды Google для переноса книг в учёт: путь к
                                JSON сервисного аккаунта или сам JSON. Пусто ⇒
                                service-account.json в корне репозитория
    FINANCE_SUPPORT_BOT_TOKEN - токен бота Telegram поддержки (@BotFather)
    FINANCE_SUPPORT_CHAT_ID   - закрытый канал поддержки, куда бот кладёт
                                обращения (`-100…`). Без обоих обращения
                                копятся в базе и уйдут, когда бот появится
    FINANCE_SECRETS_KEY       - ключ, которым шифруются пароли подключений
                                (REST-сервис 1С). Пусто - базу 1С не подключить
    FINANCE_ONEC_SYNC_MINUTES - как часто перечитывать базы 1С (30); 0 -
                                только кнопкой «Обновить»

Про валюту компании
───────────────────
Она одна и живёт в настройках, а не в коде: `amount_base` считается в ней, и
смена валюты задним числом означает пересчёт каждой строки. Поэтому значение
берётся из окружения один раз и попадает в `workspaces.base_currency` при
создании пространства - дальше правда о валюте лежит в базе, а не здесь.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/finance/config.py -> корень репозитория
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Файл по умолчанию лежит в корне репозитория и в git не попадает (.gitignore).
_DEFAULT_CREDENTIALS_PATH = str(_REPO_ROOT / "service-account.json")


class FinanceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_prefix="FINANCE_",
    )

    enabled: bool = True
    base_currency: str = "KZT"
    #: Подтверждение входа с нового устройства для слабых, но уже заданных
    #: паролей. Выключено: проект в разработке, входят с разных устройств, и
    #: барьер подтверждения мешает. Включить к запуску - вход слабым паролем с
    #: незнакомого устройства тогда ждёт «Разрешить» с того, где уже вошли
    #: (`auth.py`, `login_devices`). Новых слабых паролей политика не пускает
    #: независимо от этого флага.
    device_approval: bool = False
    import_max_rows: int = Field(default=20_000, ge=1)
    import_max_mb: int = Field(default=15, ge=1)
    grid_max_rows: int = Field(default=10_000, ge=1)
    service_account_json: str = Field(
        default=_DEFAULT_CREDENTIALS_PATH,
        validation_alias=AliasChoices("FINANCE_SERVICE_ACCOUNT_JSON"),
    )
    support_bot_token: str = Field(default="", validation_alias=AliasChoices("FINANCE_SUPPORT_BOT_TOKEN"))
    support_chat_id: str = Field(default="", validation_alias=AliasChoices("FINANCE_SUPPORT_CHAT_ID"))
    secrets_key: str = Field(default="", validation_alias=AliasChoices("FINANCE_SECRETS_KEY"))
    onec_sync_minutes: int = Field(default=30, ge=0, validation_alias=AliasChoices("FINANCE_ONEC_SYNC_MINUTES"))

    @property
    def support_ready(self) -> bool:
        """Бот поддержки настроен: обращения уходят в канал, ответы приходят обратно.

        Токен бота - только у одного API. Второй процесс с тем же токеном
        (локальный стенд) делил бы с продом события Telegram, и часть ответов
        поддержки ушла бы в чужую базу.
        """
        return bool(self.support_bot_token.strip().strip('"') and self.support_chat_id.strip().strip('"'))

    @property
    def import_max_bytes(self) -> int:
        return self.import_max_mb * 1024 * 1024

    @property
    def credentials_path(self) -> Path | None:
        raw = (self.service_account_json or "").strip()
        if not raw or raw.startswith("{"):
            return None
        path = Path(raw)
        return path if path.is_absolute() else _REPO_ROOT / path

    @property
    def credentials_available(self) -> bool:
        raw = (self.service_account_json or "").strip()
        if not raw:
            return False
        if raw.startswith("{"):  # JSON целиком - так креды лежат на Railway
            return True
        path = self.credentials_path
        return path is not None and path.is_file()


@lru_cache
def get_finance_settings() -> FinanceSettings:
    return FinanceSettings()


finance_settings = get_finance_settings()

__all__ = ["FinanceSettings", "finance_settings", "get_finance_settings"]
