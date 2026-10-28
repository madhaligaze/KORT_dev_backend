"""API модели (ядро KORT, этап 2, шаг 7): семь вызовов вместо маршрутов под экраны.

Что проверяется - то, ради чего вызовы есть:
* «спросить» даёт те же числа, что экраны («Долги», вид договоров);
* «объяснить» раскладывает каждое число на слагаемые, которые в сумме дают его
  до копейки, и доходит до фактов журнала;
* «предложить» показывает то, что сделает «применить», и не оставляет следа;
* «применить» - пачка одной точкой; откажет одна команда - не записано ничего;
* «отменить» возвращает пачку целиком;
* «подписаться» видит записанное одним курсором;
* версия: клиент другой старшей версии получает отказ, а не чужие поля.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.finance import audit, auth
from app.finance import service as finance_service
from app.finance.contracts import service
from app.finance.core import settings as core_settings
from app.finance.db import finance_session

BASE = "/api/v1/finance"
MODEL = f"{BASE}/model"
PASSWORD = "pass-12345"


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


@pytest.fixture
def owner(app: FastAPI, monkeypatch) -> TestClient:
    """Компания «Тумар (тест)»: юрлицо, два отдела, три договора, оплаты KORT с июня."""
    from app.finance.contracts import setup
    from app.finance.contracts.models import GroupEntity

    monkeypatch.setattr(service, "today", lambda: date(2026, 10, 25))
    person = TestClient(app, headers={"x-client-ip": "10.0.0.9", "user-agent": "pytest-agent"})
    response = person.post(
        f"{BASE}/auth/register",
        json={"email": "owner@tumar.kz", "password": PASSWORD, "company": "Тумар (тест)", "full_name": "Сейткали Арман"},
    )
    assert response.status_code == 201, response.text
    company = uuid.UUID(response.json()["company"]["id"])
    actor = service.Actor(uuid.UUID(response.json()["user"]["id"]), "owner@tumar.kz")
    full = service.Access(view=True, edit=True, setup=True, admin=True)
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, company)
        setup.add_entity(session, workspace, name="Тумар Сервис", code="ТС")
        setup.upsert_department(session, workspace, {"code": "ЮО"})
        setup.upsert_department(session, workspace, {"code": "НО"})
        common = {"executor": "Тумар Сервис", "billing": "month", "amount": "100000", "signed_at": "2026-06-01"}
        service.create(session, workspace, full, actor, {"customer": "ТОО Альфаком", "number": "№1", "department": "ЮО", **common})
        service.create(session, workspace, full, actor, {"customer": "ТОО Бетаком", "number": "№2", "department": "НО", **common})
        service.create(
            session, workspace, full, actor,
            {"customer": "ТОО Гаммаком", "number": "№3", "department": "ЮО", **{**common, "amount": "250000", "signed_at": "2026-08-01"}},
        )
        entity = session.scalar(sa.select(GroupEntity.counterparty_id).where(GroupEntity.code == "ТС", GroupEntity.workspace_id == company))
        account = finance_service.create_account(session, workspace, name="Kaspi ТС", group_entity_id=entity)
        core_settings.set_payments(session, workspace, full, actor, since=date(2026, 6, 1))
        _income(session, workspace, account, "ТОО Альфаком", "100000", date(2026, 6, 5))
        _income(session, workspace, account, "ТОО Альфаком", "150000", date(2026, 8, 12))
        _income(session, workspace, account, "ТОО Бетаком", "300000", date(2026, 7, 3))
    person.company_id = company  # type: ignore[attr-defined]
    return person


def _yuo(owner: TestClient) -> dict:
    """Отбор «отдел - ЮО»: в правиле - id отдела из «описать», как у листа."""
    yuo = next(item["id"] for item in _ok(owner.get(f"{MODEL}/describe"))["departments"] if item["code"] == "ЮО")
    return {"any": [{"all": [{"field": "department", "op": "eq", "value": yuo}]}]}


def _ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def _explain_all(owner: TestClient, ref: str, depth: int = 0) -> int:
    """Раскрыть число и всё, что раскрывается под ним: каждое - точно; возвращает число фактов внизу."""
    found = _ok(owner.get(f"{MODEL}/explain", params={"ref": ref}))
    assert found["exact"], f"{found['title']}: {found['value']} ≠ {found['check']}"
    facts = 0
    for part in found["parts"]:
        if part.get("ref") and depth < 4:
            facts += _explain_all(owner, part["ref"], depth + 1)
        if part.get("fact"):
            facts += 1
        for inner in part.get("parts") or []:
            facts += 1 if inner.get("fact") else 0
    return facts


def test_skhema_i_versiya(owner: TestClient) -> None:
    catalog = _ok(owner.get(MODEL))
    assert catalog["version"].startswith("1.")
    calls = {item["call"] for item in catalog["calls"]}
    assert {"describe", "ask", "explain", "propose", "apply", "undo", "subscribe"} <= calls
    names = {item["name"] for item in catalog["commands"]}
    assert {"contract.update", "contract.create", "contract.adjust", "clients.transfer", "view.save"} <= names
    assert all(item["params"]["type"] == "object" for item in catalog["commands"])

    response = owner.get(f"{MODEL}/describe")
    assert response.headers["X-Kort-Model"] == catalog["version"]
    described = response.json()
    assert {view["title"] for view in described["views"]} >= {"Реестр", "Долги"}
    assert any(field["key"] == "amount" for field in described["fields"])
    assert described["me"]["contracts"]["edit"] and described["cursor"]

    refused = owner.get(f"{MODEL}/describe", headers={"X-Kort-Model": "2.0"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "model_version"
    assert owner.get(f"{MODEL}/describe", headers={"X-Kort-Model": "1.7"}).status_code == 200


def test_sprosit_dolgi_kak_ekran_i_obyasnit_do_faktov(owner: TestClient) -> None:
    screen = _ok(owner.get(f"{BASE}/core/debts", params={"as_of": "2026-10-25", "group": "department"}))
    debts_view = next(view for view in _ok(owner.get(f"{MODEL}/describe"))["views"] if view["source"] == "debts")
    answer = _ok(owner.post(f"{MODEL}/ask", json={"view": debts_view["key"], "as_of": "2026-10-25", "params": {"group": "department"}}))
    assert answer["source"] == "debts" and answer["params"]["group"] == "department"
    assert answer["totals"]["owed"] == screen["totals"]["owed"] and answer["totals"]["debt"] == screen["totals"]["debt"]
    assert [(row["title"], row["owed"]) for row in answer["rows"]] == [(row["title"], row["owed"]) for row in screen["rows"]]
    # Долг Альфакома: июнь-октябрь по 100 000 = 500 000, оплачено 250 000.
    alfa = next(item for row in answer["rows"] for item in row["contracts"] if item["number"] == "№1")
    assert Decimal(alfa["debt"]) == Decimal("250000")

    facts = _explain_all(owner, answer["totals"]["refs"]["owed"])
    assert facts >= 2, "раскрытие итога дошло до оплат"
    debt = _ok(owner.get(f"{MODEL}/explain", params={"ref": alfa["refs"]["debt"]}))
    assert debt["exact"] and debt["formula"].startswith("входящее сальдо")
    payments = [part for part in debt["parts"] if part["sign"] == "-"]
    assert [part["value"] for part in payments] == ["100000.00", "150000.00"]
    assert all(part["fact"]["kind"] == "payment" and part["fact"]["key"] for part in payments)
    accruals = [part for part in debt["parts"] if part["title"].startswith("начислено")]
    assert len(accruals) == 5 and all(part["fact"]["kind"] == "accrual" for part in accruals)

    # Вопрос без сохранения: долги по клиентам только отдела ЮО.
    inline = _ok(owner.post(f"{MODEL}/ask", json={
        "source": "debts", "as_of": "2026-10-25", "params": {"group": "client"}, "filter": _yuo(owner),
    }))
    assert {item["number"] for row in inline["rows"] for item in row["contracts"]} == {"№1", "№3"}
    _explain_all(owner, inline["totals"]["refs"]["debt"])


def test_sprosit_dogovory_merami_i_obyasnit_ostatok(owner: TestClient) -> None:
    answer = _ok(owner.post(f"{MODEL}/ask", json={
        "source": "contracts", "measures": ["summary_paid", "summary_remaining"], "groups": ["people"], "filter": _yuo(owner),
    }))
    keys = [column["key"] for column in answer["columns"]]
    assert keys[:2] == ["number", "client"] and "summary_remaining" in keys
    rows = {row["number"]: row for row in answer["rows"]}
    assert set(rows) == {"№1", "№3"}
    assert rows["№1"]["client"] == "ТОО Альфаком" and rows["№1"]["entity"] == "ТС"
    assert Decimal(rows["№1"]["values"]["summary_paid"]) == Decimal("250000")
    assert Decimal(rows["№1"]["values"]["summary_remaining"]) == Decimal("-150000")
    left = _ok(owner.get(f"{MODEL}/explain", params={"ref": rows["№1"]["refs"]["summary_remaining"]}))
    assert left["exact"] and [part["sign"] for part in left["parts"]] == ["+", "-"]
    paid = _ok(owner.get(f"{MODEL}/explain", params={"ref": left["parts"][1]["ref"]}))
    assert paid["source"] == "kort" and paid["exact"] and all(part["fact"]["kind"] == "payment" for part in paid["parts"])
    _explain_all(owner, answer["totals"]["refs"]["summary_remaining"])
    people = answer["groups"]["people"]
    _explain_all(owner, people["totals"]["refs"]["amount"])
    for row in people["rows"]:
        _explain_all(owner, row["refs"]["amount"])

    bad = owner.get(f"{MODEL}/explain", params={"ref": "не-ссылка"})
    assert bad.status_code == 400 and bad.json()["code"] == "refused"


def _contract(owner: TestClient, number: str) -> dict:
    answer = _ok(owner.post(f"{MODEL}/ask", json={"view": ""}))
    return next(row for row in answer["rows"] if row["number"] == number)


def test_predlozhit_primenit_otmenit(owner: TestClient) -> None:
    alfa = _contract(owner, "№1")
    start = _ok(owner.get(f"{MODEL}/changes"))
    assert start["start"] and start["cursor"]
    command = {"command": "contract.adjust", "params": {"id": alfa["id"], "month": "2026-09", "amount": "40000", "reason": "скидка за сентябрь"}}

    proposal = _ok(owner.post(f"{MODEL}/propose", json={"commands": [command]}))
    assert proposal["ready"] and not proposal["problems"]
    change = next(item for item in proposal["changes"] if item["id"] == alfa["id"])
    assert change["debt"] == {"before": "250000.00", "after": "190000.00"}
    assert any(fact["kind"] == "adjustment" for fact in change["facts"]["added"])
    # Предложение не оставило следа: долг тот же, точки нет, курсор не сдвинулся.
    again = _ok(owner.post(f"{MODEL}/propose", json={"commands": [command]}))
    assert next(item for item in again["changes"] if item["id"] == alfa["id"])["debt"]["before"] == "250000.00"
    assert not _ok(owner.get(f"{MODEL}/points"))["items"]
    assert _ok(owner.get(f"{MODEL}/changes", params={"cursor": start["cursor"]}))["moved"] == {
        "contracts": False, "facts": False, "operations": False, "schema": False,
    }

    applied = _ok(owner.post(f"{MODEL}/apply", json={"commands": [command], "expect": proposal["base"]}))
    assert applied["point"] and applied["commands"][0]["name"] == "contract.adjust"
    after = _ok(owner.get(f"{MODEL}/changes", params={"cursor": start["cursor"]}))
    assert after["moved"]["contracts"] and alfa["id"] in after["contracts"]["changed"]
    assert any(fact["kind"] == "adjustment" for fact in after["facts"])
    debts = _ok(owner.post(f"{MODEL}/ask", json={"source": "debts", "as_of": "2026-10-25"}))
    row = next(item for group in debts["rows"] for item in group["contracts"] if item["number"] == "№1")
    assert row["debt"] == "190000.00"

    # То же предложение после записи - устарело: 409 с новым предложением.
    stale = owner.post(f"{MODEL}/apply", json={"commands": [command], "expect": proposal["base"]})
    assert stale.status_code == 409 and stale.json()["code"] == "changed"
    fresh = stale.json()["proposal"]
    assert fresh["base"] != proposal["base"] and fresh["ready"]
    # Та же поправка уже стоит: новое предложение долга не меняет.
    assert not any("debt" in item for item in fresh["changes"])

    undone = _ok(owner.post(f"{MODEL}/undo", json={"point": applied["point"]["id"]}))
    assert undone["done"] and not undone["skipped"]
    debts = _ok(owner.post(f"{MODEL}/ask", json={"source": "debts", "as_of": "2026-10-25"}))
    row = next(item for group in debts["rows"] for item in group["contracts"] if item["number"] == "№1")
    assert row["debt"] == "250000.00"


def test_pachka_odnoy_tochkoy_i_otkaz_bez_sleda(owner: TestClient) -> None:
    alfa, beta = _contract(owner, "№1"), _contract(owner, "№2")
    batch = [
        {"command": "contract.update", "params": {"id": alfa["id"], "values": {"note": "из холста"}}},
        {"command": "contract.adjust", "params": {"id": beta["id"], "month": "2026-10", "skip": True, "reason": "каникулы"}},
        {"command": "view.save", "params": {"source": "contracts", "title": "Мой вид", "owner": "me", "measures": ["summary_remaining"]}},
    ]
    applied = _ok(owner.post(f"{MODEL}/apply", json={"commands": batch}))
    points = _ok(owner.get(f"{MODEL}/points"))["items"]
    assert len(points) == 1 and points[0]["kind"] == "batch" and points[0]["id"] == applied["point"]["id"]
    assert "значения полей станут прежними" in points[0]["returns"] and "сохранённый вид уйдёт" in points[0]["returns"]
    assert _ok(owner.get(f"{BASE}/contracts/{alfa['id']}"))["contract"]["values"]["note"] == "из холста"
    assert any(view["title"] == "Мой вид" for view in _ok(owner.get(f"{MODEL}/describe"))["views"])

    undone = _ok(owner.post(f"{MODEL}/undo", json={"point": applied["point"]["id"]}))
    assert len(undone["done"]) == 3, undone
    assert not _ok(owner.get(f"{BASE}/contracts/{alfa['id']}"))["contract"]["values"].get("note")
    assert not any(view["title"] == "Мой вид" for view in _ok(owner.get(f"{MODEL}/describe"))["views"])

    # Вторая команда откажет - первая не записана.
    broken = [
        {"command": "contract.update", "params": {"id": alfa["id"], "values": {"note": "не должно остаться"}}},
        {"command": "contract.adjust", "params": {"id": beta["id"], "month": "2026-10", "amount": "-5", "reason": "минус"}},
    ]
    refused = owner.post(f"{MODEL}/apply", json={"commands": broken})
    assert refused.status_code == 400 and refused.json()["command"] == 1, refused.text
    assert not _ok(owner.get(f"{BASE}/contracts/{alfa['id']}"))["contract"]["values"].get("note")
    # «Предложить» ту же пачку: первая показана, вторая - в отказах словами.
    proposal = _ok(owner.post(f"{MODEL}/propose", json={"commands": broken}))
    assert not proposal["ready"] and proposal["problems"][0]["command"] == 1
    assert proposal["commands"][0]["name"] == "contract.update"

    unknown = owner.post(f"{MODEL}/apply", json={"commands": [{"command": "contract.fly", "params": {}}]})
    assert unknown.status_code == 400 and "contract.fly" in unknown.json()["detail"]


def test_novyy_dogovor_povtor_ne_dvoit(owner: TestClient) -> None:
    code = str(uuid.uuid4())
    command = {"command": "contract.create", "params": {"id": code, "values": {"customer": "ТОО Дельтаком", "number": "№4", "amount": "50000"}}}
    first = _ok(owner.post(f"{MODEL}/apply", json={"commands": [command]}))
    second = _ok(owner.post(f"{MODEL}/apply", json={"commands": [command]}))
    assert first["commands"][0]["result"]["contract"]["id"] == code == second["commands"][0]["result"]["contract"]["id"]
    assert second["point"] is None, "повтор ничего не завёл - и вернуть нечего"
    answer = _ok(owner.post(f"{MODEL}/ask", json={"view": ""}))
    assert [row["number"] for row in answer["rows"]].count("№4") == 1


def test_prava_vyzovov(app: FastAPI, owner: TestClient) -> None:
    """Сотрудник с реестром «видит»: спрашивает, но команды договоров ему закрыты; свой вид - можно."""
    from test_finance_access import activate, employee, grant

    card = employee(owner, "Касымов Ерлан", "+7 (701) 555-01-01")
    grant(owner, "employee", card["id"], {"contracts": "view"})
    person = activate(app, "87015550101")
    described = _ok(person.get(f"{MODEL}/describe"))
    assert not described["me"]["contracts"]["edit"]
    assert {item["name"]: item["open"] for item in described["commands"]}["contract.update"] is False
    alfa = next(row for row in _ok(person.post(f"{MODEL}/ask", json={"view": ""}))["rows"] if row["number"] == "№1")
    refused = person.post(f"{MODEL}/apply", json={"commands": [{"command": "contract.update", "params": {"id": alfa["id"], "values": {"note": "x"}}}]})
    assert refused.status_code == 403 and refused.json()["code"] == "forbidden"
    saved = person.post(f"{MODEL}/apply", json={"commands": [{"command": "view.save", "params": {"source": "contracts", "title": "Свой", "owner": "me"}}]})
    assert saved.status_code == 200, saved.text
    # Долги ему закрыты: «спросить» долги - отказ словами.
    denied = person.post(f"{MODEL}/ask", json={"source": "debts"})
    assert denied.status_code == 403


def test_podpiska_s_nomera_reestra(owner: TestClient) -> None:
    """Десктоп продолжает сохранённый реестр: подписка с его номера отдаёт договоры, изменённые после него."""
    registry = _ok(owner.get(f"{BASE}/contracts"))
    alfa = next(item for item in registry["contracts"] if item["values"].get("number") == "№1")
    _ok(owner.patch(f"{BASE}/contracts/{alfa['id']}", json={"values": {"note": "правка до подписки"}, "known_seq": alfa["seq"]}))
    first = _ok(owner.get(f"{MODEL}/changes", params={"contracts_since": registry["seq"], "include": "contracts"}))
    assert not first["start"] and first["contracts"]["changed"] == [alfa["id"]]
    assert first["contracts"]["rows"][0]["values"]["note"] == "правка до подписки"
    quiet = _ok(owner.get(f"{MODEL}/changes", params={"cursor": first["cursor"], "include": "contracts"}))
    assert "contracts" not in quiet and not any(quiet["moved"].values())

