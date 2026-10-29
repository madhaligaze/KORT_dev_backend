"""Сводная таблица файлом: «Переход», «Оплачено (сводка)» и «Счета» без листа Google.

До 30.10.2026 таблицу файлом можно было только сверить с KORT («Сравнить»), а
переключить звено на KORT - лишь у сводной Google: «Переход», «Оплачено» и
«Счета» читали один лист Google, каждый своим разбором. Теперь таблица файлом
становится сводной компании, и её читает один разбор на всех (`core/book.sheet`).
"""
from __future__ import annotations

import base64
import io
from datetime import date
from decimal import Decimal

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.finance import audit, auth
from app.finance.contracts import service
from app.finance.core import book as core_book

BASE = "/api/v1/finance"
MODEL = f"{BASE}/model"
HEADER = ["Месяц", "Наше ТОО", "Клиент", "№ договора", "Сумма", "Период с", "Период по", "Поступило за месяц", "Состояние счёта"]


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
    core_book.forget()

    def google(*_args, **_kwargs):
        raise AssertionError("сводная файлом не ходит в Google")

    monkeypatch.setattr(core_book, "_read", google)
    person = TestClient(finance_app(), headers={"x-client-ip": "10.0.0.9", "user-agent": "pytest-agent"})
    response = person.post(
        f"{BASE}/auth/register",
        json={"email": "owner@fail.kz", "password": "pass-12345", "company": "Файл (тест)", "full_name": "Сериков Данияр"},
    )
    assert response.status_code == 201, response.text
    yield person
    core_book.forget()


def _ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def _table(august_paid: int | None) -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Сводная"
    sheet.append(HEADER)
    sheet.append(["ИЮНЬ 2026", "СВ", "ТОО Арендатор", "А-1", 100000, date(2026, 6, 1), date(2026, 6, 30), 100000, "TRUE"])
    sheet.append(["ИЮЛЬ 2026", "СВ", "ТОО Арендатор", "А-1", 100000, date(2026, 7, 1), date(2026, 7, 31), 100000, "TRUE"])
    sheet.append(["АВГУСТ 2026", "СВ", "ТОО Арендатор", "А-1", 100000, date(2026, 8, 1), date(2026, 8, 31), august_paid, "TRUE"])
    sheet.append(["СЕНТЯБРЬ 2026", "СВ", "ТОО Арендатор", "А-1", 100000, date(2026, 9, 1), date(2026, 9, 30), None,
                  "Сегодня день выставления"])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_svodnaya_fajlom_perehod_oplacheno_scheta(owner: TestClient, monkeypatch) -> None:
    def apply(command: str, **params) -> dict:
        return _ok(owner.post(f"{MODEL}/apply", json={"commands": [{"command": command, "params": params}]}))

    entity = apply("entity.create", name="ТОО Сверка", code="СВ")["commands"][0]["result"]
    contract = apply("contract.create", values={
        "executor": {"id": entity["id"]}, "customer": "ТОО Арендатор", "number": "А-1", "amount": "400000", "billing": "month",
        "signed_at": "2026-06-01",
    })["commands"][0]["result"]["contract"]["id"]

    connected = _ok(owner.post(f"{BASE}/core/intake/summary", files={"file": ("сводная.xlsx", _table(None))}))
    assert connected["kind"] == "file" and connected["file_name"] == "сводная.xlsx" and connected["url"] == ""
    assert connected["problem"] == "" and connected["rows"] == 4 and connected["replaced"] is None

    # Один разбор на всех: «Оплачено», «Счета», сверка и «Переход» дальше не разбирают таблицу заново.
    parsed: list[str] = []
    parse = core_book.parse

    def counted(*args, **kwargs):
        parsed.append(kwargs.get("worksheet", ""))
        return parse(*args, **kwargs)

    monkeypatch.setattr(core_book, "parse", counted)

    paid = _ok(owner.get(f"{BASE}/contracts/summary"))
    assert paid["source"]["kind"] == "file"
    assert paid["contracts"][contract]["state"] == "found" and Decimal(paid["contracts"][contract]["paid"]) == 200000

    bills = _ok(owner.get(f"{BASE}/bills"))
    assert bills["source"]["kind"] == "file" and bills["source"]["url"] == ""
    waiting = [(item["month"], item["source"]) for item in bills["items"]]
    assert waiting == [("СЕНТЯБРЬ 2026", "")], "в списке - счёт, который таблица ещё не выставила"
    card = _ok(owner.get(f"{BASE}/bills/{bills['items'][0]['id']}"))
    assert card["book"] is None and "файл" in card["problem"], "строки в таблице юрлица у файла нет - отказ словами"
    refused = owner.post(f"{BASE}/bills/{bills['items'][0]['id']}", json={"values": {"bill_no": "С-9"}})
    assert refused.status_code == 409 and "Переход" in refused.json()["detail"]

    tally = _ok(owner.get(f"{BASE}/core/switch/payments"))
    assert not tally.get("error"), tally
    assert [item["code"] for item in tally["entities"]] == ["СВ"]
    # «Сравнить» без файла сверяет подключённую сводную.
    compared = _ok(owner.post(f"{MODEL}/compare", json={"links": ["payments"]}))
    assert compared["book"]["title"] == "сводная.xlsx" and compared["book"]["rows"] == 4
    assert parsed == [], "таблица разобрана один раз - при подключении"

    # «Переход» оплат командой модели: «Оплачено» юрлица - из KORT; «Вернуть» - снова из таблицы.
    switched = apply("link.switch", link="payments", entity=entity["id"], source="kort")
    assert switched["commands"][0]["result"]["sources"]["payments"] == {entity["id"]: "kort"}
    assert _ok(owner.get(f"{BASE}/contracts/summary"))["contracts"][contract].get("source") == "kort"
    _ok(owner.post(f"{MODEL}/undo", json={"point": switched["point"]["id"]}))
    after = _ok(owner.get(f"{BASE}/contracts/summary"))["contracts"][contract]
    assert after.get("source") != "kort" and Decimal(after["paid"]) == 200000

    # «Счета» юрлица на KORT: счёт пишется документом KORT, а не в таблицу юрлица, которой у файла нет.
    apply("link.switch", link="bills", entity=entity["id"], source="kort")
    board = _ok(owner.get(f"{BASE}/bills"))
    september = next(item for item in board["items"] if item["month"] == "СЕНТЯБРЬ 2026")
    assert september["source"] == "kort"
    card = _ok(owner.get(f"{BASE}/bills/{september['id']}"))
    assert card["book"]["kort"] is True and card["problem"] == ""

    # Новая версия таблицы - командой модели; «Вернуть» кладёт прежнюю.
    encoded = base64.b64encode(_table(100000)).decode("ascii")
    again = apply("table.connect", name="сводная-2.xlsx", data=encoded)
    assert again["commands"][0]["result"]["file_name"] == "сводная-2.xlsx"
    assert again["commands"][0]["result"]["replaced"] == {"kind": "file", "title": "сводная.xlsx", "file_name": "сводная.xlsx"}
    assert Decimal(_ok(owner.get(f"{BASE}/contracts/summary"))["contracts"][contract]["paid"]) == 300000
    _ok(owner.post(f"{MODEL}/undo", json={"point": again["point"]["id"]}))
    back = _ok(owner.get(f"{BASE}/contracts/summary"))
    assert back["source"]["file_name"] == "сводная.xlsx" and Decimal(back["contracts"][contract]["paid"]) == 200000


def test_svodnaya_fajlom_otmena_otklyuchaet(owner: TestClient) -> None:
    """Первая сводная, положенная командой, - «Вернуть» отключает её: до неё не было никакой."""
    _ok(owner.post(f"{MODEL}/apply", json={"commands": [{"command": "entity.create", "params": {"name": "ТОО Сверка", "code": "СВ"}}]}))
    encoded = base64.b64encode(_table(None)).decode("ascii")
    done = _ok(owner.post(f"{MODEL}/apply", json={"commands": [
        {"command": "table.connect", "params": {"name": "сводная.xlsx", "data": encoded}},
    ]}))
    assert _ok(owner.get(f"{BASE}/contracts/summary"))["source"]["kind"] == "file"
    _ok(owner.post(f"{MODEL}/undo", json={"point": done["point"]["id"]}))
    assert _ok(owner.get(f"{BASE}/contracts/summary"))["source"] is None
