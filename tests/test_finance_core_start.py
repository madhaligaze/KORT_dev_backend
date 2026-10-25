"""Первые шаги (фаза 9): новая компания по своей сводной таблице - от пустого реестра до «Долгов».

Коды «Наша Фирма» становятся юрлицами, однозначные строки без договора -
договорами, начало учёта - последний наступивший месяц с «Сальдо Начало», и
сальдо переносится из таблицы; пачка договоров возвращается точкой
восстановления. Слов BBC нет: коды и клиенты - из таблицы.
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
from app.finance.contracts import restore
from app.finance.contracts import service
from app.finance.contracts.models import Contract, GroupEntity
from app.finance.core import book as book_module
from app.finance.core import onboarding
from app.finance.db import finance_session
from app.finance.models import ActionLog

FULL = service.Access(view=True, edit=True, setup=True, admin=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
TODAY = date(2026, 10, 20)
HEADER = [
    "Мес", "Техн. 1", "Наша Фирма", "Заказчик (Название Фирмы)", "Сумма Договора", "№ Договора",
    "Период с (нач.)", "Период по (зав.)", "Сальдо Начало (без АВР)",
]
ROWS = [
    ["9", "СЕНТЯБРЬ 2026", "НОВА", "ТОО Альфа", "100 000", "А/1", "01.09.2026", "30.09.2026", "20 000"],
    ["10", "ОКТЯБРЬ 2026", "НОВА", "ТОО Альфа", "100 000", "А/1", "01.10.2026", "31.10.2026", "50 000"],
    ["10", "ОКТЯБРЬ 2026", "НОВА", "ТОО Бета", "300 000", "Б/2", "05.10.2026", "04.11.2026", ""],
    ["9", "СЕНТЯБРЬ 2026", "ВТОРАЯ", "ТОО Гамма", "70 000", "Г/3", "01.09.2026", "30.09.2026", ""],
    ["10", "ОКТЯБРЬ 2026", "ВТОРАЯ", "ТОО Гамма", "70 000", "Г/3", "01.10.2026", "31.10.2026", "70 000"],
]


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    book_module.forget()
    yield engine
    book_module.forget()


@pytest.fixture
def space(finance_db):
    with finance_session() as session:
        return finance_service.ensure_workspace(session).id


@pytest.fixture
def audit():
    token = history.CONTEXT.set(history.AuditContext(user_id=OWNER.user_id))
    yield
    history.CONTEXT.reset(token)


def _book() -> book_module.Book:
    return book_module.build_book([HEADER, *ROWS], title="Сводка новой компании", worksheet="Сводка")


def test_ot_tablicy_do_dolgov(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        book = _book()
        first = onboarding.plan(session, workspace, FULL, book, today=TODAY)
        assert [(item["code"], item["rows"], item["id"]) for item in first["entities"]["items"]] == [
            ("НОВА", 3, None), ("ВТОРАЯ", 2, None),
        ]
        assert (first["contracts"]["missing"], first["contracts"]["missing_rows"], first["contracts"]["total"]) == (3, 5, 0)
        assert first["since"] == {"current": None, "proposed": "2026-10-01"}, "последний наступивший месяц с «Сальдо Начало»"
        assert first["debts"] is None, "долг считать не с чего, пока учёт не начат"

        made = onboarding.add_entities(session, workspace, FULL, OWNER, book)
        assert made == {"done": 2, "codes": ["НОВА", "ВТОРАЯ"]}
        assert sorted(code for code in session.scalars(sa.select(GroupEntity.code))) == ["ВТОРАЯ", "НОВА"]
        assert onboarding.add_entities(session, workspace, FULL, OWNER, book)["done"] == 0, "повтор ничего не заводит"

        added = onboarding.add_contracts(session, workspace, FULL, OWNER, book)
        assert added["done"] == 3 and added["point"]
        contracts = {item.number: item for item in session.scalars(sa.select(Contract).where(Contract.deleted_at.is_(None)))}
        assert sorted(contracts) == ["А/1", "Б/2", "Г/3"]
        alpha = contracts["А/1"]
        assert (alpha.billing, alpha.amount, alpha.billing_from, alpha.period_day) == ("month", Decimal("100000"), date(2026, 9, 1), 1)
        nova = session.scalar(sa.select(GroupEntity).where(GroupEntity.code == "НОВА")).counterparty_id
        assert alpha.executor_id == nova and contracts["Б/2"].executor_id == nova
        second = onboarding.plan(session, workspace, FULL, book, today=TODAY)
        assert (second["entities"]["missing"], second["contracts"]["missing"], second["contracts"]["total"]) == (0, 0, 3)

        started = onboarding.start_since(session, workspace, FULL, OWNER, book, today=TODAY)
        assert started["since"] == "2026-10-01" and started["done"] == 2, "сальдо А/1 и Г/3 на 01.10"
        third = onboarding.plan(session, workspace, FULL, book, today=TODAY)
        assert third["since"]["current"] == "2026-10-01" and third["openings"]["new"] == 0
        # Сальдо 50 000 + 70 000 и октябрьские начисления 100 000 + 300 000 + 70 000, оплат ещё нет.
        assert third["debts"] == {"owed": "590000.00", "contracts": 3, "advance": "0.00"}
        kinds = [row.kind for row in session.scalars(sa.select(ActionLog).order_by(ActionLog.at))]
        assert kinds.count("contract.from_book") == 3 and "core.start" in kinds and "core.opening" in kinds

        # Пачку договоров возвращают одной точкой - они уходят в корзину.
        restore.restore_point(session, workspace, FULL, OWNER, uuid.UUID(added["point"]["id"]))
        back = onboarding.plan(session, workspace, FULL, book, today=TODAY)
        assert back["contracts"]["missing"] == 3 and back["contracts"]["total"] == 0


def test_shag_bez_prava_nastroyki(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        with pytest.raises(PermissionError):
            onboarding.plan(session, workspace, service.Access(view=True, edit=True), _book(), today=TODAY)


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


def test_pervye_shagi_po_pravam(app: FastAPI) -> None:
    from test_finance_access import BASE, activate, employee, grant, register

    owner = register(app)
    assert owner.get(f"{BASE}/core/start").json() == {"connected": False, "book": None, "error": ""}
    assert owner.post(f"{BASE}/core/start/nothing").status_code == 400
    person = employee(owner, "Юристов Рысбек", "+77051140001")
    grant(owner, "employee", person["id"], {"contracts": "edit"})
    lawyer = activate(app, "+77051140001", ip="10.0.4.1")
    assert lawyer.get(f"{BASE}/core/start").status_code == 403
    assert lawyer.post(f"{BASE}/core/start/entities").status_code == 403
