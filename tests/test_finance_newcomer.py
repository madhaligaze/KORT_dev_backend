"""Новичок с нуля (проход 31.10.2026): регистрация, первые шаги, первые деньги.

Человек, который о KORT знал только слух, зарегистрировал «ТОО «Ромашка
Проба»», завёл договор и загрузил выписку. По дороге KORT:
* не знал юрлица из регистрации и просил завести его второй раз;
* пустой компании говорил «Акты подписаны, ЭСФ выписаны» и «Режимы записаны»;
* «Долги: нет» без единой оплаты - при начисленных 1,2 млн;
* оплату «за услуги сентябрь 2026», пришедшую 15 октября, клал на октябрь, а
  сентябрь считал закрытым: «Нам должны 0» при неоплаченном октябре;
* счёт из шапки выписки заводил без юрлица и без остатка на начало - «Деньги
  вперёд» считали деньги с нуля.
"""
from __future__ import annotations

from datetime import date

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.finance import audit, auth
from app.finance.contracts import payments as contract_payments
from app.finance.contracts import service

BASE = "/api/v1/finance"
MODEL = f"{BASE}/model"
PASSWORD = "pass-12345"
TODAY = date(2026, 10, 31)
#: БИН с верной контрольной цифрой.
OUR_BIN = "180940012348"


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


def _client(monkeypatch, company: str) -> TestClient:
    from finance_routes import finance_app

    from app.finance.model import ask as model_ask
    from app.finance.model import questions

    for module in (service, model_ask, questions):
        monkeypatch.setattr(module, "today", lambda: TODAY)
    person = TestClient(finance_app(), headers={"x-client-ip": "10.0.0.9", "user-agent": "pytest-agent"})
    response = person.post(
        f"{BASE}/auth/register",
        json={"email": "novichok@romashka.kz", "password": PASSWORD, "company": company, "full_name": "Данияр"},
    )
    assert response.status_code == 201, response.text
    return person


def _ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def _one(client: TestClient, command: str, **params) -> dict:
    done = _ok(client.post(f"{MODEL}/apply", json={"commands": [{"command": command, "params": params}]}))
    return done["commands"][0]["result"]


def _ask(client: TestClient, source: str, **params) -> dict:
    return _ok(client.post(f"{MODEL}/ask", json={"source": source, "params": params}))


def _calm(client: TestClient) -> dict[str, str]:
    return {item["key"]: item["empty"] for item in _ask(client, "attention")["rows"]}


# ── Месяц словами назначения ─────────────────────────────────────────────────


def test_mesyac_iz_naznacheniya() -> None:
    named = contract_payments.months_named
    paid = date(2026, 10, 15)
    assert named("Оплата по договору 01/2026 от 01.09.2026 за услуги сентябрь 2026", paid) == ("2026-09", "2026-09")
    assert named("Абонентская плата за сентябрь-октябрь 2026", paid) == ("2026-09", "2026-10")
    # Месяц после числа - дата документа, а не период.
    assert named("Оплата по счёту № 84 от 15 сентября 2026", paid) is None
    # Без года - ближайший к платежу: декабрь, оплаченный в январе, - прошлого года.
    assert named("Аренда за декабрь", date(2027, 1, 10)) == ("2026-12", "2026-12")
    assert named("Оплата по договору", paid) is None
    # Перечень дат больше года - не период.
    assert named("за январь 2025 и за октябрь 2026", paid) is None


# ── Регистрация и первые шаги ────────────────────────────────────────────────


def test_registraciya_tov_srazu_nashe_yurlico_i_pervye_shagi(finance_db, monkeypatch) -> None:
    client = _client(monkeypatch, "ТОО «Ромашка Проба»")
    start = _ask(client, "start")
    steps = {row["key"]: row for row in start["rows"]}
    assert not start["done"] and list(steps) == ["entity", "contracts", "money"]
    # Юрлицо из регистрации - уже наше; шагу не хватает БИН.
    assert not steps["entity"]["done"] and steps["entity"]["text"] == "ТОО «Ромашка Проба» - БИН не указан"
    assert steps["entity"]["entity"]
    assert steps["contracts"]["text"] == "Договоров ещё нет" and steps["money"]["text"] == "Оплат ещё нет"
    assert _ok(client.get(f"{MODEL}/describe"))["home"]["start"] is True

    # Пустая компания: «считать не из чего», а не «всё хорошо».
    calm = _calm(client)
    assert calm["acts"] == "Договоров ещё нет" and calm["unbilled"] == "Договоров ещё нет"
    assert calm["debts"] == "Договоров ещё нет"
    assert calm["vat"] == "До порога далеко", "режим не записан - «Режимы записаны» было бы неправдой"
    assert calm["cash"] == "Договоров и денег ещё нет - считать не из чего"
    acts = next(item for item in _ask(client, "attention")["rows"] if item["key"] == "acts")
    assert acts["note"] == "", "сводной таблицы нет - и читать её нечего"

    entity = steps["entity"]["entity"]
    _one(client, "participant.update", id=entity, bin=OUR_BIN)
    _one(
        client, "contract.create",
        values={"executor": {"id": entity}, "customer": "ТОО «Алматы Строй»", "number": "01/2026", "amount": "1200000",
                "billing": "month", "signed_at": "2026-09-01"},
    )
    steps = {row["key"]: row for row in _ask(client, "start")["rows"]}
    assert steps["entity"]["done"] and steps["contracts"]["done"] and steps["contracts"]["text"] == "1 договор"
    # Договор есть, оплат нет: долг не посчитать - так и сказано.
    assert _calm(client)["debts"] == "Оплат в KORT ещё нет - долг посчитается с выпиской банка или 1С"


def test_gruppa_bez_formy_ne_yurlico(finance_db, monkeypatch) -> None:
    client = _client(monkeypatch, "ГК Ромашка")
    steps = {row["key"]: row for row in _ask(client, "start")["rows"]}
    assert steps["entity"]["text"] == "Наших юрлиц ещё нет" and steps["entity"]["entity"] is None
    assert _calm(client)["vat"] == "Наших юрлиц ещё нет"
    _one(client, "entity.create", name="ТОО «Ромашка Один»", bin=OUR_BIN)
    steps = {row["key"]: row for row in _ask(client, "start")["rows"]}
    assert steps["entity"]["done"] and steps["entity"]["text"] == "ТОО «Ромашка Один»"


# ── Оплата за месяц до первой оплаты ─────────────────────────────────────────


def _romashka_with_contract(client: TestClient) -> tuple[str, str]:
    entity = _ask(client, "start")["rows"][0]["entity"]
    _one(client, "account.create", entity=entity, name="Halyk ·3456")
    contract = _one(
        client, "contract.create",
        values={"executor": {"id": entity}, "customer": "ТОО «Алматы Строй»", "number": "01/2026", "amount": "1200000",
                "billing": "month", "signed_at": "2026-09-01"},
    )["contract"]["id"]
    return entity, contract


def _money(client: TestClient, contract: str) -> dict:
    """Оплаты договора по месяцам - как их видит карточка («Начисления»)."""
    return _ok(client.get(f"{BASE}/core/contracts/{contract}/months"))["money"]


def test_oplata_za_sentyabr_gasit_sentyabr_a_ne_oktyabr(finance_db, monkeypatch) -> None:
    client = _client(monkeypatch, "ТОО «Ромашка Проба»")
    entity, contract = _romashka_with_contract(client)
    _one(
        client, "payment.record", day="2026-10-15", amount="1200000", entity=entity, party="ТОО «Алматы Строй»",
        comment="Оплата по договору 01/2026 от 01.09.2026 за услуги сентябрь 2026",
    )
    money = _money(client, contract)
    assert money["since"] == "2026-10-01", "начало учёта - по первой оплате, человек его не задавал"
    months = {item["month"]: item for item in money["months"]}
    assert months["2026-09"]["paid"] == "1200000.00", "оплата назвала сентябрь - сентябрь оплачен"
    assert months["2026-10"]["paid"] == "0.00" and months["2026-10"]["debt"] == "1200000.00"
    assert money["debt"] == "1200000.00", "октябрь не оплачен - он и есть долг"
    debts = _ask(client, "attention", item="debts")["rows"][0]
    assert debts["amount"] == "1200000.00"
    # Пустые «Банковский счёт» и «Касса» из регистрации - не колонка «без юрлица».
    calendar = _ask(client, "calendar", **{"from": "2026-10-26", "days": 10})
    assert [item["title"] for item in calendar["entities"]] == ["ТОО «Ромашка Проба»"], calendar["entities"]


def test_oplata_bez_mesyaca_kak_ran_she(finance_db, monkeypatch) -> None:
    """Назначение месяца не называет - до первой оплаты KORT по-прежнему ничего не знает."""
    client = _client(monkeypatch, "ТОО «Ромашка Проба»")
    entity, contract = _romashka_with_contract(client)
    _one(client, "payment.record", day="2026-10-15", amount="1200000", entity=entity, party="ТОО «Алматы Строй»",
         comment="Оплата по договору 01/2026")
    months = {item["month"]: item for item in _money(client, contract)["months"]}
    assert "2026-09" not in months and months["2026-10"]["paid"] == "1200000.00"


def test_nachalo_ucheta_cheloveka_sentyabr_do_ucheta(finance_db, monkeypatch) -> None:
    """Начало учёта задал человек: «за сентябрь» при учёте с октября гасит входящее сальдо, а сверх него -
    «до учёта», не октябрь (полигон, О9). Октябрь остаётся долгом - и в месяцах, и в итоге договора."""
    client = _client(monkeypatch, "ТОО «Ромашка Проба»")
    entity, contract = _romashka_with_contract(client)
    _one(client, "accounting.start", since="2026-10-01")
    _one(client, "payment.record", day="2026-10-15", amount="1200000", entity=entity, party="ТОО «Алматы Строй»",
         comment="за услуги сентябрь 2026")
    money = _money(client, contract)
    months = {item["month"]: item for item in money["months"]}
    assert "2026-09" not in months and months["2026-10"]["paid"] == "0.00"
    assert money["debt"] == "1200000.00", "сверх сальдо за период до учёта - не оплата октября"
    debts = _ask(client, "attention", item="debts")["rows"][0]
    assert debts["amount"] == "1200000.00" and not debts["note"], "и не аванс"


# ── Счёт из шапки выписки ────────────────────────────────────────────────────


def test_schet_iz_vypiski_s_yurlicom_i_ostatkom(finance_db, monkeypatch) -> None:
    from app.api.routes.finance import _statement_account
    from app.finance.db import finance_session
    from app.finance.models import Workspace

    client = _client(monkeypatch, "ТОО «Ромашка Проба»")
    entity = _ask(client, "start")["rows"][0]["entity"]
    _one(client, "participant.update", id=entity, bin=OUR_BIN)
    question = {
        "kind": "account", "title": "На какой счёт лягут эти операции?", "text": "…",
        "options": [{"value": "Банковский счёт", "label": "Банковский счёт", "example": ""}, {"value": "Касса", "label": "Касса", "example": ""}],
        "create": {"name": "Halyk ·3456", "number": "KZ86601A861000123456", "currency": ""},
    }
    bank = {"period_start": "2026-10-01", "opening_balance": "500000.00", "owner": "ТОО «Ромашка Проба»", "owner_bin": OUR_BIN}
    with finance_session() as session:
        workspace = session.scalar(sa.select(Workspace))
        asked = _statement_account(session, workspace, question, bank)
        assert asked["options"] == [], "пустые «Банковский счёт» и «Касса» из регистрации - не выбор, когда выписка назвала свой счёт"
        create = asked["create"]
        assert create["group_entity_id"] == entity and create["owner"] == "ТОО «Ромашка Проба»"
        assert create["starting_balance"] == "500000.00" and create["period_start"] == "2026-10-01"
        # Без БИН - по имени целиком; чужое имя - юрлица нет, решает человек.
        by_name = _statement_account(session, workspace, question, {**bank, "owner_bin": ""})["create"]
        assert by_name["group_entity_id"] == entity
        stranger = _statement_account(session, workspace, question, {**bank, "owner_bin": "", "owner": "ТОО «Другое»"})["create"]
        assert stranger["group_entity_id"] is None and stranger["starting_balance"] == "500000.00"
        # Без начала периода остаток - не на известный день: не предлагается.
        undated = _statement_account(session, workspace, question, {**bank, "period_start": None})["create"]
        assert undated["starting_balance"] is None


# ── Счета без сводной таблицы и оплата руками ────────────────────────────────


def test_scheta_bez_svodnoy_po_dogovoram(finance_db, monkeypatch) -> None:
    """«Счета пора выставить» вели в пустой раздел журнала: без сводной «Не выставлены» не было."""
    client = _client(monkeypatch, "ТОО «Ромашка Проба»")
    _entity, contract = _romashka_with_contract(client)
    board = _ok(client.get(f"{BASE}/bills"))
    assert board["source"]["kind"] == "kort"
    bill = next(item for item in board["items"] if item["contract_id"] == contract)
    assert bill["customer"] == "ТОО «Алматы Строй»" and bill["source"] == "kort" and bill["amount"]
    card = _ok(client.get(f"{BASE}/bills/{bill['id']}"))
    assert card["book"]["kort"] is True and not card["problem"]
    _ok(client.post(f"{BASE}/bills/{bill['id']}", json={"values": {"bill_no": "1", "bill_date": "2026-10-01"},
                                                            "expect": card["book"]["fields"]}))
    board = _ok(client.get(f"{BASE}/bills"))
    assert all(item["id"] != bill["id"] for item in board["items"]), "выставленный ушёл из «Не выставлены»"
    assert any(item["id"] == bill["id"] and item["bill_no"] == "1" for item in board["issued"])


def test_prishla_oplata_v_kartochke(finance_db, monkeypatch) -> None:
    """Оплата руками из карточки договора: счёта у юрлица нет - команда заводит его, и он виден для следующей."""
    client = _client(monkeypatch, "ТОО «Ромашка Проба»")
    entity = _ask(client, "start")["rows"][0]["entity"]
    contract = _one(
        client, "contract.create",
        values={"executor": {"id": entity}, "customer": "ТОО «Алматы Строй»", "number": "01/2026", "amount": "1200000",
                "billing": "month", "signed_at": "2026-09-01"},
    )["contract"]["id"]
    months = _ok(client.get(f"{BASE}/core/contracts/{contract}/months"))
    assert months["pay_accounts"] == []
    _one(client, "payment.record", contract=contract, day="2026-10-05", amount="1200000", key="card-1")
    months = _ok(client.get(f"{BASE}/core/contracts/{contract}/months"))
    assert len(months["pay_accounts"]) == 1
    paid = {item["month"]: item for item in months["money"]["months"]}
    assert paid["2026-10"]["paid"] == "1200000.00", "без «за какой месяц» - по первой оплате, учёт с октября"
    accounts = _ok(client.get(f"{BASE}/dictionaries"))["accounts"]
    owned = [item for item in accounts if item["group_entity_id"] == entity]
    assert len(owned) == 1 and owned[0]["owner"] == "ТОО «Ромашка Проба»"
    assert {item["owner"] for item in accounts if item["name"] in ("Банковский счёт", "Касса")} == {""}


def test_pervaya_oplata_rukami_stavit_nachalo_ucheta(finance_db, monkeypatch) -> None:
    """Первая оплата руками за сентябрь 01.11: начало учёта - с первого начисления, а не месяц оплаты.

    Иначе сентябрь и октябрь молча «до начала учёта», а деньги - на ноябрь.
    Карточка шлёт пачку: начало учёта и оплата за названный месяц.
    """
    client = _client(monkeypatch, "ТОО «Ромашка Проба»")
    entity = _ask(client, "start")["rows"][0]["entity"]
    contract = _one(
        client, "contract.create",
        values={"executor": {"id": entity}, "customer": "ТОО «Алматы Строй»", "number": "01/2026", "amount": "1200000",
                "billing": "month", "signed_at": "2026-09-01"},
    )["contract"]["id"]
    _ok(client.post(f"{MODEL}/apply", json={"commands": [
        {"command": "accounting.start", "params": {"since": "2026-09-01"}},
        {"command": "payment.record", "params": {"contract": contract, "day": "2026-10-30", "amount": "1200000", "month": "2026-09", "key": "first"}},
    ]}))
    money = _money(client, contract)
    months = {item["month"]: item for item in money["months"]}
    assert money["since"] == "2026-09-01"
    assert months["2026-09"]["paid"] == "1200000.00" and months["2026-10"]["debt"] == "1200000.00"
    assert money["debt"] == "1200000.00"
