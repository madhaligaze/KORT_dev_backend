"""«Сегодня» (фаза 8): карточки дел с числами из итогов ядра.

Число карточки - то же, что в разделе, куда она ведёт; делать нечего -
карточка говорит это словами; карточка без права - отказ, а не пустота.
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
from app.finance.contracts import service, setup
from app.finance.contracts.models import GroupEntity
from app.finance.core import debts as core_debts
from app.finance.core import settings as core_settings
from app.finance.core import today as core_today
from app.finance.db import finance_session

FULL = service.Access(view=True, edit=True, setup=True, admin=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
TODAY = date(2026, 10, 25)


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


def test_kartochki_chisla_kak_v_razdelah(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        service.create(
            session, workspace, FULL, OWNER,
            {"executor": "BBC", "customer": "ТОО Альфаком", "number": "№ОКР/1", "amount": "100000", "billing": "month",
             "signed_at": "2026-06-01"},
        )
        entity = session.scalar(sa.select(GroupEntity.counterparty_id).where(GroupEntity.code == "BBC"))
        account = finance_service.create_account(session, workspace, name='Kaspi Юр. ТОО "BBC"', group_entity_id=entity)
        stranger = finance_service.ensure_counterparty(session, workspace.id, "ИП Неизвестный", role="client")
        finance_service.create_operation(
            session, workspace,
            finance_service.OperationInput(kind="income", paid_at=date(2026, 7, 2), amount=Decimal("55000"),
                                           account_to_id=account.id, counterparty_id=stranger.id),
        )
        core_settings.set_payments(session, workspace, FULL, OWNER, since=date(2026, 6, 1))

        card = core_today.debts_card(session, workspace, FULL, today=TODAY)
        same = core_debts.Debts(session, workspace, FULL, day=TODAY).totals()
        assert card["amount"] == same["owed"] == "500000.00", "долг карточки - долг раздела «Долги»"
        assert card["lines"][0] == {"label": "больше 90 дней", "amount": "200000.00", "count": 1}, "июнь и июль"
        assert card["go"]["section"] == "debts" and card["go"]["session"]["core.debts.side"] == "receivable"

        paid = core_today.payments_card(session, workspace, FULL, today=TODAY)
        assert (paid["count"], paid["amount"]) == (1, "55000.00")
        assert paid["go"]["session"]["core.shadow.paid.view"] == "without"

        shadow = core_today.shadow_card(session, workspace, FULL, today=TODAY)
        assert shadow["empty"], "таблица не прочитана - карточка говорит это словами"
        vat = core_today.vat_card(session, workspace, FULL, today=TODAY)
        assert vat["lines"][0]["label"] == "режим НДС не записан" and vat["lines"][0]["names"] == ["BBC"]
        people = core_today.participants_card(session, workspace, FULL, today=TODAY)
        assert people["go"]["section"] == "participants"
        acts = core_today.acts_card(session, workspace, FULL, today=TODAY)
        assert acts["lines"][0]["label"] == "месяц начислен - АВР нет" and acts["lines"][0]["count"] == 4, "июнь - сентябрь"


def test_poisk_dogovora_po_nomeru_kak_ni_pishi(space, audit):
    from app.finance.core import search

    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        for number, customer in (("№ОБО/62", "ТОО Альфаком"), ("№ЮО/620", "ТОО Бетаком"), ("№ЮО/6", "ТОО Гаммаком")):
            service.create(session, workspace, FULL, OWNER, {"executor": "BBC", "customer": customer, "number": number})
        assert [item["number"] for item in search.contracts(session, workspace, FULL, "обо-62")] == ["№ОБО/62"], "другая рука - тот же номер"
        assert [item["number"] for item in search.contracts(session, workspace, FULL, "ЮО/6")] == ["№ЮО/6", "№ЮО/620"], "точный номер первым"
        found = search.contracts(session, workspace, FULL, "бетаком")
        assert [(item["number"], item["by"]) for item in found] == [("№ЮО/620", "client")]
        assert search.contracts(session, workspace, FULL, "ю") == [], "одной буквы мало"
        assert search.contracts(session, workspace, service.Access(), "обо") == [], "закрытые договоры не ищутся"


def test_pustye_razdely_uhodyat_iz_kolonki(space, audit):
    from app.finance.core import usage

    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        empty = set(usage.empty_sections(session, workspace))
        assert {"journal", "cash", "projects", "plan", "recurrences", "rules", "invoices"} <= empty
        account = finance_service.create_account(session, workspace, name="Касса в офисе")
        finance_service.create_operation(
            session, workspace,
            finance_service.OperationInput(kind="income", paid_at=date(2026, 7, 2), amount=Decimal("100"), account_to_id=account.id),
        )
        empty = set(usage.empty_sections(session, workspace))
        assert "journal" not in empty and "cash" not in empty, "одна операция - раздел уже чей-то рабочий"
        assert "projects" in empty and "plan" in empty


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


def test_kartochki_po_pravam(app: FastAPI) -> None:
    from test_finance_access import BASE, activate, employee, grant, register

    owner = register(app)
    assert "projects" in owner.get(f"{BASE}/core/usage").json()["empty"]
    for card in core_today.CARDS:
        response = owner.get(f"{BASE}/core/today/{card}")
        assert response.status_code == 200, (card, response.text)
        assert response.json()["key"] == card
    person = employee(owner, "Юристов Рысбек", "+77051140001")
    grant(owner, "employee", person["id"], {"contracts": "view"})
    lawyer = activate(app, "+77051140001", ip="10.0.4.1")
    assert lawyer.get(f"{BASE}/core/today/participants").status_code == 200
    for card in ("payments", "acts", "shadow"):
        assert lawyer.get(f"{BASE}/core/today/{card}").status_code == 403, card
    assert lawyer.get(f"{BASE}/core/today/debts").status_code == 403, "раздел «Долги» не открыт"
    assert lawyer.get(f"{BASE}/core/today/vat").status_code == 403, "сводка денег не открыта"
