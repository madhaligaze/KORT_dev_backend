"""Журнал действий пишется всегда: обход всех изменяющих маршрутов «Финансов».

Один сценарий проходит каждый POST / PATCH / PUT / DELETE раздела — учёт,
договоры, люди, права, входы — и после каждого проверяет, что в журнале
появилось событие с автором, сеансом, адресом, браузером и видом. Маршрут,
который ничего не пишет, стоит в `ALLOWED` с причиной; новый маршрут без
события и без причины роняет тест.

Почему обход, а не проверка по месту: правка ячейки в листе журнала меняла
операцию молча — событие писали карточка и импорт, а лист забыли, и
«кто поменял сумму» по нему узнать было нельзя. Так забывают всегда в
одном месте; ловит это только полный перебор.
"""
from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.finance import audit, auth
from app.finance.db import finance_session
from app.finance.models import ACTION_CATEGORIES, ActionLog

BASE = "/api/v1/finance"
PASSWORD = "pass-12345"
AGENT = "pytest-agent"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: Изменяющие маршруты без события — и почему.
ALLOWED: dict[tuple[str, str], str] = {
    ("POST", "/auth/phone/start"): "шаг входа: только отвечает, какой экран показать, ничего не меняет",
    ("POST", "/contracts/{contract_id}/amendments/parse"): "читает текст соглашений, в базу не пишет",
    ("POST", "/contracts/setup/views/preview"): "считает договоры под правило листа, в базу не пишет",
    ("POST", "/contracts/imports/{batch_id}/decide"): (
        "решения протокола — черновик разбора; события пишут загрузка файла и заведение"
    ),
    ("PATCH", "/import/batches/{batch_id}/rows/{line}"): (
        "правка черновой строки партии: в учёт не попадает, событие пишет заведение партии"
    ),
    ("POST", "/sheets/preview"): (
        "нужна книга Google; ответ собирает тот же `_preview_response`, что у /import/preview, "
        "и его событие проверено там"
    ),
    ("POST", "/recurrences/materialize"): (
        "пустое продление ничего не меняет и не пишет; с новыми ожиданиями пишет recurrence.materialize"
    ),
    ("PUT", "/looks/{key}"): "личный вид листа — настройка экрана одной учётки, не данные компании",
    ("DELETE", "/looks/{key}"): "сброс личного вида листа — настройка экрана одной учётки, не данные компании",
}


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.core.config import settings
    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    monkeypatch.setattr(settings, "environment", "test")
    auth._ATTEMPTS.clear()
    audit._VIEWS.clear()
    yield engine
    auth._ATTEMPTS.clear()
    audit._VIEWS.clear()


@pytest.fixture
def app(finance_db) -> FastAPI:
    from finance_routes import finance_app

    return finance_app()


def client(app: FastAPI, ip: str = "10.0.0.7") -> TestClient:
    return TestClient(app, headers={"x-client-ip": ip, "user-agent": AGENT})


class Walk:
    """Вызов маршрута и проверка его события."""

    def __init__(self) -> None:
        self.covered: set[tuple[str, str]] = set()
        self.owner_id: uuid.UUID | None = None

    @staticmethod
    def _ids() -> set[uuid.UUID]:
        with finance_session() as session:
            return set(session.scalars(sa.select(ActionLog.id)))

    @staticmethod
    def _rows(ids: set[uuid.UUID]) -> list[dict[str, Any]]:
        if not ids:
            return []
        with finance_session() as session:
            return [
                {
                    "kind": row.kind, "category": row.category, "user_id": row.user_id,
                    "session_id": row.session_id, "ip": row.ip, "user_agent": row.user_agent,
                }
                for row in session.scalars(sa.select(ActionLog).where(ActionLog.id.in_(ids)))
            ]

    def call(
        self,
        who: TestClient,
        method: str,
        template: str,
        *,
        user: Any = "owner",
        session: bool = True,
        ip: str = "10.0.0.7",
        agent: str = AGENT,
        **kwargs: Any,
    ):
        params = {key: kwargs.pop(key) for key in list(kwargs) if "{" + key + "}" in template}
        path = BASE + template.format(**params)
        before = self._ids()
        response = who.request(method, path, **kwargs)
        key = (method, template)
        assert response.status_code < 400, f"{method} {template}: {response.status_code} {response.text}"
        rows = self._rows(self._ids() - before)
        assert rows, f"{method} {template}: событие в журнал не записано"
        expected = self.owner_id if user == "owner" else user
        good = [
            row for row in rows
            if (row["user_id"] is not None if expected == "any" else row["user_id"] == expected)
            and (row["session_id"] is not None) == session
            and row["ip"] == ip
            and row["user_agent"] == agent
            and row["category"] in ACTION_CATEGORIES
        ]
        assert good, f"{method} {template}: у события не тот автор, сеанс или адрес: {rows}"
        self.covered.add(key)
        return response


def _journal_file() -> bytes:
    import openpyxl
    from datetime import date

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Дата платежа", "Сумма", "Со счёта", "На счёт", "Категория", "Контрагент", "Комментарий"])
    sheet.append([date(2026, 8, 3), 150000, None, "Касса", "Выручка", "Клиент", "оплата"])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _section(batch: dict, key: str) -> dict:
    return next(section for section in batch["report"]["sections"] if section["key"] == key)


def test_kazhdyy_izmenyayushchiy_marshrut_pishet_sobytie(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    from finance_routes import api_routes
    from test_finance_contracts_import import _registry_file

    walk = Walk()
    owner = client(app)

    # ── регистрация ─────────────────────────────────────────────────────
    me = walk.call(
        owner, "POST", "/auth/register", user="any",
        json={"email": "owner@bbc.kz", "password": PASSWORD, "company": "BBC (тест)", "full_name": "Ермеков Нурболат"},
    ).json()
    walk.owner_id = uuid.UUID(me["user"]["id"])
    first_company = me["company"]["id"]

    # ── счета и справочники ────────────────────────────────────────────
    accounts = {item["name"]: item["id"] for item in owner.get(f"{BASE}/dictionaries").json()["accounts"]}
    cash = accounts["Касса"]
    reserve = walk.call(owner, "POST", "/accounts", json={"name": "Резерв"}).json()["id"]
    walk.call(owner, "PATCH", "/accounts/{account_id}", account_id=reserve, json={"starting_balance": "1000"})
    walk.call(owner, "PUT", "/accounts/{account_id}/number", account_id=reserve, json={"number": "KZ86125KZT5004100100"})
    category = walk.call(
        owner, "POST", "/dictionaries/{kind}", kind="categories", json={"name": "Аренда офиса", "side": "expense"}
    ).json()["id"]
    walk.call(owner, "PATCH", "/dictionaries/categories/{item_id}/nature", item_id=category, json={"nature": "operating"})
    tag = owner.post(f"{BASE}/dictionaries/tags", json={"name": "срочно"}).json()["id"]
    walk.call(owner, "DELETE", "/dictionaries/{kind}/{item_id}", kind="tags", item_id=tag)

    # ── журнал и лист ───────────────────────────────────────────────────
    operation = walk.call(
        owner, "POST", "/operations",
        json={"kind": "income", "paid_at": "2026-09-01", "amount": "1000", "account_to_id": cash, "comment": "аренда"},
    ).json()["id"]
    walk.call(owner, "PATCH", "/operations/{operation_id}", operation_id=operation, json={"comment": "аренда сентябрь"})
    walk.call(
        owner, "PATCH", "/grid/cell",
        json={"operation_id": operation, "column": "comment", "value": "аренда · из листа"},
    )
    grid_row = walk.call(
        owner, "POST", "/grid/row",
        json={"cells": {"paid_at": "02.09.2026", "amount": "500", "account_to": "Касса", "comment": "строка листа"}},
    ).json()["id"]
    # Правка из листа откатывается, как правка карточки.
    cell_edit = next(
        item for item in owner.get(f"{BASE}/audit", params={"kind": "operation.update"}).json()["items"]
        if item["title"].startswith("правка в таблице")
    )
    assert cell_edit["can_undo"] and cell_edit["before"]["comment"] == "аренда сентябрь"
    walk.call(owner, "POST", "/history/{entry_id}/undo", entry_id=cell_edit["id"])
    created = next(
        item for item in owner.get(f"{BASE}/audit", params={"kind": "operation.create", "entity_id": grid_row}).json()["items"]
    )
    walk.call(owner, "POST", "/audit/{entry_id}/undo", entry_id=created["id"])
    walk.call(owner, "DELETE", "/operations/{operation_id}", operation_id=operation)

    # ── планы, правила, разметка ────────────────────────────────────────
    walk.call(owner, "POST", "/plans", json={"month": "2026-09-01", "side": "income", "amount": "100000"})
    rule = walk.call(
        owner, "POST", "/rules",
        json={"name": "Аренда", "conditions": [{"field": "comment", "op": "contains", "value": "аренда"}],
              "actions": {"category": "Аренда офиса"}},
    ).json()["id"]
    walk.call(owner, "PATCH", "/rules/{rule_id}", rule_id=rule, json={"active": False})
    walk.call(owner, "POST", "/rules/apply", json={})
    walk.call(owner, "DELETE", "/rules/{rule_id}", rule_id=rule)
    walk.call(owner, "POST", "/autotag", json={"groups": [{"side": "expense", "category": "Аренда"}]})

    # ── загрузка выписки ────────────────────────────────────────────────
    batch = walk.call(
        owner, "POST", "/import/preview", files={"file": ("выписка.xlsx", _journal_file(), XLSX)}
    ).json()["batch_id"]
    walk.call(owner, "POST", "/import/batches/{batch_id}/apply", batch_id=batch, json={})

    # ── счета на оплату, повторения ─────────────────────────────────────
    invoice = walk.call(
        owner, "POST", "/invoices",
        json={"issued_at": "2026-09-01", "due_at": "2026-09-15", "lines": [{"title": "Услуга", "price": "1000"}]},
    ).json()["id"]
    walk.call(owner, "POST", "/invoices/{invoice_id}/void", invoice_id=invoice)
    recurrence = walk.call(
        owner, "POST", "/recurrences",
        json={"title": "Аренда", "kind": "expense", "amount": "1000", "start_at": "2026-09-01", "account_from_id": cash},
    ).json()["id"]
    walk.call(owner, "PATCH", "/recurrences/{recurrence_id}", recurrence_id=recurrence, params={"active": "false"})
    walk.call(owner, "DELETE", "/recurrences/{recurrence_id}", recurrence_id=recurrence)

    # ── подключения ─────────────────────────────────────────────────────
    integration = walk.call(
        owner, "POST", "/integrations", json={"slug": "kaspi", "kind": "api", "title": "Kaspi", "account_id": cash}
    ).json()
    token = walk.call(owner, "POST", "/integrations/{integration_id}/token", integration_id=integration["id"]).json()["token"]
    walk.call(owner, "PATCH", "/integrations/{integration_id}", integration_id=integration["id"], params={"state": "active"})
    # Приём по токену: автора-человека нет, подпись — подключение.
    walk.call(
        client(app), "POST", "/integrations/inbox", user=None, session=False, ip="", agent="",
        headers={"x-finance-token": token},
        json={"operations": [{"paid_at": "2026-09-03", "amount": "700", "comment": "из банка"}]},
    )
    walk.call(owner, "DELETE", "/integrations/{integration_id}", integration_id=integration["id"])

    # ── компании ────────────────────────────────────────────────────────
    second = walk.call(owner, "POST", "/auth/companies", json={"title": "Вторая"}).json()["id"]
    walk.call(owner, "POST", "/auth/switch", json={"company_id": second})
    owner.post(f"{BASE}/auth/switch", json={"company_id": first_company})
    walk.call(owner, "PATCH", "/auth/company", json={"title": "BBC (тест)"})

    # ── приглашение по почте (прежние роли) ─────────────────────────────
    invited = walk.call(
        owner, "POST", "/auth/members",
        json={"email": "buh@bbc.kz", "password": "temp-pass-1", "role": "accountant", "full_name": "Бухгалтер Б"},
    ).json()["id"]
    walk.call(owner, "PATCH", "/auth/members/{user_id}", user_id=invited, json={"role": "viewer"})
    walk.call(owner, "DELETE", "/auth/members/{user_id}", user_id=invited)

    # ── свои сеансы, пароль, профиль ────────────────────────────────────
    other = client(app)
    walk.call(other, "POST", "/auth/login", json={"email": "owner@bbc.kz", "password": PASSWORD})
    foreign = next(item for item in owner.get(f"{BASE}/auth/sessions").json()["items"] if not item["current"])
    walk.call(owner, "DELETE", "/auth/sessions/{session_id}", session_id=foreign["id"])
    client(app).post(f"{BASE}/auth/login", json={"email": "owner@bbc.kz", "password": PASSWORD})
    walk.call(owner, "POST", "/auth/sessions/end-others")
    walk.call(owner, "POST", "/auth/password", json={"old_password": PASSWORD, "new_password": "new-pass-123"})
    walk.call(owner, "PATCH", "/auth/profile", json={"phone": "+77770001122"})
    leaving = client(app)
    leaving.post(f"{BASE}/auth/login", json={"email": "owner@bbc.kz", "password": "new-pass-123"})
    walk.call(leaving, "POST", "/auth/logout")

    # ── люди, права, вход по номеру ─────────────────────────────────────
    department = walk.call(owner, "POST", "/people/departments", json={"code": "ЮО", "title": "Юридический отдел"}).json()["id"]
    walk.call(owner, "PATCH", "/people/departments/{department_id}", department_id=department, json={"title": "Юристы"})
    person = walk.call(
        owner, "POST", "/people/employees",
        json={"full_name": "Сейтова Айдана", "phone": "+77025550122", "department_id": department, "access": True},
    ).json()
    person_user = uuid.UUID(person["account"]["user_id"])
    walk.call(owner, "PATCH", "/people/employees/{employee_id}", employee_id=person["id"], json={"job_title": "Юрист"})
    walk.call(owner, "PUT", "/access/{kind}/{subject_id}", kind="employee", subject_id=person["id"],
              json={"changes": {"journal": "view"}})
    phone = client(app, "10.0.0.9")
    assert phone.post(f"{BASE}/auth/phone/start", json={"phone": "+77025550122"}).json() == {"step": "set_password"}
    walk.call(phone, "POST", "/auth/phone/set-password", user=person_user, session=False, ip="10.0.0.9",
              json={"phone": "+77025550122", "password": "secret-123"})
    walk.call(phone, "POST", "/auth/phone/login", user=person_user, ip="10.0.0.9",
              json={"phone": "+77025550122", "password": "secret-123"})
    walk.call(phone, "POST", "/auth/phone/forgot", user=None, session=False, ip="10.0.0.9",
              json={"phone": "+77025550122"})
    request = next(item for item in owner.get(f"{BASE}/notifications").json()["items"] if item["actionable"])
    walk.call(owner, "POST", "/notifications/{notification_id}/resolve", notification_id=request["id"])
    walk.call(owner, "POST", "/people/employees/{employee_id}/end-sessions", employee_id=person["id"])
    walk.call(owner, "POST", "/people/employees/{employee_id}/block", employee_id=person["id"])
    walk.call(owner, "POST", "/people/employees/{employee_id}/unblock", employee_id=person["id"])
    walk.call(owner, "POST", "/people/employees/{employee_id}/reset", employee_id=person["id"])
    helper = owner.post(f"{BASE}/people/employees", json={"full_name": "Помощник Юриста"}).json()
    walk.call(owner, "POST", "/people/employees/{employee_id}/account", employee_id=helper["id"],
              json={"phone": "+77025550123"})
    walk.call(owner, "DELETE", "/people/employees/{employee_id}", employee_id=helper["id"])
    walk.call(owner, "POST", "/audit/view", json={"section": "journal"})

    # ── договоры ────────────────────────────────────────────────────────
    def contract_values(customer: str, number: str, **extra: Any) -> dict:
        return {"values": {"executor": "BBC", "customer": customer, "number": number, "amount": "500000", **extra}}

    first = walk.call(owner, "POST", "/contracts", json=contract_values("ТОО Альфа", "ЮО/141")).json()["contract"]["id"]
    twin = owner.post(f"{BASE}/contracts", json=contract_values("ТОО Бета", "ЮО/141")).json()["contract"]["id"]
    walk.call(owner, "PATCH", "/contracts/{contract_id}", contract_id=first, json={"values": {"note": "звонили"}})
    walk.call(owner, "POST", "/contracts/{contract_id}/acknowledge", contract_id=twin, json={"code": "number_taken"})
    customer = owner.get(f"{BASE}/contracts/{first}").json()["contract"]["values"]["customer"]
    payment = owner.post(
        f"{BASE}/operations",
        json={"kind": "income", "paid_at": "2026-09-02", "amount": "500000", "account_to_id": cash,
              "counterparty_id": customer},
    ).json()["id"]
    walk.call(owner, "POST", "/contracts/{contract_id}/payments", contract_id=first,
              json={"operation_id": payment, "action": "link"})
    dated = owner.patch(
        f"{BASE}/contracts/{first}",
        json={"values": {"amount": "750000"}, "mode": {"kind": "from_date", "effective_from": "2026-01-01"}},
    )
    assert dated.status_code == 200, dated.text
    amendment = owner.get(f"{BASE}/contracts/{first}/amendments").json()["items"][0]["id"]
    walk.call(owner, "DELETE", "/contracts/{contract_id}/amendments/{amendment_id}",
              contract_id=first, amendment_id=amendment)
    with_text = owner.post(
        f"{BASE}/contracts", json=contract_values("ТОО Гамма", "ЮО/150", amendments_text="№ 1 от 23.07.2024")
    ).json()["contract"]["id"]
    pieces = owner.post(f"{BASE}/contracts/{with_text}/amendments/parse").json()["pieces"]
    walk.call(owner, "POST", "/contracts/{contract_id}/amendments/confirm", contract_id=with_text,
              json={"piece": pieces[0]})
    walk.call(owner, "DELETE", "/contracts/{contract_id}", contract_id=twin)
    # Корзина: удалённый договор вернуть, снова удалить и стереть насовсем.
    listed = owner.get(f"{BASE}/trash")
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == twin for item in listed.json()["items"])
    walk.call(owner, "POST", "/trash/{kind}/{item_id}/restore", kind="contract", item_id=twin)
    owner.delete(f"{BASE}/contracts/{twin}")
    walk.call(owner, "DELETE", "/trash/{kind}/{item_id}", kind="contract", item_id=twin)

    # ── настройка реестра ───────────────────────────────────────────────
    field = walk.call(owner, "POST", "/contracts/setup/fields", json={"title": "Ссылка на Битрикс", "type": "url"}).json()["key"]
    walk.call(owner, "PATCH", "/contracts/setup/fields/{key}", key=field, json={"title": "Битрикс"})
    paused = walk.call(owner, "POST", "/contracts/setup/lists/{field_key}", field_key="status",
                       json={"value": "Приостановлен"}).json()["id"]
    pause = owner.post(f"{BASE}/contracts/setup/lists/status", json={"value": "На паузе"}).json()["id"]
    walk.call(owner, "PATCH", "/contracts/setup/values/{value_id}", value_id=paused, json={"value": "Приостановлен."})
    walk.call(owner, "POST", "/contracts/setup/values/merge", json={"keep": paused, "drop": pause})
    entity = walk.call(owner, "POST", "/contracts/setup/entities", json={"name": "BBCL", "code": "BBCL"}).json()["id"]
    walk.call(owner, "PATCH", "/contracts/setup/entities/{party_id}", party_id=entity, json={"full_name": "ТОО BBC Legal"})
    parties = {item["name"]: item["id"] for item in owner.get(f"{BASE}/contracts/parties", params={"q": "ТОО"}).json()["parties"]}
    walk.call(owner, "POST", "/contracts/setup/parties/merge", json={"keep": parties["ТОО Альфа"], "drop": parties["ТОО Бета"]})
    registry_department = walk.call(owner, "POST", "/contracts/setup/departments", json={"code": "НО"}).json()["id"]
    walk.call(owner, "PATCH", "/contracts/setup/departments/{department_id}", department_id=registry_department,
              json={"title": "Налоговый отдел"})
    rule_filter = {"any": [{"all": [{"field": "number", "op": "contains", "value": "ЮО"}]}]}
    assert owner.post(f"{BASE}/contracts/setup/views/preview", json={"filter": rule_filter}).status_code == 200
    view = walk.call(owner, "POST", "/contracts/setup/views",
                     json={"title": "Юристы", "blocks": [{"filter": rule_filter}]}).json()["id"]
    walk.call(owner, "PATCH", "/contracts/setup/views/{view_id}", view_id=view, json={"title": "Юристы ЮО"})

    # ── сводка оплат: книга Google подменена — сеть в тесте не нужна ────
    from app.finance.contracts import summary as summary_module

    monkeypatch.setattr(
        summary_module,
        "_read",
        lambda spreadsheet_id, tab: summary_module.build_index(
            [["Заказчик (Название Фирмы)", "№ Договора", "Сумма Факт Поступ."], ["ТОО Альфа", "№ 1", "1000"]],
            title="Сводка",
            worksheet=tab,
        ),
    )
    walk.call(owner, "POST", "/contracts/setup/summary",
              json={"link": "https://docs.google.com/spreadsheets/d/abc123/edit", "worksheet": "Сводка"})
    walk.call(owner, "DELETE", "/contracts/setup/summary")

    # ── загрузка реестра ────────────────────────────────────────────────
    upload = walk.call(owner, "POST", "/contracts/imports",
                       files={"file": ("реестр.xlsx", _registry_file(), XLSX)}).json()
    columns = {
        column["id"]: {"action": "note"}
        for block in _section(upload, "columns")["items"]
        for column in block["columns"]
        if column["decision"].get("action") == "ask"
    }
    own = [item["key"] for item in _section(upload, "entities")["items"] if item["own"]]
    decided = owner.post(
        f"{BASE}/contracts/imports/{upload['id']}/decide",
        json={"decisions": {"columns": columns, "entities": {"confirmed": True, "own": own}}},
    )
    assert decided.status_code == 200, decided.text
    walk.call(owner, "POST", "/contracts/imports/{batch_id}/apply", batch_id=upload["id"])
    again = owner.post(f"{BASE}/contracts/imports", files={"file": ("реестр.xlsx", _registry_file(), XLSX)}).json()
    walk.call(owner, "POST", "/contracts/imports/{batch_id}/cancel", batch_id=again["id"])

    # ── все изменяющие маршруты пройдены или названы с причиной ─────────
    mutating = {
        (method, full[len(BASE):])
        for full, route in api_routes()
        if full.startswith(BASE)
        for method in route.methods
        if method in ("POST", "PATCH", "PUT", "DELETE")
    }
    # Пустой обход — не «всё покрыто», а слепой тест (FastAPI 0.141).
    assert mutating, "обход не нашёл ни одного изменяющего маршрута"
    missing = sorted(mutating - walk.covered - set(ALLOWED))
    assert not missing, "изменяющие маршруты без проверенного события: " + ", ".join(f"{m} {p}" for m, p in missing)
    stale = sorted(set(ALLOWED) - mutating)
    assert not stale, f"в ALLOWED маршруты, которых больше нет: {stale}"
    assert not (walk.covered & set(ALLOWED)), "маршрут и пишет событие, и стоит в ALLOWED — уберите из списка"
