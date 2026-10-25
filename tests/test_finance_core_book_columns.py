"""Словарь колонок сводной таблицы (фаза 9, R14): названия компании к названиям ядра.

Ядро читает сводную таблицу по названиям колонок BBC. Другая компания
называет колонки по-своему - и лист не читается. Словарь добавляет её
названия (не номера) к названиям ядра; шапка листа, который не разобрался,
остаётся, чтобы её можно было сопоставить.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.finance import history
from app.finance import service as finance_service
from app.finance.contracts import service
from app.finance.core import book as book_module
from app.finance.core import settings as core_settings
from app.finance.db import finance_session
from app.finance.layout import inspect_layout
from app.finance.models import ActionLog

FULL = service.Access(view=True, edit=True, setup=True, admin=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
KNOWN = {column.key: column.title for column in book_module.COLUMNS}


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    yield engine


def test_nazvaniya_kompanii_chitayutsya_vmeste_s_nazvaniyami_yadra():
    header = ["Наша Фирма", "Заказчик", "Контракт", "Сумма по договору"]
    rows = [header, ["BBC", "ТОО Альфаком", "№ОКР/1", "100 000"]]
    with pytest.raises(book_module.LayoutBroken) as broken:
        book_module.build_book(rows, title="Сводка", worksheet="Сводка")
    assert broken.value.header == header, "шапка не разобравшегося листа - для сопоставления"
    seen = {item["key"]: item for item in inspect_layout(book_module.COLUMNS, header)}
    assert seen["number"]["problem"] == "нет в таблице" and seen["customer"]["header"] == "Заказчик"
    assert seen["amount"]["index"] is None and seen["amount"]["problem"] == "", "необязательная - не отказ"

    columns = book_module.columns_for({"number": ["Контракт"], "amount": ["Сумма по договору"]})
    book = book_module.build_book(rows, title="Сводка", worksheet="Сводка", columns=columns)
    assert (book.rows[0].number, book.rows[0].amount) == ("№ОКР/1", Decimal("100000"))
    assert book.header == header


def test_slovar_v_nastroykah_s_istoriey_i_pravami(finance_db):
    token = history.CONTEXT.set(history.AuditContext(user_id=OWNER.user_id))
    try:
        with finance_session() as session:
            workspace = finance_service.ensure_workspace(session)
            core_settings.set_book_column(session, workspace, FULL, OWNER, key="amount", name="Сумма по  договору", on=True, known=KNOWN)
            assert core_settings.book_columns(session, workspace.id) == {"amount": ["Сумма по договору"]}, "пробелы схлопнуты"
            core_settings.set_book_column(session, workspace, FULL, OWNER, key="amount", name="сумма ПО договору", on=False, known=KNOWN)
            assert core_settings.book_columns(session, workspace.id) == {}, "снимается без учёта регистра"
            titles = session.scalars(sa.select(ActionLog.title).where(ActionLog.kind == "core.settings")).all()
            assert any("Сумма Договора" in title and "читается и как" in title for title in titles)
            with pytest.raises(PermissionError):
                core_settings.set_book_column(
                    session, workspace, service.Access(view=True, edit=True), OWNER, key="amount", name="Х", on=True, known=KNOWN
                )
            with pytest.raises(Exception, match="Такой колонки"):
                core_settings.set_book_column(session, workspace, FULL, OWNER, key="nope", name="Х", on=True, known=KNOWN)
    finally:
        history.CONTEXT.reset(token)
