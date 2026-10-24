"""Счета ядра (фаза 3): «Не выставлены» KORT против списка книги и счёт из карточки документом.

Срок счёта - «Период с», «скоро» - за три дня: то же правило, что у формулы
книги. Счёт известен из KORT (документ, который пишет карточка счёта) и из
книги («№ Счета»); месяц без счёта ни там, ни там - KORT ждёт счёт.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.finance import service as finance_service
from app.finance.contracts import bills as bills_module
from app.finance.contracts import service, setup
from app.finance.core import billing
from app.finance.core import book as book_module
from app.finance.core.models import Document
from app.finance.db import finance_session

FULL = service.Access(view=True, edit=True, setup=True, admin=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
HEADER = [
    "Мес", "Техн. 1", "Наша Фирма", "Вид Услуги", "Заказчик (Название Фирмы)", "Сумма Договора", "№ Договора",
    "Период с (нач.)", "Период по (зав.)", "Счет", "№ Счета", "Дата выстав. Счета",
]
MONTHS = {9: "СЕНТЯБРЬ", 10: "ОКТЯБРЬ"}


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    yield engine


@pytest.fixture
def space(finance_db):
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC", code="BBC")
        return workspace.id


def _row(month: int, number: str, customer: str, day: int, state: str, bill_no: str = "") -> list[str]:
    start = date(2026, month, day)
    return [
        str(month), f"{MONTHS[month]} 2026", "BBC", "Абон.П.", customer, "100 000", number,
        f"{start:%d.%m.%Y}", f"{start + timedelta(days=29):%d.%m.%Y}", state, bill_no, "",
    ]


def test_ne_vystavleny_kort_protiv_knigi(space):
    today = date(2026, 10, 20)
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        make = lambda customer, number: service.create(  # noqa: E731
            session, workspace, FULL, OWNER,
            {"executor": "BBC", "customer": customer, "number": number, "amount": "100000",
             "type": "Абонентское обслуживание", "signed_at": "2026-09-01"},
        )
        make("ТОО Альфаком", "№1")   # счёт за сентябрь в книге, октябрь просрочен у обоих
        make("ТОО Бетаком", "№2")    # в книге строки нет - KORT ждёт счёт один
        make("ТОО Гаммаком", "№3")   # в книге октябрь «Еще рано», у KORT просрочен - срок другой
        book = book_module.build_book([
            HEADER,
            _row(9, "№1", "Альфаком", 1, "TRUE", "101"),
            _row(10, "№1", "Альфаком", 1, "Просрочка более 5 дней"),
            _row(9, "№3", "Гаммаком", 1, "TRUE", "103"),
            _row(10, "№3", "Гаммаком", 1, "Еще рано"),
        ], title="Сводка", worksheet="Сводка все ЮР лица")
        out = billing.reconcile_bills(session, workspace, FULL, book, today=today)
        codes = sorted((out["contracts"][line["contract_id"]]["number"], line["month"], line["code"]) for line in out["lines"])
        assert codes == [
            ("№2", "2026-09", "no_book"), ("№2", "2026-10", "no_book"), ("№3", "2026-10", "group"),
        ]
        assert out["counts"]["same"] == 3 and out["counts"]["kort_waits"] == 4 and out["counts"]["book_waits"] == 2

        # Карточка выставила октябрьский счёт №1 в книге юрлица - KORT знает его сам,
        # пока сводка не догнала книгу.
        bill = bills_module.Bill(
            line=3, group=bills_module.OVERDUE, text="Просрочка более 5 дней", own="BBC", customer="Альфаком",
            number="№1", month="ОКТЯБРЬ 2026", month_no=10, kind="Абон.П.", staff="", department="",
            amount=Decimal("100000"), period_from=date(2026, 10, 1), period_to=None, paid="", status="", note="",
        )
        doc = billing.record(session, workspace, bill, {"bill_no": "201", "bill_date": "20.10.2026"})
        assert doc is not None and doc.number == "201" and doc.issued_on == date(2026, 10, 20) and doc.period == date(2026, 10, 1)
        again = billing.record(session, workspace, bill, {"bill_no": "201", "bill_date": "20.10.2026"})
        assert again.id == doc.id, "повторная запись не двоит документ"
        after = billing.reconcile_bills(session, workspace, FULL, book, today=today)
        assert ("№1", "2026-10", "kort_issued") in {
            (after["contracts"][line["contract_id"]]["number"], line["month"], line["code"]) for line in after["lines"]
        }
        # Отметку сняли - документ уходит.
        billing.record(session, workspace, bill, {"bill_no": "", "bill_date": ""})
        assert session.scalar(sa.select(Document).where(Document.id == doc.id)).deleted_at is not None
