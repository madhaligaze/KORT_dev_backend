"""Участники ядра: БИН, поиск, двойники, сведение с возвратом, написания из сводки.

24.10.2026 на проде было 17 клиентов, заведённых дважды («ТОО Х» и «Х»), и ни
одного БИН; в книгах у 96 клиентов по три написания. Набор держит правило
ядра: участник - это БИН, имя - одно из написаний; сводит человек, и сведение
возвращается.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.finance import service as finance_service
from app.finance.contracts import restore, service, setup
from app.finance.contracts.models import Contract, CounterpartyName, GroupEntity, RestorePoint
from app.finance.core import bin as bins
from app.finance.core import book as book_module
from app.finance.core import participants, tax
from app.finance.core.models import EntityTaxPeriod
from app.finance.db import finance_session
from app.finance.models import Account, Counterparty, Operation

FULL = service.Access(view=True, edit=True, setup=True, admin=True)
EDITOR = service.Access(view=True, edit=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")


def make_bin(prefix: str) -> str:
    """Дописать номер до двенадцати цифр так, чтобы контрольная сошлась."""
    for serial in range(10 ** (11 - len(prefix))):
        body = prefix + str(serial).zfill(11 - len(prefix))
        for tail in range(10):
            if bins.checksum_ok(body + str(tail)):
                return body + str(tail)
    raise AssertionError(f"к {prefix} контрольной цифры нет")


CLIENT_BIN = "260540028486"
BBC_BIN = "090540002228"


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    book_module.forget()
    yield engine
    book_module.forget()


@pytest.fixture
def space(finance_db):
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC", code="BBC")
        setup.add_entity(session, workspace, name="BBC legal support")
        return workspace.id


def _ws(session, space_id):
    return finance_service.get_workspace(session, space_id)


def _make(session, space_id, **values):
    return service.create(session, _ws(session, space_id), FULL, OWNER, values)


def _party(session, space_id, name: str) -> Counterparty:
    return session.scalars(
        sa.select(Counterparty).where(Counterparty.workspace_id == space_id, Counterparty.name == name)
    ).one()


# ── БИН ──────────────────────────────────────────────────────────────────────


def test_bin_kontrolnaya_cifra_i_vid():
    assert bins.check("0905 4000 2228").ok and bins.check(BBC_BIN).kind == bins.ORG
    assert not bins.check("090540002229").ok
    assert "12 цифр" in bins.check("12345").error
    person = make_bin("8501153")
    assert bins.kind_of(person, "ИП Ахметова") == bins.IP and bins.kind_of(person, "Ахметова А.") == bins.PERSON
    assert bins.found_in("ТОО Алатау Пауэр БИН 260540028486 и счёт 123456789012") == [CLIENT_BIN]


def test_bin_odin_na_uchastnika_i_chuzhoy_otkaz(space):
    with finance_session() as session:
        first = _make(session, space, executor="BBC", customer="ТОО Альфа", type="Разовая услуга", amount="1000")
        second = _make(session, space, executor="BBC", customer="ТОО Бета", type="Разовая услуга", amount="1000")
        workspace = _ws(session, space)
        out = participants.set_bin(session, workspace, EDITOR, OWNER, first.customer_id, "2605 4002 8486")
        assert out["bin"] == CLIENT_BIN and out["kind"] == "org"
        with pytest.raises(participants.ParticipantError, match="уже записан у «ТОО Альфа»"):
            participants.set_bin(session, workspace, EDITOR, OWNER, second.customer_id, CLIENT_BIN)
        with pytest.raises(participants.ParticipantError, match="Контрольная"):
            participants.set_bin(session, workspace, EDITOR, OWNER, second.customer_id, "260540028487")


# ── Поиск и досье ────────────────────────────────────────────────────────────


def test_poisk_po_napisaniyu_banka_i_nachalu_bin(space):
    with finance_session() as session:
        contract = _make(session, space, executor="BBC", customer="ТОО Алатау Пауэр", type="Абонентское обслуживание", amount="100000")
        workspace = _ws(session, space)
        participants.add_alias(
            session, workspace, EDITOR, OWNER, contract.customer_id,
            'Товарищество с ограниченной ответственностью "Алатау Пауэр"', "bank",
        )
        participants.set_bin(session, workspace, EDITOR, OWNER, contract.customer_id, CLIENT_BIN)
        assert participants.search(session, workspace, FULL, "Товарищество")[0]["id"] == str(contract.customer_id)
        assert participants.search(session, workspace, FULL, "2605 4")[0]["bin"] == CLIENT_BIN
        found = participants.dossier(session, workspace, FULL, contract.customer_id)
        assert found["bin"] == CLIENT_BIN
        assert {item["source"] for item in found["aliases"]} == {"bank"}
        assert found["groups"][0]["own"] and found["groups"][0]["contracts"][0]["number"] == contract.number


def test_spisok_klienty_pervymi_yurlica_v_konce_i_plitki(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        _make(session, space, executor="BBC", customer="ТОО Альфа", type="Разовая услуга", amount="1")
        _make(session, space, executor="BBC", customer="ТОО Альфа", type="Разовая услуга", amount="2")
        beta = _make(session, space, executor="BBC", customer="ТОО Бета", type="Разовая услуга", amount="3")
        # Контрагент без договоров (журнал, удалённый договор) в список не попадает - только поиском.
        finance_service.ensure_counterparty(session, workspace.id, "ТОО Без договоров")
        participants.set_bin(session, workspace, EDITOR, OWNER, beta.customer_id, CLIENT_BIN)

        listed = participants.listing(session, workspace, FULL)
        names = [item["name"] for item in listed["items"]]
        # Клиенты - по числу договоров; наши юрлица (BBC с тремя договорами) - в конце.
        assert names[:2] == ["ТОО Альфа", "ТОО Бета"] and names[-2:] == ["BBC", "BBC legal support"]
        assert "ТОО Без договоров" not in names and listed["total"] == 4
        own = participants.listing(session, workspace, FULL, show="own")["items"]
        assert [item["own"] for item in own] == ["BBC", "BBC legal support"] and own[0]["regime"] is None
        no_bin = [item["name"] for item in participants.listing(session, workspace, FULL, show="no_bin")["items"]]
        assert "ТОО Бета" not in no_bin and "ТОО Альфа" in no_bin
        found = participants.listing(session, workspace, FULL, q="без догов")["items"]
        assert [item["name"] for item in found] == ["ТОО Без договоров"]

        tiles = participants.overview(session, workspace, EDITOR)
        assert (tiles["all"], tiles["own"], tiles["no_bin"]) == (4, 2, 3)
        assert tiles["access"] == {"edit": True, "setup": False}
        with pytest.raises(participants.ParticipantError):
            participants.listing(session, workspace, FULL, show="twins")


# ── Двойники и сведение ──────────────────────────────────────────────────────


def test_dvoyniki_tot_zhe_klient_bez_formy_no_ne_raznye_formy(space):
    with finance_session() as session:
        _make(session, space, executor="BBC", customer="ТОО Бергауф Астана", type="Разовая услуга", amount="1")
        _make(session, space, executor="BBC", customer="Бергауф Астана", type="Разовая услуга", amount="2")
        _make(session, space, executor="BBC", customer="ТОО Альфа", type="Разовая услуга", amount="3")
        _make(session, space, executor="BBC", customer="ИП Альфа", type="Разовая услуга", amount="4")
        groups = participants.duplicates(session, _ws(session, space), FULL)
        names = [sorted(item["name"] for item in group["members"]) for group in groups]
        assert names == [["Бергауф Астана", "ТОО Бергауф Астана"]]
        # «Это разные» - группа уходит.
        ids = [uuid.UUID(item["id"]) for item in groups[0]["members"]]
        participants.mark_distinct(session, _ws(session, space), EDITOR, OWNER, ids)
        assert participants.duplicates(session, _ws(session, space), FULL) == []


def test_svedenie_perenosit_vsyo_i_vozvrashchaetsya(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        kept_contract = _make(session, space, executor="BBC", customer="ТОО Бергауф Астана", type="Разовая услуга", amount="1")
        dropped_contract = _make(session, space, executor="BBC", customer="Бергауф Астана", type="Разовая услуга", amount="2")
        keep, drop = kept_contract.customer_id, dropped_contract.customer_id
        # Одно и то же написание у обоих - раньше сведение падало на уникальном ключе.
        participants.add_alias(session, workspace, EDITOR, OWNER, keep, "БЕРГАУФ АСТАНА ТОО", "bank")
        participants.add_alias(session, workspace, EDITOR, OWNER, drop, "БЕРГАУФ АСТАНА ТОО", "bank")
        participants.add_alias(session, workspace, EDITOR, OWNER, drop, "Bergauf Astana", "1c")
        participants.set_bin(session, workspace, EDITOR, OWNER, drop, CLIENT_BIN)
        account = finance_service.create_account(session, workspace, name="Kaspi BBC")
        payer = finance_service.create_operation(
            session, workspace,
            finance_service.OperationInput(kind="income", paid_at=date(2026, 5, 1), amount=Decimal("2"),
                                           account_to_id=account.id, counterparty_id=drop),
        )
        result = participants.merge(session, workspace, FULL, OWNER, keep=keep, drop=drop)
        assert result["moved"]["contracts.customer"] == 1
        assert session.get(Contract, dropped_contract.id).customer_id == keep
        assert session.get(Operation, payer.id).counterparty_id == keep
        kept = session.get(Counterparty, keep)
        assert kept.bin == CLIENT_BIN, "БИН сведённого переезжает к оставшемуся, если своего нет"
        names = {item.name for item in session.scalars(sa.select(CounterpartyName).where(CounterpartyName.counterparty_id == keep))}
        assert {"Bergauf Astana", "Бергауф Астана", "БЕРГАУФ АСТАНА ТОО"} <= names
        dropped = session.get(Counterparty, drop)
        assert dropped.merged_into_id == keep and dropped.archived_at is not None
        assert participants.dossier(session, workspace, FULL, drop)["merged_into"] == str(keep)

        point = session.scalars(sa.select(RestorePoint).where(RestorePoint.kind == "party_merge")).one()
        back = restore.restore_point(session, workspace, FULL, OWNER, point.id)
        assert back["done"] and not back["skipped"], back
        assert session.get(Contract, dropped_contract.id).customer_id == drop
        assert session.get(Operation, payer.id).counterparty_id == drop
        assert session.get(Counterparty, drop).merged_into_id is None
        assert session.get(Counterparty, drop).archived_at is None
        assert session.get(Counterparty, keep).bin == "", "свой БИН оставшегося - как был до сведения"
        drop_names = {
            item.name for item in session.scalars(sa.select(CounterpartyName).where(CounterpartyName.counterparty_id == drop))
        }
        assert {"Bergauf Astana", "БЕРГАУФ АСТАНА ТОО"} <= drop_names


def test_svodit_nashe_yurlico_so_schetami_i_rezhimom(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        legal = _party(session, space, "BBC legal support")
        # Двойник нашего юрлица, заведённый выпиской как обычный контрагент.
        twin = finance_service.ensure_counterparty(session, space, 'ТОО "BBC LEGAL SUPPORT"')
        account = finance_service.create_account(session, workspace, name="Kaspi BBCL")
        account.group_entity_id = legal.id
        tax.add_period(session, workspace, FULL, OWNER, legal.id, {"valid_from": "2026-01-01", "regime": "general", "vat_payer": False})
        session.flush()
        participants.merge(session, workspace, FULL, OWNER, keep=twin.id, drop=legal.id)
        assert session.get(GroupEntity, twin.id) is not None
        assert session.get(Account, account.id).group_entity_id == twin.id, "счёт юрлица не теряется"
        assert session.scalars(sa.select(EntityTaxPeriod.entity_id)).one() == twin.id


def test_svedenie_otkazy(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        a = _make(session, space, executor="BBC", customer="ТОО Дельта", type="Разовая услуга", amount="1").customer_id
        b = _make(session, space, executor="BBC", customer="Дельта", type="Разовая услуга", amount="1").customer_id
        participants.set_bin(session, workspace, EDITOR, OWNER, a, CLIENT_BIN)
        participants.set_bin(session, workspace, EDITOR, OWNER, b, BBC_BIN)
        with pytest.raises(participants.ParticipantError, match="разные БИН"):
            participants.merge(session, workspace, FULL, OWNER, keep=a, drop=b)
        with pytest.raises(PermissionError):
            participants.merge(session, workspace, EDITOR, OWNER, keep=a, drop=b)
        bbc, legal = _party(session, space, "BBC"), _party(session, space, "BBC legal support")
        with pytest.raises(participants.ParticipantError, match="Оба - наши юрлица"):
            participants.merge(session, workspace, FULL, OWNER, keep=bbc.id, drop=legal.id)


# ── Из сводки ────────────────────────────────────────────────────────────────


HEADER = [
    "Мес", "Техн. 1", "Наша Фирма", "Вид Услуги", "Заказчик (Название Фирмы)", "По Выписке Банка", "По 1C",
    "Сумма Договора", "№ Договора", "Период с (нач.)", "Период по (зав.)", "№ Счета", "Дата выстав. Счета",
    "Сумма Факт Поступ.", "Сальдо Конец (АВР)",
]


def _book(rows: list[list[str]]) -> book_module.Book:
    return book_module.build_book([HEADER, *rows], title="Сводка", worksheet="Сводка все ЮР лица")


def test_svodka_chitaetsya_po_nazvaniyam_i_mesyac_iz_teksta():
    book = _book([
        ["10", "ОКТЯБРЬ 2026", "BBC", "Абон.П.", "Алатау Пауэр", "ТОО \"Алатау Пауэр\" БИН 260540028486", "ТОО Алатау Пауэр",
         "100 000", "№ОБО/146", "25.10.2026", "24.11.2026", "514", "25.10.2026", "#N/A", "-100 000"],
        ["6", "Старые (до Май/Июня 2026)", "BBC", "Разовый", "Бета", "", "", "50 000", "№ЮО/1", "", "", "", "", "0", "0"],
    ])
    first, second = book.rows
    assert first.month == "2026-10" and first.amount == Decimal("100000") and first.paid is None
    assert first.closing_acts == Decimal("-100000") and first.bill_no == "514"
    assert second.month == "", "«Старые» - без месяца"


def test_napisaniya_i_kody_iz_svodki(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, executor="BBC legal support", customer="ТОО Алатау Пауэр",
                         type="Абонентское обслуживание", amount="100000", number="№ОБО/146")
        book = _book([
            ["10", "ОКТЯБРЬ 2026", "BBCL", "Абон.П.", "Алатау Пауэр", "ТОО \"Алатау Пауэр\" БИН 260540028486",
             "ТОО «Алатау Пауэр»", "100 000", "№ ОБО/146", "25.10.2026", "24.11.2026", "", "", "", ""],
            ["10", "ОКТЯБРЬ 2026", "BBCL", "Разовый", "Кто-то другой", "", "", "1", "№ОБО/146", "", "", "", "", "", ""],
        ])
        preview = participants.harvest(session, workspace, FULL, OWNER, book, apply=False)
        assert preview["matched"] == 1 and preview["aliases"] == {"registry": 1, "bank": 1}
        assert preview["codes"] == [
            {"entity_id": str(contract.executor_id), "name": "BBC legal support", "code": "BBCL", "rows": 1, "how": "rows"}
        ]
        participants.harvest(session, workspace, FULL, OWNER, book, apply=True)
        assert session.get(GroupEntity, contract.executor_id).code == "BBCL"
        names = {item.source for item in session.scalars(sa.select(CounterpartyName).where(CounterpartyName.counterparty_id == contract.customer_id))}
        assert names == {"registry", "bank"}
        # БИН из строки банка - предложением, однозначным.
        proposals = participants.bin_proposals(session, workspace, FULL, book)
        assert proposals[0]["clear"] and proposals[0]["numbers"][0]["bin"] == CLIENT_BIN
        participants.reject_bin(session, workspace, EDITOR, OWNER, contract.customer_id, CLIENT_BIN)
        assert participants.bin_proposals(session, workspace, FULL, book) == []


# ── Режим наших юрлиц ────────────────────────────────────────────────────────


def test_rezhim_po_datam_i_nds_tolko_na_obshcheustanovlennom(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        legal = _party(session, space, "BBC legal support")
        with pytest.raises(tax.TaxError, match="специальном режиме"):
            tax.add_period(session, workspace, FULL, OWNER, legal.id, {"valid_from": "2026-01-01", "regime": "simplified", "vat_payer": True})
        tax.add_period(session, workspace, FULL, OWNER, legal.id, {"valid_from": "2026-01-01", "regime": "simplified"})
        tax.add_period(session, workspace, FULL, OWNER, legal.id, {"valid_from": "2026-07-01", "regime": "general", "vat_payer": True})
        assert not tax.regime_on(session, legal.id, date(2026, 6, 30)).vat_payer
        assert tax.regime_on(session, legal.id, date(2026, 7, 1)).vat_payer
        assert tax.regime_on(session, legal.id, date(2025, 12, 31)) is None


def test_nalogovye_gody_rk_i_prognoz(finance_db):
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        y2026 = tax.year_of(session, workspace.id, 2026)
        assert y2026.threshold == Decimal("43250000") and y2026.vat_rate == Decimal("16")
        y2027 = tax.year_of(session, workspace.id, 2027)
        assert y2027.forecast and y2027.threshold_mrp == 10000 and y2027.mrp > y2026.mrp
        tax.set_year(session, workspace, FULL, OWNER, 2027, {"mrp": "4600", "threshold_mrp": 10000, "vat_rate": "16"})
        assert tax.year_of(session, workspace.id, 2027).threshold == Decimal("46000000")
