from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _strip_wrapping_quotes(value: str) -> str:
    trimmed = value.strip()
    if (
        (trimmed.startswith('"') and trimmed.endswith('"'))
        or (trimmed.startswith("'") and trimmed.endswith("'"))
    ):
        return trimmed[1:-1].strip()
    return trimmed


class Settings(BaseSettings):
    """Настройки процесса. Настройки учёта — `app/finance/config.py` (префикс FINANCE_)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "KORT API"
    environment: str = "development"
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"
    app_host: str = Field(default="0.0.0.0", validation_alias=AliasChoices("APP_HOST"))
    app_port: int = Field(default=8080, validation_alias=AliasChoices("PORT", "APP_PORT"))
    # Перезапуск uvicorn по правке файлов. Не задано — по окружению: development
    # ⇒ да. В контейнере его выключают явно: исходников снаружи там нет, а
    # перезапуск стоит двух лишних процессов и постоянной слежки за файлами.
    app_reload: bool | None = Field(default=None, validation_alias=AliasChoices("APP_RELOAD"))
    allowed_origins: Annotated[str, NoDecode] = Field(
        default="http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001",
        validation_alias=AliasChoices("ALLOWED_ORIGINS"),
    )
    database_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/kort",
        validation_alias=AliasChoices("DATABASE_URL"),
    )
    # Распознавание сканов выписок (app/services/ocr_service.py). Без ключа —
    # локальный RapidOCR.
    azure_document_intelligence_endpoint: str | None = None
    azure_document_intelligence_key: str | None = None

    @field_validator(
        "app_name",
        "environment",
        "api_v1_prefix",
        "log_level",
        "app_host",
        "allowed_origins",
        "database_url",
        "azure_document_intelligence_endpoint",
        "azure_document_intelligence_key",
        mode="before",
    )
    @classmethod
    def normalize_env_string(cls, v: str | None) -> str | None:
        if isinstance(v, str):
            return _strip_wrapping_quotes(v)
        return v

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        if isinstance(v, str):
            v = _strip_wrapping_quotes(v)
            if v.startswith("postgres://"):
                return v.replace("postgres://", "postgresql+psycopg://", 1)
            if v.startswith("postgresql://"):
                return v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
