"""Виды как данные (ядро KORT, этап 2, шаг 6).

Вид - сохранённый вопрос к ядру: отбор, меры, группировки, форма, владелец.
Колонка разделов собирается из закреплённых видов; «Реестр», «Долги»,
«Разовые ЮО» - строки `views`, а не разделы, зашитые в код. Готово по
странице этапа: у новой компании нет ни слова BBC; у ЮО «Разовые ЮО» с теми
же числами (числа - `test_finance_oneoff_trash.py`).
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI

from app.finance import history
from app.finance import service as finance_service
from app.finance.contracts import service, setup
from app.finance.core import debts as core_debts
from app.finance.core import settings as core_settings
from app.finance.core import views as core_views
from app.finance.db import finance_session
from test_finance_access import BASE, activate, department, employee, grant, register

FULL = service.Access(view=True, edit=True, setup=True, admin=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
ADMIN = core_views.Viewer(user_id=None, admin=True)
#: Слова одной компании, которых у новой быть не должно: её юрлица, её книги,
#: её отдел и сводная таблица. «Разовая услуга» - вид договора отрасли, не BBC.
BBC_WORDS = ("bbc", "битрикс", "bitrix", "разовые", "сводк", "юо", "юротдел")


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
def app(finance_db) -> FastAPI:
    from finance_routes import finance_app

    return finance_app()


@pytest.fixture
def space(finance_db):
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="Тумар Сервис", code="ТС")
        return workspace.id


@pytest.fixture
def audit():
    token = history.CONTEXT.set(history.AuditContext(user_id=OWNER.user_id))
    yield
    history.CONTEXT.reset(token)


def _words(payload) -> list[str]:
    text = json.dumps(payload, ensure_ascii=False).lower()
    return [word for word in BBC_WORDS if word in text]


# ── Новая компания ───────────────────────────────────────────────────────────


def test_novaya_kompaniya_bez_slov_bbc(app: FastAPI) -> None:
    """Готово по странице: у новой компании нет ни слова BBC - ни в схеме
    реестра (поля, написания шапок, значения, листы), ни в видах колонки.
    Компания своя, не «BBC (тест)» общего помощника: её название - наше юрлицо."""
    from test_finance_access import PASSWORD, client

    owner = client(app)
    made = owner.post(
        f"{BASE}/auth/register",
        json={"email": "owner@tumar.kz", "password": PASSWORD, "company": "Тумар (тест)", "full_name": "Сейткали Айгерим"},
    )
    assert made.status_code == 201, made.text
    schema = owner.get(f"{BASE}/contracts/schema")
    assert schema.status_code == 200, schema.text
    schema = schema.json()
    listed = owner.get(f"{BASE}/core/views").json()
    assert [(view["key"], view["title"], view["source"], view["pinned"]) for view in listed["views"]] == [
        ("", "Реестр", "contracts", True),
        ("dolgi", "Долги", "debts", True),
    ]
    # Книга одна - реестр; «Разовых» нет ни листом, ни видом.
    assert {view["book"] for view in schema["views"]} == {""}
    assert [book["key"] for book in schema["books"]] == [""]
    # Меры - не поля договора: колонкой и условием бывают, в полях их нет.
    keys = {field["key"] for field in schema["fields"]}
    assert not keys & {"summary_paid", "summary_remaining", "age_months"}
    assert [item["title"] for item in schema["measures"]] == ["Оплачено", "Остаток", "Срок, мес"]
    assert _words(schema) == [], "в схеме новой компании - слова BBC"
    assert _words(listed["views"]) == [], "в видах новой компании - слова BBC"
    # Написания шапок - тоже: разбор файла узнаёт колонки по ним.
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, uuid.UUID(owner.get(f"{BASE}/auth/me").json()["company"]["id"]))
        fields = service.Registry(session, workspace).fields
        assert _words([list(field.names or []) for field in fields]) == []
    # Шаблон отрасли «Разовые договоры по возрасту долга» предлагается - но не заведён.
    assert "oneoff_age" in {item["key"] for item in listed["templates"]}
    # Без сводной оплаты не ждут таблицы: «Обновить» звать нечего.
    assert owner.get(f"{BASE}/contracts/summary").json()["source"] is None


# ── Кто сохраняет и у кого в колонке ─────────────────────────────────────────


def test_vid_sohranyaet_vladelec_i_kolonka_po_vladeltsu(app: FastAPI) -> None:
    owner = register(app)
    yuo, no = department(owner, "ЮО"), department(owner, "НО")
    head = employee(owner, "Начальникова Жанель", "+77051130001", yuo)
    employee(owner, "Юристов Рысбек", "+77051130002", yuo)
    employee(owner, "Налоговая Дана", "+77051130003", no)
    grant(owner, "department", yuo, {"contracts": {"level": "edit", "scope": {"rows": "department"}}})
    grant(owner, "department", no, {"contracts": "view"})
    grant(owner, "employee", head["id"], {"people": {"level": "edit", "scope": {"rows": "department"}}})
    boss = activate(app, "+77051130001", ip="10.0.2.1")
    first = activate(app, "+77051130002", ip="10.0.2.2")
    other = activate(app, "+77051130003", ip="10.0.2.3")

    def pinned(who) -> dict[str, bool]:
        return {view["title"]: view["pinned"] for view in who.get(f"{BASE}/core/views").json()["views"]}

    # Юрист отделу вид не сохраняет - только себе.
    refused = first.post(f"{BASE}/core/views", json={"template": "oneoff_age", "owner": {"department": yuo}})
    assert refused.status_code == 403, refused.text
    mine = first.post(f"{BASE}/core/views", json={"source": "contracts", "title": "Мои договоры", "owner": "me"})
    assert mine.status_code == 200, mine.text
    assert pinned(first)["Мои договоры"] is True
    assert "Мои договоры" not in pinned(other), "личный вид - только его хозяину"

    # Начальник отдела сохраняет шаблон отрасли отделу: в колонке у людей отдела.
    saved = boss.post(f"{BASE}/core/views", json={"template": "oneoff_age", "owner": {"department": yuo}})
    assert saved.status_code == 200, saved.text
    view = saved.json()
    assert (view["title"], view["pin"], view["owner"]["code"]) == ("Разовые ЮО", "owner", "ЮО")
    assert pinned(first)["Разовые ЮО"] is True
    assert pinned(other)["Разовые ЮО"] is False, "человеку другого отдела - в «Ещё», не в колонке"
    # Чужой вид не правят; администратор ставит его в колонку всем.
    assert first.patch(f"{BASE}/core/views/{view['id']}", json={"pin": "all"}).status_code == 403
    assert owner.patch(f"{BASE}/core/views/{view['id']}", json={"pin": "all"}).status_code == 200
    assert pinned(other)["Разовые ЮО"] is True
    # Листы вида пришли вместе с ним и видны в схеме реестра.
    schema = first.get(f"{BASE}/contracts/schema").json()
    sheets = [sheet["title"] for sheet in schema["views"] if sheet["book"] == view["key"]]
    assert sheets == ["Все", "до 2 мес", "2-3 мес", "3-6 мес", "6+ мес", "Остатки"]
    assert next(book for book in schema["books"] if book["key"] == view["key"])["share_department"] == yuo

    # Реестр не убирается; убранный вид уходит с листами.
    registry_id = next(item["id"] for item in owner.get(f"{BASE}/core/views").json()["views"] if item["key"] == "")
    assert owner.delete(f"{BASE}/core/views/{registry_id}").status_code == 400
    assert boss.delete(f"{BASE}/core/views/{view['id']}").status_code == 200
    schema = owner.get(f"{BASE}/contracts/schema").json()
    assert not [sheet for sheet in schema["views"] if sheet["book"] == view["key"]]


# ── «Долги» видом ────────────────────────────────────────────────────────────


def _income(session, workspace, account, payer: str, amount: str, day: date):
    party = finance_service.ensure_counterparty(session, workspace.id, payer, role="client")
    start = date(day.year, day.month, 1)
    return finance_service.create_operation(
        session, workspace,
        finance_service.OperationInput(
            kind="income", paid_at=day, amount=Decimal(amount), counterparty_id=party.id,
            period_start=start, period_end=start, account_to_id=account.id,
        ),
    )


def test_dolgi_otdela_vidom_i_oplacheno_kort_bez_svodnoy(space, audit):
    """«Долги ЮО» - вид «Долгов» с отбором отдела; у компании без сводной
    «Оплачено» и «Остаток» видов - оплаты KORT, а не пусто."""
    import sqlalchemy as sa

    from app.finance.contracts.models import GroupEntity

    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        yuo = setup.upsert_department(session, workspace, {"code": "ЮО"})
        setup.upsert_department(session, workspace, {"code": "НО"})
        common = {"executor": "Тумар Сервис", "billing": "month", "amount": "100000", "signed_at": "2026-06-01"}
        alfa = service.create(session, workspace, FULL, OWNER, {"customer": "ТОО Альфаком", "number": "№1", "department": "ЮО", **common})
        beta = service.create(session, workspace, FULL, OWNER, {"customer": "ТОО Бетаком", "number": "№2", "department": "НО", **common})
        entity = session.scalar(sa.select(GroupEntity.counterparty_id).where(GroupEntity.code == "ТС"))
        account = finance_service.create_account(session, workspace, name="Kaspi ТС", group_entity_id=entity)
        core_settings.set_payments(session, workspace, FULL, OWNER, since=date(2026, 6, 1))
        _income(session, workspace, account, "ТОО Альфаком", "100000", date(2026, 6, 5))

        saved = core_views.create(session, workspace, ADMIN, "owner@test", {"template": "debts", "owner": {"department": str(yuo.id)}})
        assert saved["title"] == "Долги ЮО" and saved["params"]["group"] == "client"
        view = core_views.find(session, workspace, saved["key"])
        day = date(2026, 10, 25)
        everyone = core_debts.Debts(session, workspace, FULL, day=day).items
        only = core_debts.Debts(session, workspace, FULL, day=day, rule=view.filter).items
        assert {alfa.id, beta.id} <= set(everyone)
        assert set(only) == {alfa.id}, "в «Долгах ЮО» - только договоры ЮО"

        # Оплачено KORT - мера вида у компании без сводной.
        match = service.Registry(session, workspace).summary_of(alfa)
        assert (match.state, match.source, match.paid) == ("found", "kort", Decimal("100000"))
        assert match.months == ("ИЮНЬ 2026",)
