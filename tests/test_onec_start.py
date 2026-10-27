"""Вход из 1С: «Тумар Сервис» из одного снимка 1С и ОСВ - до «Нам должны».

Компания полигона («Тумар», `tests/polygon`) без таблиц: только база 1С в
интернете и ОСВ по 1210 файлом. Человек отвечает лишь на вопросы KORT,
ответами каталога. Долг на 25.10 обязан совпасть с правдой полигона до
тенге.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from polygon import sources
from polygon.world import TODAY, build

from app.finance import secretbox
from app.finance import service as finance_service
from app.finance.config import finance_settings
from app.finance.contracts import service
from app.finance.contracts.models import Contract
from app.finance.core import onec_start
from app.finance.core.debts import Debts
from app.finance.core.payments import Bucket, Payment, allocate_months
from app.finance.db import finance_session
from app.finance.models import Counterparty
from app.finance.onec import osv as osv_module
from app.finance.onec import sync as onec_sync
from app.finance.onec.models import OnecBase

FULL = service.Access(view=True, edit=True, setup=True)
ACTOR = service.Actor(None, "robot@polygon")


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    monkeypatch.setattr(finance_settings, "secrets_key", finance_settings.secrets_key or "polygon-test")
    yield engine


@pytest.fixture(scope="module")
def world_files():
    world = build()
    return world, sources.render(world)


def _connected(session, files):
    odata = files["1С Тумар Сервис/odata.json"]
    org = odata["Catalog_Организации"][0]
    workspace = finance_service.ensure_workspace(session)
    base = OnecBase(
        workspace_id=workspace.id, title="1С Тумар Сервис", url="https://1cfresh.kz/a/ea201/1", login="odata",
        secret=secretbox.seal("x"), organization_ref=org["Ref_Key"], organization_name=org["Description"],
        organization_bin=org["ИдентификационныйНомер"], sync_from=date(2026, 1, 1),
    )
    session.add(base)
    session.flush()

    class Fake:
        def rows(self, entity, **_):
            yield from odata.get(entity, [])

    snapshot = onec_sync.read(Fake(), organization_ref=org["Ref_Key"], since=date(2026, 1, 1))
    onec_sync.apply(session, base, snapshot, since=base.sync_from)
    return workspace, base


def test_osv_chitaetsya_po_urovnyam(world_files) -> None:
    _world, files = world_files
    osv = osv_module.read(files["1С Тумар Сервис/ОСВ 1210 июнь 2026.xlsx"], "ОСВ.xlsx")
    assert osv.start == date(2026, 6, 1) and osv.account == "1210" and not osv.problems
    assert sum((line.opening for line in osv.lines), Decimal(0)) == osv.opening_total
    bolashak = next(line for line in osv.lines if "Болашак" in line.party)
    assert bolashak.opening < 0  # С3: кредитовое сальдо - аванс клиента
    assert any(line.main for line in osv.lines)  # С2: сальдо на «Основном договоре»


def test_dogovory_po_aktam(world_files, finance_db) -> None:
    """Д5 - сумма с даты, Д6 - опечатка акта не сдвигает сумму, Д8 - пауза, Д9 - вопрос «закончился?», Д15 - разовый на акт."""
    _world, files = world_files
    with finance_session() as session:
        workspace, base = _connected(session, files)
        onec_start.add_entity(session, workspace, FULL, ACTOR, base.id)
        source = onec_start.read_source(session, workspace, base, today=TODAY)
        drafts = {draft.number: draft for draft in source.drafts.values() if draft.number}
        assert drafts["ТС-09/2025"].amount == Decimal(150000)
        assert drafts["ТС-09/2025"].changes == [(date(2026, 7, 1), Decimal(180000))]
        assert drafts["ТС-10/2026"].amount == Decimal(150000) and drafts["ТС-10/2026"].outliers
        assert drafts["ТС-11/2025"].pauses == [(date(2026, 7, 1), date(2026, 8, 31))]
        assert drafts["ТС-12/2025"].last_day == date(2026, 5, 31)
        singles = [draft for draft in source.drafts.values() if draft.act is not None]
        assert singles and all(draft.billing == "total" for draft in singles)


def test_tumar_servis_iz_1s_vidit_nam_dolzhny(world_files, finance_db) -> None:
    world, files = world_files
    with finance_session() as session:
        workspace, base = _connected(session, files)
        onec_start.add_entity(session, workspace, FULL, ACTOR, base.id)
        participants = onec_start.add_participants(session, workspace, FULL, ACTOR, base.id, today=TODAY)
        assert participants["counts"].get("dispute", 0) == 0
        # У5: две карточки одного БИН - один участник с двумя написаниями.
        taza = world.parties["taza"]
        assert session.scalar(sa.select(sa.func.count()).select_from(Counterparty).where(Counterparty.bin == taza.number)) == 1
        osv = osv_module.read(files["1С Тумар Сервис/ОСВ 1210 июнь 2026.xlsx"], "ОСВ.xlsx")
        plan = onec_start.plan(session, workspace, FULL, base.id, today=TODAY, osv=osv)
        questions = plan["contracts"]["questions"]
        assert [q["number"] for q in questions] == ["ТС-12/2025"]  # Д9 - спросит
        waiting = onec_start.add_contracts(session, workspace, FULL, ACTOR, base.id, today=TODAY)
        assert waiting["waiting"] == [questions[0]["key"]]  # без ответа не заводится
        answered = onec_start.add_contracts(
            session, workspace, FULL, ACTOR, base.id, today=TODAY, answers={questions[0]["key"]: {"end": "2026-05-31"}}
        )
        assert answered["done"] == 1
        moved = onec_start.start_since(session, workspace, FULL, ACTOR, base.id, osv)
        assert moved["missing"] == [] and moved["since"] == "2026-06-01"
        onec_start.use_payments(session, workspace, FULL, ACTOR, base.id)
        session.flush()
        totals = Debts(session, workspace, FULL, day=TODAY).totals()
        assert Decimal(totals["owed"]) == world.owed()["ТС"]
        made = session.scalar(sa.select(sa.func.count()).select_from(Contract).where(Contract.workspace_id == workspace.id))
        assert made == len(plan["contracts"]["items"])


def test_vozvrat_zabiraet_pozdnie_oplaty() -> None:
    """О6: возврат переплаты - минус к прежним оплатам, долг растёт обратно."""
    june = Bucket("2026-06", Decimal(100))
    july = Bucket("2026-07", Decimal(100))
    pay = Payment(key="a", source="1c", contract_id=None, paid_at=date(2026, 6, 5), amount=Decimal(250), hint=None, how="")
    back = Payment(key="b", source="1c", contract_id=None, paid_at=date(2026, 7, 20), amount=Decimal(-50), hint=None, how="")
    over = allocate_months([pay, back], {"2026-06": june, "2026-07": july})
    assert june.left == 0 and july.left == 0
    assert sum((part.amount for part in over), Decimal(0)) == Decimal(0)
