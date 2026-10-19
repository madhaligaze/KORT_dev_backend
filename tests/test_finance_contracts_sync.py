"""Шлюз сверки `POST /contracts/sync` (29.09.2026).

Коллега со своим Claude и доступом к книгам Google актуализирует реестр, не
зная устройства KORT: строки с номером, заказчиком и значениями по
названиям колонок. Проверяется то, на чём сверка вручную чуть не ошиблась бы:
не угадывать при одинаковых номерах, не затирать заполненное без явного
«all», не стирать пустым, писать правки в историю договора.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.finance import audit, auth

BASE = "/api/v1/finance"


@pytest.fixture
def owner(tmp_path, monkeypatch) -> TestClient:
    from sqlalchemy import create_engine

    from app.core.config import settings
    from app.finance import db as finance_db_module
    from finance_routes import finance_app

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    monkeypatch.setattr(settings, "environment", "test")
    auth._ATTEMPTS.clear()
    audit._VIEWS.clear()
    app: FastAPI = finance_app()
    client = TestClient(app)
    answer = client.post(
        f"{BASE}/auth/register",
        json={"email": "boss@bbc.kz", "password": "pass-12345", "company": "BBC", "full_name": "Ермеков Нурболат"},
    )
    assert answer.status_code == 201, answer.text
    for values in (
        {"executor": "BBC", "customer": "ТОО Альфа", "number": "ЮО/1", "amount": "500000", "status": "Действующий"},
        {"executor": "BBC", "customer": "ТОО Бета", "number": "ЮО/2", "amount": "100000"},
        {"executor": "BBC", "customer": "ТОО Гамма", "number": "ЮО/2", "amount": "200000"},
    ):
        made = client.post(f"{BASE}/contracts", json={"values": values})
        assert made.status_code == 201, made.text
    return client


def _sync(client: TestClient, rows, **extra):
    answer = client.post(f"{BASE}/contracts/sync", json={"source": "тест", "rows": rows, **extra})
    assert answer.status_code == 200, answer.text
    return answer.json()


def _field(line, key):
    return next(item for item in line["fields"] if item["key"] == key)


def test_probnyy_progon_nichego_ne_pishet_i_govorit_chto_budet(owner: TestClient) -> None:
    row = {"number": "№ ЮО/1", "values": {"Текущее состояние": "Исполнен", "Примечания": "из книги", "Сумма Договора": "500 000"}}
    report = _sync(owner, [row])
    line = report["rows"][0]
    assert report["dry_run"] is True and line["status"] == "matched"
    assert _field(line, "status")["action"] == "conflict"  # в KORT «Действующий» - не трогаем без «all»
    assert _field(line, "note")["action"] == "fill"
    assert _field(line, "amount")["action"] == "same"
    contract = owner.get(f"{BASE}/contracts/{line['contract_id']}").json()["contract"]
    assert not contract["values"].get("note")


def test_primenenie_zapolnyaet_pustoe_a_rashozhdenie_tolko_s_all(owner: TestClient) -> None:
    row = {"number": "ЮО/1", "values": {"Текущее состояние": "Исполнен", "Примечания": "из книги"}}
    report = _sync(owner, [row], apply=True)
    line = report["rows"][0]
    assert line["applied"] is True
    one = owner.get(f"{BASE}/contracts/{line['contract_id']}").json()["contract"]
    schema = owner.get(f"{BASE}/contracts/schema").json()
    status = {item["id"]: item["value"] for item in schema["lists"]["status"]}
    assert one["values"]["note"] == "из книги"
    assert status[one["values"]["status"]] == "Действующий"
    # Повтор - всё «совпадает», ничего не применяется.
    again = _sync(owner, [row], apply=True)
    assert again["summary"]["fill"] == 0 and again["summary"]["applied"] == 0
    # С «all» - расходящееся меняется, и в истории договора видно, что и как.
    changed = _sync(owner, [row], apply=True, overwrite="all")
    assert changed["summary"]["change"] == 1 and changed["summary"]["applied"] == 1
    one = owner.get(f"{BASE}/contracts/{line['contract_id']}").json()["contract"]
    assert status[one["values"]["status"]] == "Исполнен"
    titles = [item["title"] for item in owner.get(f"{BASE}/contracts/{line['contract_id']}/history").json()["items"]]
    assert any("Действующий → Исполнен" in title for title in titles), titles


def test_pustoe_v_istochnike_ne_stiraet(owner: TestClient) -> None:
    _sync(owner, [{"number": "ЮО/1", "values": {"Примечания": "было"}}], apply=True)
    report = _sync(owner, [{"number": "ЮО/1", "values": {"Примечания": ""}}], apply=True, overwrite="all")
    assert _field(report["rows"][0], "note")["action"] == "keep"
    one = owner.get(f"{BASE}/contracts/{report['rows'][0]['contract_id']}").json()["contract"]
    assert one["values"]["note"] == "было"


def test_odinakovye_nomera_bez_zakazchika_ne_ugadyvaem(owner: TestClient) -> None:
    report = _sync(owner, [{"number": "ЮО/2", "values": {"Примечания": "x"}}])
    assert report["rows"][0]["status"] == "ambiguous" and len(report["rows"][0]["candidates"]) == 2
    report = _sync(owner, [{"number": "ЮО/2", "customer": "ТОО «Гамма»", "values": {"Примечания": "x"}}])
    assert report["rows"][0]["status"] == "matched"


def test_sverka_vidit_tolko_otkrytye_dogovory(owner: TestClient) -> None:
    """01.10.2026: пробный прогон отдавал в отчёте значения любого договора
    компании по номеру - сотруднику с правкой только своих договоров тоже.
    Теперь чужой договор не сопоставляется, не показывается и не заводится
    второй раз; скрытое поле не читается."""
    app = owner.app
    # Без отдела - права как записаны лично (отдел был бы потолком).
    card = owner.post(
        f"{BASE}/people/employees",
        json={"full_name": "Сейтова Айдана", "phone": "+77025550122", "access": True},
    ).json()
    grant = owner.put(
        f"{BASE}/access/employee/{card['id']}",
        json={"changes": {"contracts": {"level": "edit", "scope": {"rows": "own"}}, "contracts.field.note": "none"}},
    )
    assert grant.status_code == 200, grant.text
    person = TestClient(app)
    assert person.post(f"{BASE}/auth/phone/start", json={"phone": "+77025550122"}).json() == {"step": "set_password"}
    assert person.post(f"{BASE}/auth/phone/set-password", json={"phone": "+77025550122", "password": "secret-123"}).status_code == 200
    report = _sync(person, [{"number": "ЮО/1", "values": {"Примечания": "x", "Текущее состояние": "Исполнен"}}], apply=True)
    line = report["rows"][0]
    assert line["status"] == "closed" and "fields" not in line and "contract_id" not in line, line
    assert report["summary"]["closed"] == 1 and report["summary"]["create"] == 0
    numbers = [item["values"].get("number") for item in owner.get(f"{BASE}/contracts").json()["contracts"]]
    assert numbers.count("ЮО/1") == 1
    assert any("не открыто" in item for item in line.get("unknown", [])), line


def test_novyy_dogovor_zavoditsya_a_neznakomaya_kolonka_nazyvaetsya(owner: TestClient) -> None:
    row = {
        "number": "№ЮО/143",
        "customer": "Болекпаев Болатжан",
        "values": {"Исполнитель": "BBC", "Сумма Договора": "400 000", "Плановая дата": "28.02.2027", "Колонка X": "?"},
    }
    report = _sync(owner, [row], apply=True)
    line = report["rows"][0]
    assert line["status"] == "create" and line["applied"] is True, line
    assert "Колонка X" in line["unknown"]
    numbers = [item["values"].get("number") for item in owner.get(f"{BASE}/contracts").json()["contracts"]]
    assert "№ЮО/143" in numbers
    # Только чтение - не пишется.
    report = _sync(owner, [{"number": "ЮО/1", "values": {"Оплачено по выписке": "100"}}])
    assert any("только чтение" in item for item in report["rows"][0]["unknown"])
