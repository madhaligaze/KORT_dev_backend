"""Вход из 1С: чему научил прогон на живой 1С:Fresh BBC LEGAL SUPPORT (27.10.2026).

Полигон «Тумар» - идеальная 1С: периоды с первого числа, акт в месяце периода,
аванс кредитом 1210. Живая база Бухгалтерии для Казахстана устроена иначе, и
каждое отличие стоило денег в «Нам должны» или договора, который не завёлся:

* помесячные периоды с любого числа («с 20.05 по 19.06») - были разовыми;
* аванс покупателя на отдельном счёте 3510, а не кредитом 1210;
* акт за период до начала учёта проведён после него, оплата до начала учёта
  по счёту месяца учёта - месяц терялся или оплата считалась дважды;
* полное имя банка-посредника у нескольких карточек - клиенты становились
  спорными, их договоры не заводились;
* договор только с предоплатой - не заводился вовсе;
* разовый договор с номером ставился «Исполнен» без даты - не начислялся.

Здесь каждое - на своих данных, без сети. Живая база проверяется прогоном
`pw-finmap\\kort\\core-1026\\onec\\tail\\live_run.py`: долг KORT по АВР = остаток
1210 + 3510 самой 1С по каждому договору.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from polygon.sources import OdataFake

from app.finance import secretbox
from app.finance import service as finance_service
from app.finance.config import finance_settings
from app.finance.contracts import service
from app.finance.contracts.models import Contract
from app.finance.core import onec_start
from app.finance.core.debts import Debts, acts_debt
from app.finance.db import finance_session
from app.finance.onec import balances
from app.finance.onec import sync as onec_sync
from app.finance.onec.models import OnecBase

FULL = service.Access(view=True, edit=True, setup=True)
ACTOR = service.Actor(None, "robot@live")
TODAY = date(2026, 10, 27)
ORG = "00000000-0000-0000-0000-0000000000aa"
BIN_ORG = "250240017943"


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    monkeypatch.setattr(finance_settings, "secrets_key", finance_settings.secrets_key or "live-lessons")
    yield engine


def _ref(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _snapshot() -> dict:
    """База юротдела: абонентский с 20-го числа, предоплата без актов, клиенты с именем банка-посредника."""
    kaspi = 'Филиал "Южная столица" АО "Kaspi Bank" в г. Алматы'
    parties = [
        {"Ref_Key": _ref(1), "Description": "ТОО Аудит Плюс", "НаименованиеПолное": "ТОО Аудит Плюс", "ИдентификационныйКодЛичности": "", "IsFolder": False},
        {"Ref_Key": _ref(2), "Description": "ИП Ишамет", "НаименованиеПолное": kaspi, "ИдентификационныйКодЛичности": "", "IsFolder": False},
        {"Ref_Key": _ref(3), "Description": "ИП Табанова", "НаименованиеПолное": kaspi, "ИдентификационныйКодЛичности": "", "IsFolder": False},
    ]
    contracts = [
        {"Ref_Key": _ref(11), "Owner_Key": _ref(1), "НомерДоговора": "ЮО/35", "ДатаДоговора": "2026-05-15T00:00:00", "Description": "ЮО/35", "IsFolder": False},
        {"Ref_Key": _ref(12), "Owner_Key": _ref(2), "НомерДоговора": "ЮО/97", "ДатаДоговора": "2026-06-01T00:00:00", "Description": "ЮО/97", "IsFolder": False},
        {"Ref_Key": _ref(13), "Owner_Key": _ref(3), "НомерДоговора": "ЮО/122", "ДатаДоговора": "2026-08-10T00:00:00", "Description": "ЮО/122", "IsFolder": False},
    ]

    def doc(n, kind_date, party, contract, amount, **extra):
        return {"Ref_Key": _ref(n), "DataVersion": "x", "Number": f"{n:011d}", "Date": kind_date, "Posted": True, "DeletionMark": False,
                "Организация_Key": ORG, "Контрагент_Key": _ref(party), "ДоговорКонтрагента_Key": _ref(contract), "СуммаДокумента": amount, **extra}

    def lines(text):
        return [{"LineNumber": "1", "Содержание": text}]

    sales, invoices, payments = [], [], []
    periods = [("2026-05-20", "2026-06-19", "2026-06-30"), ("2026-06-20", "2026-07-19", "2026-07-20"),
               ("2026-07-20", "2026-08-19", "2026-08-25"), ("2026-08-20", "2026-09-19", "2026-09-25")]
    for index, (start, end, posted) in enumerate(periods):
        text = f"Юридическое сопровождение за период с {start[8:]}.{start[5:7]}.{start[:4]} по {end[8:]}.{end[5:7]}.{end[:4]}"
        sales.append({**doc(100 + index, posted + "T18:00:00", 1, 11, 350000), "ВидОперации": "Услуги",
                      "ДатаНачалаОтчетногоПериода": start + "T00:00:00", "ДатаОкончанияОтчетногоПериода": end + "T00:00:00", "Услуги": lines(text)})
        invoices.append({**doc(200 + index, start + "T09:00:00", 1, 11, 350000), "Услуги": lines(text)})
    # Оплата в мае по счёту за период с 20.05 - до начала учёта, а месяц счёта - май: в учёт не идёт.
    payments.append({**doc(300, "2026-05-25T12:00:00", 1, 11, 350000), "ВидОперации": "ОплатаПокупателя", "НазначениеПлатежа": "по счёту",
                     "РасшифровкаПлатежа": [{"LineNumber": "1", "ДоговорКонтрагента_Key": _ref(11), "СуммаПлатежа": 350000, "СчетНаОплату_Key": _ref(200)}]})
    # Предоплата без счёта и акта (ЮО/97) и разовый договор с актом у клиента с тем же «полным именем» (ЮО/122).
    for n, day, amount in ((301, "2026-06-08", 120000), (302, "2026-07-13", 500000)):
        payments.append({**doc(n, day + "T12:00:00", 2, 12, amount), "ВидОперации": "ОплатаПокупателя", "НазначениеПлатежа": "Kaspi Pay",
                         "РасшифровкаПлатежа": [{"LineNumber": "1", "ДоговорКонтрагента_Key": _ref(12), "СуммаПлатежа": amount,
                                                 "СчетНаОплату_Key": "00000000-0000-0000-0000-000000000000"}]})
    sales.append({**doc(110, "2026-08-28T18:00:00", 3, 13, 150000), "ВидОперации": "Услуги",
                  "ДатаНачалаОтчетногоПериода": "2026-08-11T00:00:00", "ДатаОкончанияОтчетногоПериода": "2026-08-28T00:00:00",
                  "Услуги": lines("Разработка договоров")})

    account, advances = _ref(900), _ref(901)
    on_june = [  # 1210 на 01.06: пусто - майский акт проведён 30.06; 3510: майская предоплата
        {"Account_Key": advances, "ExtDimension1": _ref(1), "ExtDimension1_Type": balances.PARTY_TYPE, "ExtDimension2": _ref(11),
         "ExtDimension2_Type": balances.CONTRACT_TYPE, "СуммаBalance": -350000, "СуммаBalanceDr": 0, "СуммаBalanceCr": 350000,
         "СуммаSplittedBalanceDr": 0, "СуммаSplittedBalanceCr": 350000, "ВалютнаяСуммаBalanceCr": 350000},
    ]
    return {
        "Catalog_Организации": [{"Ref_Key": ORG, "Description": 'ТОО "BBC LEGAL SUPPORT"', "ИдентификационныйНомер": BIN_ORG, "DeletionMark": False}],
        "Catalog_Контрагенты": parties,
        "Catalog_ДоговорыКонтрагентов": contracts,
        "Document_РеализацияТоваровУслуг": sales,
        "Document_СчетНаОплатуПокупателю": invoices,
        "Document_ПлатежноеПоручениеВходящее": payments,
        "ChartOfAccounts_Типовой": [{"Ref_Key": account, "Code": "1210"}, {"Ref_Key": advances, "Code": "3510"}],
        "AccountingRegister_Типовой/Balance@2026-06-01": on_june,
    }


class AccountFake(OdataFake):
    """Остатки по счёту: виртуальная таблица отбирает строки своего `Account_Key`."""

    def rows(self, entity, *, where="", **kwargs):
        rows = list(super().rows(entity, where=where, **kwargs))
        if "/Balance(" in entity and "Account_Key eq guid''" in entity:
            account = entity.split("Account_Key eq guid''", 1)[1].split("''", 1)[0]
            rows = [row for row in rows if row.get("Account_Key") == account]
        yield from rows


def test_mesyachnyy_period_s_lyubogo_chisla() -> None:
    assert onec_start.Act("1", date(2026, 6, 30), date(2026, 5, 20), date(2026, 6, 19), Decimal(1)).monthly
    assert onec_start.Act("1", date(2026, 6, 30), date(2026, 6, 1), date(2026, 6, 30), Decimal(1)).monthly
    assert not onec_start.Act("1", date(2026, 8, 28), date(2026, 8, 11), date(2026, 8, 28), Decimal(1)).monthly


def test_avans_3510_chitaetsya_vmeste_s_1210() -> None:
    fake = AccountFake(_snapshot())
    osv = balances.read(fake, organization_ref=ORG, day=date(2026, 6, 1))
    assert osv.file.endswith("1210, 3510")
    assert [(line.number, line.opening) for line in osv.lines] == [("ЮО/35", Decimal(-350000))]


def test_dolg_po_avr_bez_aktov_eto_avans() -> None:
    found = acts_debt([], None, Decimal(500), day=TODAY, since=date(2026, 6, 1), complete=True)
    assert found is not None and found[0] == Decimal(-500) and found[2] == Decimal(500)
    assert acts_debt([], None, Decimal(0), day=TODAY, since=date(2026, 6, 1), complete=True) is None
    # Акты юрлица KORT видит не все (1С не подключена) - оплаты без актов ничего не доказывают.
    assert acts_debt([], None, Decimal(500), day=TODAY, since=date(2026, 6, 1)) is None


def test_zhivaya_baza_dolg_po_avr_kak_v_1s(finance_db) -> None:
    data = _snapshot()
    fake = AccountFake(data)
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        base = OnecBase(workspace_id=workspace.id, title="1С ЮО", url="https://1cfresh.kz/a/ea201/1", login="odata", secret=secretbox.seal("x"),
                        organization_ref=ORG, organization_name='ТОО "BBC LEGAL SUPPORT"', organization_bin=BIN_ORG, sync_from=date(2026, 1, 1))
        session.add(base)
        session.flush()
        onec_sync.apply(session, base, onec_sync.read(fake, organization_ref=ORG, since=base.sync_from), since=base.sync_from)
        onec_start.add_entity(session, workspace, FULL, ACTOR, base.id)
        participants = onec_start.add_participants(session, workspace, FULL, ACTOR, base.id, today=TODAY)
        # Имя банка-посредника у двух карточек - не их написание: участники не спорные.
        assert participants["counts"].get("dispute", 0) == 0
        made = onec_start.add_contracts(session, workspace, FULL, ACTOR, base.id, today=TODAY)
        contracts = {c.number: c for c in session.scalars(sa.select(Contract))}
        assert set(contracts) == {"ЮО/35", "ЮО/97", "ЮО/122"}, made
        monthly = contracts["ЮО/35"]
        assert (monthly.billing, monthly.amount, monthly.billing_from, monthly.period_day) == ("month", Decimal(350000), date(2026, 5, 20), 20)
        # Только предоплата - договор без суммы, деньги на нём; сказано словами.
        assert contracts["ЮО/97"].amount is None and any(item["label"] == "ЮО/97" for item in made["skipped"])
        # Разовый с актом - «Исполнен» до конца акта, а не без даты.
        assert contracts["ЮО/122"].end_date == date(2026, 8, 28)
        onec_start.take_balances(session, workspace, FULL, ACTOR, base.id, date(2026, 6, 1), client=fake)
        onec_start.start_since(session, workspace, FULL, ACTOR, base.id, onec_start.stored_osv(session, workspace, base.id))
        onec_start.use_payments(session, workspace, FULL, ACTOR, base.id)
        session.flush()
        by_acts = {item.contract.number: item.debt for item in Debts(session, workspace, FULL, day=TODAY, basis="acts").every["receivable"].values()}
        # 1С: ЮО/35 - 4 акта по 350 000, оплата 350 000 → 1 050 000; ЮО/97 - аванс 620 000; ЮО/122 - 150 000.
        assert by_acts == {"ЮО/35": Decimal(1050000), "ЮО/97": Decimal(-620000), "ЮО/122": Decimal(150000)}
