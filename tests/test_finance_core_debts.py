"""Итоги ядра (фаза 6): долг по начислениям и по АВР на любой день, старение, доли.

Долг не хранится: входящее сальдо из книги на начало учёта, начисления, АВР
и оплаты дают его на любой день. Чей долг - клиента, юрлица, отдела и
человека по долям; доли, которые не открыты, не делятся. Сальдо KORT на конец
месяца сверяется со «Сальдо Конец» книги-сводки.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from app.finance import history
from app.finance import service as finance_service
from app.finance.contracts import restore, service, setup, shares
from app.finance.contracts.models import GroupEntity
from app.finance.core import book as book_module
from app.finance.core import debts as core_debts
from app.finance.core import opening
from app.finance.core import payments as core_payments
from app.finance.core import settings as core_settings
from app.finance.core.models import Document
from app.finance.db import finance_session

import company_layer


@pytest.fixture(autouse=True)
def _company_layer(monkeypatch):
    """Компания теста - с шапкой таблиц копии прода: её знает словарь компании, а не ядро (п.9 «Пакеты»)."""
    company_layer.everywhere(monkeypatch)


FULL = service.Access(view=True, edit=True, setup=True, admin=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
TODAY = date(2026, 10, 25)
SINCE = date(2026, 6, 1)
ACCOUNT = 'Kaspi Юр. ТОО "BBC"'
HEADER = [
    "Мес", "Техн. 1", "Наша Фирма", "Заказчик (Название Фирмы)", "Сумма Договора", "№ Договора",
    "Период с (нач.)", "Период по (зав.)", "Сальдо Начало (АВР)", "АВР (наша)", "Сумма (наша)", "№ АВР (наша)",
    "Дата (наша)", "Сальдо Конец (АВР)", "Сальдо Начало (без АВР)", "Сальдо Конец (без АВР)",
]
MONTHS = {6: "ИЮНЬ", 7: "ИЮЛЬ", 8: "АВГУСТ", 9: "СЕНТЯБРЬ"}


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


@pytest.fixture
def audit():
    token = history.CONTEXT.set(history.AuditContext(user_id=OWNER.user_id))
    yield
    history.CONTEXT.reset(token)


def _contract(session, workspace, customer: str, number: str, *, signed: str = "2026-06-01", amount: str = "100000", **extra):
    return service.create(
        session, workspace, FULL, OWNER,
        {"executor": "BBC", "customer": customer, "number": number, "amount": amount, "billing": "month",
         "signed_at": signed, **extra},
    )


def _account(session, workspace):
    entity = session.scalar(sa.select(GroupEntity.counterparty_id).where(GroupEntity.code == "BBC"))
    return finance_service.create_account(session, workspace, name=ACCOUNT, group_entity_id=entity)


def _income(session, workspace, account, payer: str, amount: str, day: date, *, month: str = "", kind: str = "income"):
    party = finance_service.ensure_counterparty(session, workspace.id, payer, role="client" if kind == "income" else "supplier")
    start = date(int(month[:4]), int(month[5:7]), 1) if month else None
    accounts = {"account_to_id": account.id} if kind == "income" else {"account_from_id": account.id}
    return finance_service.create_operation(
        session, workspace,
        finance_service.OperationInput(
            kind=kind, paid_at=day, amount=Decimal(amount), counterparty_id=party.id,
            period_start=start, period_end=start, **accounts,
        ),
    )


def _row(month: int, customer: str, number: str, **cells: str) -> list[str]:
    start = date(2026, month, 1)
    values = {key: "" for key in HEADER}
    values.update({
        "Мес": str(month), "Техн. 1": f"{MONTHS[month]} 2026", "Наша Фирма": "BBC", "Заказчик (Название Фирмы)": customer,
        "Сумма Договора": "100 000", "№ Договора": number, "Период с (нач.)": f"{start:%d.%m.%Y}", "АВР (наша)": "FALSE",
    })
    values.update({name: value for name, value in cells.items()})
    return [values[key] for key in HEADER]


def _book(*rows: list[str]) -> book_module.Book:
    return book_module.build_book([HEADER, *rows], title="Сводка", worksheet="Сводка все ЮР лица")


def _cell(name: str) -> str:
    return {
        "opening_acts": "Сальдо Начало (АВР)", "closing_acts": "Сальдо Конец (АВР)",
        "opening": "Сальдо Начало (без АВР)", "closing": "Сальдо Конец (без АВР)",
        "act": "АВР (наша)", "act_sum": "Сумма (наша)", "act_no": "№ АВР (наша)", "act_date": "Дата (наша)",
    }[name]


def _cells(**values: str) -> dict[str, str]:
    return {_cell(key): value for key, value in values.items()}


# ── Входящее сальдо двух оснований ───────────────────────────────────────────


def test_saldo_po_nachisleniyam_i_po_avr_iz_knigi(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ОКР/15")
        beta = _contract(session, workspace, "ТОО Бетаком", "№ОКР/16")
        book = _book(
            _row(6, "ТОО Альфаком", "№ОКР/15", **_cells(opening="120 000", opening_acts="90 000")),
            _row(6, "ТОО Бетаком", "№ОКР/16", **_cells(opening="-30 000")),
        )
        plan = opening.plan(session, workspace, FULL, book, since=SINCE)
        assert plan["counts"]["new"] == 3, "у Альфакома два сальдо, у Бетакома одно"
        assert (plan["debt"], plan["advance"], plan["acts_debt"]) == ("120000.00", "30000.00", "90000.00")
        assert {(item["number"], item["basis"]) for item in plan["items"]} == {
            ("№ОКР/15", "accruals"), ("№ОКР/15", "acts"), ("№ОКР/16", "accruals"),
        }
        done = opening.apply(session, workspace, FULL, OWNER, book, since=SINCE)
        assert done["done"] == 2 and done["documents"] == 3
        assert opening.plan(session, workspace, FULL, book, since=SINCE)["counts"]["same"] == 3, "повтор не двоит"
        acts = opening.current(session, workspace, SINCE, "acts")
        assert list(acts) == [alfa.id] and acts[alfa.id].amount == Decimal("90000")
        core_settings.set_payments(session, workspace, FULL, OWNER, since=SINCE)
        money = core_payments.Money(session, workspace, today=TODAY)
        assert money.contract(alfa).opening == Decimal("120000"), "оплаты гасят сальдо по начислениям, не по АВР"
        assert money.contract(beta).opening == Decimal("-30000")
        opening.set_one(session, workspace, FULL, OWNER, beta.id, since=SINCE, amount=Decimal("15000"), basis="acts")
        assert opening.current(session, workspace, SINCE, "acts")[beta.id].amount == Decimal("15000")
        again = opening.plan(session, workspace, FULL, book, since=SINCE)
        assert again["counts"]["manual"] == 1, "поставленное руками книга не перебивает"
        restore.restore_point(session, workspace, FULL, OWNER, uuid.UUID(done["point"]["id"]))
        left = session.scalars(sa.select(Document).where(Document.kind == "opening", Document.deleted_at.is_(None))).all()
        assert [doc.contract_id for doc in left] == [beta.id], "«Восстановление» снимает перенесённое, ручное остаётся"


# ── Долг по начислениям ──────────────────────────────────────────────────────


def test_dolg_po_nachisleniyam_starenie_i_na_datu(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ОКР/15")
        account = _account(session, workspace)
        core_settings.set_payments(session, workspace, FULL, OWNER, since=SINCE)
        opening.set_one(session, workspace, FULL, OWNER, alfa.id, since=SINCE, amount=Decimal("50000"))
        _income(session, workspace, account, "ТОО Альфаком", "100000", date(2026, 6, 5), month="2026-06")
        _income(session, workspace, account, "ТОО Альфаком", "100000", date(2026, 8, 10))

        now = core_debts.Debts(session, workspace, FULL, day=TODAY).items[alfa.id]
        assert now.debt == Decimal("350000"), "50 000 + пять месяцев по 100 000 - 200 000"
        assert now.aging == {
            "d90plus": Decimal("50000"), "d90": Decimal("100000"), "d60": Decimal("100000"), "d30": Decimal("100000"),
        }, "оплата без месяца гасит сальдо и половину июля; остальное - по возрасту от начала периода"
        assert sum(now.aging.values()) == now.debt and not now.advance

        july = core_debts.Debts(session, workspace, FULL, day=date(2026, 7, 15)).items[alfa.id]
        assert july.debt == Decimal("150000"), "на 15.07: сальдо, июнь и июль, минус июньская оплата"
        assert july.aging == {"opening": Decimal("50000"), "d30": Decimal("100000")}, "августовской оплаты ещё нет"
        assert july.paid == Decimal("100000")


def test_avans_i_oplacheno_vpered(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ОКР/15", signed="2026-09-01")
        account = _account(session, workspace)
        core_settings.set_payments(session, workspace, FULL, OWNER, since=date(2026, 9, 1))
        _income(session, workspace, account, "ТОО Альфаком", "250000", date(2026, 9, 3), month="2026-09")
        item = core_debts.Debts(session, workspace, FULL, day=TODAY).items[alfa.id]
        assert item.debt == Decimal("-50000") and item.aging == {}
        assert item.advance == Decimal("50000"), "ноябрь оплачен вперёд - аванс, а не долг"
        totals = core_debts.Debts(session, workspace, FULL, day=TODAY).totals()
        assert totals["advance"] == "50000.00" and totals["owed"] == "0.00"


def test_dogovor_bez_nachisleniy_dolg_neizvesten(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        service.create(
            session, workspace, FULL, OWNER,
            {"executor": "BBC", "customer": "ТОО Бетаком", "number": "№НО/41", "billing": "terms", "signed_at": "2026-06-01"},
        )
        core_settings.set_payments(session, workspace, FULL, OWNER, since=SINCE)
        debts = core_debts.Debts(session, workspace, FULL, day=TODAY)
        assert not debts.items and debts.unknown == 1, "KORT начислений не считает - долга не выдумывает"


# ── Долг по АВР ──────────────────────────────────────────────────────────────


def test_dolg_po_avr_akty_po_poryadku_dat(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ОКР/15")
        account = _account(session, workspace)
        book = _book(
            _row(6, "ТОО Альфаком", "№ОКР/15", **_cells(opening_acts="30 000", act="TRUE", act_sum="100 000", act_no="457",
                                                       act_date="02.07.2026")),
            _row(7, "ТОО Альфаком", "№ОКР/15", **_cells(act="TRUE", act_sum="100 000", act_no="502", act_date="01.08.2026")),
        )
        core_settings.set_payments(session, workspace, FULL, OWNER, since=SINCE)
        opening.apply(session, workspace, FULL, OWNER, book, since=SINCE)
        _income(session, workspace, account, "ТОО Альфаком", "150000", date(2026, 7, 10))

        item = core_debts.Debts(session, workspace, FULL, day=TODAY, basis="acts", book=book).items[alfa.id]
        assert item.debt == Decimal("80000"), "30 000 + два акта - 150 000"
        assert item.aging == {"d90": Decimal("80000")}, "оплата гасит сальдо, июньский акт и часть июльского"
        early = core_debts.Debts(session, workspace, FULL, day=date(2026, 7, 20), basis="acts", book=book).items[alfa.id]
        assert early.debt == Decimal("-20000") and early.advance == Decimal("20000"), "июльского акта на 20.07 ещё нет"
        without = core_debts.Debts(session, workspace, FULL, day=TODAY, basis="acts")
        assert without.items[alfa.id].debt == Decimal("-120000"), "без книги - только сальдо и оплата: актов KORT и 1С нет"


# ── Чей долг ─────────────────────────────────────────────────────────────────


def test_dolg_po_otdelam_po_dolyam_i_zakrytye_doli(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        yuo = setup.upsert_department(session, workspace, {"code": "ЮО"})
        hr = setup.upsert_department(session, workspace, {"code": "HR"})
        split = _contract(session, workspace, "ТОО Альфаком", "№ОКР/59", signed="2026-10-01", department="ЮО, HR")
        _contract(session, workspace, "ТОО Бетаком", "№ЮО/1", signed="2026-10-01", department="ЮО")
        _contract(session, workspace, "ТОО Гамма", "№ЮО/2", signed="2026-10-01", department="ЮО, HR")
        shares.set_departments(
            session, workspace, FULL, OWNER, split.id, "percent",
            [{"department_id": str(hr.id), "value": "70"}, {"department_id": str(yuo.id), "value": "30"}],
        )
        core_settings.set_payments(session, workspace, FULL, OWNER, since=date(2026, 10, 1))

        rows = {row["title"]: row for row in core_debts.Debts(session, workspace, FULL, day=TODAY).rows("department")}
        assert {title: row["debt"] for title, row in rows.items()} == {
            "ЮО": "130000.00", "HR": "70000.00", "не распределено": "100000.00",
        }, "ЮО - 30% совместного и свой целиком; без долей у двух отделов - не распределено"
        part = next(item for item in rows["ЮО"]["contracts"] if item["number"] == "№ОКР/59")
        assert part["fraction"] == "30.00" and part["debt"] == "30000.00" and part["whole"] == "100000.00"
        assert [row["title"] for row in core_debts.Debts(session, workspace, FULL, day=TODAY).rows("department")][-1] == (
            "не распределено"
        ), "особые группы - в конце"

        # Сотрудник с реестром, но без права на доли отделов: совместные не делятся.
        reader = service.Access(view=True, edit=False)
        closed = {row["title"]: row["debt"] for row in core_debts.Debts(session, workspace, reader, day=TODAY).rows("department")}
        assert closed == {"ЮО": "100000.00", "доли закрыты": "200000.00"}, "долю не видно и по долгу"

        clients = core_debts.Debts(session, workspace, FULL, day=TODAY).rows("client")
        assert [row["title"] for row in clients] == ["ТОО Альфаком", "ТОО Бетаком", "ТОО Гамма"]
        entity = core_debts.Debts(session, workspace, FULL, day=TODAY).rows("entity")
        assert [(row["title"], row["debt"]) for row in entity] == [("BBC", "300000.00")]


def test_nam_dolzhny_my_dolzhny_i_mezhdu_svoimi(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        setup.add_entity(session, workspace, name="BBCA", code="BBCA")
        account = _account(session, workspace)
        october = {"signed": "2026-10-01"}
        _contract(session, workspace, "ТОО Альфаком", "№ОКР/1", **october)
        service.create(
            session, workspace, FULL, OWNER,
            {"executor": "ТОО Поставщик", "customer": "BBC", "number": "№П/1", "amount": "50000", "billing": "month",
             "signed_at": "2026-10-01"},
        )
        service.create(
            session, workspace, FULL, OWNER,
            {"executor": "BBCA", "customer": "BBC", "number": "FIN/72", "amount": "30000", "billing": "month",
             "signed_at": "2026-10-01"},
        )
        core_settings.set_payments(session, workspace, FULL, OWNER, since=date(2026, 10, 1))
        _income(session, workspace, account, "ТОО Поставщик", "20000", date(2026, 10, 5), month="2026-10", kind="expense")

        sides = core_debts.Debts(session, workspace, FULL, day=TODAY).sides()
        assert {key: value["owed"] for key, value in sides.items()} == {
            "receivable": "100000.00", "payable": "30000.00", "intra": "30000.00",
        }, "покупка - наш долг за вычетом нашей оплаты; договор между своими - не долг клиента"
        theirs = core_debts.Debts(session, workspace, FULL, day=TODAY, side="payable")
        assert [(row["title"], row["owed"]) for row in theirs.rows("client")] == [("ТОО Поставщик", "30000.00")]
        assert [row["title"] for row in theirs.rows("entity")] == ["BBC"], "у покупки наше юрлицо - заказчик"
        inside = core_debts.Debts(session, workspace, FULL, day=TODAY, side="intra")
        assert [row["title"] for row in inside.rows("client")] == ["BBC"] and [row["title"] for row in inside.rows("entity")] == ["BBCA"]
        assert inside.rows("client")[0]["contracts"][0]["party"] == "BBC"


# ── Выручка по договорам ─────────────────────────────────────────────────────


def test_vyruchka_po_mesyacam_dolyam_i_mezhdu_svoimi(space, audit):
    from app.finance.core import revenue as core_revenue

    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        yuo = setup.upsert_department(session, workspace, {"code": "ЮО"})
        hr = setup.upsert_department(session, workspace, {"code": "HR"})
        setup.add_entity(session, workspace, name="BBCA", code="BBCA")
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ОКР/59", department="ЮО, HR")
        shares.set_departments(
            session, workspace, FULL, OWNER, alfa.id, "percent",
            [{"department_id": str(hr.id), "value": "70"}, {"department_id": str(yuo.id), "value": "30"}],
        )
        service.create(
            session, workspace, FULL, OWNER,
            {"executor": "BBCA", "customer": "BBC", "number": "FIN/72", "amount": "30000", "billing": "month",
             "signed_at": "2026-10-01"},
        )
        account = _account(session, workspace)
        core_settings.set_payments(session, workspace, FULL, OWNER, since=SINCE)
        _income(session, workspace, account, "ТОО Альфаком", "100000", date(2026, 6, 5), month="2026-06")
        _income(session, workspace, account, "ТОО Альфаком", "50000", date(2026, 8, 3))

        revenue = core_revenue.Revenue(session, workspace, FULL, year=2026, today=TODAY)
        out = revenue.out("entity")
        assert [row["title"] for row in out["rows"]] == ["BBC"], "договор между своими - не продажа клиенту"
        bbc = out["rows"][0]
        assert bbc["months"]["2026-06"] == {"accrued": "100000.00", "acts": "0.00", "paid": "100000.00", "received": "100000.00"}
        assert bbc["months"]["2026-07"]["paid"] == "50000.00" and bbc["months"]["2026-08"]["received"] == "50000.00", (
            "оплата без месяца - июлю (старейший долг), а поступила в августе"
        )
        assert bbc["months"]["2026-03"]["paid"] is None, "до начала учёта оплат KORT их не знает - пусто, а не ноль"
        assert bbc["total"]["accrued"] == "700000.00" and out["forecast_from"] == "2026-11", "ноябрь и декабрь - прогноз"
        departments = {row["title"]: row["months"]["2026-06"]["accrued"] for row in revenue.rows("department")}
        assert departments == {"HR": "70000.00", "ЮО": "30000.00"}
        inside = core_revenue.Revenue(session, workspace, FULL, year=2026, today=TODAY, side="intra").out("entity")
        assert [(row["title"], row["months"]["2026-10"]["accrued"]) for row in inside["rows"]] == [("BBCA", "30000.00")]


# ── Сверка с «Сальдо Конец» книги ────────────────────────────────────────────


def test_saldo_konec_po_nachisleniyam_i_po_avr(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        _contract(session, workspace, "ТОО Альфаком", "№ОКР/15")
        account = _account(session, workspace)
        book = _book(
            _row(6, "ТОО Альфаком", "№ОКР/15", **_cells(
                opening="50 000", closing="50 000", opening_acts="20 000", act="TRUE", act_sum="100 000",
                act_no="457", act_date="02.07.2026", closing_acts="20 000",
            )),
            _row(7, "ТОО Альфаком", "№ОКР/15", **_cells(closing="150 000", closing_acts="20 000")),
        )
        core_settings.set_payments(session, workspace, FULL, OWNER, since=SINCE)
        opening.apply(session, workspace, FULL, OWNER, book, since=SINCE)
        _income(session, workspace, account, "ТОО Альфаком", "100000", date(2026, 6, 5), month="2026-06")

        same = core_debts.reconcile_saldo(session, workspace, FULL, book, today=TODAY)
        assert same["window"] == ["2026-06", "2026-07"]
        assert same["counts"] == {"contracts": 1, "same": 1, "months": 2, "months_same": 2}
        acts = core_debts.reconcile_saldo(session, workspace, FULL, book, today=TODAY, basis="acts")
        assert acts["counts"]["same"] == 1, "сальдо по АВР: 20 000 + июньский акт - июньская оплата"

        _income(session, workspace, account, "ТОО Альфаком", "30000", date(2026, 7, 20), month="2026-07")
        differ = core_debts.reconcile_saldo(session, workspace, FULL, book, today=TODAY)
        assert differ["counts"]["months_same"] == 1
        line = differ["lines"][0]
        assert line["first"] == "2026-07" and line["diff"] == "-30000.00"
        assert line["months"][-1] == {"month": "2026-07", "kort": "120000.00", "book": "150000.00", "same": False}


# ── Маршрут ──────────────────────────────────────────────────────────────────


@pytest.fixture
def app(finance_db, monkeypatch) -> FastAPI:
    from app.core.config import settings
    from app.finance import audit as audit_module
    from app.finance import auth
    from finance_routes import finance_app

    monkeypatch.setattr(settings, "environment", "test")
    auth._ATTEMPTS.clear()
    audit_module._VIEWS.clear()
    return finance_app()


def test_marshrut_dolgov_prava_na_summu_i_oplaty(app: FastAPI) -> None:
    from test_finance_access import BASE, activate, employee, grant, register

    owner = register(app)
    assert owner.post(f"{BASE}/contracts/setup/entities", json={"name": "BBC", "code": "BBC"}).status_code == 201
    made = owner.post(
        f"{BASE}/contracts",
        json={"values": {"number": "№ОКР/1", "executor": "BBC", "customer": "ТОО Альфаком", "amount": "100000",
                         "billing": "month", "signed_at": "2026-06-01"}},
    )
    assert made.status_code == 201, made.text
    assert owner.put(f"{BASE}/core/payments/settings", json={"since": "2026-06-01"}).status_code == 200
    full = owner.get(f"{BASE}/core/debts", params={"group": "client", "as_of": "2026-07-15"})
    assert full.status_code == 200, full.text
    body = full.json()
    assert body["as_of"] == "2026-07-15" and body["basis"] == "accruals" and body["totals"]["contracts"] == 1
    assert body["side"] == "receivable" and body["sides"]["payable"]["contracts"] == 0
    assert [(row["title"], row["debt"]) for row in body["rows"]] == [("ТОО Альфаком", "200000.00")], "июнь и июль"
    assert {age["key"] for age in body["ages"]} >= {"opening", "d30", "d90plus"}

    # Без отдела - живёт по личным записям (у человека отдела потолок - права отдела).
    person = employee(owner, "Бухгалтерова Айгерим", "+77051130001")
    grant(owner, "employee", person["id"], {"contracts": "view", "journal": "view", "reports.debts": "view",
                                            "contracts.field.amount": "none"})
    reader = activate(app, "+77051130001", ip="10.0.3.1")
    hidden = reader.get(f"{BASE}/core/debts")
    assert hidden.status_code == 403 and "сумма договора" in hidden.json()["detail"]
    grant(owner, "employee", person["id"], {"contracts.field.amount": None})
    assert reader.get(f"{BASE}/core/debts").status_code == 200
    grant(owner, "employee", person["id"], {"reports.debts": "none"})
    assert reader.get(f"{BASE}/core/debts").status_code == 403
    assert reader.get(f"{BASE}/core/debts/shadow").status_code == 403, "сверку ведёт владелец или администратор"
