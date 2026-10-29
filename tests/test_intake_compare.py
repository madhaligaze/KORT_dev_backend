"""«Сравнить» - режим приёмника: любая таблица «договор за месяц» против KORT.

Сверка была только у сводной Google, подключённой компанией. Теперь файл той же
формы - помесячные листы отдела, выгрузка другой системы - сверяется теми же
правилами: начисления с предложениями правок условий, оплаты, АВР, счета.
"""
from __future__ import annotations

import base64
import io
import uuid
from datetime import date

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.finance import audit, auth
from app.finance.contracts import service

BASE = "/api/v1/finance"
MODEL = f"{BASE}/model"


@pytest.fixture
def owner(tmp_path, monkeypatch) -> TestClient:
    from finance_routes import finance_app
    from sqlalchemy import create_engine

    from app.core.config import settings
    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(service, "today", lambda: date(2026, 9, 20))
    auth._ATTEMPTS.clear()
    audit._VIEWS.clear()
    person = TestClient(finance_app(), headers={"x-client-ip": "10.0.0.8", "user-agent": "pytest-agent"})
    response = person.post(
        f"{BASE}/auth/register",
        json={"email": "owner@sverka.kz", "password": "pass-12345", "company": "Сверка (тест)", "full_name": "Нурланова Асель"},
    )
    assert response.status_code == 201, response.text
    return person


def _ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def _table(rows: list[list]) -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Начисления"
    sheet.append(["Месяц", "Наше ТОО", "Клиент", "№ договора", "Сумма", "Период с", "Период по", "Поступило за месяц"])
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_tablica_fajlom_sveryaetsya_kak_svodnaya(owner: TestClient) -> None:
    def apply(command: str, **params) -> dict:
        return _ok(owner.post(f"{MODEL}/apply", json={"commands": [{"command": command, "params": params}]}))["commands"][0]["result"]

    entity = apply("entity.create", name="ТОО Сверка", code="СВ")
    apply("contract.create", values={
        "executor": {"id": entity["id"]}, "customer": "ТОО Арендатор", "number": "А-1", "amount": "100000", "billing": "month",
        "signed_at": "2026-06-01",
    })
    data = _table([
        ["ИЮНЬ 2026", "СВ", "ТОО Арендатор", "А-1", 100000, date(2026, 6, 1), date(2026, 6, 30), None],
        ["ИЮЛЬ 2026", "СВ", "ТОО Арендатор", "А-1", 100000, date(2026, 7, 1), date(2026, 7, 31), None],
        ["АВГУСТ 2026", "СВ", "ТОО Арендатор", "А-1", 120000, date(2026, 8, 1), date(2026, 8, 31), None],
        ["АВГУСТ 2026", "СВ", "ТОО Незнакомец", "Н-9", 50000, date(2026, 8, 1), date(2026, 8, 31), None],
    ])
    found = _ok(owner.post(f"{BASE}/core/intake/compare", files={"file": ("начисления.xlsx", data)}))
    assert found["book"]["rows"] == 4 and found["links"] == ["accruals", "payments", "acts", "bills"]
    accruals = found["accruals"]
    assert accruals["window"] == ["2026-06", "2026-07", "2026-08"]
    lines = [(line["month"], line["codes"]) for line in accruals["lines"]]
    assert ("2026-08", ["amount"]) in lines, "август в таблице 120 000, у KORT - 100 000"
    assert any(item["kind"] in ("amount_from", "amount_fix") for item in accruals["proposals"]), "предложение правки суммы"
    assert accruals["unknown"] and accruals["unknown"][0]["number"] == "Н-9", "строка без договора в KORT - «нет в реестре»"

    # То же - вызовом API модели (файл base64), только начисления.
    model = _ok(owner.post(f"{MODEL}/compare", json={
        "name": "начисления.xlsx", "data": base64.b64encode(data).decode("ascii"), "links": ["accruals"],
    }))
    assert model["links"] == ["accruals"] and [line["month"] for line in model["accruals"]["lines"] if line["codes"]] == ["2026-08"]
    # Без файла и без сводной - отказ словами.
    refused = owner.post(f"{MODEL}/compare", json={})
    assert refused.status_code == 400 and "Сводная таблица" in refused.json()["detail"]

    # Договор, которого нет в KORT, - из той же таблицы, тем же правилом, что «Первые шаги» со сводной.
    encoded = base64.b64encode(data).decode("ascii")
    key = accruals["unknown"][0]["key"]
    created = _ok(owner.post(f"{MODEL}/apply", json={"commands": [
        {"command": "table.create", "params": {"name": "начисления.xlsx", "data": encoded, "keys": [key]}},
    ]}))
    contract_id = created["commands"][0]["result"]["created"][0]
    again = _ok(owner.post(f"{MODEL}/compare", json={"name": "начисления.xlsx", "data": encoded, "links": ["accruals"]}))
    assert not again["accruals"]["unknown"], "договор заведён - строк «нет в реестре» больше нет"
    contract = _ok(owner.get(f"{BASE}/contracts/{contract_id}"))["contract"]["values"]
    assert contract["number"] == "Н-9" and contract["amount"] == "50000.00"
    _ok(owner.post(f"{MODEL}/undo", json={"point": created["point"]["id"]}))
    assert _ok(owner.post(f"{MODEL}/compare", json={"name": "начисления.xlsx", "data": encoded, "links": ["accruals"]}))["accruals"]["unknown"]


def test_ne_tablica_mesyacev_otkaz(owner: TestClient) -> None:
    book = openpyxl.Workbook()
    book.active.append(["ФИО", "Телефон"])
    book.active.append(["Касымов Ерлан", "+77010000000"])
    buffer = io.BytesIO()
    book.save(buffer)
    refused = owner.post(f"{BASE}/core/intake/compare", files={"file": ("люди.xlsx", buffer.getvalue())})
    assert refused.status_code == 400 and "договор за месяц" in refused.json()["detail"]
    assert uuid.UUID(int=0)
