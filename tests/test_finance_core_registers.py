"""Регистры итогов и проведение по ключам (этап 2, шаг 2).

Набор держит то, ради чего шаг сделан:

* регистр - тот же итог, что живой расчёт: долги всех оснований, сторон и
  групп, выручка, оборот и НДС совпадают до тенге;
* регистр пересчитывает только договор, чьи входы сменились: оплату, условия,
  соглашение; правка, которая денег не трогает, не пересчитывает ничего, а
  смена настроек оплат - всё;
* оплата будущим числом входит в учёт в свой день;
* журнал проводит то, что изменилось: операцию - эту операцию, форму реестра -
  всю часть, примечание договора - ничего; номер правки без строки очереди
  (писал процесс, не знающий очереди) - всё; savepoint не теряет правку.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.finance import history
from app.finance import service as finance_service
from app.finance.contracts import service, setup
from app.finance.contracts.fields import bump
from app.finance.contracts.fields import current as counter
from app.finance.contracts.models import Contract, GroupEntity
from app.finance.core import changes, journal, opening, registers
from app.finance.core import debts as core_debts
from app.finance.core import revenue as core_revenue
from app.finance.core import settings as core_settings
from app.finance.core import vat as core_vat
from app.finance.core.models import FactInput, RegisterAccrual, RegisterContract
from app.finance.core.payments import Money
from app.finance.db import finance_session

FULL = service.Access(view=True, edit=True, setup=True, admin=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
TODAY = date(2026, 10, 25)
SINCE = date(2026, 6, 1)


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    monkeypatch.setattr(journal, "_CHECKED", set())
    # Регистр ведётся на сегодня: «сегодня» набора - 25.10.2026.
    monkeypatch.setattr(service, "today", lambda: TODAY)
    yield engine


@pytest.fixture
def audit():
    token = history.CONTEXT.set(history.AuditContext(user_id=OWNER.user_id))
    yield
    history.CONTEXT.reset(token)


def _contract(session, workspace, customer: str, number: str, *, signed: str, amount: str, **extra) -> Contract:
    return service.create(
        session, workspace, FULL, OWNER,
        {"executor": "BBC", "customer": customer, "number": number, "amount": amount, "billing": "month",
         "signed_at": signed, **extra},
    )


def _income(session, workspace, account_id, payer: str, amount: str, day: date, *, month: str = ""):
    party = finance_service.ensure_counterparty(session, workspace.id, payer, role="client")
    start = date(int(month[:4]), int(month[5:7]), 1) if month else None
    return finance_service.create_operation(
        session, workspace,
        finance_service.OperationInput(
            kind="income", paid_at=day, amount=Decimal(amount), counterparty_id=party.id, period_start=start,
            period_end=start, account_to_id=account_id,
        ),
    )


@pytest.fixture
def space(finance_db, audit) -> dict:
    """BBC: Альфаком 100 000 в месяц с июня с сальдо 120 000, Бетаком 50 000 с 15.07 с поправкой августа, оплаты."""
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC", code="BBC")
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ОКР/15", signed="2026-06-01", amount="100000")
        beta = _contract(session, workspace, "ТОО Бетаком", "№ОКР/16", signed="2026-07-15", amount="50000")
        gamma = _contract(session, workspace, "ТОО Гаммаком", "№ОКР/17", signed="2025-11-10", amount="30000")
        entity = session.scalar(sa.select(GroupEntity.counterparty_id).where(GroupEntity.code == "BBC"))
        account = finance_service.create_account(session, workspace, name='Kaspi Юр. ТОО "BBC"', group_entity_id=entity)
        core_settings.set_payments(session, workspace, FULL, OWNER, since=SINCE)
        opening.set_one(session, workspace, FULL, OWNER, alfa.id, since=SINCE, amount=Decimal("120000"), basis="accruals")
        changes.set_adjustment(session, workspace, FULL, OWNER, beta.id, "2026-08", amount="20000", reason="простой")
        for payer, amount, day, month in (
            ("ТОО Альфаком", "150000", date(2026, 6, 10), ""),
            ("ТОО Альфаком", "100000", date(2026, 8, 5), "2026-08"),
            ("ТОО Альфаком", "100000", date(2026, 9, 5), "2026-09"),
            ("ТОО Бетаком", "120000", date(2026, 9, 20), ""),
            ("ТОО Гаммаком", "30000", date(2026, 7, 1), "2026-07"),
            ("ИП Незнакомец", "7000", date(2026, 7, 9), ""),
        ):
            _income(session, workspace, account.id, payer, amount, day, month=month)
        return {"workspace": workspace.id, "alfa": alfa.id, "beta": beta.id, "gamma": gamma.id, "account": account.id}


def _ws(session, space):
    return finance_service.get_workspace(session, space["workspace"])


def _numbers(session, workspace) -> str:
    out: dict = {}
    for basis in core_debts.BASES:
        for side in core_debts.SIDES:
            debts = core_debts.Debts(session, workspace, FULL, day=TODAY, basis=basis, side=side)
            out[f"{basis} {side}"] = {group: debts.out(group) for group in core_debts.GROUPS}
    for year in (2025, 2026, 2027):
        revenue = core_revenue.Revenue(session, workspace, FULL, year=year, today=TODAY)
        out[f"revenue {year}"] = {group: revenue.out(group) for group in core_revenue.GROUPS}
        turnover = core_vat.Turnover(session, workspace, FULL, year, today=TODAY)
        out[f"vat {year}"] = core_vat.year_out(turnover, turnover.run())
    return json.dumps(out, sort_keys=True, default=str)


def _built(space) -> int:
    with finance_session() as session:
        return registers.Registers(session, _ws(session, space), today=service.today()).recomputed


# ── Регистр = живой расчёт ───────────────────────────────────────────────────


def test_registr_daet_te_zhe_chisla_chto_zhivoy_raschet(space, monkeypatch):
    with finance_session() as session:
        workspace = _ws(session, space)
        stored = _numbers(session, workspace)
        assert session.scalar(sa.select(sa.func.count()).select_from(RegisterContract)) == 3
        monkeypatch.setattr(service, "today", lambda: date(1900, 1, 1))  # регистр выключен
        live = _numbers(session, workspace)
    assert stored == live
    debts = json.loads(stored)["accruals receivable"]["client"]
    owed = {row["title"]: row["owed"] for row in debts["rows"]}
    # Альфаком: сальдо 120 000 + июнь-октябрь 500 000 - оплаты 350 000 = 270 000.
    assert owed["ТОО Альфаком"] == "270000.00"
    # Бетаком с 15.07: июль 50 000, август поправкой 20 000, сентябрь и октябрь по 50 000 - оплачено 120 000.
    assert owed["ТОО Бетаком"] == "50000.00"


def test_pereschityvaetsya_tolko_izmenennyy_dogovor(space, monkeypatch):
    assert _built(space) == 3, "первое чтение строит регистр всех договоров"
    assert _built(space) == 0, "без правок - читает готовое"
    with finance_session() as session:
        workspace = _ws(session, space)
        _income(session, workspace, space["account"], "ТОО Альфаком", "100000", date(2026, 10, 5), month="2026-10")
    assert _built(space) == 1, "новая оплата Альфакома - пересчёт одного договора"
    with finance_session() as session:
        debts = core_debts.Debts(session, _ws(session, space), FULL, day=TODAY)
        assert debts.items[space["alfa"]].owed == Decimal("170000.00")
    # Примечание денег не трогает: ни журнал, ни регистр.
    posted: list = []
    original = journal.post
    monkeypatch.setattr(journal, "post", lambda *args, **keys: posted.append(keys) or original(*args, **keys))
    with finance_session() as session:
        session.get(Contract, space["beta"]).note = "позвонить бухгалтеру"
    assert _built(space) == 0
    assert posted == [], "правка примечания журнал не проводит"
    # Дата начала - условие начисления: пересчитан один договор, журнал - форма реестра.
    with finance_session() as session:
        session.get(Contract, space["beta"]).signed_at = date(2026, 8, 1)
    assert _built(space) == 1
    with finance_session() as session:
        schedule = registers.of(session, _ws(session, space), today=TODAY).ledger([session.get(Contract, space["beta"])])
        months = [item.month for item in schedule.accruals(session.get(Contract, space["beta"]), since=SINCE, until=date(2026, 10, 1))]
        assert months == ["2026-08", "2026-09", "2026-10"]


def test_nastroyki_oplat_pereschityvayut_vse(space):
    assert _built(space) == 3
    with finance_session() as session:
        core_settings.set_payments(session, _ws(session, space), FULL, OWNER, since=date(2026, 7, 1))
    assert _built(space) == 3, "начало учёта - вход всех договоров"
    with finance_session() as session:
        assert registers.of(session, _ws(session, space), today=TODAY).since == date(2026, 7, 1)


def test_oplata_budushchim_chislom_vhodit_v_svoy_den(space, monkeypatch):
    with finance_session() as session:
        _income(session, _ws(session, space), space["account"], "ТОО Гаммаком", "30000", TODAY + timedelta(days=3), month="2026-10")
    with finance_session() as session:
        built = registers.Registers(session, _ws(session, space), today=TODAY)
        item = built.settlements()[space["gamma"]]
        row = session.get(RegisterContract, (space["workspace"], space["gamma"]))
        assert row.valid_until == TODAY + timedelta(days=2)
        assert item.paid == Decimal("30000.00"), "оплата 28.10 в учёт 25.10 не входит"
    later = TODAY + timedelta(days=3)
    monkeypatch.setattr(service, "today", lambda: later)
    with finance_session() as session:
        built = registers.Registers(session, _ws(session, space), today=later)
        assert built.recomputed == 1, "в свой день - пересчёт одного договора"
        assert built.settlements()[space["gamma"]].paid == Decimal("60000.00")


def test_udalennyy_dogovor_uhodit_iz_registra(space):
    assert _built(space) == 3
    with finance_session() as session:
        session.get(Contract, space["beta"]).deleted_at = datetime.now(timezone.utc)
    _built(space)
    with finance_session() as session:
        left = set(session.scalars(sa.select(RegisterContract.contract_id)))
        assert left == {space["alfa"], space["gamma"]}
        assert not session.scalar(sa.select(sa.func.count()).select_from(RegisterAccrual).where(RegisterAccrual.contract_id == space["beta"]))


# ── Проведение по ключам ─────────────────────────────────────────────────────


def _spy(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    original = journal.post

    def counting(session, workspace, parts=journal.PARTS, **keys):
        calls.append({"parts": tuple(parts), **{key: set(value) for key, value in keys.items() if value is not None}})
        return original(session, workspace, parts, **keys)

    monkeypatch.setattr(journal, "post", counting)
    return calls


def test_zhurnal_provodit_tolko_izmenennoe(space, monkeypatch):
    with finance_session() as session:
        Money(session, _ws(session, space), today=TODAY)
    calls = _spy(monkeypatch)
    with finance_session() as session:
        workspace = _ws(session, space)
        operation = _income(session, workspace, space["account"], "ТОО Бетаком", "50000", date(2026, 10, 20), month="2026-10")
        operation_id = operation.id
    with finance_session() as session:
        rows = list(session.execute(sa.select(FactInput.part, FactInput.ref)))
        assert rows == [("journal", operation_id)], "в очереди - одна операция"
        money = Money(session, _ws(session, space), today=TODAY)
        assert f"op:{operation_id}" in {payment.key for payment in money.by_contract[space["beta"]]}
    assert calls == [{"parts": ("journal",), "operations": {operation_id}}], "проведена одна операция, без 1С и документов"
    with finance_session() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(FactInput)) == 0, "проведённое уходит из очереди"
    calls.clear()
    # Сумма договора - форма реестра: разнесение и 1С заново, документы - нет.
    with finance_session() as session:
        session.get(Contract, space["gamma"]).amount = Decimal("31000")
    with finance_session() as session:
        Money(session, _ws(session, space), today=TODAY)
    assert sorted(call["parts"] for call in calls) == [("1c",), ("journal",)]
    assert all("operations" not in call for call in calls)


def test_nomer_pravki_bez_ocheredi_provodit_vse(space, monkeypatch):
    with finance_session() as session:
        Money(session, _ws(session, space), today=TODAY)
    calls = _spy(monkeypatch)
    # Так сдвигает счётчик процесс, который очереди не знает (выкладка).
    with finance_session() as session:
        bump(session, space["workspace"], journal.INPUTS)
    with finance_session() as session:
        Money(session, _ws(session, space), today=TODAY)
    assert sorted(call["parts"] for call in calls) == [("1c",), ("journal",), ("kort",)]


def test_savepoint_ne_teryaet_pravku(space):
    with finance_session() as session:
        Money(session, _ws(session, space), today=TODAY)
        before = counter(session, space["workspace"], journal.INPUTS)
    with finance_session() as session:
        workspace = _ws(session, space)
        operation_id = _income(session, workspace, space["account"], "ТОО Бетаком", "50000", date(2026, 10, 21)).id
        # Первый `bump` нового счётчика открывает savepoint: его коммит - не коммит сессии.
        bump(session, space["workspace"], "test.savepoint")
    with finance_session() as session:
        assert counter(session, space["workspace"], journal.INPUTS) == before + 1
        assert list(session.execute(sa.select(FactInput.part, FactInput.ref))) == [("journal", operation_id)]
