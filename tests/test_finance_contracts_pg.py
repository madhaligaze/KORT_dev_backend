"""Реестр договоров на настоящем Postgres: одновременная правка одного поля.

Зачем отдельный набор
─────────────────────
Конфликт правок считается по полю: второй, кто правит то же поле по
устаревшему номеру, получает 409. На SQLite эта проверка проходит всегда —
там нет ни настоящей параллельности, ни `SELECT … FOR UPDATE`. Прогон двух
окон на стенде показал, что без блокировки строки оба запроса читали старый
номер поля, оба проходили проверку, и правка первого пропадала молча. Ловить
это можно только на Postgres, поэтому набор пропускается без него — как
`test_migrations.py`.

    TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5434/pdf_converter_dev \\
        uv run pytest tests/test_finance_contracts_pg.py
"""
from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

from app.finance import service as finance_service
from app.finance.contracts import service, setup
from app.finance.db import finance_session

FULL = service.Access(view=True, edit=True, setup=True)


def _base_url() -> str | None:
    for name in ("TEST_DATABASE_URL", "DATABASE_URL"):
        value = os.environ.get(name, "").strip()
        if value.startswith("postgresql"):
            return value
    return None


@pytest.fixture
def pg(monkeypatch):
    base = _base_url()
    if base is None:
        pytest.skip("нужен Postgres: задайте TEST_DATABASE_URL")
    import sqlalchemy as sa

    from app.finance import db as finance_db_module

    admin = sa.create_engine(base.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT", future=True)
    name = f"contracts_pg_{uuid.uuid4().hex[:10]}"
    with admin.connect() as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine = sa.create_engine(base.rsplit("/", 1)[0] + f"/{name}", future=True, pool_size=5)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_odnovremennaya_pravka_odnogo_polya_ne_teryaetsya(pg):
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC")
        contract = service.create(
            session, workspace, FULL, service.Actor(None, "a@test"),
            {"executor": "BBC", "customer": "ТОО Альфа", "note": "было"},
        )
        contract_id, seen, space_id = contract.id, contract.seq, workspace.id

    locked = threading.Event()
    outcome: dict[str, object] = {}

    def first() -> None:
        with finance_session() as session:
            workspace = finance_service.get_workspace(session, space_id)
            service.patch(session, workspace, FULL, service.Actor(None, "a@test"), contract_id,
                          {"note": "первый"}, known_seq=seen)
            locked.set()
            # Держим транзакцию открытой: второй запрос должен ждать, а не
            # читать старый номер поля мимо нас.
            time.sleep(0.6)
        outcome["first"] = "ok"

    def second() -> None:
        locked.wait(5)
        try:
            with finance_session() as session:
                workspace = finance_service.get_workspace(session, space_id)
                service.patch(session, workspace, FULL, service.Actor(None, "b@test"), contract_id,
                              {"note": "второй"}, known_seq=seen)
            outcome["second"] = "ok"
        except service.FieldConflict as exc:
            outcome["second"] = exc.conflicts

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert outcome["first"] == "ok"
    assert outcome["second"] == ["note"], "вторая правка того же поля должна получить конфликт"
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space_id)
        assert service.get_contract(session, workspace, contract_id).note == "первый"


def test_raznye_polya_odnovremenno_obe_prohodyat(pg):
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC")
        contract = service.create(
            session, workspace, FULL, service.Actor(None, "a@test"),
            {"executor": "BBC", "customer": "ТОО Бета"},
        )
        contract_id, seen, space_id = contract.id, contract.seq, workspace.id

    errors: list[Exception] = []

    def edit(field: str, value: str) -> None:
        try:
            with finance_session() as session:
                workspace = finance_service.get_workspace(session, space_id)
                service.patch(session, workspace, FULL, service.Actor(None, f"{field}@test"), contract_id,
                              {field: value}, known_seq=seen)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=edit, args=("note", "примечание")),
        threading.Thread(target=edit, args=("folder_url", "https://folder")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space_id)
        contract = service.get_contract(session, workspace, contract_id)
        assert (contract.note, contract.folder_url) == ("примечание", "https://folder")


def _small_registry(rows: int = 30) -> bytes:
    import io
    from datetime import datetime

    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Сводная"
    sheet.append(["№", "Текущее состояние", "Исполнитель \nBBC", "Заказчик \n(клиент)", "№ Договора",
                  "Дата заключения Договора", "Вид услуги", "Предмет Договора", "Сумма Договора"])
    for n in range(1, rows + 1):
        # Второе наше ТОО, которого ещё нет в компании: «Завести» заведёт его до
        # строк — это сдвиг номера схемы, который раньше держался всю загрузку.
        executor = "BBC" if n % 2 else "BBCA"
        sheet.append([n, "действующий", executor, f"ТОО Клиент {n}", f"№ П-{n}", datetime(2025, 1, 1 + n % 27),
                      "Абонентское обслуживание", "Бухгалтерское сопровождение", 1000 * n])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_zavedenie_ne_derzhit_schetchik_i_stavit_odin_nomer(pg, monkeypatch):
    """Пока «Завести» собирает договоры, правка в той же компании не ждёт.

    Прежде номер изменения брался до заведения строк, и блокировка строки
    счётчика держалась всю загрузку: на 20 000 строк — две минуты, в которые
    любая правка в компании висела. Теперь номер берётся последним.
    """
    import sqlalchemy as sa

    from app.finance.contracts import importer
    from app.finance.contracts.fields import current
    from app.finance.contracts.models import Contract

    actor = service.Actor(None, "owner@test")
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC")
        contract = service.create(session, workspace, FULL, actor, {"executor": "BBC", "customer": "ТОО Альфа", "note": "было"})
        batch = importer.start(session, workspace, actor, _small_registry(), "реестр.xlsx")
        own = [item["key"] for s in batch.report["sections"] if s["key"] == "entities" for item in s["items"] if item["own"]]
        importer.decide(session, workspace, batch.id, {"entities": {"confirmed": True, "own": own}})
        contract_id, batch_id, space_id = contract.id, batch.id, workspace.id

    started, release = threading.Event(), threading.Event()
    original = importer._contract_from_row

    def slow(*args, **kwargs):
        if not started.is_set():
            started.set()
            release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(importer, "_contract_from_row", slow)
    outcome: dict[str, object] = {}

    def run_apply() -> None:
        try:
            with finance_session() as session:
                workspace = finance_service.get_workspace(session, space_id)
                outcome["apply"] = importer.apply(session, workspace, FULL, actor, batch_id)
        except Exception as exc:  # noqa: BLE001
            outcome["apply"] = exc

    thread = threading.Thread(target=run_apply)
    thread.start()
    try:
        assert started.wait(10), "«Завести» не дошло до строк"
        began = time.perf_counter()
        with finance_session() as session:
            session.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
            workspace = finance_service.get_workspace(session, space_id)
            edited = service.patch(session, workspace, FULL, service.Actor(None, "b@test"), contract_id,
                                   {"note": "во время загрузки"}, known_seq=None)
            edited_seq = edited.seq
        waited = time.perf_counter() - began
        # Номер схемы: «Завести» уже завело юрлица и значения списков, но
        # сдвинет схему только на выходе — коллега, заводящий новое значение
        # списка, тоже не ждёт.
        began = time.perf_counter()
        with finance_session() as session:
            session.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
            workspace = finance_service.get_workspace(session, space_id)
            setup.add_value(session, workspace, "status", "Заведён во время загрузки")
        waited_schema = time.perf_counter() - began
    finally:
        release.set()
        thread.join(30)
    assert not isinstance(outcome.get("apply"), Exception), outcome.get("apply")
    assert waited < 1.5, f"правка ждала «Завести» {waited:.1f} с"
    assert waited_schema < 1.5, f"новое значение списка ждало «Завести» {waited_schema:.1f} с"
    assert outcome["apply"]["created"] == 30

    with finance_session() as session:
        seq = current(session, space_id, "contracts")
        imported = session.scalars(sa.select(Contract).where(Contract.import_id == batch_id)).all()
        # Номер партии взят после правки — опрос с её номером увидит всю партию.
        assert seq == edited_seq + 1
        assert {item.seq for item in imported} == {seq}
        assert all(set(item.field_seq.values()) == {seq} and "number" in item.field_seq for item in imported)
        workspace = finance_service.get_workspace(session, space_id)
        assert len(service.changes(session, workspace, FULL, edited_seq)["contracts"]) == 30
