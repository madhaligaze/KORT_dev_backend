"""1С без интернета: «Тумар Офис» и «Тумар Плюс» из выгрузок 1С и выписок банка - до «Нам должны».

Одна 1С на компьютере бухгалтера ведёт два ТОО (полигон «Тумар», `tests/
polygon`): KORT видит её только файлами - журналы реализаций и счетов,
контрагенты, ОСВ по 1210 на оба юрлица. Оплаты - выписки Kaspi Business.
Человек заводит счета юрлиц (номер счёта и чей он) и отвечает только на
вопросы KORT, ответами каталога. Долг на 25.10 обязан совпасть с правдой.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from polygon import sources
from polygon.world import TODAY, build

from app.finance import importing
from app.finance import service as finance_service
from app.finance.config import finance_settings
from app.finance.contracts import service
from app.finance.contracts.models import Contract
from app.finance.core import onec_start
from app.finance.core.debts import Debts
from app.finance.db import finance_session
from app.finance.onec import files as onec_files
from app.finance.onec.models import OnecBase

FULL = service.Access(view=True, edit=True, setup=True)
ACTOR = service.Actor(None, "robot@polygon")
OFFICE = "1С Тумар Офис/"


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


def _upload(files):
    return onec_files.read([(name[len(OFFICE):], data) for name, data in files.items() if name.startswith(OFFICE)])


def _answers(world, plan) -> dict:
    """Человек-робот: «договор закончился?» - по правде полигона."""
    out = {}
    for question in plan["contracts"]["questions"]:
        truth = next(
            (c for c in world.contracts.values() if c.number == question["number"] and c.entity in ("ТО", "ТП")),
            None,
        )
        assert truth is not None, question
        out[question["key"]] = {"end": truth.end.isoformat()} if truth.end else {"active": True}
    return out


def _statements(session, workspace, world, files, entities) -> None:
    """Счета юрлиц заводит человек; выписки Kaspi - в журнал денег."""
    for key, entity_id in entities.items():
        entity = world.entities[key]
        finance_service.create_account(
            session, workspace, name=f"Kaspi {entity.short}", number=entity.iban, group_entity_id=entity_id,
        )
    names = [account.name for account in finance_service.list_accounts(session, workspace.id)]
    numbers = finance_service.account_numbers(session, workspace.id)
    for name, data in sorted(files.items()):
        if not name.startswith("Банк/") or not any(world.entities[key].short in name for key in entities):
            continue
        preview = importing.analyze(data, Path(name).name, names, account_numbers=numbers)
        batch = finance_service.save_preview(session, workspace, preview)
        finance_service.apply_batch(session, workspace, batch.id, actor="robot")


def _answer_disputes(session, workspace, world, entities) -> list[str]:
    """Спорные оплаты: человек-робот относит каждую к договору по правде полигона."""
    from app.finance.contracts import payments as contract_payments
    from app.finance.core.payments import Money
    from app.finance.models import Operation

    money = Money(session, workspace, today=TODAY)
    codes = {entity_id: key for key, entity_id in entities.items()}
    contracts = list(session.scalars(sa.select(Contract).where(Contract.deleted_at.is_(None))))
    asked = []
    for operation_id, candidates in sorted(money.disputed.items(), key=lambda item: str(item[0])):
        operation = session.get(Operation, operation_id)
        truth = next(
            p for p in world.payments
            if p.day == operation.paid_at and p.amount == operation.amount_base and p.dst in entities and p.alloc
            and p.purpose == operation.comment
        )
        wanted = world.contracts[truth.alloc[0][0]]
        chosen = next(c for c in contracts if c.id in candidates and c.number == wanted.number)
        assert codes.get(chosen.executor_id) in (wanted.entity, wanted.executor_on(TODAY)), truth
        contract_payments.decide(session, workspace, FULL, ACTOR, chosen.id, operation_id, "link")
        asked.append(f"{truth.day} {truth.purpose} → {wanted.number}")
    session.flush()
    return asked


def _owed_by_contract(session, workspace, entity_id) -> dict[str, Decimal]:
    debts = Debts(session, workspace, FULL, day=TODAY)
    out = {}
    for item in debts.items.values():
        owed = item.owed_to(entity_id)
        if owed:
            out[item.contract.number] = owed
    return out


def test_vygruzka_chitaetsya(world_files) -> None:
    _world, files = world_files
    upload = _upload(files)
    kinds = {journal.kind for journal in upload.journals}
    assert kinds == {"sale", "invoice"}
    assert upload.organizations == ['ТОО "Тумар Офис"', 'ТОО "Тумар Плюс"']
    assert upload.osv is not None and upload.osv.start == date(2026, 6, 1)
    assert all(item["bin"] for item in upload.parties.values())


def test_tumar_ofis_iz_faylov_1s_vidit_nam_dolzhny(world_files, finance_db) -> None:
    world, files = world_files
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        upload = _upload(files)
        report = onec_files.apply(session, workspace, upload, actor="robot")
        bases = {base.organization_name: base for base in session.scalars(sa.select(OnecBase))}
        assert len(bases) == 2 and len(report["bases"]) == 2
        entities = {}
        osv = upload.osv
        for name, key in (('ТОО "Тумар Офис"', "ТО"), ('ТОО "Тумар Плюс"', "ТП")):
            base = bases[name]
            onec_start.add_entity(session, workspace, FULL, ACTOR, base.id)
            entities[key] = base.group_entity_id
        questions, moves = [], []
        for name in ('ТОО "Тумар Офис"', 'ТОО "Тумар Плюс"'):
            base = bases[name]
            onec_start.add_participants(session, workspace, FULL, ACTOR, base.id, today=TODAY)
            plan = onec_start.plan(session, workspace, FULL, base.id, today=TODAY, osv=osv)
            questions += [item["number"] for item in plan["contracts"]["questions"]]
            moves += [(item["number"], item["since"]) for item in plan["contracts"]["moves"]]
            onec_start.add_contracts(session, workspace, FULL, ACTOR, base.id, today=TODAY, answers=_answers(world, plan))
        # Ф8 - «договор закончился?» только у «Кайнар Бетон»; Д7 - трое арендаторов у «Тумар Плюс» с 01.10.
        assert questions == ["А-03"]
        assert sorted(moves) == [("А-05", "2026-10-01"), ("А-06", "2026-10-01"), ("А-08", "2026-10-01")]
        # Д11 - аренда с коммунальными по счётчикам начисляется по счетам и актам.
        codes = {entity_id: key for key, entity_id in entities.items()}
        by_documents = {
            (number, codes[executor])
            for number, executor in session.execute(sa.select(Contract.number, Contract.executor_id).where(Contract.billing == "documents"))
        }
        assert by_documents == {(c.number, c.executor_on(TODAY)) for c in world.contracts.values() if c.utilities}
        # Аренда «Green Office» с 01.09: один АВР (сентябрь) и счёт за октябрь - это ряд «в месяц», не разовый договор.
        green = session.scalar(sa.select(Contract).where(Contract.number == "А-01", Contract.executor_id == entities["ТП"]))
        assert green.billing == "month" and green.amount == Decimal(280000)
        moved = onec_start.start_since(session, workspace, FULL, ACTOR, bases['ТОО "Тумар Офис"'].id, osv)
        assert moved["missing"] == []
        _statements(session, workspace, world, files, entities)
        session.flush()
        # У7 (холдинг платит за дочку «по дог. А-07») KORT решает по номеру в назначении сам; спрашивает
        # О5 (номер чужого договора) и оплаты «Береке» без номера при двух договорах на одну сумму (О4).
        asked = _answer_disputes(session, workspace, world, entities)
        assert len(asked) == 4, asked
        # Д12: обеспечительный платёж Green Office - не оплата А-01 (аванса нет), скрыт своей причиной.
        from app.finance.core.payments import Money

        money = Money(session, workspace, today=TODAY)
        money.without_contract()
        assert money.hidden["deposit"] == 1
        green = session.scalar(sa.select(Contract).where(Contract.number == "А-01", Contract.executor_id == entities["ТП"]))
        assert money.contract(green).paid == sum(
            (a for p in world.payments if p.kind == "contract" for c, _m, a in p.alloc if c == "greenoffice"), Decimal(0)
        )
        for key in ("ТО", "ТП"):
            got = _owed_by_contract(session, workspace, entities[key])
            truth = {
                c.number: world.debt(k) for k, c in world.contracts.items()
                if c.side == "sale" and c.executor_on(TODAY) == key and world.debt(k) > 0
            }
            assert got == truth
            assert sum(got.values(), Decimal(0)) == world.owed()[key]
