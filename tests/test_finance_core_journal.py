"""Журнал фактов (этап 2, шаг 1): одна форма для оплат, документов, соглашений, сальдо и решений.

Набор держит то, ради чего журнал заведён:

* факты проводятся из хранилищ: операция журнала денег - оплата с договором по
  разнесению, документ 1С - оплата, счёт, АВР и ЭСФ, документ ядра - поправка и
  сальдо, соглашение - соглашение, решение человека - решение;
* журнал не правится: правка источника - новая версия со ссылкой на прежнюю,
  снятие - пометка «снят», проведение без правок не пишет ничего;
* журнал свежий перед чтением: правка в другой сессии сдвигает счётчик правок,
  своя незакоммиченная видна сразу;
* итоги читают журнал: оплаты, начисления, АВР;
* вызовы: что было с договором, что изменилось после номера, долг по кускам.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from app.finance import history
from app.finance import service as finance_service
from app.finance.contracts import service, setup
from app.finance.contracts.fields import current as counter
from app.finance.contracts.models import GroupEntity, ListValue
from app.finance.core import acts as acts_module
from app.finance.core import calls, changes, journal
from app.finance.core.accruals import Ledger
from app.finance.core.models import Fact
from app.finance.core.payments import Money
from app.finance.db import finance_session
from app.finance.models import Operation
from app.finance.onec.models import OnecBase, OnecDocument

FULL = service.Access(view=True, edit=True, setup=True, admin=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
TODAY = date(2026, 10, 25)


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    monkeypatch.setattr(journal, "_CHECKED", set())
    yield engine


@pytest.fixture
def audit():
    token = history.CONTEXT.set(history.AuditContext(user_id=OWNER.user_id))
    yield
    history.CONTEXT.reset(token)


@pytest.fixture
def space(finance_db, audit) -> dict:
    """BBC, договор с Альфакомом на 100 000 в месяц с июня и счёт BBC в банке."""
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC", code="BBC")
        alfa = service.create(
            session, workspace, FULL, OWNER,
            {"executor": "BBC", "customer": "ТОО Альфаком", "number": "№ОКР/15", "amount": "100000", "billing": "month",
             "signed_at": "2026-06-01"},
        )
        entity = session.scalar(sa.select(GroupEntity.counterparty_id).where(GroupEntity.code == "BBC"))
        account = finance_service.create_account(session, workspace, name='Kaspi Юр. ТОО "BBC"', group_entity_id=entity)
        return {"workspace": workspace.id, "alfa": alfa.id, "account": account.id, "bbc": entity}


def _ws(session, space):
    return finance_service.get_workspace(session, space["workspace"])


def _operation(session, space, payer: str, amount: str, day: date, *, kind: str = "income", month: str = "") -> Operation:
    workspace = _ws(session, space)
    party = finance_service.ensure_counterparty(session, workspace.id, payer, role="client" if kind == "income" else "supplier")
    start = date(int(month[:4]), int(month[5:7]), 1) if month else None
    accounts = {"account_to_id": space["account"]} if kind == "income" else {"account_from_id": space["account"]}
    return finance_service.create_operation(
        session, workspace,
        finance_service.OperationInput(
            kind=kind, paid_at=day, amount=Decimal(amount), counterparty_id=party.id, period_start=start,
            period_end=start, **accounts,
        ),
    )


def _facts(session, space, *, everything: bool = False) -> list[Fact]:
    query = sa.select(Fact).where(Fact.workspace_id == space["workspace"])
    if not everything:
        query = query.where(Fact.ended_seq.is_(None))
    return list(session.scalars(query.order_by(Fact.seq, Fact.key)))


def _by_key(session, space, key: str, *, everything: bool = False) -> list[Fact]:
    return [fact for fact in _facts(session, space, everything=everything) if fact.key == key]


# ── Проведение ───────────────────────────────────────────────────────────────


def test_operacii_provodyatsya_oplatami(space):
    with finance_session() as session:
        paid = _operation(session, space, "ТОО Альфаком", "100000", date(2026, 6, 5), month="2026-06")
        stray = _operation(session, space, "ИП Незнакомец", "7000", date(2026, 6, 9))
        salary = _operation(session, space, "Сотрудник", "300000", date(2026, 6, 10), kind="expense")
        ids = (paid.id, stray.id, salary.id)
    with finance_session() as session:
        money = Money(session, _ws(session, space), today=TODAY)
        assert [payment.key for payment in money.by_contract[space["alfa"]]] == [f"op:{ids[0]}"]
        facts = {fact.key: fact for fact in _facts(session, space) if fact.kind == "payment"}
        assert set(facts) == {f"op:{ids[0]}", f"op:{ids[1]}"}, "списание без договора - не оплата и в журнал не идёт"
        found = facts[f"op:{ids[0]}"]
        assert (found.contract_id, found.amount, found.day, found.entity_id) == (
            space["alfa"], Decimal("100000.00"), date(2026, 6, 5), space["bbc"]
        )
        assert found.data["how"] == "auto" and found.data["months"] == ["2026-06", "2026-06"]
        assert found.data["direction"] == "in" and found.source == "journal"
        assert facts[f"op:{ids[1]}"].contract_id is None, "поступление без договора - факт без договора"
        assert money.since == date(2026, 6, 1)


def test_pravka_istochnika_novaya_versiya_snyatie_pometkoy(space):
    with finance_session() as session:
        operation_id = _operation(session, space, "ТОО Альфаком", "100000", date(2026, 6, 5), month="2026-06").id
    with finance_session() as session:
        Money(session, _ws(session, space), today=TODAY)
        first = _by_key(session, space, f"op:{operation_id}")[0].id
        seq = counter(session, space["workspace"], journal.SEQ)
    with finance_session() as session:
        # Повторное чтение без правок ничего не пишет.
        Money(session, _ws(session, space), today=TODAY)
        assert counter(session, space["workspace"], journal.SEQ) == seq
        finance_service.update_operation(session, _ws(session, space), operation_id, {"amount": Decimal("60000")})
    with finance_session() as session:
        money = Money(session, _ws(session, space), today=TODAY)
        assert money.paid_of(space["alfa"]) == Decimal("60000.00")
        versions = _by_key(session, space, f"op:{operation_id}", everything=True)
        assert [item.amount for item in versions] == [Decimal("100000.00"), Decimal("60000.00")]
        assert versions[0].id == first and versions[0].ended_seq == versions[1].seq > seq
        assert versions[1].replaces == first and versions[1].ended_seq is None
        finance_service.delete_operation(session, _ws(session, space), operation_id)
    with finance_session() as session:
        money = Money(session, _ws(session, space), today=TODAY)
        assert money.paid_of(space["alfa"]) == Decimal("0")
        last = _by_key(session, space, f"op:{operation_id}")
        assert len(last) == 1 and last[0].void and last[0].amount is None, "снятое - пометкой, а не стиранием"
        assert last[0].contract_id == space["alfa"], "пометка «снят» остаётся у договора: видно, что ушло"
        assert len(_by_key(session, space, f"op:{operation_id}", everything=True)) == 3


def test_svezhest_mezhdu_sessiyami_i_svoya_pravka(space, monkeypatch):
    calls_made: list[tuple] = []
    original = journal.post

    def counting(session, workspace, parts=journal.PARTS, **keys):
        calls_made.append(tuple(parts))
        return original(session, workspace, parts, **keys)

    monkeypatch.setattr(journal, "post", counting)
    with finance_session() as session:
        Money(session, _ws(session, space), today=TODAY)
    assert calls_made, "первое чтение проводит"
    calls_made.clear()
    with finance_session() as session:
        Money(session, _ws(session, space), today=TODAY)
        Ledger(session, space["workspace"], [])
    assert not calls_made, "без правок - читает готовое"
    # Правка в другой сессии без чтения: счётчик правок сдвинут коммитом.
    with finance_session() as session:
        operation_id = _operation(session, space, "ТОО Альфаком", "100000", date(2026, 7, 5), month="2026-07").id
    with finance_session() as session:
        assert counter(session, space["workspace"], journal.INPUTS) > (journal._counter(session, space["workspace"], journal.POSTED) or 0)
        money = Money(session, _ws(session, space), today=TODAY)
        assert [payment.key for payment in money.by_contract[space["alfa"]]] == [f"op:{operation_id}"]
    assert calls_made
    calls_made.clear()
    # Своя незакоммиченная правка - видна тут же, в той же транзакции.
    with finance_session() as session:
        Money(session, _ws(session, space), today=TODAY)
        assert not calls_made
        other = _operation(session, space, "ТОО Альфаком", "100000", date(2026, 8, 5), month="2026-08").id
        money = Money(session, _ws(session, space), today=TODAY)
        assert {payment.key for payment in money.by_contract[space["alfa"]]} == {f"op:{operation_id}", f"op:{other}"}
    calls_made.clear()
    with finance_session() as session:
        Money(session, _ws(session, space), today=TODAY)
    assert not calls_made, "проведённое в той же транзакции, что и правка, - уже свежее для других"


def test_perevod_budushchim_chislom_provodit_razneseniye(space):
    """Перевод клиента на другое ТОО с 01.11 договор не трогает, но исполнители из
    соглашения видны разнесению (`contracts/payments.former_executors`): оплата
    клиента на счёт второго ТОО ложится на договор. Журнал проводит её сразу, а
    не ждёт чужой правки формы реестра."""
    from app.finance.core import vat

    with finance_session() as session:
        workspace = _ws(session, space)
        setup.add_entity(session, workspace, name="BBC Legal", code="BBCL")
        legal = session.scalar(sa.select(GroupEntity.counterparty_id).where(GroupEntity.code == "BBCL"))
        account = finance_service.create_account(session, workspace, name='Kaspi Юр. ТОО "BBC Legal"', group_entity_id=legal)
        party = finance_service.ensure_counterparty(session, workspace.id, "ТОО Альфаком", role="client")
        operation_id = finance_service.create_operation(
            session, workspace,
            finance_service.OperationInput(
                kind="income", paid_at=date(2026, 10, 20), amount=Decimal("100000"), counterparty_id=party.id,
                account_to_id=account.id,
            ),
        ).id
    with finance_session() as session:
        journal.ensure(session, space["workspace"])
        assert _by_key(session, space, f"op:{operation_id}")[0].contract_id is None, "BBC Legal - не исполнитель договора"
    with finance_session() as session:
        changes.transfer(session, _ws(session, space), FULL, OWNER, [vat.Move(space["alfa"], legal, date(2026, 11, 1))])
    with finance_session() as session:
        assert service.get_contract(session, _ws(session, space), space["alfa"]).executor_id == space["bbc"], "перевод - впереди"
        journal.ensure(session, space["workspace"])
        assert _by_key(session, space, f"op:{operation_id}")[0].contract_id == space["alfa"]
        assert journal.post(session, _ws(session, space), ["journal", "1c"]) == {"new": 0, "changed": 0, "void": 0}, (
            "проведённое по очереди - то же, что проведение целиком"
        )


def test_podpiska_vidit_zapisannoe_fonom(space):
    """Чтение 1С пишет документы, не двигая счётчиков договоров, операций и схемы:
    подписка видит записанное, но не проведённое, и проводит журнал сама. Время
    чтения базы без новых документов журнал не трогает."""
    from datetime import datetime, timezone

    from app.finance.model import context, stream

    with finance_session() as session:
        journal.ensure(session, space["workspace"])
    with finance_session() as session:
        ctx = context.system(session, _ws(session, space))
        cursor = stream.cursor_now(ctx)
        assert not stream.moved(ctx, cursor)
    with finance_session() as session:
        base = OnecBase(
            workspace_id=space["workspace"], title="ИБ ТОО BBC", url="https://1cfresh.kz/a/ea201/1", login="odata.user",
            secret="-", group_entity_id=space["bbc"], organization_ref="org", sync_from=date(2026, 1, 1),
        )
        session.add(base)
        session.flush()
        session.add(OnecDocument(
            workspace_id=base.workspace_id, base_id=base.id, kind="payment", ref=str(uuid.uuid4()),
            doc_date=date(2026, 10, 20), party_name="ТОО АЛЬФАКОМ", amount=Decimal("100000"), posted=True,
        ))
        base_id = base.id
    with finance_session() as session:
        ctx = context.system(session, _ws(session, space))
        assert stream.moved(ctx, cursor), "документ 1С записан, журнал не проведён - подписке пора"
        out = stream.changes(ctx, cursor)
        assert out["moved"]["facts"] and [item["source"] for item in out["facts"]] == ["1c"]
        cursor = out["cursor"]
    with finance_session() as session:
        ctx = context.system(session, _ws(session, space))
        assert not stream.moved(ctx, cursor)
        base = session.get(OnecBase, base_id)
        base.last_sync_at, base.counts = datetime.now(timezone.utc), {"payment": 1}
    with finance_session() as session:
        assert not stream.moved(context.system(session, _ws(session, space)), cursor), "перечитали базу - новых документов нет"


def test_nachisleniyam_hvataet_dokumentov_yadra(space, monkeypatch):
    with finance_session() as session:
        Money(session, _ws(session, space), today=TODAY)
    parts_seen: list[tuple] = []
    original = journal.post

    def counting(session, workspace, parts=journal.PARTS, contracts=None, **keys):
        parts_seen.append((tuple(parts), sorted(contracts) if contracts is not None else None))
        return original(session, workspace, parts, contracts=contracts, **keys)

    monkeypatch.setattr(journal, "post", counting)
    with finance_session() as session:
        workspace = _ws(session, space)
        changes.set_adjustment(session, workspace, FULL, OWNER, space["alfa"], "2026-07", amount="40000", reason="скидка за простой")
        contract = service.get_contract(session, workspace, space["alfa"])
        schedule = Ledger(session, workspace.id, [contract]).schedule(contract, since=date(2026, 6, 1), until=date(2026, 8, 1))
        assert [(item.month, item.amount) for item in schedule.accruals] == [
            ("2026-06", Decimal("100000")), ("2026-07", Decimal("40000")), ("2026-08", Decimal("100000")),
        ]
    assert parts_seen == [(("kort",), [space["alfa"]])], (
        "правка договора не стоит начислениям ни разнесения журнала денег, ни документов всей компании"
    )
    with finance_session() as session:
        workspace = _ws(session, space)
        adjustment = [fact for fact in _facts(session, space) if fact.kind == "adjustment"]
        assert len(adjustment) == 1 and adjustment[0].period == date(2026, 7, 1) and adjustment[0].author == OWNER.user_id
        assert adjustment[0].data["reason"] == "скидка за простой"
        paused = session.scalar(
            sa.select(ListValue.id).where(ListValue.workspace_id == workspace.id, ListValue.field_key == "status")
            .where(ListValue.meaning["phase"].as_string() == "suspended")
        )
        assert paused is not None, "«Приостановлен» засевается каждой компании"
        changes.status_from(session, workspace, FULL, OWNER, space["alfa"], paused, date(2026, 8, 1))
        contract = service.get_contract(session, workspace, space["alfa"])
        schedule = Ledger(session, workspace.id, [contract]).schedule(contract, since=date(2026, 6, 1), until=date(2026, 8, 1))
        assert schedule.paused == ["2026-08"], "соглашение из журнала видно сразу, в той же транзакции"
        agreement = [fact for fact in _facts(session, space) if fact.kind == "agreement"]
        assert len(agreement) == 1 and agreement[0].day == date(2026, 8, 1) and agreement[0].data["applied"] is True


def test_dokumenty_1c_provodyatsya_i_chitayutsya(space):
    with finance_session() as session:
        base = OnecBase(
            workspace_id=space["workspace"], title="ИБ ТОО BBC", url="https://1cfresh.kz/a/ea201/1", login="odata.user",
            secret="-", group_entity_id=space["bbc"], organization_ref="org", sync_from=date(2026, 1, 1),
        )
        session.add(base)
        session.flush()

        def doc(kind: str, **values) -> OnecDocument:
            item = OnecDocument(workspace_id=base.workspace_id, base_id=base.id, kind=kind, ref=str(uuid.uuid4()), **values)
            session.add(item)
            session.flush()
            return item

        invoice = doc("invoice", doc_date=date(2026, 6, 28), number="00000000514", party_name='ТОО "Альфаком"',
                      contract_number="ОКР/15", amount=Decimal("100000"), period_from=date(2026, 7, 1))
        payment = doc("payment", doc_date=date(2026, 7, 3), party_name="ТОО АЛЬФАКОМ", basis_ref=invoice.ref,
                      amount=Decimal("100000"), posted=True)
        sale = doc("sale", doc_date=date(2026, 7, 31), number="00000000464", party_name='ТОО "Альфаком"',
                   contract_number="ОКР/15", amount=Decimal("100000"), period_from=date(2026, 7, 1), posted=True,
                   signed_at=date(2026, 8, 2))
        sf = doc("sf", doc_date=date(2026, 8, 1), number="00000000464", party_name='ТОО "Альфаком"', basis_ref=sale.ref,
                 status="Электронно")
        esf = doc("esf", doc_date=date(2026, 8, 3), party_name='ТОО "Альфаком"', basis_ref=sf.ref, status="Доставлен")
        ids = {name: item.id for name, item in {"invoice": invoice, "payment": payment, "sale": sale, "sf": sf, "esf": esf}.items()}
    with finance_session() as session:
        workspace = _ws(session, space)
        acts = acts_module.Acts(session, workspace, FULL, None, today=TODAY)
        act = acts.months[space["alfa"]]["2026-07"].chosen
        assert (act.source, act.number, act.day, act.signed, act.esf, act.esf_state) == (
            "1c", "464", date(2026, 7, 31), date(2026, 8, 2), date(2026, 8, 3), "Доставлен"
        ), "ЭСФ сильнее счёта-фактуры"
        facts = {fact.key: fact for fact in _facts(session, space) if fact.source == "1c"}
        kinds = {name: facts[f"1c:{value}"].kind for name, value in ids.items()}
        assert kinds == {"invoice": "bill", "payment": "payment", "sale": "act", "sf": "esf", "esf": "esf"}
        assert facts[f"1c:{ids['esf']}"].data["act"] == f"1c:{ids['sale']}"
        assert facts[f"1c:{ids['payment']}"].data["months"] == ["2026-07", "2026-07"], "оплата - за месяц своего счёта"
        assert all(fact.contract_id == space["alfa"] for fact in facts.values())
        # Источник оплат BBC - журнал денег: оплата 1С в журнале есть, но в оплатах договора - нет.
        assert not Money(session, workspace, today=TODAY).by_contract.get(space["alfa"])
        from app.finance.onec import service as onec_service

        onec_service.decide(session, workspace, ids["sale"], action="none", contract_id=None, user_id=OWNER.user_id)
    with finance_session() as session:
        acts = acts_module.Acts(session, _ws(session, space), FULL, None, today=TODAY)
        assert "2026-07" not in acts.months.get(space["alfa"], {}), "«не по договору» - решение человека"
        decision = _by_key(session, space, f"1cm:{ids['sale']}")
        assert decision and decision[0].kind == "decision" and decision[0].data["action"] == "none"
        assert decision[0].author == OWNER.user_id


# ── Вызовы ───────────────────────────────────────────────────────────────────


def test_chto_bylo_s_dogovorom_i_chto_izmenilos(space):
    with finance_session() as session:
        operation_id = _operation(session, space, "ТОО Альфаком", "100000", date(2026, 6, 5), month="2026-06").id
        _operation(session, space, "ТОО Альфаком", "50000", date(2026, 7, 6), month="2026-07")
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = service.get_contract(session, workspace, space["alfa"])
        first = calls.changes(session, workspace, FULL, since="0")
        cursor = first["cursor"]
        assert {item["key"] for item in first["facts"]} >= {f"op:{operation_id}"}
        out = calls.contract_facts(session, workspace, FULL, contract)
        assert [item["kind"] for item in out["facts"]] == ["payment", "payment"]
        explained = calls.explain(session, workspace, FULL, contract, day=date(2026, 7, 31))
        # Сальдо нет, начислено июнь и июль - 200 000, оплачено 150 000.
        assert (explained["debt"], explained["accrued"], explained["paid"]) == ("50000.00", "200000.00", "150000.00")
        assert explained["check"] == explained["debt"], "долг складывается из кусков"
        assert [item["fact"] for item in explained["payments"]][0] == f"op:{operation_id}"
        finance_service.update_operation(session, workspace, operation_id, {"amount": Decimal("90000")})
    with finance_session() as session:
        workspace = _ws(session, space)
        later = calls.changes(session, workspace, FULL, since=cursor)
        assert [item["key"] for item in later["facts"]] == [f"op:{operation_id}"]
        assert later["facts"][0]["amount"] == "90000.00" and later["facts"][0]["replaces"]
        assert calls.changes(session, workspace, FULL, since=later["cursor"])["facts"] == []
        paged = calls.changes(session, workspace, FULL, since="0", limit=1)
        assert paged["more"] and ":" in paged["cursor"], "проведение больше выборки - курсор с ключом"
        history_out = calls.contract_facts(session, workspace, FULL, service.get_contract(session, workspace, space["alfa"]), history=True)
        assert len(history_out["facts"]) == 3, "с историей - и прежняя версия"
        with pytest.raises(PermissionError):
            calls.changes(session, workspace, service.Access(view=True, edit=True), since="0")


@pytest.fixture
def app(finance_db, monkeypatch) -> FastAPI:
    from app.core.config import settings
    from app.finance import audit as audit_module
    from app.finance import auth
    from finance_routes import finance_app

    monkeypatch.setattr(settings, "environment", "test")
    auth._ATTEMPTS.clear()
    audit_module._VIEWS.clear()
    return finance_app()


def test_marshruty_fakty_dogovora_i_potok(app: FastAPI) -> None:
    from test_finance_access import BASE, activate, employee, grant, register

    owner = register(app)
    assert owner.post(f"{BASE}/contracts/setup/entities", json={"name": "BBC", "code": "BBC"}).status_code == 201
    made = owner.post(
        f"{BASE}/contracts",
        json={"values": {"number": "№ОКР/1", "executor": "BBC", "customer": "ТОО Альфаком", "amount": "100000",
                         "billing": "month", "signed_at": "2026-06-01"}},
    )
    assert made.status_code == 201, made.text
    contract_id = made.json()["contract"]["id"]
    put = owner.post(
        f"{BASE}/core/contracts/{contract_id}/adjustments", json={"month": "2026-07", "skip": True, "reason": "простой"}
    )
    assert put.status_code == 201, put.text
    response = owner.get(f"{BASE}/core/contracts/{contract_id}/facts")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(item["kind"], item["month"], item["author"]) for item in body["facts"]] == [
        ("adjustment", "2026-07", "Ермеков Нурболат")
    ]
    assert "explain" in body
    feed = owner.get(f"{BASE}/core/facts", params={"since": "0"})
    assert feed.status_code == 200 and [item["kind"] for item in feed.json()["facts"]] == ["adjustment"]
    assert owner.get(f"{BASE}/core/facts", params={"since": "x"}).status_code == 400

    # Без журнала денег - факты договора с деньгами закрыты; поток всей компании - только владельцу.
    person = employee(owner, "Бухгалтерова Айгерим", "+77051130001")
    grant(owner, "employee", person["id"], {"contracts": "view"})
    reader = activate(app, "+77051130001", ip="10.0.3.1")
    assert reader.get(f"{BASE}/core/contracts/{contract_id}/facts").status_code == 403
    grant(owner, "employee", person["id"], {"journal": "view"})
    assert reader.get(f"{BASE}/core/contracts/{contract_id}/facts").status_code == 200
    assert reader.get(f"{BASE}/core/facts").status_code == 403
