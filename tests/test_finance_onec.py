"""Базы 1С: адрес, пароль, чтение в снимок, договор и месяц каждого документа.

Финансисты BBC переносили из 1С в книги руками пять колонок на договор и
месяц: «Счёт», «Факт оплата», «АВР (наша)», «АВР (клиент принял)», «ЭСФ
(отпр.)». Набор держит то, что заменяет эту работу:

* адрес базы - из того, что вставил человек, и только во внешнюю сеть;
* пароль REST-сервиса - только зашифрованным;
* чтение - в снимок, повторное чтение не задваивает и не стирает решения;
* договор - по номеру и клиенту, клиент без номера - только если договор
  один; номер у другого клиента и два подходящих - не угадываются;
* месяц - месяц услуги: счёт за октябрь, выписанный 29 сентября, лежит в
  октябре; АВР - по отчётному периоду; ЭСФ и оплата - за своей реализацией
  и счётом.
"""
from __future__ import annotations

import socket
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.finance import secretbox
from app.finance import service as finance_service
from app.finance.config import finance_settings
from app.finance.contracts import service, setup
from app.finance.db import finance_session
from app.finance.onec import client as onec_client
from app.finance.onec import service as onec_service
from app.finance.onec import sync as onec_sync
from app.finance.onec.client import OnecError
from app.finance.onec.models import OnecBase, OnecDocument

FULL = service.Access(view=True, edit=True, setup=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
ORG = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    monkeypatch.setattr(finance_settings, "secrets_key", "test-secret-key")
    yield engine


def _ws(session, space_id):
    return finance_service.get_workspace(session, space_id)


@pytest.fixture
def space(finance_db) -> dict[str, Any]:
    """BBC и BBCL; у BBC клиенты Альфа, Бета, Гамма (номер ОБО/2 у двоих), Дельта (два договора)."""
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        bbc = setup.add_entity(session, workspace, name="BBC", code="BBC", bin="090540002228")
        bbcl = setup.add_entity(session, workspace, name="BBCL", code="BBCL")

        def make(**values):
            return service.create(session, workspace, FULL, OWNER, values).id

        ids = {
            "alpha": make(executor="BBC", customer="ТОО Альфа", number="№ОБО/1", type="Абонентское обслуживание",
                          amount="250 000", signed_at="01.01.2026", status="Действующий"),
            "beta": make(executor="BBC", customer="ТОО Бета", number="№ОБО/2", type="Абонентское обслуживание",
                         amount="100 000", signed_at="01.01.2026", status="Действующий"),
            "gamma": make(executor="BBC", customer="ТОО Гамма", number="№ ОБО/2", type="Разовая услуга",
                          amount="300 000", signed_at="01.02.2026"),
            "delta_old": make(executor="BBC", customer="ТОО Дельта", number="ОБО/5", type="Абонентское обслуживание",
                              amount="50 000", signed_at="01.01.2025", end_date="31.03.2025"),
            "delta_new": make(executor="BBC", customer="ТОО Дельта", number="ОБО/6", type="Абонентское обслуживание",
                              amount="70 000", signed_at="01.01.2026"),
            "alpha_legal": make(executor="BBCL", customer="ТОО Альфа", number="ЮО/9", type="Разовая услуга",
                                amount="90 000"),
            "quiet": make(executor="BBC", customer="ТОО Тишина", number="ОБО/8", type="Абонентское обслуживание",
                          amount="40 000", signed_at="01.01.2026", status="Действующий"),
        }
        return {"workspace": workspace.id, "bbc": bbc.counterparty_id, "bbcl": bbcl.counterparty_id, **ids}


def _base(session, space, *, entity: str | None = "bbc") -> OnecBase:
    base = OnecBase(
        workspace_id=space["workspace"],
        title="ИБ ТОО BBC",
        url=f"https://1cfresh.kz/a/ea201/{uuid.uuid4().int % 1000000}",
        login="odata.user",
        secret=secretbox.seal("пароль"),
        group_entity_id=space[entity] if entity else None,
        organization_ref=ORG,
        sync_from=date(2025, 1, 1),
    )
    session.add(base)
    session.flush()
    return base


def _doc(session, base: OnecBase, kind: str, **values) -> OnecDocument:
    doc = OnecDocument(
        workspace_id=base.workspace_id,
        base_id=base.id,
        kind=kind,
        ref=values.pop("ref", str(uuid.uuid4())),
        doc_date=values.pop("doc_date"),
        **values,
    )
    session.add(doc)
    session.flush()
    return doc


# ── Адрес и пароль ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "https://1cfresh.kz/a/ea201/284171",
        "https://1cfresh.kz/a/ea201/284171/",
        "https://1cfresh.kz/a/ea201/284171/ru_RU/",
        "1cfresh.kz/a/ea201/284171?ProgressiveWebApplicationName=%D0%98%D0%91",
        "https://1cfresh.kz/a/ea201/284171/odata/standard.odata/",
        "https://1CFRESH.KZ/a/ea201/284171/ru_RU/#e1cib/list/Документ.СчетНаОплатуПокупателю",
    ],
)
def test_adres_bazy_iz_togo_chto_vstavili(raw: str) -> None:
    assert onec_client.normalize_url(raw) == "https://1cfresh.kz/a/ea201/284171"


def test_adres_tolko_https() -> None:
    with pytest.raises(OnecError, match="https"):
        onec_client.normalize_url("http://1cfresh.kz/a/ea201/284171")


def test_vnutrennyaya_set_servera_ne_otkryvaetsya(monkeypatch) -> None:
    """Адрес, введённый человеком, не должен вести к базе KORT во внутренней сети."""
    with pytest.raises(OnecError, match="внешней сети"):
        onec_client.check_host("https://postgres.railway.internal/a/1")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 443))])
    with pytest.raises(OnecError, match="внешней сети"):
        onec_client.check_host("https://evil.example.com/a/1")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("185.22.67.10", 443))])
    onec_client.check_host("https://1cfresh.kz/a/ea201/284171")


def test_parol_hranitsya_tolko_zashifrovannym(monkeypatch) -> None:
    monkeypatch.setattr(finance_settings, "secrets_key", "")
    with pytest.raises(secretbox.SecretError, match="FINANCE_SECRETS_KEY"):
        secretbox.seal("X-800")
    monkeypatch.setattr(finance_settings, "secrets_key", "ключ-1")
    sealed = secretbox.seal("X-800")
    assert "X-800" not in sealed and secretbox.unseal(sealed) == "X-800"
    # Ключ сменили - старый пароль не читается, и отказ это говорит.
    monkeypatch.setattr(finance_settings, "secrets_key", "ключ-2")
    with pytest.raises(secretbox.SecretError, match="заново"):
        secretbox.unseal(sealed)


def test_period_uslugi_iz_soderzhaniya_scheta() -> None:
    assert onec_sync.period_in("Бухгалтерское сопровождение за период с 02.10.2026 по 01.11.2026") == (
        date(2026, 10, 2),
        date(2026, 11, 1),
    )
    assert onec_sync.period_in("Сопровождение с 03.10.2026 года по 02.11.2026 года") == (
        date(2026, 10, 3),
        date(2026, 11, 2),
    )
    assert onec_sync.period_in("06.2026 Н.П. - Татьяна") == (None, None)


# ── Чтение базы ──────────────────────────────────────────────────────────────


class FakeClient:
    """REST-сервис 1С из словаря: сущность → строки."""

    def __init__(self, data: dict[str, list[dict[str, Any]]], missing: tuple[str, ...] = ()):
        self.data, self.missing = data, missing

    def rows(self, entity: str, **_: Any):
        if entity in self.missing:
            raise onec_client.OnecMissing(f"«{entity}» не открыт в составе REST-сервиса базы")
        yield from self.data.get(entity, [])


def _fake_1c(*, with_invoice: bool = True) -> FakeClient:
    party, contract, sale, invoice, sf = (str(uuid.uuid4()) for _ in range(5))
    data: dict[str, list[dict[str, Any]]] = {
        "Catalog_Организации": [{"Ref_Key": ORG, "Description": 'ТОО "Big Business Consulting"', "ИдентификационныйНомер": "090540002228"}],
        "Catalog_Контрагенты": [{"Ref_Key": party, "Description": 'ТОО "Альфа"', "НаименованиеПолное": 'ТОО "Альфа"', "ИдентификационныйКодЛичности": ""}],
        "Catalog_ДоговорыКонтрагентов": [{"Ref_Key": contract, "Owner_Key": party, "НомерДоговора": "ОБО/1", "ДатаДоговора": "2026-01-01T00:00:00", "Description": "Договор № ОБО/1 от 01.01.2026"}],
        "Document_СчетНаОплатуПокупателю": [
            {
                "Ref_Key": invoice, "Number": "00000000514", "Date": "2026-09-29T09:00:00", "Posted": True, "DeletionMark": False,
                "Контрагент_Key": party, "ДоговорКонтрагента_Key": contract, "СуммаДокумента": 250000,
                "Услуги": [{"Содержание": "Бухгалтерское сопровождение за период с 01.10.2026 по 31.10.2026"}],
            }
        ]
        if with_invoice
        else [],
        "Document_РеализацияТоваровУслуг": [
            {
                "Ref_Key": sale, "Number": "00000000441", "Date": "2026-09-30T18:00:00", "Posted": True, "DeletionMark": False,
                "Контрагент_Key": party, "ДоговорКонтрагента_Key": contract, "СуммаДокумента": 250000,
                "ДатаНачалаОтчетногоПериода": "2026-09-01T00:00:00", "ДатаОкончанияОтчетногоПериода": "2026-09-30T00:00:00",
                "Услуги": [{"Содержание": "Бухгалтерское сопровождение за период с 01.09.2026 по 30.09.2026"}],
            }
        ],
        "InformationRegister_СведенияПоАктамВыполненныхРабот": [
            {"ДокументРеализацииУслуг": sale, "ДокументРеализацииУслуг_Type": onec_sync.SALE_TYPE, "ДатаПодписания": "2026-10-02T00:00:00"}
        ],
        "Document_СчетФактураВыданный": [
            {
                "Ref_Key": sf, "Number": "00000000441", "Date": "2026-10-02T18:30:00", "Posted": True, "DeletionMark": False,
                "Контрагент_Key": party, "ДоговорКонтрагента_Key": contract, "СуммаДокумента": 250000,
                "ДокументОснование": sale, "ДокументОснование_Type": onec_sync.SALE_TYPE, "СпособВыставления": "Электронно",
            }
        ],
        "Document_ПлатежноеПоручениеВходящее": [
            {
                "Ref_Key": str(uuid.uuid4()), "Number": "00000000007", "Date": "2026-10-05T12:00:00", "Posted": True, "DeletionMark": False,
                "Контрагент_Key": party, "СуммаДокумента": 250000, "НазначениеПлатежа": "Оплата по счёту № 514",
                "РасшифровкаПлатежа": [
                    {"LineNumber": "1", "ДоговорКонтрагента_Key": contract, "СуммаПлатежа": 150000, "СчетНаОплату_Key": invoice},
                    {"LineNumber": "2", "ДоговорКонтрагента_Key": contract, "СуммаПлатежа": 100000, "СчетНаОплату_Key": "00000000-0000-0000-0000-000000000000"},
                ],
            }
        ],
    }
    return FakeClient(data, missing=("Document_ЭСФ",))


def test_chtenie_v_snimok_povtor_ne_zadvaivaet_i_reshenie_derzhitsya(space) -> None:
    fake = _fake_1c()
    snapshot = onec_sync.read(fake, organization_ref=ORG, since=date(2025, 1, 1))
    assert snapshot.counts() == {"invoice": 1, "sale": 1, "sf": 1, "payment": 2}
    # Вида данных нет в составе REST-сервиса - предупреждение, а не отказ.
    assert any("ЭСФ" in item for item in snapshot.warnings)
    sale = next(item for item in snapshot.documents if item["kind"] == "sale")
    assert sale["signed_at"] == date(2026, 10, 2) and sale["period_from"] == date(2026, 9, 1)
    invoice = next(item for item in snapshot.documents if item["kind"] == "invoice")
    assert invoice["period_from"] == date(2026, 10, 1) and invoice["contract_number"] == "ОБО/1"

    with finance_session() as session:
        base = _base(session, space)
        onec_sync.apply(session, base, snapshot, since=base.sync_from)
        payment = session.query(OnecDocument).filter_by(base_id=base.id, kind="payment").first()
        payment.manual, payment.manual_contract_id = "link", space["alpha"]
        again = onec_sync.read(_fake_1c(with_invoice=False), organization_ref=ORG, since=date(2025, 1, 1))
        # Тот же документ 1С - та же строка, а не вторая: ключ - Ref_Key.
        for item in again.documents:
            if item["kind"] != "invoice":
                item["ref"] = next(
                    old["ref"] for old in snapshot.documents if old["kind"] == item["kind"] and old["amount"] == item["amount"]
                )
        onec_sync.apply(session, base, again, since=base.sync_from)
        docs = session.query(OnecDocument).filter_by(base_id=base.id).all()
        assert len(docs) == 5
        gone = [doc for doc in docs if doc.gone]
        assert [doc.kind for doc in gone] == ["invoice"]
        kept = session.get(OnecDocument, payment.id)
        assert kept.manual == "link" and kept.manual_contract_id == space["alpha"]


# ── Договор документа ────────────────────────────────────────────────────────


def _month(session, space, month: str) -> dict[str, Any]:
    return onec_service.month_view(session, _ws(session, space["workspace"]), month)


def _row(view: dict[str, Any], contract_id: uuid.UUID) -> dict[str, Any] | None:
    return next((row for row in view["rows"] if row["contract_id"] == str(contract_id)), None)


def test_dogovor_po_nomeru_i_klientu_ne_ugadyvaya(space) -> None:
    with finance_session() as session:
        base = _base(session, space)
        sept = date(2026, 9, 10)
        _doc(session, base, "invoice", doc_date=sept, number="00000000001", party_name='ТОО "Альфа"', contract_number="ОБО/1", amount=Decimal("250000"))
        _doc(session, base, "invoice", doc_date=sept, number="00000000002", party_name="ТОО ГАММА", contract_number="ОБО/2", amount=Decimal("300000"))
        # Номер ОБО/2 есть, но у Беты и Гаммы, а не у Омеги - не наш договор.
        omega = _doc(session, base, "invoice", doc_date=sept, number="00000000003", party_name="ТОО Омега", contract_number="ОБО/2", amount=Decimal("1"))
        # Номера нет - у Дельты два договора, решает срок: в 2026 году действует новый.
        _doc(session, base, "invoice", doc_date=sept, number="00000000004", party_name="ТОО Дельта", amount=Decimal("70000"))
        # Номер в 1С другой, чем в реестре, - кандидат, но не решение.
        stray = _doc(session, base, "invoice", doc_date=sept, number="00000000005", party_name="ТОО Альфа", contract_number="ОБО/77", amount=Decimal("5"))
        view = _month(session, space, "2026-09")
        assert [item["number"] for item in _row(view, space["alpha"])["invoices"]] == ["1"]
        assert [item["number"] for item in _row(view, space["gamma"])["invoices"]] == ["2"]
        assert [item["number"] for item in _row(view, space["delta_new"])["invoices"]] == ["4"]
        assert _row(view, space["beta"])["invoices"] == []
        states = {item["id"]: item for item in view["unmatched"]}
        assert states[str(omega.id)]["state"] == "other_client"
        assert states[str(stray.id)]["state"] == "missing"
        assert states[str(stray.id)]["candidates"] == [str(space["alpha"])]
        # Договор другого нашего юрлица не подставляется: база BBC - договоры BBC.
        assert _row(view, space["alpha_legal"]) is None


def test_imena_zapisannye_raznoy_rukoy(space) -> None:
    """Реестр BBC и 1С пишут одного клиента по-разному (случаи с копии прода 19.10.2026).

    «й» в реестре разложена на «и» и надстрочный знак; «ATEX» в 1С латиницей,
    «АТЕХ» в реестре кириллицей; «INDUSTRIALL» с опечаткой и пояснением в
    скобках; «ОБОиНО-62» против «ОБОиНО/62». Договор с тем же номером и
    клиентом на другом нашем юрлице - не подставляется, а показывается.
    """
    with finance_session() as session:
        workspace = _ws(session, space["workspace"])

        def make(executor: str, customer: str, number: str) -> uuid.UUID:
            values = {"executor": executor, "customer": customer, "number": number, "type": "Абонентское обслуживание"}
            return service.create(session, workspace, FULL, OWNER, values).id

        teplo = make("BBC", "ТОО Теплобетонстрой", "№ 17-7-BBC-BUH")
        atex = make("BBC", 'Завод "АТЕХ"', "№ОБО/111")
        recycling = make("BBC", "ТОО Industrial Recycling", "№ ОБОиНО-67")
        voshod = make("BBC", "ТОО Дом одежды Восход", "№ ОБОиНО/62")
        bergauf = make("BBCL", "ТОО Бергауф Астана", "№ 200-BBCA-BUH-HR")
        base = _base(session, space)
        day = date(2026, 9, 5)
        docs = {
            teplo: _doc(session, base, "invoice", doc_date=day, number="00000000444",
                        party_name='ТОО "Теплобетонстрой"', contract_number="17-7-BBC-BUH"),
            atex: _doc(session, base, "invoice", doc_date=day, number="00000000496",
                       party_name="ТОО Завод ATEX", contract_number="ОБО/111"),
            recycling: _doc(session, base, "invoice", doc_date=day, number="00000000500",
                            party_name='ТОО "INDUSTRIALL RECYCLING" (ИНДАСТРИАЛ РЕСАЙКЛИН)', contract_number="ОБОиНО-67"),
            voshod: _doc(session, base, "invoice", doc_date=day, number="00000000505",
                         party_name='ТОО "Дом одежды Восход"', contract_number="ОБОиНО-62"),
        }
        elsewhere = _doc(session, base, "invoice", doc_date=day, number="00000000467",
                         party_name='ТОО "Бергауф Астана"', contract_number="200-BBCA-BUH-HR")
        view = _month(session, space, "2026-09")
        for contract_id, doc in docs.items():
            assert [item["id"] for item in _row(view, contract_id)["invoices"]] == [str(doc.id)], contract_id
        loose = {item["id"]: item for item in view["unmatched"]}
        assert loose[str(elsewhere.id)]["state"] == "other_entity"
        assert loose[str(elsewhere.id)]["candidates"] == [str(bergauf)]
        assert view["candidates"][str(bergauf)]["executor"] == "BBCL"


def test_baza_bez_yurlica_ne_raskladyvaetsya(space) -> None:
    with finance_session() as session:
        base = _base(session, space, entity=None)
        _doc(session, base, "invoice", doc_date=date(2026, 9, 1), party_name="ТОО Альфа", contract_number="ОБО/1")
        view = _month(session, space, "2026-09")
        assert [item["state"] for item in view["unmatched"]] == ["no_entity"]


def test_mesyac_uslugi_a_ne_data_bumagi(space) -> None:
    """Счёт за октябрь от 29 сентября - в октябре; ЭСФ и оплата - за своей реализацией и счётом."""
    with finance_session() as session:
        base = _base(session, space)
        invoice = _doc(session, base, "invoice", doc_date=date(2026, 9, 29), number="00000000514",
                       party_name='ТОО "Альфа"', contract_number="ОБО/1", amount=Decimal("250000"),
                       period_from=date(2026, 10, 1), period_to=date(2026, 10, 31))
        sale = _doc(session, base, "sale", doc_date=date(2026, 10, 7), number="00000000464",
                    party_name='ТОО "Альфа"', contract_number="ОБО/1", amount=Decimal("250000"),
                    period_from=date(2026, 9, 2), period_to=date(2026, 10, 1), signed_at=date(2026, 10, 8))
        _doc(session, base, "sf", doc_date=date(2026, 10, 8), number="00000000464", party_name='ТОО "Альфа"',
             basis_ref=sale.ref, status="Электронно")
        # Поступление по счёту - в месяц счёта, даже без номера договора в строке.
        _doc(session, base, "payment", doc_date=date(2026, 10, 2), party_name="ТОО АЛЬФА", basis_ref=invoice.ref,
             amount=Decimal("250000"))
        september, october = _month(session, space, "2026-09"), _month(session, space, "2026-10")
        sep_row, oct_row = _row(september, space["alpha"]), _row(october, space["alpha"])
        assert [item["number"] for item in sep_row["sales"]] == ["464"]
        assert sep_row["signed_at"] == "2026-10-08"
        assert sep_row["esf_kind"] == "sf" and sep_row["esf"][0]["date"] == "2026-10-08"
        assert sep_row["invoices"] == [] and sep_row["paid"] is None
        assert [item["number"] for item in oct_row["invoices"]] == ["514"]
        assert oct_row["paid"] == "250000.00" and oct_row["payments"][0]["how"] == "invoice"
        assert oct_row["sales"] == []


def test_abonentskiy_bez_dokumentov_viden_pustoy_strokoy(space) -> None:
    """Действующий абонентский договор без счёта в месяце - пустая строка, а не ничего."""
    with finance_session() as session:
        _base(session, space)
        view = _month(session, space, "2026-10")
        quiet = _row(view, space["quiet"])
        assert quiet is not None and quiet["invoices"] == [] and quiet["sales"] == []
        # Договор без статуса не ждёт документов - статус не угадывается.
        assert _row(view, space["delta_new"]) is None


def test_reshenie_cheloveka_i_zapomnennoe_napisanie(space) -> None:
    with finance_session() as session:
        workspace = _ws(session, space["workspace"])
        base = _base(session, space)
        first = _doc(session, base, "invoice", doc_date=date(2026, 9, 3), number="00000000010",
                     party_name="Бета ТОО (филиал)", contract_number="")
        assert _month(session, space, "2026-09")["unmatched"][0]["state"] == "missing"
        onec_service.decide(session, workspace, first.id, action="link", contract_id=space["beta"], user_id=None)
        view = _month(session, space, "2026-09")
        assert [item["how"] for item in _row(view, space["beta"])["invoices"]] == ["manual"]
        # Написание из 1С запомнено за клиентом - следующий счёт находит договор сам.
        _doc(session, base, "invoice", doc_date=date(2026, 9, 20), number="00000000011",
             party_name="Бета ТОО (филиал)", contract_number="")
        view = _month(session, space, "2026-09")
        assert [item["number"] for item in _row(view, space["beta"])["invoices"]] == ["10", "11"]
        onec_service.decide(session, workspace, first.id, action="none", contract_id=None, user_id=None)
        view = _month(session, space, "2026-09")
        assert [item["number"] for item in _row(view, space["beta"])["invoices"]] == ["11"]
        assert not view["unmatched"]


# ── Маршруты ────────────────────────────────────────────────────────────────


def test_podklyuchenie_i_chtenie_bazy_po_http(finance_db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from finance_routes import finance_app

    from app.core.config import settings

    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(onec_client, "check_host", lambda url: None)
    monkeypatch.setattr(onec_service, "check_host", lambda url: None)
    fake = _fake_1c()
    monkeypatch.setattr(onec_service, "Client", lambda connection: fake)
    owner = TestClient(finance_app(), headers={"x-client-ip": "10.0.0.7"})
    base_url = "/api/v1/finance"
    assert owner.post(f"{base_url}/auth/register", json={
        "email": "owner@bbc.kz", "password": "pass-12345", "company": "BBC", "full_name": "Владелец",
    }).status_code < 400
    entity = owner.post(f"{base_url}/contracts/setup/entities", json={"name": "BBC", "code": "BBC"})
    assert entity.status_code < 400, entity.text
    created = owner.post(f"{base_url}/onec/bases", json={
        "url": "https://1cfresh.kz/a/ea201/284171/ru_RU/", "login": "odata.user", "password": "секрет",
    })
    assert created.status_code == 201, created.text
    base = created.json()
    assert base["url"] == "https://1cfresh.kz/a/ea201/284171"
    assert base["organization_name"] == 'ТОО "Big Business Consulting"'
    # Пароль наружу не отдаётся никогда.
    assert "секрет" not in created.text
    with finance_session() as session:
        assert session.get(OnecBase, uuid.UUID(base["id"])).secret != "секрет"
    again = owner.post(f"{base_url}/onec/bases", json={
        "url": "https://1cfresh.kz/a/ea201/284171", "login": "odata.user", "password": "секрет",
    })
    assert again.status_code == 400 and "уже подключена" in again.json()["detail"]

    patched = owner.patch(f"{base_url}/onec/bases/{base['id']}", json={"group_entity_id": entity.json()["id"]})
    assert patched.status_code == 200, patched.text
    assert patched.json()["group_entity"] == "BBC"
    synced = owner.post(f"{base_url}/onec/bases/{base['id']}/sync")
    assert synced.status_code == 200 and synced.json()["ok"], synced.text
    assert synced.json()["base"]["counts"] == {"invoice": 1, "sale": 1, "sf": 1, "payment": 2}
    month = owner.get(f"{base_url}/onec/month", params={"month": "2026-09"})
    assert month.status_code == 200, month.text
    # Договора ОБО/1 в реестре нет - реализация ждёт решения человека.
    assert month.json()["unmatched"][0]["state"] == "missing"
    assert owner.delete(f"{base_url}/onec/bases/{base['id']}").json() == {"ok": True}
