"""НДС и порог (фаза 7): оборот наших юрлиц к порогу, НДС начислений, перевод клиентов.

Решение владельца 24.10.2026: расчёт НДС и порога - функция ядра без
оговорок. Оборот - выручка юрлица без НДС по начислениям, где оно исполнитель
на день начала периода; перевод клиента - изменение «исполнитель с даты».
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.finance import history
from app.finance import service as finance_service
from app.finance.contracts import restore, service, setup
from app.finance.contracts.models import Contract, ContractAmendment
from app.finance.core import changes, tax, vat
from app.finance.db import finance_session

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
    yield engine


@pytest.fixture
def space(finance_db):
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC", code="BBC")
        setup.add_entity(session, workspace, name="BBC legal support", code="BBCL")
        return workspace.id


def _entity(session, code: str) -> uuid.UUID:
    from app.finance.contracts.models import GroupEntity

    return session.scalar(sa.select(GroupEntity.counterparty_id).where(GroupEntity.code == code))


def _contract(session, workspace, customer: str, amount: str, **values) -> Contract:
    return service.create(
        session, workspace, FULL, OWNER,
        {"executor": "BBC", "customer": customer, "amount": amount, "type": "Абонентское обслуживание",
         "signed_at": "2026-01-01", "number": f"№{customer[-3:]}", **values},
    )


def test_nds_v_summe_sverhu_i_ne_platelshchik():
    rate = Decimal("16")
    assert vat.vat_of(Decimal("116000"), payer=True, mode="included", rate=rate) == (Decimal("100000"), Decimal("16000"), Decimal("116000"))
    assert vat.vat_of(Decimal("100000"), payer=True, mode="on_top", rate=rate) == (Decimal("100000"), Decimal("16000"), Decimal("116000"))
    assert vat.vat_of(Decimal("100000"), payer=False, mode="on_top", rate=rate) == (Decimal("100000"), Decimal("0"), Decimal("100000"))
    assert vat.vat_of(Decimal("100000"), payer=True, mode="none", rate=rate)[1] == Decimal("0")


def test_oborot_k_porogu_i_nds_s_daty_postanovki(space):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        bbc, legal = _entity(session, "BBC"), _entity(session, "BBCL")
        _contract(session, workspace, "ТОО Альфаком", "5000000")
        _contract(session, workspace, "ТОО Бетаком", "1160000", executor={"id": str(legal)})
        # Внутри группы - не выручка: в оборот не идёт.
        service.create(
            session, workspace, FULL, OWNER,
            {"executor": "BBC", "customer": {"id": str(legal)}, "amount": "999999", "type": "Абонентское обслуживание",
             "signed_at": "2026-01-01", "number": "№ВГ"},
        )
        tax.add_period(session, workspace, FULL, OWNER, legal, {"valid_from": "2026-07-01", "regime": "general", "vat_payer": True})
        turnover = vat.Turnover(session, workspace, FULL, 2026, today=TODAY)
        years = turnover.run()
        threshold = turnover.tax_year.threshold
        assert threshold == Decimal("43250000")
        a = years[bbc]
        assert a.fact == Decimal("50000000") and a.forecast == Decimal("60000000"), "январь - октябрь факт, весь год - прогноз"
        assert a.crossing(threshold) == "2026-09" and not a.payer_now
        b = years[legal]
        assert b.months[5].net == Decimal("1160000") and b.months[5].vat == 0, "до постановки на учёт НДС нет"
        assert b.months[6].net == Decimal("1000000") and b.months[6].vat == Decimal("160000"), "с июля - НДС в сумме"
        assert b.payer_now and b.payer_since == date(2026, 7, 1) and b.crossing(threshold) is None
        out = vat.year_out(turnover, years)
        assert out["year"]["threshold"] == "43250000" and out["entities"][0]["code"] == "BBC"


def test_platelshchik_ne_idet_k_porogu_i_dovod_iz_naznacheniy(space):
    """25.10.2026, владелец: часть ТОО группы уже на НДС - для них гонки к порогу нет."""
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        setup.add_entity(session, workspace, name="BBC HR", code="BBC HR")
        setup.add_entity(session, workspace, name="BBC Omar", code="BBC Omar")
        bbc, legal, hr, omar = (_entity(session, code) for code in ("BBC", "BBCL", "BBC HR", "BBC Omar"))
        _contract(session, workspace, "ТОО Альфаком", "5800000")
        _contract(session, workspace, "ТОО Бетаком", "1000000", executor={"id": str(legal)})
        tax.add_period(session, workspace, FULL, OWNER, bbc, {"valid_from": "2025-01-01", "regime": "general", "vat_payer": True})
        tax.add_period(session, workspace, FULL, OWNER, legal, {"valid_from": "2026-07-01", "regime": "general", "vat_payer": True})
        tax.add_period(session, workspace, FULL, OWNER, hr, {"valid_from": "2026-01-01", "regime": "simplified", "vat_payer": False})
        turnover = vat.Turnover(session, workspace, FULL, 2026, today=TODAY)
        years = turnover.run()
        threshold = turnover.tax_year.threshold
        assert (years[bbc].status, years[bbc].payer_from) == ("payer", date(2025, 1, 1))
        assert years[bbc].crossing(threshold) is None, "плательщику порог не нужен, хоть оборот и выше"
        assert years[bbc].vat_fact == Decimal("800000") * 10, "НДС с начала года: 16/116 от 5 800 000 за десять месяцев"
        assert (years[legal].status, years[legal].payer_from) == ("becomes", date(2026, 7, 1))
        assert years[hr].status == "racing" and years[omar].status == "unknown"

        account = finance_service.create_account(session, workspace, name='Kaspi Юр. ТОО "BBC"', group_entity_id=bbc)
        for day, text in (
            (date(2026, 6, 3), "Счет на оплату № 273 от 3 июня 2026 г (в том числе НДС = 20 689,66 KZT)"),
            (date(2026, 5, 20), "за услуги по счету 150 в т.ч. НДС(16%) 13793-10 тенге"),
            (date(2026, 7, 1), "Оплата за услуги, без НДС"),
            (date(2026, 7, 2), "Оплата по договору"),
        ):
            finance_service.create_operation(
                session, workspace,
                finance_service.OperationInput(kind="income", paid_at=day, amount=Decimal("100"), account_to_id=account.id, comment=text),
            )
        hints = vat.vat_hints(session, workspace, 2026)
        assert hints[bbc] | {"sample": ""} == {"total": 4, "with": 2, "without": 1, "first": "2026-05-20", "sample": ""}
        out = vat.year_out(turnover, years, hints)
        rows = {row["code"]: row for row in out["entities"]}
        assert rows["BBC"]["status"] == "payer" and rows["BBC"]["remaining"] is None and rows["BBC"]["hint"]["with"] == 2
        assert Decimal(rows["BBCL"]["remaining"]) == threshold - Decimal("6000000"), "до порога - оборот до даты постановки"


def test_perevod_klientov_predprosmotr_zapis_i_vozvrat(space):
    token = history.CONTEXT.set(history.AuditContext(user_id=OWNER.user_id))
    try:
        with finance_session() as session:
            workspace = finance_service.get_workspace(session, space)
            bbc, legal = _entity(session, "BBC"), _entity(session, "BBCL")
            first = _contract(session, workspace, "ТОО Альфаком", "5000000")
            second = _contract(session, workspace, "ТОО Бетаком", "1000000")
            turnover = vat.Turnover(session, workspace, FULL, 2026, today=TODAY)
            assert {item["number"] for item in turnover.clients(bbc)} == {first.number, second.number}
            moves = [vat.Move(first.id, legal, date(2026, 11, 1))]
            before, after = turnover.run(), turnover.run(moves)
            assert before[bbc].forecast - after[bbc].forecast == Decimal("10000000")
            assert after[legal].forecast == Decimal("10000000")
            assert before[bbc].crossing(turnover.tax_year.threshold) == "2026-08"

            with pytest.raises(PermissionError):
                changes.transfer(session, workspace, service.Access(view=True, edit=True), OWNER, moves)
            out = changes.transfer(session, workspace, FULL, OWNER, moves)
            assert out["done"] == 1 and out["point"] is not None
            session.refresh(first)
            assert first.executor_id == bbc, "перевод с 01.11 - впереди, договор ещё у BBC"
            moved = vat.Turnover(session, workspace, FULL, 2026, today=TODAY).run()
            assert moved[legal].forecast == Decimal("10000000"), "оборот считается по соглашению «с даты»"
            amendment = session.scalar(sa.select(ContractAmendment).where(ContractAmendment.contract_id == first.id))
            assert amendment.effect == "executor" and amendment.effective_from == date(2026, 11, 1)

            restore.restore_point(session, workspace, FULL, OWNER, uuid.UUID(out["point"]["id"]))
            assert session.scalar(sa.select(ContractAmendment).where(ContractAmendment.contract_id == first.id)) is None
            back = vat.Turnover(session, workspace, FULL, 2026, today=TODAY).run()
            assert back[legal].forecast == 0 and back[bbc].forecast == before[bbc].forecast
    finally:
        history.CONTEXT.reset(token)


def test_nds_mesyaca_v_lente_dogovora(space):
    """НДС счёта и АВР месяца - в ленте договора: у исполнителя-плательщика на день периода, по режиму договора."""
    from app.finance.core import shadow

    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        included = _contract(session, workspace, "ТОО Альфаком", "116000")
        on_top = _contract(session, workspace, "ТОО Бетаком", "100000", vat_mode="on_top")
        tax.add_period(session, workspace, FULL, OWNER, _entity(session, "BBC"), {"valid_from": "2026-08-01", "regime": "general", "vat_payer": True})

        def months(contract):
            out = shadow.contract_months(session, workspace, FULL, contract, None, money=True, on=TODAY)
            return {row["month"]: row["vat"] for row in out["months"]}

        before, after = months(included)["2026-07"], months(included)["2026-08"]
        assert before is None, "до постановки на НДС у месяца НДС нет"
        assert after["mode"] == "included" and after["rate"] == "16" and Decimal(after["vat"]) == Decimal("16000")
        assert Decimal(after["net"]) == Decimal("100000") and Decimal(after["gross"]) == Decimal("116000")
        top = months(on_top)["2026-09"]
        assert top["mode"] == "on_top" and Decimal(top["vat"]) == Decimal("16000") and Decimal(top["gross"]) == Decimal("116000")
