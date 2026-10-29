"""Компания руками - только командами API модели, без файлов, 1С и экранов.

Проверяется то, ради чего команды ручного ввода есть:
* компанию можно завести с нуля: наше юрлицо, начало учёта, участник по БИН,
  договор, сальдо, оплата с договором, документы месяца (АВР, счёт);
* итоги считают записанное руками так же, как пришедшее из 1С и выписок:
  долг, «Не выставлены» без сводной таблицы, «объяснить» до фактов;
* деньги - командами: оплата без договора разносится правилом выписки, деление
  оплаты, решение «не по договору»;
* «Отменить» возвращает ручной ввод; права - разделом каждой команды;
* файл (выписка) - командой: вопрос «чей счёт» приходит отказом с вариантами,
  ответ - следующей командой.
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
PASSWORD = "pass-12345"
#: Годный БИН (контрольная цифра сходится).
CLIENT_BIN = "260540028486"


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
        json={"email": "owner@ruki.kz", "password": PASSWORD, "company": "Руки (тест)", "full_name": "Оспанова Дана"},
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


def _debt(client: TestClient, number: str, day: str = "2026-10-25") -> str:
    """Долг договора из «Долгов»; договора без долга и аванса в списке нет - «0.00»."""
    found = _ok(client.post(f"{MODEL}/ask", json={"source": "debts", "as_of": day, "params": {"group": "client"}}))
    for group in found["rows"]:
        for item in group["contracts"]:
            if item["number"] == number:
                return item["debt"]
    return "0.00"


def test_kompaniya_rukami_ot_yurlica_do_dolga(owner: TestClient) -> None:
    entity = _one(owner, "entity.create", name="ТОО Руки", code="РУК")
    _one(owner, "accounting.start", since="2026-09-01")
    client = _one(owner, "participant.create", name='ТОО "Клиент Один"', bin=CLIENT_BIN)
    assert client["created"] and client["bin"] == CLIENT_BIN
    again = _one(owner, "participant.create", name="Клиент Один ТОО", bin=CLIENT_BIN)
    assert again["id"] == client["id"] and not again["created"], "тот же БИН - тот же участник, имя - написанием"
    made = _one(
        owner, "contract.create",
        values={"executor": {"id": entity["id"]}, "customer": {"id": client["id"]}, "number": "Р-1", "amount": "100000",
                "billing": "month", "signed_at": "2026-09-01"},
    )
    contract = made["contract"]["id"]
    _one(owner, "opening.set", contract=contract, amount="50000")
    paid = _one(owner, "payment.record", day="2026-09-05", amount="100000", contract=contract, month="2026-09", key="p-1")
    assert paid["created"] and paid["account"] == "Счёт ТОО Руки", "счёта у юрлица не было - заведён один, на его имя"
    assert not _one(owner, "payment.record", day="2026-09-05", amount="100000", contract=contract, key="p-1")["created"], (
        "тот же код клиента - вторая оплата не заводится"
    )
    act = _one(owner, "document.record", contract=contract, kind="act", month="2026-09", number="1", day="2026-09-30",
               signed="2026-10-02")
    assert act["amount"] == "100000.00", "сумма АВР - начисление месяца"
    _one(owner, "document.record", contract=contract, kind="bill", month="2026-10", number="15", day="2026-10-01")

    # Долг: сальдо 50 000 + сентябрь и октябрь по 100 000 - оплата 100 000.
    assert _debt(owner, "Р-1") == "150000.00"
    debts = _ok(owner.post(f"{MODEL}/ask", json={"source": "debts", "as_of": "2026-10-25", "params": {"group": "client"}}))
    ref = next(item["refs"]["debt"] for group in debts["rows"] for item in group["contracts"] if item["number"] == "Р-1")
    explained = _ok(owner.get(f"{MODEL}/explain", params={"ref": ref}))
    assert explained["exact"] and [part["title"] for part in explained["parts"]][0] == "входящее сальдо"
    assert any(part.get("fact", {}).get("kind") == "payment" for part in explained["parts"]), "оплата - факт журнала"

    # «Не выставлены» без сводной: сентябрь - счёта нет, октябрь - счёт KORT.
    unbilled = _ok(owner.post(f"{MODEL}/ask", json={"source": "unbilled"}))
    assert unbilled["board"]["source"] == "kort"
    assert [(row["number"], row["month"]) for row in unbilled["rows"]] == [("Р-1", "СЕНТЯБРЬ 2026")]
    assert [(row["number"], row["bill_no"]) for row in unbilled["done"]] == [("Р-1", "15")]
    bill_ref = unbilled["rows"][0]["refs"]["amount"]
    assert _ok(owner.get(f"{MODEL}/explain", params={"ref": bill_ref}))["value"] == "100000.00"

    # АВР руками - долг по актам: акты сентября и октября по 100 000 - оплата 100 000.
    _one(owner, "document.record", contract=contract, kind="act", month="2026-10", number="2", day="2026-10-20")
    by_acts = _ok(owner.post(f"{MODEL}/ask", json={"source": "debts", "as_of": "2026-10-25", "params": {"basis": "acts"}}))
    row = next(item for group in by_acts["rows"] for item in group["contracts"] if item["number"] == "Р-1")
    assert row["debt"] == "100000.00"


def test_dengi_komandami_razneseniye_delenie_i_otmena(owner: TestClient) -> None:
    entity = _one(owner, "entity.create", name="ТОО Руки", code="РУК")
    _one(owner, "accounting.start", since="2026-10-01")
    values = {"executor": {"id": entity["id"]}, "customer": "ТОО Клиент Два", "billing": "month", "signed_at": "2026-10-01"}
    first = _one(owner, "contract.create", values={**values, "number": "Д-1", "amount": "100000"})["contract"]["id"]
    second = _one(owner, "contract.create", values={**values, "number": "Д-2", "amount": "50000"})["contract"]["id"]
    # Без договора: плательщик - клиент двух договоров, сумма - обоих вместе: спор.
    stray = _one(owner, "payment.record", day="2026-10-05", amount="150000", entity=entity["id"], party="ТОО Клиент Два")
    assert _debt(owner, "Д-1") == "100000.00" and _debt(owner, "Д-2") == "50000.00"
    split = _apply(owner, {"command": "payment.split", "params": {
        "operation": stray["operation_id"], "parts": [{"contract": first, "amount": "100000"}, {"contract": second, "amount": "50000"}],
    }})
    assert _debt(owner, "Д-1") == "0.00" and _debt(owner, "Д-2") == "0.00"
    # «Отменить» возвращает деление - снова спор.
    _ok(owner.post(f"{MODEL}/undo", json={"point": split["point"]["id"]}))
    assert _debt(owner, "Д-1") == "100000.00"
    # Решение «к договору» и его отмена.
    linked = _apply(owner, {"command": "payment.decide", "params": {"operation": stray["operation_id"], "action": "link", "contract": first}})
    assert _debt(owner, "Д-1") == "-50000.00", "150 000 на договор в 100 000 - аванс 50 000"
    _ok(owner.post(f"{MODEL}/undo", json={"point": linked["point"]["id"]}))
    assert _debt(owner, "Д-1") == "100000.00"
    # Оплата командой убирается «Отменить» целиком.
    paid = _apply(owner, {"command": "payment.record", "params": {"day": "2026-10-06", "amount": "50000", "contract": second}})
    assert _debt(owner, "Д-2") == "0.00"
    _ok(owner.post(f"{MODEL}/undo", json={"point": paid["point"]["id"]}))
    assert _debt(owner, "Д-2") == "50000.00"


def test_predlozhit_ruchnoy_vvod_bez_sleda_i_prava(owner: TestClient) -> None:
    entity = _one(owner, "entity.create", name="ТОО Руки", code="РУК")
    proposal = _ok(owner.post(f"{MODEL}/propose", json={"commands": [
        {"command": "participant.create", "params": {"name": "ТОО Третий"}},
        {"command": "payment.record", "params": {"day": "2026-10-05", "amount": "1000", "entity": entity["id"], "party": "ТОО Третий"}},
    ]}))
    assert proposal["ready"] and len(proposal["commands"]) == 2
    described = _ok(owner.get(f"{MODEL}/describe"))
    assert not any(item["name"] == "ТОО Третий" for item in described.get("entities", [])), "предложение не записывает"
    assert {item["name"] for item in described["commands"] if item["open"]} >= {"payment.record", "document.record", "entity.create"}
    found = _ok(owner.get(f"{BASE}/core/participants", params={"q": "Третий"}))
    assert not found.get("items") and not found.get("rows"), "участник из предложения не заведён"
    bad = owner.post(f"{MODEL}/apply", json={"commands": [{"command": "payment.record", "params": {"day": "2026-10-05", "amount": "-5", "entity": entity["id"]}}]})
    assert bad.status_code == 400 and bad.json()["command"] == 0


def _statement(rows: list[list]) -> str:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Дата платежа", "Сумма", "Со счёта", "На счёт", "Категория", "Контрагент", "Комментарий"])
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def test_vypiska_komandoy_vopros_chey_schet_i_otvet(owner: TestClient) -> None:
    entity = _one(owner, "entity.create", name="ТОО Руки", code="РУК")
    _one(owner, "accounting.start", since="2026-10-01")
    _one(owner, "contract.create", values={
        "executor": {"id": entity["id"]}, "customer": "ТОО Клиент Три", "number": "В-1", "amount": "70000", "billing": "month",
        "signed_at": "2026-10-01",
    })
    data = _statement([[date(2026, 10, 7), 70000, None, "Kaspi Руки", "Выручка", "ТОО Клиент Три", "оплата за октябрь"]])
    asked = owner.post(f"{MODEL}/apply", json={"commands": [{"command": "statement.apply", "params": {"name": "выписка.xlsx", "data": data}}]})
    assert asked.status_code == 422, asked.text
    assert asked.json()["code"] == "question" and asked.json()["question"]["names"] == ["Kaspi Руки"]
    done = _apply(owner, {"command": "statement.apply", "params": {
        "name": "выписка.xlsx", "data": data, "accounts": {"Kaspi Руки": entity["id"]},
    }})
    assert done["commands"][0]["result"]["imported"] == 1
    assert _debt(owner, "В-1") == "0.00", "оплата из выписки разнесена правилом выписки на единственный договор клиента"
    _ok(owner.post(f"{MODEL}/undo", json={"point": done["point"]["id"]}))
    assert _debt(owner, "В-1") == "70000.00", "«Отменить» убирает выписку целиком"


def test_prava_komand_po_razdelu(owner: TestClient) -> None:
    from test_finance_access import activate, employee, grant

    entity = _one(owner, "entity.create", name="ТОО Руки", code="РУК")
    person = employee(owner, "Бухгалтерова Айгерим", "+77051130009")
    grant(owner, "employee", person["id"], {"contracts": "edit"})
    clerk = activate(owner.app, "+77051130009", ip="10.0.3.9")
    denied = clerk.post(f"{MODEL}/apply", json={"commands": [{"command": "payment.record", "params": {
        "day": "2026-10-05", "amount": "1000", "entity": entity["id"],
    }}]})
    assert denied.status_code == 403 and "Журнал" in denied.json()["detail"]
    setup_only = clerk.post(f"{MODEL}/apply", json={"commands": [{"command": "accounting.start", "params": {"since": "2026-10-01"}}]})
    assert setup_only.status_code == 403
    described = _ok(clerk.get(f"{MODEL}/describe"))
    opened = {item["name"]: item["open"] for item in described["commands"]}
    assert opened["contract.create"] and not opened["payment.record"] and not opened["entity.create"]
    assert uuid.UUID(entity["id"])
