"""Оплаты по договору из журнала и двойники в списках.

«Оплачено» и «Остаток» в реестре BBC вели руками по выпискам. Набор держит
разнесение: банк пишет стороной «ТОО "АЛЬФА"» (своя запись контрагента), а
реестр — «ТОО Альфа»; у клиента два договора — платёж по сроку; неясно —
спорный, в «Оплачено» не входит; человек решает — решение держится.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.finance import service as finance_service
from app.finance.contracts import payments, service, setup
from app.finance.contracts.fields import is_similar
from app.finance.contracts.models import GroupEntity
from app.finance.db import finance_session
from app.finance.models import Account

FULL = service.Access(view=True, edit=True, setup=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")


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
    """BBC со своим счётом в банке и BBCA без привязанного счёта."""
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        bbc = setup.add_entity(session, workspace, name="BBC", code="BBC")
        setup.add_entity(session, workspace, name="BBCA", code="BBCA")
        account = finance_service.create_account(session, workspace, name="Kaspi BBC")
        account.group_entity_id = bbc.counterparty_id
        session.flush()
        return workspace.id


def _ws(session, space_id):
    return finance_service.get_workspace(session, space_id)


def _make(session, space_id, **values):
    return service.create(session, _ws(session, space_id), FULL, OWNER, values)


def _income(session, space_id, payer: str, amount: str, day: date, *, bin_value: str = "") -> uuid.UUID:
    workspace = _ws(session, space_id)
    account = session.query(Account).filter(Account.workspace_id == space_id, Account.name == "Kaspi BBC").one()
    party = finance_service.ensure_counterparty(session, space_id, payer)
    if bin_value:
        party.details = {**(party.details or {}), "bin": bin_value}
    operation = finance_service.create_operation(
        session,
        workspace,
        finance_service.OperationInput(
            kind="income", paid_at=day, amount=Decimal(amount), account_to_id=account.id, counterparty_id=party.id
        ),
    )
    session.flush()
    return operation.id


def _summary(session, space_id, contract_id):
    return payments.summaries(session, _ws(session, space_id), FULL)["contracts"].get(str(contract_id))


def test_oplata_nahoditsya_po_imeni_bez_kavychek(space):
    with finance_session() as session:
        contract = _make(session, space, executor="BBC", customer="ТОО Альфа", type="Разовая услуга",
                         amount="300 000", signed_at="10.01.2026")
        # Банк пишет иначе — выписка заводит себе отдельного контрагента.
        _income(session, space, 'ТОО "АЛЬФА"', "100000", date(2026, 2, 1))
        _income(session, space, "ТОО АЛЬФА", "50000", date(2026, 3, 1))
        summary = _summary(session, space, contract.id)
        assert summary["paid"] == "150000.00"
        assert summary["remaining"] == "150000.00"
        assert summary["count"] == 2 and summary["last_at"] == "2026-03-01"


def test_oplata_po_bin(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, executor="BBC", customer="ТОО Бета", type="Разовая услуга", amount="1000")
        registry = service.Registry(session, workspace)
        party = registry.parties[contract.customer_id]
        party.details = {"bin": "123456789012"}
        _income(session, space, "BETA LLP", "400", date(2026, 5, 5), bin_value="123456789012")
        assert _summary(session, space, contract.id)["paid"] == "400.00"


def test_dva_dogovora_platezh_po_sroku_inache_spornyy(space):
    with finance_session() as session:
        old = _make(session, space, executor="BBC", customer="ТОО Гамма", type="Абонентское обслуживание",
                    amount="100000", signed_at="01.01.2025", end_date="31.03.2025")
        new = _make(session, space, executor="BBC", customer="ТОО Гамма", type="Абонентское обслуживание",
                    amount="120000", signed_at="01.01.2026")
        _income(session, space, "ТОО Гамма", "100000", date(2025, 2, 10))
        _income(session, space, "ТОО Гамма", "120000", date(2026, 2, 10))
        assert _summary(session, space, old.id)["paid"] == "100000.00"
        assert _summary(session, space, new.id)["paid"] == "120000.00"
        # У абонентского остатка нет — в файле BBC его у них не вели.
        assert _summary(session, space, new.id)["remaining"] is None
        # Третий договор с датой внутри срока нового — платёж подходит к обоим,
        # тогда решает сумма: 5 000 ровно у разового, 120 000 — месячная у нового.
        third = _make(session, space, executor="BBC", customer="ТОО Гамма", type="Разовая услуга", amount="5000",
                      signed_at="01.06.2026")
        _income(session, space, "ТОО Гамма", "5000", date(2026, 6, 1))
        assert _summary(session, space, third.id)["paid"] == "5000.00"
        assert _summary(session, space, new.id)["paid"] == "120000.00"
        # Сумма не совпала ни с одним — платёж спорный, в «Оплачено» не входит.
        disputed = _income(session, space, "ТОО Гамма", "7000", date(2026, 7, 1))
        detail = payments.of_contract(session, _ws(session, space), FULL, third.id)
        assert detail["summary"]["paid"] == "5000.00" and detail["summary"]["open"] == 1
        assert [item["how"] for item in detail["items"]] == ["open", "auto"]
        assert _summary(session, space, new.id)["open"] == 1
        # Человек решает — платёж уходит в этот договор и из спорных у второго.
        payments.decide(session, _ws(session, space), FULL, OWNER, third.id, disputed, "link")
        assert _summary(session, space, third.id)["paid"] == "12000.00"
        assert _summary(session, space, new.id)["open"] == 0
        # «Не по договору» — платёж пропадает из «Оплачено».
        payments.decide(session, _ws(session, space), FULL, OWNER, third.id, disputed, "unlink")
        assert _summary(session, space, third.id)["paid"] == "5000.00"
        # «Как по выписке» — снова спорный.
        payments.decide(session, _ws(session, space), FULL, OWNER, third.id, disputed, "auto")
        assert _summary(session, space, third.id)["open"] == 1


def test_dogovor_bez_daty_ne_delaet_vse_oplaty_spornymi(space):
    with finance_session() as session:
        dated = _make(session, space, executor="BBC", customer="ТОО Эпсилон", type="Разовая услуга",
                      amount="100000", signed_at="01.02.2026")
        _income(session, space, "ТОО Эпсилон", "40000", date(2026, 3, 1))
        # Второй договор клиента заведён без даты — прежняя оплата остаётся за первым.
        _make(session, space, executor="BBC", customer="ТОО Эпсилон", type="Разовая услуга", amount="3000")
        assert _summary(session, space, dated.id)["paid"] == "40000.00"
        assert _summary(session, space, dated.id)["open"] == 0


def test_chuzhoy_schet_ne_nashego_yurlica_ne_oplata(space):
    with finance_session() as session:
        contract = _make(session, space, executor="BBCA", customer="ТОО Дельта", type="Разовая услуга", amount="900")
        # Поступление на счёт BBC — не оплата договора BBCA.
        _income(session, space, "ТОО Дельта", "900", date(2026, 4, 4))
        assert _summary(session, space, contract.id) is None


def test_oplaty_zakryty_bez_zhurnala(space):
    with finance_session() as session:
        no_money = service.Access(view=True, edit=True, hidden=frozenset({"paid", "remaining"}))
        with pytest.raises(PermissionError):
            payments.summaries(session, _ws(session, space), no_money)


def test_polya_po_vypiske_tolko_chtenie_i_v_liste_za_faylom(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        fields = {item["key"]: item for item in setup.schema(session, workspace, FULL)["fields"]}
        assert fields["paid"]["editable"] is False and fields["remaining"]["editable"] is False
        contract = _make(session, space, executor="BBC", customer="ТОО А")
        with pytest.raises(finance_service.FinanceError, match="считается по выписке"):
            service.patch(session, workspace, FULL, OWNER, contract.id, {"paid": "1"}, known_seq=None)


def test_dvoyniki_v_spiske(space):
    assert is_similar("Абонентское обслуживание", "Абонентское обслуживаниее")
    assert is_similar("Бух. сопровождение", "Бух сопровождение")
    assert not is_similar("НО", "ЮО")
    assert not is_similar("Аудит", "Кадровый аудит")
    # Две правки по счёту, но смысл противоположный — не двойник.
    assert not is_similar("Действующий", "Недействующий")
    assert not is_similar("Исполнен", "Неисполнен")
    with finance_session() as session:
        workspace = _ws(session, space)
        _make(session, space, executor="BBC", customer="ТОО А", type="Абонентское обслуживаниее")
        lists = setup.schema(session, workspace, FULL)["lists"]["type"]
        twin = next(item for item in lists if item["value"] == "Абонентское обслуживаниее")
        base = next(item for item in lists if item["value"] == "Абонентское обслуживание")
        assert twin["similar"] == base["id"]
        # «Это разные» — подсказка уходит и не возвращается после правки смысла.
        setup.update_value(session, workspace, uuid.UUID(twin["id"]), {"distinct": base["id"]})
        setup.update_value(session, workspace, uuid.UUID(twin["id"]), {"meaning": {"billing": "month"}})
        lists = setup.schema(session, workspace, FULL)["lists"]["type"]
        assert "similar" not in next(item for item in lists if item["value"] == "Абонентское обслуживаниее")


def test_novaya_kompaniya_polya_po_vypiske_v_liste(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        from app.finance.contracts.fields import with_live_columns

        columns = [{"key": "amount"}, {"key": "paid_snapshot"}, {"key": "remaining_snapshot"}, {"key": "note"}]
        assert [item["key"] for item in with_live_columns(columns)] == [
            "amount", "paid_snapshot", "remaining_snapshot", "paid", "remaining", "note",
        ]
        assert with_live_columns([{"key": "amount"}]) == [{"key": "amount"}]
        assert session.get(GroupEntity, _make(session, space, executor="BBC").executor_id) is not None
