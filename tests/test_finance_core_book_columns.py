"""Словарь колонок сводной таблицы (фаза 9, R14; этап 2, п.9): названия компании к словам ядра.

У ядра для колонки - ключ и общие слова отрасли. Компания называет колонки
по-своему - и лист без словаря не читается. Словарь добавляет её названия
(не номера) к словам ядра; шапка листа, который не разобрался, остаётся,
чтобы её можно было сопоставить.
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

    columns = book_module.columns_for({"own": ["Наша Фирма"], "number": ["Контракт"], "amount": ["Сумма по договору"]})
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
            assert any("Сумма договора" in title and "читается и как" in title for title in titles)
            with pytest.raises(PermissionError):
                core_settings.set_book_column(
                    session, workspace, service.Access(view=True, edit=True), OWNER, key="amount", name="Х", on=True, known=KNOWN
                )
            with pytest.raises(Exception, match="Такой колонки"):
                core_settings.set_book_column(session, workspace, FULL, OWNER, key="nope", name="Х", on=True, known=KNOWN)
    finally:
        history.CONTEXT.reset(token)


def test_odin_slovar_dlya_vseh_chitateley_tablicy(finance_db, monkeypatch):
    """Своё название колонки, записанное раз, понимают ядро, «Счета» и сводка оплат; таблица со
    своими названиями подключается с объяснением, а не отказом."""
    from app.finance.contracts import bills as bills_module
    from app.finance.contracts import summary as summary_module
    from app.finance.contracts.fields import number_key

    header = ["Наша Фирма", "Клиент", "Контракт", "Оплачено", "Состояние счёта", "Период с"]
    values = [header, ["BBC", "ТОО Альфаком", "№ОКР/1", "50 000", "Просрочка более 5 дней", "01.10.2026"]]
    monkeypatch.setattr(
        summary_module, "_read",
        lambda spreadsheet_id, tab, extra=None: summary_module.build_index(
            values, title="Сводка компании", worksheet=tab, columns=summary_module.columns_for(extra)
        ),
    )
    token = history.CONTEXT.set(history.AuditContext(user_id=OWNER.user_id))
    try:
        with finance_session() as session:
            workspace = finance_service.ensure_workspace(session)
            source, index, problem = summary_module.connect(
                session, workspace.id, link="https://docs.google.com/spreadsheets/d/abc123/edit", worksheet="Сводка", user_id=None
            )
            assert index is None and "№ договора" in problem and source.worksheet == "Сводка", "подключена, с объяснением"
            for key, name in (
                ("own", "Наша Фирма"), ("customer", "Клиент"), ("number", "Контракт"), ("paid", "Оплачено"),
                ("bill_state", "Состояние счёта"),
            ):
                core_settings.set_book_column(session, workspace, FULL, OWNER, key=key, name=name, on=True, known=KNOWN)
            extra = core_settings.book_columns(session, workspace.id)
            summary_module.forget(workspace.id)
            slot = summary_module.get(session, workspace.id, wait=5)
            assert slot is not None and slot.index is not None and slot.index.rows == 1, slot.error if slot else ""
            [row] = slot.index.by_number[number_key("№ОКР/1")]
            assert row.paid == Decimal("50000")
    finally:
        history.CONTEXT.reset(token)
    board = bills_module.build_board(values, title="Сводка", worksheet="Сводка", columns=bills_module.columns_for(extra))
    [bill] = board.bills
    assert (bill.number, bill.group, bill.text) == ("№ОКР/1", bills_module.OVERDUE, "Просрочка более 5 дней"), "«Счет» ядра - «bill» у «Счетов»"
