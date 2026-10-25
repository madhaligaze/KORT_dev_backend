"""АВР и ЭСФ ядра (фаза 5): один АВР на договор и месяц - KORT, потом 1С, потом книга.

АВР 1С - реализация со своим месяцем (отчётный период), подписанием покупателя
и ЭСФ за счётом-фактурой; АВР книги - «АВР (наша)» строки месяца. Где база 1С
юрлица подключена, 1С сверяется с книгой; из выбранных АВР - списки работы:
не подписан, без ЭСФ после срока, месяц начислен - АВР нет.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.finance import service as finance_service
from app.finance.contracts import service, setup
from app.finance.contracts.models import GroupEntity
from app.finance.core import acts as core_acts
from app.finance.core import book as book_module
from app.finance.core.accruals import Ledger
from app.finance.db import finance_session
from app.finance.onec.models import OnecBase, OnecDocument

FULL = service.Access(view=True, edit=True, setup=True, admin=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
TODAY = date(2026, 10, 25)
HEADER = [
    "Мес", "Техн. 1", "Наша Фирма", "Заказчик (Название Фирмы)", "Сумма Договора", "№ Договора",
    "Период с (нач.)", "Период по (зав.)", "АВР (наша)", "Сумма (наша)", "№ АВР (наша)", "Дата (наша)",
    "АВР (клиент принял)", "Дата АВР (клиент принял)", "ЭСФ (отпр.)", "Дата ЭСФ (отпр.)",
]
MONTHS = {6: "ИЮНЬ", 7: "ИЮЛЬ", 8: "АВГУСТ", 9: "СЕНТЯБРЬ"}


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


def _row(month: int, act: tuple[str, str, str] | None = None, accepted: str = "", esf: str = "") -> list[str]:
    start = date(2026, month, 1)
    number, amount, day = act or ("", "", "")
    return [
        str(month), f"{MONTHS[month]} 2026", "BBC", "Альфаком", "100 000", "№ОБО/1",
        f"{start:%d.%m.%Y}", "", "TRUE" if act else "FALSE", amount, number, day,
        "TRUE" if accepted else "FALSE", accepted, "TRUE" if esf else "FALSE", esf,
    ]


def test_avr_po_prioritetu_sverka_1s_s_knigoy_i_spiski(space):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        contract = service.create(
            session, workspace, FULL, OWNER,
            {"executor": "BBC", "customer": "ТОО Альфаком", "number": "№ОБО/1", "amount": "100000",
             "billing": "month", "signed_at": "2026-06-01"},
        )
        entity = session.scalar(sa.select(GroupEntity.counterparty_id).where(GroupEntity.code == "BBC"))
        base = OnecBase(
            workspace_id=workspace.id, title="ИБ BBC", url="https://1cfresh.kz/a/ea201/1", login="odata.user",
            secret="x", group_entity_id=entity, sync_from=date(2026, 1, 1),
        )
        session.add(base)
        session.flush()

        def doc(kind: str, ref: str, **values) -> OnecDocument:
            item = OnecDocument(
                workspace_id=workspace.id, base_id=base.id, kind=kind, ref=ref, posted=True,
                party_name="ТОО Альфаком", contract_number="ОБО/1", **values,
            )
            session.add(item)
            return item

        doc("sale", "s6", number="00000000457", doc_date=date(2026, 7, 2), amount=Decimal("100000"),
            period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), signed_at=date(2026, 7, 9))
        doc("sf", "f6", number="00000000301", doc_date=date(2026, 7, 2), basis_ref="s6")
        doc("esf", "e6", number="ESF-1", doc_date=date(2026, 7, 3), basis_ref="f6", status="Доставлен")
        doc("sale", "s7", number="00000000502", doc_date=date(2026, 8, 3), amount=Decimal("100000"),
            period_from=date(2026, 7, 1), period_to=date(2026, 7, 31))
        session.flush()
        book = book_module.build_book([
            HEADER,
            _row(6, ("457", "100 000", "02.07.2026"), accepted="09.07.2026", esf="03.07.2026"),
            _row(7),
            _row(8, ("530", "100 000", "01.09.2026")),
            _row(9),
        ], title="Сводка", worksheet="Сводка все ЮР лица")

        acts = core_acts.Acts(session, workspace, FULL, book, today=TODAY)
        months = acts.contract(contract.id)
        assert months["2026-06"]["act"]["source"] == "1c" and months["2026-06"]["act"]["number"] == "457"
        assert months["2026-06"]["act"]["signed"] == "2026-07-09" and months["2026-06"]["act"]["esf"] == "2026-07-03"
        assert months["2026-06"]["sources"] == ["1c", "book"] and months["2026-06"]["codes"] == []
        assert months["2026-07"]["codes"] == ["act_no_book"], "у июля АВР есть в 1С, книга не знает"
        assert months["2026-08"]["act"]["source"] == "book" and months["2026-08"]["codes"] == ["act_no_1c"]

        shadow = acts.reconcile({contract.id: contract}, window=["2026-06", "2026-07", "2026-08", "2026-09"])
        assert shadow["counts"]["same"] == 1 and shadow["counts"]["act_no_book"] == 1 and shadow["counts"]["act_no_1c"] == 1

        ledger = Ledger(session, workspace.id, [contract])
        accrued = core_acts.accrued_months(ledger, [contract], since=date(2026, 6, 1), until=core_acts.previous_month(TODAY))
        work = acts.worklists({contract.id: contract}, accrued)
        assert [item["act"]["number"] for item in work["unsigned"]] == ["502", "530"], "июль и август не подписаны"
        assert [item["act"]["number"] for item in work["no_esf"]] == ["502", "530"], "ЭСФ нет дольше 15 дней"
        assert [item["month"] for item in work["missing"]] == ["2026-09"], "сентябрь начислен - АВР нет; октябрь ещё рано"


def test_bez_1s_kniga_odna_i_ne_sveryaetsya(space):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        contract = service.create(
            session, workspace, FULL, OWNER,
            {"executor": "BBC", "customer": "ТОО Альфаком", "number": "№ОБО/1", "amount": "100000",
             "billing": "month", "signed_at": "2026-06-01"},
        )
        book = book_module.build_book([HEADER, _row(6, ("457", "100 000", "02.07.2026"), esf="03.07.2026")],
                                      title="Сводка", worksheet="Сводка все ЮР лица")
        acts = core_acts.Acts(session, workspace, FULL, book, today=TODAY)
        june = acts.contract(contract.id)["2026-06"]
        assert june["act"]["source"] == "book" and june["codes"] == [], "без 1С книге не с чем расходиться"
        assert acts.reconcile({contract.id: contract}, window=[])["counts"]["months"] == 0
