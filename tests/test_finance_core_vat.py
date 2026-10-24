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
