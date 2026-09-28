"""Живой режим журнала: лист «Таблица» видит правки коллег без «Перечитать».

Номер изменения ставят события сессии, а не вызывающие, — поэтому набор
пишет операцию разными дорогами (форма, ячейка листа, удаление из журнала,
удаление плана `DELETE`) и проверяет одно: каждая правка приходит в опрос с
номером больше прежнего, удалённая — списком на снятие, и ничто не приходит
дважды после своего номера.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.finance import grid, live, service
from app.finance.db import finance_session
from app.finance.models import Operation


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    yield engine


def _income(session, space, comment: str, amount: str = "1000") -> Operation:
    cash = next(a for a in service.list_accounts(session, space.id) if a.name == "Касса")
    return service.create_operation(
        session,
        space,
        service.OperationInput(
            kind="income",
            paid_at=date(2026, 9, 3),
            amount=Decimal(amount),
            account_to_id=cash.id,
            comment=comment,
        ),
    )


def test_novaya_i_izmenennaya_operatsiya_prihodyat_v_opros(finance_db):
    with finance_session() as session:
        space = service.ensure_workspace(session)
        start = live.current_seq(session, space.id)
        first = _income(session, space, "первая")
        first_id = first.id

    with finance_session() as session:
        space = service.ensure_workspace(session)
        batch = live.changes(session, space, start)
        assert [row["cells"]["comment"] for row in batch["rows"]] == ["первая"]
        assert batch["removed"] == []
        after_create = batch["seq"]
        assert after_create > start
        # Пустой опрос — пусто, тот же номер.
        again = live.changes(session, space, after_create)
        assert again == {"rows": [], "removed": [], "seq": after_create}

    # Правка ячейкой листа — та же дорога, что у человека в «Таблице».
    with finance_session() as session:
        space = service.ensure_workspace(session)
        operation = session.get(Operation, first_id)
        grid.apply_cell(session, space, first_id, "comment", "поправлено", version=operation.version)

    with finance_session() as session:
        space = service.ensure_workspace(session)
        batch = live.changes(session, space, after_create)
        assert [row["cells"]["comment"] for row in batch["rows"]] == ["поправлено"]
        assert batch["seq"] > after_create


def test_udalennaya_iz_zhurnala_snimaetsya_s_lista(finance_db):
    with finance_session() as session:
        space = service.ensure_workspace(session)
        operation = _income(session, space, "уйдёт")
        operation_id = operation.id
        seq = live.current_seq(session, space.id)

    with finance_session() as session:
        space = service.ensure_workspace(session)
        service.delete_operation(session, space, operation_id)

    with finance_session() as session:
        space = service.ensure_workspace(session)
        batch = live.changes(session, space, seq)
        assert batch["rows"] == []
        assert batch["removed"] == [str(operation_id)]


def test_udalennaya_nasovsem_tozhe_snimaetsya(finance_db):
    """План из счёта и повторения удаляются `DELETE` — строки нет, запись удаления есть."""
    with finance_session() as session:
        space = service.ensure_workspace(session)
        operation = _income(session, space, "план")
        operation_id = operation.id
        seq = live.current_seq(session, space.id)

    with finance_session() as session:
        space = service.ensure_workspace(session)
        session.delete(session.get(Operation, operation_id))

    with finance_session() as session:
        space = service.ensure_workspace(session)
        batch = live.changes(session, space, seq)
        assert batch["removed"] == [str(operation_id)]
        assert batch["seq"] > seq


def test_odna_tranzaktsiya_odin_nomer_i_otkat_bez_nomera(finance_db):
    """Много операций одной транзакцией — один номер; откаченная правка номера не двигает."""
    with finance_session() as session:
        space = service.ensure_workspace(session)
        seq = live.current_seq(session, space.id)
        for index in range(5):
            _income(session, space, f"пачка {index}")

    with finance_session() as session:
        space = service.ensure_workspace(session)
        batch = live.changes(session, space, seq)
        assert len(batch["rows"]) == 5
        assert batch["seq"] == seq + 1
        numbers = {item.seq for item in session.query(Operation).filter(Operation.workspace_id == space.id)}
        assert numbers == {seq + 1}
        after = batch["seq"]

    with pytest.raises(RuntimeError):
        with finance_session() as session:
            space = service.ensure_workspace(session)
            _income(session, space, "откатится")
            raise RuntimeError("откат")

    with finance_session() as session:
        space = service.ensure_workspace(session)
        assert live.changes(session, space, after) == {"rows": [], "removed": [], "seq": after}
