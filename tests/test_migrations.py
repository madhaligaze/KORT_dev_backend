"""Миграции и модели - одна схема: проверка на настоящем Postgres.

На SQLite схему заводит `create_all` из моделей, а на проде - миграции. Колонка,
добавленная в модель без ревизии (или ревизия, расходящаяся с моделью), в
тестах не видна вовсе и всплывает на выкладке. Поэтому база заводится с нуля
всеми миграциями и сравнивается с моделями тем же сравнением, что у
автогенерации alembic (`app/migrations/metadata.py`); последняя ревизия
откатывается и накатывается снова.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

pytestmark = pytest.mark.migrations

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(postgres_server) -> Iterator[str]:
    admin = sa.create_engine(postgres_server + "/postgres", isolation_level="AUTOCOMMIT", future=True)
    name = f"migrations_{uuid.uuid4().hex[:10]}"
    with admin.connect() as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        yield postgres_server + f"/{name}"
    finally:
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _config(url: str) -> Config:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "app" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _drift(url: str) -> list:
    from app.migrations.metadata import target_metadata

    known = {metadata.schema for metadata in target_metadata}
    engine = sa.create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={
                "compare_type": True, "include_schemas": True,
                "include_name": lambda name, kind, _parents: name in known if kind == "schema" else True,
            })
            return compare_metadata(context, target_metadata)
    finally:
        engine.dispose()


def test_migracii_s_nulya_sovpadayut_s_modelyami(database: str) -> None:
    config = _config(database)
    command.upgrade(config, "head")
    assert _drift(database) == [], "модель и миграции разошлись - нужна ревизия"
    # Последняя ревизия откатывается и накатывается снова: выкладку можно вернуть.
    command.downgrade(config, "-1")
    command.upgrade(config, "head")
    assert _drift(database) == []
