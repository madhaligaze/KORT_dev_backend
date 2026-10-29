"""Общее для наборов: настоящий Postgres - свой, временный, а не «если задали адрес».

Наборы, которым нужен Postgres (одновременные правки, блокировки, миграции), до
29.10.2026 пропускались без `TEST_DATABASE_URL` - то есть всегда: переменную
никто не задавал, и четыре теста о потере денег при гонках молча не шли.
Теперь сервер поднимается сам: `initdb` и `pg_ctl` из установленного Postgres -
временный кластер на свободном порту, на весь прогон. Задан
`TEST_DATABASE_URL` - берётся он. Нет ни того, ни другого - пропуск с
причиной (машина без Postgres).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Iterator

import pytest


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
