"""Формулы компании (этап 3, решение владельца 31.10.2026): мера, которую компания заводит сама.

Проверяется то, ради чего формулы есть:
* формула - команда `formula.save`: язык - меры договора, числа и четыре
  действия; неверная - отказ словами, ничего не записано;
* «спросить» вида договоров отдаёт её значение у каждого договора, рядом -
  ссылка «объяснить»; деление на ноль - пусто, а не 0;
* «объяснить» раскрывает её до мер, у мер - свои ссылки;
* вид с формулой сохраняется и отвечает ею; «Вернуть» и `formula.remove`
  убирают её; заводит владелец или администратор.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.finance import audit, auth
from app.finance.contracts import service

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
def owner(finance_db, monkeypatch) -> TestClient:
    from finance_routes import finance_app

    from app.finance.model import ask as model_ask

    monkeypatch.setattr(service, "today", lambda: date(2026, 10, 25))
    monkeypatch.setattr(model_ask, "today", lambda: date(2026, 10, 25))
    person = TestClient(finance_app(), headers={"x-client-ip": "10.0.0.7", "user-agent": "pytest-agent"})
    response = person.post(
        f"{BASE}/auth/register",
        json={"email": "owner@formula.kz", "password": PASSWORD, "company": "Формулы (тест)", "full_name": "Ахметова Сауле"},
    )
    assert response.status_code == 201, response.text
    return person


def _ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def _apply(client: TestClient, *commands: dict) -> dict:
    return _ok(client.post(f"{MODEL}/apply", json={"commands": list(commands)}))


def _one(client: TestClient, command: str, **params) -> dict:
    return _apply(client, {"command": command, "params": params})["commands"][0]["result"]


def _company(client: TestClient) -> tuple[str, str]:
    entity = _one(client, "entity.create", name="ТОО Формула", code="ФМ")
    _one(client, "accounting.start", since="2026-10-01")
    values = {"executor": {"id": entity["id"]}, "customer": "ТОО Клиент", "billing": "total", "signed_at": "2026-10-01"}
    paid = _one(client, "contract.create", values={**values, "number": "Ф-1", "amount": "200000"})["contract"]["id"]
    free = _one(client, "contract.create", values={**values, "number": "Ф-2", "amount": "0"})["contract"]["id"]
    _one(client, "payment.record", day="2026-10-05", amount="50000", contract=paid, key="p-1")
    return paid, free


def _rows(client: TestClient, *measures: str) -> dict[str, dict]:
    answer = _ok(client.post(f"{MODEL}/ask", json={"source": "contracts", "measures": list(measures)}))
    return {row["number"]: row for row in answer["rows"]}, answer["columns"]


def test_formula_v_spravke_i_obyasnit(owner: TestClient) -> None:
    _company(owner)
    made = _one(owner, "formula.save", title="Оплачено, %", text="round(paid / amount * 100, 1)", type="percent", digits=1)
    assert made["key"].startswith("f_") and made["uses"] == ["amount", "paid"]

    described = _ok(owner.get(f"{MODEL}/describe"))
    assert [item["title"] for item in described["formulas"]["items"]] == ["Оплачено, %"]
    assert any(item["key"] == made["key"] and item.get("formula") for item in described["measures"]), "формула - мера вида"

    rows, columns = _rows(owner, "summary_paid", made["key"])
    column = next(item for item in columns if item["key"] == made["key"])
    assert (column["title"], column["type"], column["formula"]) == ("Оплачено, %", "percent", "round(paid / amount * 100, 1)")
    assert rows["Ф-1"]["values"][made["key"]] == "25.0", "50 000 из 200 000"
    assert rows["Ф-2"]["values"][made["key"]] is None and made["key"] not in rows["Ф-2"]["refs"], "деление на ноль - пусто"

    explained = _ok(owner.get(f"{MODEL}/explain", params={"ref": rows["Ф-1"]["refs"][made["key"]]}))
    assert explained["value"] == "25.0" and explained["exact"]
    assert explained["formula"] == "round(оплачено / сумма * 100, 1)"
    parts = {part["name"]: part for part in explained["parts"]}
    assert parts["amount"]["value"] == "200000.00" and parts["paid"]["value"] == "50000.00"
    paid = _ok(owner.get(f"{MODEL}/explain", params={"ref": parts["paid"]["ref"]}))
    assert paid["value"] == "50000.00", "мера формулы раскрывается своей ссылкой - до оплат"


def test_vid_s_formuloy_vernut_i_ubrat(owner: TestClient) -> None:
    _company(owner)
    saved = _apply(owner, {"command": "formula.save", "params": {"title": "Остаток, тыс", "text": "remaining / 1000", "type": "number", "digits": 0}})
    key = saved["commands"][0]["result"]["key"]
    rule = {"any": [{"all": [{"field": "number", "op": "contains", "value": "Ф-"}]}]}
    view = _one(owner, "view.save", title="Деньги по договорам", source="contracts", filter=rule,
                measures=["summary_remaining", key])["view"]
    answer = _ok(owner.post(f"{MODEL}/ask", json={"view": view["key"]}))
    by_number = {row["number"]: row for row in answer["rows"]}
    assert by_number["Ф-1"]["values"][key] == "150", "остаток 150 000 - 150 тыс"

    # «Вернуть» заведение - формулы нет, вид её больше не отдаёт.
    _ok(owner.post(f"{MODEL}/undo", json={"point": saved["point"]["id"]}))
    assert _ok(owner.get(f"{MODEL}/describe"))["formulas"]["items"] == []
    again = _ok(owner.post(f"{MODEL}/ask", json={"view": view["key"]}))
    assert all(key not in row["values"] for row in again["rows"])

    # Заново - и убрать командой; «Вернуть» убирание - снова на месте.
    key = _one(owner, "formula.save", title="Остаток, тыс", text="remaining / 1000")["key"]
    removed = _apply(owner, {"command": "formula.remove", "params": {"key": key}})
    assert _ok(owner.get(f"{MODEL}/describe"))["formulas"]["items"] == []
    _ok(owner.post(f"{MODEL}/undo", json={"point": removed["point"]["id"]}))
    assert [item["key"] for item in _ok(owner.get(f"{MODEL}/describe"))["formulas"]["items"]] == [key]


def test_nevernaya_formula_otkaz_slovami(owner: TestClient) -> None:
    for text, words in (
        ("paid ** 2", "Действия - только + - * /"),
        ("__import__('os')", "Функции - только"),
        ("profit / amount", "«profit» в формуле нет"),
        ("paid / ", "не читается"),
    ):
        response = owner.post(f"{MODEL}/apply", json={"commands": [{"command": "formula.save", "params": {"title": "Т", "text": text}}]})
        assert response.status_code == 400, response.text
        assert words in response.json()["detail"], (text, response.json())
    assert _ok(owner.get(f"{MODEL}/describe"))["formulas"]["items"] == [], "отказ ничего не записал"
    _one(owner, "formula.save", title="Доля", text="paid / amount")
    twin = owner.post(f"{MODEL}/apply", json={"commands": [{"command": "formula.save", "params": {"title": "доля", "text": "paid"}}]})
    assert twin.status_code == 400 and "уже есть" in twin.json()["detail"]


def test_formulu_zavodit_vladelets(owner: TestClient) -> None:
    from app.finance.contracts.service import Access
    from app.finance.core import formulas
    from app.finance.core import views as views_module
    from app.finance.db import finance_session
    from app.finance.model import apply as apply_module
    from app.finance.model.context import Ctx
    from app.finance.models import Workspace
    import sqlalchemy as sa

    with finance_session() as session:
        workspace = session.scalars(sa.select(Workspace)).one()
        editor = Ctx(
            session=session, workspace=workspace,
            access=Access(view=True, edit=True, setup=False, admin=False, full_contracts=True, user_id=None),
            viewer=views_module.Viewer(user_id=None, admin=False), actor=service.Actor(None, "редактор"),
            levels={"contracts": "edit"},
        )
        with pytest.raises(apply_module.Failed) as failed:
            apply_module.apply(editor, [{"command": "formula.save", "params": {"title": "Т", "text": "paid"}}])
        assert isinstance(failed.value.__cause__, PermissionError)
        assert formulas.of(session, workspace.id) == []


def test_formula_kolonkoy_lista(owner: TestClient) -> None:
    """Формула - мера схемы реестра: колонкой листа она сохраняется, без формулы - уходит из листа."""
    _company(owner)
    key = _one(owner, "formula.save", title="Оплачено, %", text="paid / amount * 100", type="percent", digits=1)["key"]
    schema = _ok(owner.get(f"{BASE}/contracts/schema"))
    measure = next(item for item in schema["measures"] if item["key"] == key)
    assert (measure["formula"], measure["kind"], measure["type"], measure["digits"]) == ("paid / amount * 100", "percent", "number", 1)
    view = schema["views"][0]
    blocks = [{**block, "columns": [*block["columns"], {"key": key, "label": "", "width": None}]} for block in view["blocks"]]
    _ok(owner.patch(f"{BASE}/contracts/setup/views/{view['id']}", json={"blocks": blocks}))
    placed = next(item for item in _ok(owner.get(f"{BASE}/contracts/schema"))["views"] if item["id"] == view["id"])
    assert all(any(column["key"] == key for column in block["columns"]) for block in placed["blocks"]), "колонка формулы сохранилась"
    # Формулу убрали - колонка, сохранённая заново, уже не проходит.
    _one(owner, "formula.remove", key=key)
    _ok(owner.patch(f"{BASE}/contracts/setup/views/{view['id']}", json={"blocks": blocks}))
    after = next(item for item in _ok(owner.get(f"{BASE}/contracts/schema"))["views"] if item["id"] == view["id"])
    assert not any(column["key"] == key for block in after["blocks"] for column in block["columns"])


def test_formula_v_vygruzke_excel(owner: TestClient) -> None:
    """Колонка формулы в выгрузке листа - то же число, что «спросить», а не пустая ячейка."""
    import io

    import openpyxl

    _company(owner)
    key = _one(owner, "formula.save", title="Оплачено, %", text="paid / amount * 100", type="percent", digits=1)["key"]
    view = _ok(owner.get(f"{BASE}/contracts/schema"))["views"][0]
    blocks = [{**block, "columns": [{"key": "number", "label": "", "width": None}, {"key": key, "label": "", "width": None}]}
              for block in view["blocks"]]
    _ok(owner.patch(f"{BASE}/contracts/setup/views/{view['id']}", json={"blocks": blocks}))
    response = owner.get(f"{BASE}/contracts/export.xlsx", params={"views": view["key"]})
    assert response.status_code == 200, response.text
    sheet = openpyxl.load_workbook(io.BytesIO(response.content)).active
    rows = {str(row[0]): row[1] for row in sheet.iter_rows(values_only=True) if row and row[0] in ("Ф-1", "Ф-2")}
    assert rows == {"Ф-1": 25.0, "Ф-2": None}, rows
