"""Общее для наборов: настоящий Postgres - свой, временный, а не «если задали адрес».

Наборы, которым нужен Postgres (одновременные правки, блокировки, миграции), до
29.10.2026 пропускались без `TEST_DATABASE_URL` - то есть всегда: переменную
никто не задавал, и четыре теста о потере денег при гонках молча не шли.
Теперь сервер поднимается сам: `initdb` и `pg_ctl` из установленного Postgres -
временный кластер на свободном порту, на весь прогон. Задан
`TEST_DATABASE_URL` - берётся он. Нет ни того, ни другого - пропуск с
причиной (машина без Postgres).

Тестовая SQLite не ждёт диска (`_sqlite_without_fsync`), пароли хешируются
дёшево (`_cheap_password_hash`).
"""
from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
from collections.abc import Iterator

import pytest
from argon2 import PasswordHasher
from sqlalchemy import event
from sqlalchemy.engine import Engine


@event.listens_for(Engine, "connect")
def _sqlite_without_fsync(dbapi_connection, _record) -> None:
    """Каждый тест заводит свою базу-файл, и `create_all` пишет в неё 63 таблицы.

    pysqlite фиксирует каждый `CREATE` отдельно, а фиксация ждёт сброса на
    диск: на этой машине 10,5 с на тест, то есть почти весь прогон уходил на
    запись пустых таблиц. Без ожидания диска - 0,12 с (замер 30.10.2026).

    Смысл транзакций, блокировок и отката тот же: журнал отката лежит в
    памяти, а не в файле рядом. Теряется только переживание сбоя питания -
    базе, которую тест стирает за собой, оно не нужно.
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA synchronous = OFF")
        cursor.execute("PRAGMA journal_mode = MEMORY")
        cursor.close()


#: argon2id той же формы, но самой низкой стоимости, какую допускает алгоритм.
_CHEAP_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


@pytest.fixture(autouse=True)
def _cheap_password_hash(request, monkeypatch) -> None:
    """Каждая учётка теста - хеш, каждый вход - проверка хеша.

    С боевой стоимостью (t=3, 64 МиБ) это 65 мс на раз, и в наборах с
    командой из шести человек четверть времени уходила на argon2 (замер
    30.10.2026). Наборы проверяют, что пароль не хранится открытым, что
    неверный не подходит и что вход не выдаёт почту, - от стоимости хеша это
    не зависит. Саму стоимость держит `test_paroli_heshiruyutsya_s_boevoy_stoimostyu`
    с меткой `real_hasher`: ему подмена не делается.
    """
    if request.node.get_closest_marker("real_hasher"):
        return
    from app.finance import auth

    monkeypatch.setattr(auth, "_hasher", _CHEAP_HASHER)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _given() -> str | None:
    value = os.environ.get("TEST_DATABASE_URL", "").strip()
    return value if value.startswith("postgresql") else None


@pytest.fixture(scope="session")
def postgres_server(tmp_path_factory) -> Iterator[str]:
    """Адрес сервера Postgres без базы (`postgresql+psycopg://postgres@127.0.0.1:N`) - наборы заводят свои базы."""
    given = _given()
    if given is not None:
        yield given.rsplit("/", 1)[0]
        return
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.skip("нужен Postgres: initdb и pg_ctl в PATH или TEST_DATABASE_URL")
    root = tmp_path_factory.mktemp("postgres")
    data = root / "data"
    created = subprocess.run(
        [initdb, "-D", str(data), "-U", "postgres", "-A", "trust", "-E", "UTF8", "--no-locale"],
        capture_output=True, text=True, check=False,
    )
    if created.returncode != 0:
        pytest.skip(f"временный Postgres не завёлся: {created.stderr.strip()[-300:]}")
    port = _free_port()
    log = root / "server.log"
    # Вывод `pg_ctl` - не в трубу, а мимо: сервер наследует её, и на Windows
    # `run` ждал бы закрытия трубы до остановки сервера (прогон висел).
    started = subprocess.run(
        [pg_ctl, "-D", str(data), "-l", str(log), "-w", "-t", "60", "-o", f"-p {port} -c listen_addresses=127.0.0.1", "start"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if started.returncode != 0:
        pytest.skip(f"временный Postgres не запустился: {log.read_text(errors='replace')[-300:] if log.exists() else ''}")
    try:
        yield f"postgresql+psycopg://postgres@127.0.0.1:{port}"
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
