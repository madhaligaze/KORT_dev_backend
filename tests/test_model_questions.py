"""Решить, под угрозой, пришло (этап 3, шаг 2): главный экран - вызовами модели.

Проверяется то, ради чего источники есть:
* оплата без договора приходит вопросом с вариантами; у варианта - команда
  «применить» с готовыми параметрами; ответ - обычная пачка, вопрос уходит из
  списка, «Вернуть» возвращает его;
* двойники и БИН из выписки - вопросы участников: «свести», «это разные»,
  «записать БИН», «не его номер» - командами, и каждая возвращается;
* права: оплаты без договора видят владелец и администратор, а вариант,
  закрытый спрашивающему, помечен `open: false`;
* «пришло» - новые факты с дня, а не переписанные версии старых;
* «под угрозой» - пункты порознь, у каждого `open` - вопрос «спросить»;
* «документы» - дела по АВР тем же счётом, что пункт угроз.
"""
from __future__ import annotations

from datetime import date

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.finance import audit, auth
from app.finance.contracts import service

BASE = "/api/v1/finance"
MODEL = f"{BASE}/model"
PASSWORD = "pass-12345"
#: Годный ИИН человека (контрольная цифра сходится).
PERSON_IIN = "900515300008"


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
    from app.finance.model import questions

    monkeypatch.setattr(service, "today", lambda: date(2026, 10, 25))
    monkeypatch.setattr(model_ask, "today", lambda: date(2026, 10, 25))
    monkeypatch.setattr(questions, "today", lambda: date(2026, 10, 25))
    person = TestClient(finance_app(), headers={"x-client-ip": "10.0.0.7", "user-agent": "pytest-agent"})
    response = person.post(
        f"{BASE}/auth/register",
        json={"email": "owner@resh.kz", "password": PASSWORD, "company": "Решить (тест)", "full_name": "Нурланова Айгерим"},
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


def _ask(client: TestClient, source: str, **params) -> dict:
    return _ok(client.post(f"{MODEL}/ask", json={"source": source, "params": params}))


def _answer(client: TestClient, option: dict, **extra) -> dict:
    """Ответить вариантом вопроса: его пачка команд, плюс то, чего вариант просит (`needs`), - в каждую."""
    assert option["open"], option
    return _apply(client, *({"command": item["command"], "params": {**item["params"], **extra}} for item in option["commands"]))


def _debt(client: TestClient, number: str) -> str:
    found = _ok(client.post(f"{MODEL}/ask", json={"source": "debts", "as_of": "2026-10-25", "params": {"group": "client"}}))
    for group in found["rows"]:
        for item in group["contracts"]:
            if item["number"] == number:
                return item["debt"]
    return "0.00"


def _company(client: TestClient) -> tuple[dict, str, str]:
    entity = _one(client, "entity.create", name="ТОО Решить", code="РШ")
    _one(client, "accounting.start", since="2026-10-01")
    values = {"executor": {"id": entity["id"]}, "customer": "ТОО Клиент Два", "billing": "month", "signed_at": "2026-10-01"}
    first = _one(client, "contract.create", values={**values, "number": "Д-1", "amount": "100000"})["contract"]["id"]
    second = _one(client, "contract.create", values={**values, "number": "Д-2", "amount": "50000"})["contract"]["id"]
    return entity, first, second


def test_oplata_bez_dogovora_voprosom_otvet_komandoy_i_vozvrat(owner: TestClient) -> None:
    entity, first, second = _company(owner)
    stray = _one(owner, "payment.record", day="2026-10-05", amount="150000", entity=entity["id"], party="ТОО Клиент Два")

    asked = _ask(owner, "questions", kind="payments")
    assert asked["source"] == "questions" and asked["kinds"]["payments"]["count"] == 1
    question = asked["rows"][0]
    assert question["key"] == f"payment:{stray['operation_id']}" and question["kind"] == "payment"
    assert question["amount"] == "150000.00" and "ТОО Клиент Два" in question["text"]
    keys = [option["key"] for option in question["options"]]
    assert set(keys[:2]) == {f"link:{first}", f"link:{second}"} and keys[-2:] == ["pick", "none"], keys
    assert all(option["open"] for option in question["options"]), "владельцу открыто всё"
    pick = next(option for option in question["options"] if option["key"] == "pick")
    assert pick["needs"] == [{"param": "contract", "type": "contract", "title": "Договор"}]

    # Ответ - вариант «К Д-1»: обычная пачка, вопрос уходит, долг считает ответ.
    link = next(option for option in question["options"] if option["key"] == f"link:{first}")
    done = _answer(owner, link)
    assert _ask(owner, "questions", kind="payments")["rows"] == []
    assert _debt(owner, "Д-1") == "-50000.00", "150 000 на договор в 100 000 - аванс 50 000"
    # «Вернуть» - вопрос снова в списке.
    _ok(owner.post(f"{MODEL}/undo", json={"point": done["point"]["id"]}))
    again = _ask(owner, "questions", kind="payments")["rows"]
    assert [item["key"] for item in again] == [question["key"]]

    # «Другой договор» - вариант просит договор: он приходит в той же команде.
    _answer(owner, pick, contract=second)
    assert _ask(owner, "questions", kind="payments")["rows"] == []
    assert _debt(owner, "Д-2") == "-100000.00"

    # Всё сразу - тот же список с видами вопросов.
    whole = _ask(owner, "questions")
    assert set(whole["kinds"]) == {"payments", "bins", "twins", "reconcile"}


def test_dvoyniki_svesti_i_raznye_s_vozvratom(owner: TestClient) -> None:
    entity = _one(owner, "entity.create", name="ТОО Решить", code="РШ")
    values = {"executor": {"id": entity["id"]}, "billing": "month", "signed_at": "2026-10-01", "amount": "1000"}
    _one(owner, "contract.create", values={**values, "customer": "ТОО Бергауф Астана", "number": "Б-1"})
    _one(owner, "contract.create", values={**values, "customer": "Бергауф Астана", "number": "Б-2"})

    rows = _ask(owner, "questions", kind="twins")["rows"]
    assert len(rows) == 1 and rows[0]["kind"] == "participant_twins"
    twins = rows[0]
    assert sorted(item["title"] for item in twins["objects"]) == ["Бергауф Астана", "ТОО Бергауф Астана"]
    distinct = next(option for option in twins["options"] if option["key"] == "distinct")
    merge = next(option for option in twins["options"] if option["key"] == "merge")

    # «Это разные» - группа уходит; «Вернуть» - снова предлагается.
    done = _answer(owner, distinct)
    assert _ask(owner, "questions", kind="twins")["rows"] == []
    _ok(owner.post(f"{MODEL}/undo", json={"point": done["point"]["id"]}))
    assert len(_ask(owner, "questions", kind="twins")["rows"]) == 1

    # «Свести» - участник один, договоры обоих у него; «Вернуть» - снова двое.
    merged = _answer(owner, merge)
    assert _ask(owner, "questions", kind="twins")["rows"] == []
    contracts = _ok(owner.post(f"{MODEL}/ask", json={"source": "contracts"}))["rows"]
    assert {row["client"] for row in contracts if row["number"] in ("Б-1", "Б-2")} == {
        next(item["title"] for item in twins["objects"] if item["id"] == merge["commands"][0]["params"]["keep"])
    }
    _ok(owner.post(f"{MODEL}/undo", json={"point": merged["point"]["id"]}))
    assert len(_ask(owner, "questions", kind="twins")["rows"]) == 1


def test_bin_iz_vypiski_zapisat_ili_ne_ego(owner: TestClient) -> None:
    import uuid

    from app.finance.db import finance_session
    from app.finance.models import Counterparty

    entity = _one(owner, "entity.create", name="ТОО Решить", code="РШ")
    _one(owner, "accounting.start", since="2026-10-01")
    _one(owner, "contract.create", values={
        "executor": {"id": entity["id"]}, "customer": "Касымов Ерлан", "number": "К-1", "amount": "90000", "billing": "month",
        "signed_at": "2026-10-01",
    })
    payer = _one(owner, "participant.create", name="КАСЫМОВ Е.")
    _one(owner, "payment.record", day="2026-10-07", amount="90000", entity=entity["id"], party="КАСЫМОВ Е.")
    # Выписка кладёт ИИН плательщика в его пометки (`service.import_rows`): так и здесь.
    with finance_session() as session:
        party = session.get(Counterparty, uuid.UUID(payer["id"]))
        party.details = {**(party.details or {}), "bin": PERSON_IIN}

    rows = _ask(owner, "questions", kind="bins")["rows"]
    assert len(rows) == 1 and rows[0]["party"] == "Касымов Ерлан"
    question = rows[0]
    assert question["suggested"] == f"bin:{PERSON_IIN}", "номер один и ни у кого не записан - KORT предлагает записать"
    write = next(option for option in question["options"] if option["key"] == f"bin:{PERSON_IIN}")
    reject = next(option for option in question["options"] if option["key"] == "reject")

    # «Записать» - вопрос уходит; «Вернуть» снимает БИН, и вопрос снова в списке.
    done = _answer(owner, write)
    assert _ask(owner, "questions", kind="bins")["rows"] == []
    _ok(owner.post(f"{MODEL}/undo", json={"point": done["point"]["id"]}))
    assert len(_ask(owner, "questions", kind="bins")["rows"]) == 1

    # «Не его номер» - больше не предлагается; «Вернуть» - предлагается снова.
    rejected = _answer(owner, reject)
    assert _ask(owner, "questions", kind="bins")["rows"] == []
    _ok(owner.post(f"{MODEL}/undo", json={"point": rejected["point"]["id"]}))
    assert len(_ask(owner, "questions", kind="bins")["rows"]) == 1


def test_prava_voprosov_i_zakrytye_varianty(owner: TestClient) -> None:
    entity, _first, _second = _company(owner)
    _one(owner, "payment.record", day="2026-10-05", amount="150000", entity=entity["id"], party="ТОО Клиент Два")
    values = {"executor": {"id": entity["id"]}, "billing": "month", "signed_at": "2026-10-01", "amount": "1000"}
    _one(owner, "contract.create", values={**values, "customer": "ТОО Бергауф Астана", "number": "Б-1"})
    _one(owner, "contract.create", values={**values, "customer": "Бергауф Астана", "number": "Б-2"})

    described = _ok(owner.get(f"{MODEL}/describe"))
    assert described["version"] == "1.5"
    assert described["home"]["questions"] == ["payments", "bins", "twins", "reconcile"]
    assert {"participant.merge", "participant.distinct", "participant.bin_reject"} <= {item["name"] for item in described["commands"]}

    # Система без права настройки: оплаты без договора ей не показываются, «свести» закрыто.
    from app.finance.contracts.service import Access
    from app.finance.core import views as views_module
    from app.finance.db import finance_session
    from app.finance.model import questions
    from app.finance.model.context import Ctx
    from app.finance.models import Workspace

    with finance_session() as session:
        workspace = session.scalars(sa.select(Workspace)).one()
        viewer = Ctx(
            session=session, workspace=workspace,
            access=Access(view=True, edit=True, setup=False, admin=False, full_contracts=True, user_id=None),
            viewer=views_module.Viewer(user_id=None, admin=False), actor=None, levels={"contracts": "edit"},
        )
        answer = questions.questions(viewer, {"source": "questions"})
        assert answer["kinds"]["payments"]["count"] == 0, "оплаты без договора - владельцу и администратору"
        twins = next(row for row in answer["rows"] if row["kind"] == "participant_twins")
        options = {option["key"]: option["open"] for option in twins["options"]}
        assert options == {"merge": False, "distinct": True}, "сводит владелец или администратор"


def test_prishlo_novye_fakty_a_ne_versii(owner: TestClient) -> None:
    entity, first, _second = _company(owner)
    _one(owner, "payment.record", day="2026-10-05", amount="100000", contract=first, month="2026-10", key="p-1")
    _one(owner, "payment.record", day="2026-10-06", amount="25000", entity=entity["id"], party="ТОО Клиент Два", key="p-2")

    arrived = _ask(owner, "arrived", since="2026-10-01")
    assert arrived["since"] == "2026-10-01"
    payments = arrived["payments"]
    assert (payments["count"], payments["amount"], payments["placed"], payments["without"]) == (2, "125000.00", 1, 1)
    assert any(item["kind"] == "payment" and item["contract"] and item["contract"]["number"] == "Д-1" for item in arrived["latest"])

    # Оплату перенесли на другой договор - новая версия факта, но не новое поступление.
    stray = _ask(owner, "questions", kind="payments")["rows"][0]
    _answer(owner, next(option for option in stray["options"] if option["key"].startswith("link:")))
    again = _ask(owner, "arrived", since="2026-10-01")["payments"]
    assert (again["count"], again["placed"], again["without"]) == (2, 2, 0)
    # Завтрашний день: ничего не пришло.
    assert _ask(owner, "arrived", since="2026-12-01")["totals"]["facts"] == 0


def test_pod_ugrozoy_porozn_i_dokumenty(owner: TestClient) -> None:
    entity, first, _second = _company(owner)
    _one(owner, "document.record", contract=first, kind="act", month="2026-10", number="1", day="2026-10-20")

    whole = _ask(owner, "attention")
    assert whole["items"] == ["cash", "vat", "debts", "unbilled", "acts"]
    by_key = {item["key"]: item for item in whole["rows"]}
    assert by_key["debts"]["open"] == {"source": "debts", "params": {"side": "receivable", "basis": "accruals", "group": "client"}}
    assert by_key["unbilled"]["open"] == {"source": "unbilled"} and by_key["unbilled"]["count"] >= 1
    # Пункт порознь - тот же ответ одного пункта.
    single = _ask(owner, "attention", item="debts")
    assert [item["key"] for item in single["rows"]] == ["debts"] and single["rows"][0]["amount"] == by_key["debts"]["amount"]
    with pytest.raises(AssertionError):
        _ask(owner, "attention", item="profit")

    # Дела по АВР: акт без подписи - в списке «не подписан», счёт тот же, что у пункта угроз.
    acts = by_key["acts"]
    unsigned = _ask(owner, "documents", view="unsigned")
    assert unsigned["counts"]["unsigned"] == 1 and unsigned["rows"][0]["contract"]["number"] == "Д-1"
    assert sum(unsigned["counts"].values()) == acts["count"]
