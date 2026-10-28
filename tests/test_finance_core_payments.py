"""Оплаты ядра (фаза 4): журнал с «№Дог.» и «ОПиУ период», разнесение по договору и месяцу.

Номер договора из журнала - довод сильнее плательщика (платят через Kaspi Pay
и другие ТОО), но спор номера и плательщика решает человек. Месяц - сначала
«за какой месяц» источника, остаток - самому старому долгу: входящему сальдо,
потом начислениям по порядку.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.finance import history, importing
from app.finance import service as finance_service
from app.finance.contracts import restore, service, setup
from app.finance.contracts.models import GroupEntity
from app.finance.contracts.payments import allocate
from app.finance.core import book as book_module
from app.finance.core import opening, participants
from app.finance.core import payments as core_payments
from app.finance.core import settings as core_settings
from app.finance.core.models import Document
from app.finance.db import finance_session

import company_layer


@pytest.fixture(autouse=True)
def _company_layer(monkeypatch):
    """Компания теста - с шапкой таблиц копии прода: её знает словарь компании, а не ядро (п.9 «Пакеты»)."""
    company_layer.everywhere(monkeypatch)


FULL = service.Access(view=True, edit=True, setup=True, admin=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
TODAY = date(2026, 10, 25)
ACCOUNT = 'Kaspi Юр. ТОО "BBC"'


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    yield engine


@pytest.fixture
def space(finance_db):
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC", code="BBC")
        return workspace.id


@pytest.fixture
def audit():
    token = history.CONTEXT.set(history.AuditContext(user_id=OWNER.user_id))
    yield
    history.CONTEXT.reset(token)


def _entity(session, code: str) -> uuid.UUID:
    return session.scalar(sa.select(GroupEntity.counterparty_id).where(GroupEntity.code == code))


def _contract(session, workspace, customer: str, number: str, amount: str = "100000", signed: str = "2026-06-01"):
    return service.create(
        session, workspace, FULL, OWNER,
        {"executor": "BBC", "customer": customer, "number": number, "amount": amount, "billing": "month", "signed_at": signed},
    )


def _account(session, workspace, name: str = ACCOUNT, entity: str = "BBC"):
    return finance_service.create_account(session, workspace, name=name, group_entity_id=_entity(session, entity))


def _income(
    session, workspace, account, payer: str | None, amount: str, day: date, *, ref: str = "", month: str = "", label: str = ""
):
    party = finance_service.ensure_counterparty(session, workspace.id, payer, role="client") if payer else None
    start = date(int(month[:4]), int(month[5:7]), 1) if month else None
    return finance_service.create_operation(
        session, workspace,
        finance_service.OperationInput(
            kind="income", paid_at=day, amount=Decimal(amount), account_to_id=account.id,
            counterparty_id=party.id if party else None, contract_ref=ref, period_start=start, period_end=start,
            period_label=label,
        ),
    )


# ── Разбор журнала ───────────────────────────────────────────────────────────


def test_zhurnal_nomer_dogovora_mesyac_i_kommentarii():
    rows = [
        ["ОПиУ период", "Дата", "Счет", " Приход ", " Расход ", " Контрагент ", " №Дог. ", " Комментарии ", "ДДС Месяц"],
        ["ИЮНЬ 2026", "пн 01.06.26 ", ACCOUNT, "200 000,00", "", 'ТОО "Альфаком"', "№ОКР/15", "Оплата за июнь", "ИЮНЬ"],
        ["Старые (до Май/Июня 2026)", "вт 02.06.26 ", ACCOUNT, "50 000,00", "", "ИП Пазылова", "", "старый долг", "ИЮНЬ"],
        ["2027 год", "ср 03.06.26 ", ACCOUNT, "10 000,00", "", "ИП Пазылова", "", "аванс", "ИЮНЬ"],
    ]
    preview = importing.analyze_rows(rows, "Журнал", [ACCOUNT])
    first, old, year = preview.rows
    assert first.state == "imported", first.problems
    assert first.values["contract_ref"] == "№ОКР/15"
    assert (first.values["period_start"], first.values["period_end"]) == ("2026-06-01", "2026-06-30")
    assert first.values["accrued_at"] == "2026-06-01", "месяц отчёта о прибылях - дата начисления"
    assert first.values["comment"] == "Оплата за июнь", "«Комментарии» - колонка комментария"
    assert old.values["period_start"] is None and old.values["accrued_at"] is None, "«Старые (…)» - не месяц"
    assert old.values["period_label"] == "Старые (до Май/Июня 2026)", "подпись периода остаётся у операции"
    assert first.values["period_label"] == "" and year.values["period_label"] == ""
    assert (year.values["period_start"], year.values["period_end"]) == ("2027-01-01", "2027-12-31")
    assert year.values["accrued_at"] is None, "год - период, но не дата начисления"
    assert "ДДС Месяц" in " ".join(preview.unused_columns), "месяц движения денег - не месяц начисления"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ИЮНЬ 2026", ("2026-06-01", "2026-06-30", "month")),
        ("мая 2026 г.", ("2026-05-01", "2026-05-31", "month")),
        ("06.2026", ("2026-06-01", "2026-06-30", "month")),
        ("2026-02", ("2026-02-01", "2026-02-28", "month")),
        ("2027 год", ("2027-01-01", "2027-12-31", "year")),
        ("3 квартал 2026", ("2026-07-01", "2026-09-30", "quarter")),
        ("IV кв. 2026", ("2026-10-01", "2026-12-31", "quarter")),
        ("Q1 2027", ("2027-01-01", "2027-03-31", "quarter")),
        ("Старые (до Май/Июня 2026)", None),
        ("6", None),
        (6, None),
        ("", None),
    ],
)
def test_period_tolko_zapis_celikom(raw, expected):
    span = importing.parse_period(raw, importing.DateReading())
    assert (tuple(item if isinstance(item, str) else item.isoformat() for item in span) if span else None) == expected


# ── Договор оплаты ───────────────────────────────────────────────────────────


def test_nomer_dogovora_silnee_platelshchika_a_spor_reshaet_chelovek(space):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ОКР/15")
        beta = _contract(session, workspace, "ТОО Бетаком", "№ОКР/16")
        account = _account(session, workspace)
        via_pay = _income(session, workspace, account, "Опл. через Pay ИП Пазылова", "100000", date(2026, 6, 2), ref="№ ОКР/15")
        argued = _income(session, workspace, account, "ТОО Бетаком", "100000", date(2026, 6, 3), ref="№ОКР/15")
        plain = _income(session, workspace, account, "ТОО Альфаком", "100000", date(2026, 6, 4))
        nowhere = _income(session, workspace, account, None, "100000", date(2026, 6, 5), ref="№НЕТ/1")
        registry = service.Registry(session, workspace)
        result = allocate(session, workspace, registry, [alfa, beta])
        paid = {operation.id: how for operation, how in result.paid.get(alfa.id, [])}
        assert paid == {via_pay.id: "number", plain.id: "auto"}, "плательщик не узнан - решает номер"
        assert argued.id in {item.id for item in result.open.get(alfa.id, [])}
        assert argued.id in {item.id for item in result.open.get(beta.id, [])}, "номер и плательщик спорят"
        assert all(nowhere.id not in {item.id for item in items} for items in result.open.values())


def test_neznakomyy_nomer_ne_zamenyaetsya_platelshchikom(space):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ЮО/35")
        account = _account(session, workspace)
        other = _income(session, workspace, account, "ТОО Альфаком", "300000", date(2026, 6, 2), ref="№ЮО/35-2")
        spelled = _income(session, workspace, account, "ТОО Альфаком", "300000", date(2026, 7, 2), ref="ЮО-35")
        result = allocate(session, workspace, service.Registry(session, workspace), [alfa])
        assert [(operation.id, how) for operation, how in result.paid.get(alfa.id, [])] == [(spelled.id, "number")], (
            "«ЮО-35» - тот же номер другой рукой, «ЮО/35-2» - другой договор"
        )
        listed = core_payments.Money(session, workspace, today=TODAY).without_contract()
        assert [(item["operation_id"], [c["why"] for c in item["candidates"]]) for item in listed] == [(str(other.id), ["клиент"])]


def test_latinica_dvoynik_ta_zhe_storona(space):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        eco = _contract(session, workspace, "ТОО ECO Bazar", "№НО/40")
        account = _account(session, workspace)
        # «ЕСО» - кириллицей: так банк набрал имя клиента.
        paid = _income(session, workspace, account, 'ТОО "ЕСО Bazar"', "100000", date(2026, 6, 2))
        result = allocate(session, workspace, service.Registry(session, workspace), [eco])
        assert [(operation.id, how) for operation, how in result.paid.get(eco.id, [])] == [(paid.id, "auto")]


def test_napisanie_neskolkih_klientov_ne_uznayot_storonu(space):
    from app.finance.contracts.models import CounterpartyName
    from app.finance.contracts.fields import party_key

    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        alfa = _contract(session, workspace, "ИП Алтын", "№ЮО/1", amount="125000")
        beta = _contract(session, workspace, "ИП Жанар", "№ЮО/2", amount="99000")
        bank = 'Филиал "Южная столица" АО "Kaspi Bank" в г. Алматы'
        for contract in (alfa, beta):
            session.add(CounterpartyName(
                workspace_id=workspace.id, counterparty_id=contract.customer_id, name=bank, normalized=party_key(bank), source="bank",
            ))
        session.flush()
        account = _account(session, workspace)
        paid = _income(session, workspace, account, bank, "125000", date(2026, 6, 2))
        result = allocate(session, workspace, service.Registry(session, workspace), [alfa, beta])
        assert not result.paid and not result.open, "сумма совпала у одного - но сторону это не называет"
        listed = core_payments.Money(session, workspace, today=TODAY).without_contract()
        assert [item["operation_id"] for item in listed] == [str(paid.id)]
        assert {item["why"] for item in listed[0]["candidates"]} == {"клиент"}


def test_shapka_vnutri_dannyh_ne_prosit_zavesti_schet():
    rows = [
        ["Дата", "Счет", "Приход", "Расход", "Контрагент"],
        ["Дата", "ТОО", "Сумма", "СтоимостьуслугKaspi", ""],
        ["01.06.2026", ACCOUNT, "140 000,00", "", "ИП Пазылова"],
    ]
    preview = importing.analyze_rows(rows, "Pay Журнал", [ACCOUNT])
    assert preview.accounts_missing == [], "«ТОО» из повтора шапки - не счёт"
    assert preview.rows[0].state == "failed" and preview.rows[1].state == "imported"


# ── Месяц оплаты ─────────────────────────────────────────────────────────────


def test_mesyac_po_podskazke_ostatok_starshemu_dolgu_saldo_pervym(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ОКР/15")
        account = _account(session, workspace)
        core_settings.set_payments(session, workspace, FULL, OWNER, since=date(2026, 6, 1))
        opening.set_one(session, workspace, FULL, OWNER, alfa.id, since=date(2026, 6, 1), amount=Decimal("50000"))
        _income(session, workspace, account, "ТОО Альфаком", "100000", date(2026, 6, 5), month="2026-06")
        _income(session, workspace, account, "ТОО Альфаком", "150000", date(2026, 7, 10))
        _income(session, workspace, account, "ТОО Альфаком", "100000", date(2026, 7, 20), month="2026-09")
        _income(session, workspace, account, "ТОО Альфаком", "30000", date(2026, 8, 1))
        money = core_payments.Money(session, workspace, today=TODAY).contract(alfa)
        paid = {key: bucket.paid for key, bucket in money.buckets.items()}
        assert paid[core_payments.OPENING] == Decimal("50000"), "без подсказки - сначала входящее сальдо"
        assert paid["2026-06"] == Decimal("100000") and paid["2026-07"] == Decimal("100000")
        assert paid["2026-08"] == Decimal("30000") and paid["2026-09"] == Decimal("100000"), "сентябрь - по подсказке, заранее"
        assert paid["2026-10"] == 0
        assert [part.by for part in money.buckets["2026-09"].parts] == ["hint"]
        assert money.debt_on(TODAY) == Decimal("50000") + 5 * Decimal("100000") - Decimal("380000")
        out = money.out(today=TODAY)
        assert out["debt"] == "170000.00" and out["opening"]["paid"] == "50000.00"


def test_podskazka_ne_bolshe_nachislennogo_i_sverh_nachisleniy(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ОКР/15", signed="2026-09-01")
        account = _account(session, workspace)
        core_settings.set_payments(session, workspace, FULL, OWNER, since=date(2026, 9, 1))
        _income(session, workspace, account, "ТОО Альфаком", "250000", date(2026, 9, 3), month="2026-09")
        money = core_payments.Money(session, workspace, today=TODAY).contract(alfa)
        assert money.buckets["2026-09"].paid == Decimal("100000"), "месяц по подсказке - не больше начисленного"
        assert money.buckets["2026-10"].paid == Decimal("100000")
        assert money.buckets["2026-11"].paid == Decimal("50000"), "остаток - предоплата следующего месяца"
        assert not money.over
        assert money.debt_on(TODAY) == Decimal("-50000"), "аванс - долг со знаком минус"


def test_mesyac_podskazki_bez_nachisleniya_i_dogovor_bez_nachisleniy(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        monthly = _contract(session, workspace, "ТОО Альфаком", "№ОКР/15", signed="2026-07-01")
        terms = service.create(
            session, workspace, FULL, OWNER,
            {"executor": "BBC", "customer": "ТОО Бетаком", "number": "№НО/41", "billing": "terms", "signed_at": "2026-06-01"},
        )
        account = _account(session, workspace)
        core_settings.set_payments(session, workspace, FULL, OWNER, since=date(2026, 6, 1))
        _income(session, workspace, account, "ТОО Альфаком", "40000", date(2026, 6, 20), month="2026-06")
        _income(session, workspace, account, "ТОО Бетаком", "15000000", date(2026, 7, 9), month="2026-07")
        _income(session, workspace, account, "ТОО Бетаком", "500000", date(2026, 8, 12))
        money = core_payments.Money(session, workspace, today=TODAY)
        alfa = money.contract(monthly)
        assert alfa.buckets["2026-06"].paid == Decimal("40000"), "июня нет в начислениях - оплата всё равно июньская"
        assert alfa.buckets["2026-06"].accrued == 0 and alfa.buckets["2026-07"].paid == 0
        beta = money.contract(terms)
        assert not beta.accrues and not beta.over
        assert beta.buckets["2026-07"].paid == Decimal("15000000") and beta.buckets["2026-08"].paid == Decimal("500000")
        assert [part.by for part in beta.buckets["2026-08"].parts] == ["date"]
        assert beta.out(today=TODAY)["debt"] is None, "долг договора без начислений неизвестен"


def test_bez_dogovora_ne_perevody_i_ne_otmechennoe(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        account = _account(session, workspace, name='Kaspi Юр. ТОО "Big Business Consulting"')
        _income(session, workspace, account, 'ТОО "Big Business Consulting"', "900000", date(2026, 6, 3))
        _income(session, workspace, account, "Вознаграждение", "12", date(2026, 6, 4))
        _income(session, workspace, account, "ИП Клиентов", "50000", date(2026, 6, 5))
        money = core_payments.Money(session, workspace, today=TODAY)
        assert [item["payer"] for item in money.without_contract()] == ["ИП Клиентов", "Вознаграждение"]
        assert money.hidden["own"] == 1, "ТОО, на чьё имя наш счёт, - перевод своих денег"
        core_settings.set_ignore(session, workspace, FULL, OWNER, kind="payers", name="вознаграждение ", on=True)
        again = core_payments.Money(session, workspace, today=TODAY)
        assert [item["payer"] for item in again.without_contract()] == ["ИП Клиентов"]
        assert again.hidden == {"own": 1, "ignored": 1, "decided": 0, "returned": 0, "deposit": 0}
        core_settings.set_ignore(session, workspace, FULL, OWNER, kind="payers", name="Вознаграждение", on=False)
        assert len(core_payments.Money(session, workspace, today=TODAY).without_contract()) == 2


def test_starye_tolko_saldo_a_predoplata_do_ucheta_v_svoy_mesyac(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ОКР/15")
        account = _account(session, workspace)
        since = date(2026, 6, 1)
        core_settings.set_payments(session, workspace, FULL, OWNER, since=since)
        opening.set_one(session, workspace, FULL, OWNER, alfa.id, since=since, amount=Decimal("50000"))
        _income(session, workspace, account, "ТОО Альфаком", "100000", date(2026, 5, 28), month="2026-06")
        _income(session, workspace, account, "ТОО Альфаком", "70000", date(2026, 5, 20))
        _income(session, workspace, account, "ТОО Альфаком", "80000", date(2026, 7, 3), label="Старые (до Май/Июня 2026)")
        money = core_payments.Money(session, workspace, today=TODAY).contract(alfa)
        assert money.buckets["2026-06"].paid == Decimal("100000"), "«ИЮНЬ 2026», оплаченный в мае, - июньский"
        assert money.buckets[core_payments.OPENING].paid == Decimal("50000"), "«Старые» гасят входящее сальдо"
        assert money.buckets["2026-07"].paid == 0, "«Старые» не уходят в месяцы учёта"
        assert [(part.amount, part.by) for part in money.over] == [(Decimal("30000"), "old")]
        assert money.paid == Decimal("180000"), "майская оплата без месяца - до учёта, её нет"


def test_istochnik_1s_ne_skladyvaetsya_s_zhurnalom(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ОКР/15")
        account = _account(session, workspace)
        _income(session, workspace, account, "ТОО Альфаком", "100000", date(2026, 6, 5))
        assert core_payments.Money(session, workspace, today=TODAY).contract(alfa).paid == Decimal("100000")
        core_settings.set_payments(session, workspace, FULL, OWNER, entity_id=_entity(session, "BBC"), source="1c")
        assert core_payments.Money(session, workspace, today=TODAY).contract(alfa).paid == 0, "оплаты BBC - из 1С"
        with pytest.raises(PermissionError):
            core_settings.set_payments(session, workspace, service.Access(view=True, edit=True), OWNER, since=None)


# ── Счета журнала и оплаты без договора ──────────────────────────────────────


def test_yurlico_scheta_po_imeni_celikom(space):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        setup.add_entity(session, workspace, name="BBC HR", code="BBC HR")
        setup.add_entity(session, workspace, name="BBC legal support", code="BBCL")
        names = ['Kaspi Юр. ТОО "BBC HR"', 'Pay ТОО "BBC LEGAL SUPPORT"', 'Депозит TOO "BBC"',
                 'Kaspi Юр. ТОО "BBC MARKETING AND SALES"', "Касса"]
        found = {name: entity.code for name, entity in participants.entities_of_accounts(session, workspace, names).items()}
        assert found == {names[0]: "BBC HR", names[1]: "BBCL", names[2]: "BBC"}, "«BBC» внутри «BBC HR» - не BBC"


def test_bez_dogovora_kandidaty_i_reshenie(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ОКР/15", amount="77000")
        account = _account(session, workspace)
        stranger = _income(session, workspace, account, "ИП Неизвестный", "77000", date(2026, 6, 9))
        own = finance_service.ensure_counterparty(session, workspace.id, "BBC", role="client")
        transfer = _income(session, workspace, account, own.name, "500000", date(2026, 6, 10))
        money = core_payments.Money(session, workspace, today=TODAY)
        listed = {item["operation_id"]: item for item in money.without_contract()}
        assert str(transfer.id) not in listed, "перевод со своего юрлица - не оплата"
        assert listed[str(stranger.id)]["candidates"][0] == {
            "id": str(alfa.id), "number": "№ОКР/15", "customer": "ТОО Альфаком",
            "executor_id": str(_entity(session, "BBC")), "why": "сумма",
        }
        core_payments.decide(session, workspace, FULL, OWNER, stranger.id, action="link", contract_id=alfa.id)
        assert core_payments.Money(session, workspace, today=TODAY).contract(alfa).paid == Decimal("77000")
        core_payments.decide(session, workspace, FULL, OWNER, stranger.id, action="none")
        again = core_payments.Money(session, workspace, today=TODAY)
        assert again.contract(alfa).paid == 0 and not again.without_contract(), "«не по договору» уходит из списка"


# ── Входящее сальдо из книги ─────────────────────────────────────────────────


def test_saldo_iz_knigi_paketom_s_vozvratom(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        alfa = _contract(session, workspace, "ТОО Альфаком", "№ОКР/15")
        beta = _contract(session, workspace, "ТОО Бетаком", "№ОКР/16")
        book = book_module.build_book([
            ["Мес", "Техн. 1", "Наша Фирма", "Заказчик (Название Фирмы)", "Сумма Договора", "№ Договора",
             "Период с (нач.)", "Период по (зав.)", "Сальдо Начало (без АВР)"],
            ["6", "ИЮНЬ 2026", "BBC", "ТОО Альфаком", "100 000", "№ОКР/15", "01.06.2026", "30.06.2026", "120 000"],
            ["6", "ИЮНЬ 2026", "BBC", "ТОО Бетаком", "100 000", "№ОКР/16", "01.06.2026", "30.06.2026", "-30 000"],
            ["7", "ИЮЛЬ 2026", "BBC", "ТОО Альфаком", "100 000", "№ОКР/15", "01.07.2026", "31.07.2026", "220 000"],
        ], title="Сводка", worksheet="Сводка все ЮР лица")
        since = date(2026, 6, 1)
        plan = opening.plan(session, workspace, FULL, book, since=since)
        assert plan["counts"]["new"] == 2 and plan["debt"] == "120000.00" and plan["advance"] == "30000.00"
        done = opening.apply(session, workspace, FULL, OWNER, book, since=since)
        assert done["done"] == 2 and done["point"]["kind"] == "core_opening"
        assert opening.plan(session, workspace, FULL, book, since=since)["counts"]["same"] == 2, "повтор не двоит"
        core_settings.set_payments(session, workspace, FULL, OWNER, since=since)
        money = core_payments.Money(session, workspace, today=TODAY)
        assert money.contract(alfa).opening == Decimal("120000")
        credit = money.contract(beta)
        assert credit.buckets["2026-06"].paid == Decimal("30000"), "аванс на начало гасит первый месяц"
        assert credit.paid == 0, "аванс - не оплата"
        restore.restore_point(session, workspace, FULL, OWNER, uuid.UUID(done["point"]["id"]))
        left = session.scalars(sa.select(Document).where(Document.kind == "opening", Document.deleted_at.is_(None))).all()
        assert not left, "«Восстановление» снимает перенесённое сальдо"
        again = opening.apply(session, workspace, FULL, OWNER, book, since=since)
        assert again["done"] == 2, "снятое переносится снова тем же документом"
        assert session.scalar(sa.select(sa.func.count(Document.id)).where(Document.kind == "opening")) == 2
