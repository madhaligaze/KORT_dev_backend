"""Запасная SQLite: только без адреса базы и не на проде.

22.10.2026, при переезде прода на Render, вход минуту отвечал «Неверная
почта или пароль», а в журнале базы прода не было ни одного отказа: так
отвечает пустая база. Сервер с заданным, но недоступным `DATABASE_URL`
должен падать, а не подниматься на SQLite.
"""
from __future__ import annotations

import pytest

from app.core import database
from app.core.config import Settings

UNREACHABLE = "postgresql+psycopg://postgres:x@postgres.railway.internal:5432/railway"


@pytest.fixture
def fresh_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_resolved_database_url", None)
    monkeypatch.setattr(database, "_pick_database_url", lambda: UNREACHABLE)
    monkeypatch.setattr(database, "_default_sqlite_url", lambda: f"sqlite:///{(tmp_path / 'kort.db').as_posix()}")

    def unreachable(url: str):
        raise OSError(f"could not translate host name: {url}")

    monkeypatch.setattr(database, "_connect_primary", unreachable)


def _settings(monkeypatch, **env: str) -> None:
    """Настройки, как их прочитает процесс с этими переменными окружения."""
    for name in ("DATABASE_URL", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(database, "settings", Settings(_env_file=None))


def test_explicit_database_url_never_falls_back(fresh_engine, monkeypatch):
    # Адрес с Railway на новом хостинге и забытый ENVIRONMENT.
    _settings(monkeypatch, DATABASE_URL=UNREACHABLE)
    with pytest.raises(OSError):
        database.get_engine()
    assert database._engine is None


def test_production_never_falls_back(fresh_engine, monkeypatch):
    _settings(monkeypatch, ENVIRONMENT="production")
    with pytest.raises(OSError):
        database.get_engine()
    assert database._engine is None


def test_local_run_without_settings_still_gets_sqlite(fresh_engine, monkeypatch):
    _settings(monkeypatch)
    assert database.get_engine().dialect.name == "sqlite"
