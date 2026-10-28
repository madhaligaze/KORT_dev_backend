"""Начисления ядра и сверка с книгой-сводкой (фаза 2).

Начисление не хранится - считается из условий договора на день начала
периода: сумма и исполнитель по соглашениям с датой, статус по датам, день
периода, поправка месяца. Сверка сравнивает это со строками книги и
предлагает дописать в условия то, чего не хватает; после записи
предложения договор совпадает с книгой.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.finance import service as finance_service
from app.finance.contracts import service, setup
from app.finance.contracts.models import ContractAmendment, GroupEntity, ListValue
from app.finance.core import book as book_module
from app.finance.core import changes, shadow
from app.finance.core.accruals import Ledger, periods
from app.finance.db import finance_session

import company_layer


@pytest.fixture(autouse=True)
def _company_layer(monkeypatch):
    """Компания теста - с шапкой таблиц копии прода: её знает словарь компании, а не ядро (п.9 «Пакеты»)."""
    company_layer.everywhere(monkeypatch)


FULL = service.Access(view=True, edit=True, setup=True, admin=True)
EDITOR = service.Access(view=True, edit=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")

HEADER = [
    "Мес", "Техн. 1", "Наша Фирма", "Вид Услуги", "Заказчик (Название Фирмы)", "По Выписке Банка", "По 1C",
    "Сумма Договора", "№ Договора", "Период с (нач.)", "Период по (зав.)", "№ Счета", "Дата выстав. Счета",
    "Сумма Факт Поступ.", "Сальдо Конец (АВР)",
]
MONTHS = {6: "ИЮНЬ", 7: "ИЮЛЬ", 8: "АВГУСТ", 9: "СЕНТЯБРЬ", 10: "ОКТЯБРЬ"}


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
        bbc = setup.add_entity(session, workspace, name="BBC", code="BBC")
        setup.add_entity(session, workspace, name="BBC legal support", code="BBCL")
        session.flush()
        return workspace.id


def _ws(session, space_id):
    return finance_service.get_workspace(session, space_id)


def _status(session, space_id, phase: str) -> ListValue:
    return next(
        row
        for row in session.scalars(sa.select(ListValue).where(ListValue.workspace_id == space_id, ListValue.field_key == "status"))
        if (row.meaning or {}).get("phase") == phase
    )


def _make(session, space_id, **values):
    values.setdefault("executor", "BBC")
    values.setdefault("type", "Абонентское обслуживание")
    return service.create(session, _ws(session, space_id), FULL, OWNER, values)


def _row(month: int, number: str, customer: str, amount: str, day: int, *, own: str = "BBC") -> list[str]:
    start = date(2026, month, day)
    nxt = date(2026 + (month == 12), month % 12 + 1, day) - timedelta(days=1)
    return [
        str(month), f"{MONTHS[month]} 2026", own, "Абон.П.", customer, "", "", amount, number,
        f"{start:%d.%m.%Y}", f"{nxt:%d.%m.%Y}", "", "", "", "",
    ]


def _book(rows: list[list[str]]) -> book_module.Book:
    return book_module.build_book([HEADER, *rows], title="Сводка", worksheet="Сводка все ЮР лица")


def _schedule(session, contract, since=date(2026, 6, 1), until=date(2026, 10, 1)):
    session.refresh(contract)
    return Ledger(session, contract.workspace_id, [contract]).schedule(contract, since=since, until=until)


# ── Периоды и условия на дату ────────────────────────────────────────────────


def test_periody_s_dnya_dogovora_i_s_dnya_perioda():
    first = list(zip(range(3), periods(date(2026, 6, 25), 25)))
    assert [item[1] for item in first] == [
        (date(2026, 6, 25), date(2026, 7, 24)), (date(2026, 7, 25), date(2026, 8, 24)), (date(2026, 8, 25), date(2026, 9, 24)),
    ]
    # Договор от 25.05, а период с 1-го: первый период короче.
    second = [item[1] for item in zip(range(2), periods(date(2026, 5, 25), 1))]
    assert second == [(date(2026, 5, 25), date(2026, 5, 31)), (date(2026, 6, 1), date(2026, 6, 30))]
    # 31-е в коротком месяце - последний день месяца.
    third = [item[1][0] for item in zip(range(3), periods(date(2026, 1, 31), 31))]
    assert third == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]


def test_nachisleniya_v_mesyac_summa_i_ispolnitel_po_soglasheniyam(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, customer="ТОО Альфа", amount="100000", signed_at="2026-06-25", number="№1",
                         status="Действующий")
        assert contract.billing == "month"
        legal = session.scalar(sa.select(GroupEntity).where(GroupEntity.code == "BBCL")).counterparty_id
        # Перевод на другое ТОО с августовского периода и новая цена с сентябрьского.
        service.patch(session, workspace, FULL, OWNER, contract.id, {"executor": {"id": str(legal)}}, known_seq=None,
                      mode=service.Mode(kind="from_date", effective_from=date(2026, 8, 25)))
        service.patch(session, workspace, FULL, OWNER, contract.id, {"amount": "150000"}, known_seq=None,
                      mode=service.Mode(kind="from_date", effective_from=date(2026, 9, 25)))
        months = _schedule(session, contract).by_month()
        assert list(months) == ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
        assert [months[m].amount for m in months] == [Decimal("100000")] * 3 + [Decimal("150000")] * 2
        assert months["2026-07"].executor_id != legal and months["2026-08"].executor_id == legal
        # Будущая цена - уже в будущем месяце, хотя договор её ещё не знает.
        service.patch(session, workspace, FULL, OWNER, contract.id, {"amount": "200000"}, known_seq=None,
                      mode=service.Mode(kind="from_date", effective_from=date(2099, 1, 25)))
        future = _schedule(session, contract, since=date(2098, 12, 1), until=date(2099, 1, 1)).by_month()
        assert future["2098-12"].amount == Decimal("150000") and future["2099-01"].amount == Decimal("200000")


def test_opechatka_v_tekushchem_otrezke_beretsya_iz_dogovora(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, customer="ТОО Альфа", amount="100000", signed_at="2026-06-01", number="№1")
        service.patch(session, workspace, FULL, OWNER, contract.id, {"amount": "120000"}, known_seq=None,
                      mode=service.Mode(kind="from_date", effective_from=date(2026, 8, 1)))
        # Опечатка: на самом деле с августа 125 000 - соглашение не меняется, договор меняется.
        service.patch(session, workspace, FULL, OWNER, contract.id, {"amount": "125000"}, known_seq=None,
                      mode=service.Mode(kind="fix"))
        months = _schedule(session, contract).by_month()
        assert months["2026-07"].amount == Decimal("100000") and months["2026-08"].amount == Decimal("125000")


def test_status_s_daty_pauza_i_okonchanie(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, customer="ТОО Альфа", amount="100000", signed_at="2026-06-01", number="№1",
                         status="Действующий")
        active, paused = _status(session, space, "active"), _status(session, space, "suspended")
        changes.status_from(session, workspace, EDITOR, OWNER, contract.id, paused.id, date(2026, 7, 1))
        changes.status_from(session, workspace, EDITOR, OWNER, contract.id, active.id, date(2026, 9, 1))
        session.refresh(contract)
        assert contract.status_id == active.id, "последнее по дате изменение - текущий статус"
        plan = _schedule(session, contract)
        assert list(plan.by_month()) == ["2026-06", "2026-09", "2026-10"] and plan.paused == ["2026-07", "2026-08"]
        with pytest.raises(changes.ChangeError, match="и так"):
            changes.status_from(session, workspace, EDITOR, OWNER, contract.id, active.id, date(2026, 9, 15))
        # Окончание обрывает начисления; расторгнутый без даты не начисляется - не угадываем.
        service.patch(session, workspace, FULL, OWNER, contract.id, {"end_date": "2026-08-31"}, known_seq=None)
        assert list(_schedule(session, contract).by_month()) == ["2026-06"]
        other = _make(session, space, customer="ТОО Бета", amount="1", signed_at="2026-06-01", number="№2",
                      status="Недействующий")
        assert _schedule(session, other).why == "ended_unknown"


def test_razovyy_uslovie_i_popravki_mesyaca(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        once = _make(session, space, customer="ТОО Альфа", amount="50000", signed_at="2026-07-10", number="№1",
                     type="Разовая услуга")
        plan = _schedule(session, once)
        assert [(item.month, item.amount) for item in plan.accruals] == [("2026-07", Decimal("50000"))]
        terms = _make(session, space, customer="ТОО Бета", amount="20% по разовым", signed_at="2026-07-10", number="№2",
                      type="Агентский")
        assert _schedule(session, terms).why == "terms"

        monthly = _make(session, space, customer="ТОО Гамма", amount="100000", signed_at="2026-06-01", number="№3")
        with pytest.raises(changes.ChangeError, match="причину"):
            changes.set_adjustment(session, workspace, EDITOR, OWNER, monthly.id, "2026-07", amount="50000")
        changes.set_adjustment(session, workspace, EDITOR, OWNER, monthly.id, "2026-07", amount="50000", reason="скидка")
        changes.set_adjustment(session, workspace, EDITOR, OWNER, monthly.id, "2026-08", skip=True, reason="каникулы")
        months = _schedule(session, monthly).by_month()
        assert months["2026-07"].amount == Decimal("50000") and months["2026-07"].base == Decimal("100000")
        assert months["2026-08"].skipped and months["2026-08"].out()["amount"] is None
        # Поправка заменяет прежнюю за тот же месяц, а убранная возвращает месяц к договору.
        doc = changes.set_adjustment(session, workspace, EDITOR, OWNER, monthly.id, "2026-07", amount="70000", reason="скидка")
        assert _schedule(session, monthly).by_month()["2026-07"].amount == Decimal("70000")
        changes.remove_adjustment(session, workspace, EDITOR, OWNER, monthly.id, doc.id)
        assert _schedule(session, monthly).by_month()["2026-07"].amount == Decimal("100000")
        # Доначисление: у разового в другом месяце.
        changes.set_adjustment(session, workspace, EDITOR, OWNER, once.id, "2026-09", amount="5000", reason="доп. работа")
        assert [item.month for item in _schedule(session, once).accruals] == ["2026-07", "2026-09"]


# ── Сверка с книгой ──────────────────────────────────────────────────────────


def test_sverka_predlagaet_i_posle_zapisi_sovpadaet(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        legal = session.scalar(sa.select(GroupEntity).where(GroupEntity.code == "BBCL")).counterparty_id
        # День периода: подписан 25-го, а в книге периоды с 1-го.
        a = _make(session, space, customer="ТОО Альфа", amount="100000", signed_at="2025-05-25", number="№A")
        # Цена сменилась с сентября, а договор прежней не знает. Имя - не короче
        # пяти букв: короткое «Бета» внутри чужого имени сопоставитель не считает
        # тем же клиентом.
        b = _make(session, space, customer="ТОО Бетаком", amount="100000", signed_at="2025-01-10", number="№B")
        # Клиента перевели на BBCL с августа, а договор не знает, кто был до.
        c = _make(session, space, customer="ТОО Гамма", amount="100000", signed_at="2025-01-05", number="№C",
                  executor={"id": str(legal)})
        # Разовая скидка в июле.
        d = _make(session, space, customer="ТОО Дельта", amount="100000", signed_at="2025-01-15", number="№D")
        # Книга кончилась раньше: октября нет.
        e = _make(session, space, customer="ТОО Эпсилон", amount="100000", signed_at="2025-01-20", number="№E")
        rows = []
        for month in (6, 7, 8, 9, 10):
            rows.append(_row(month, "№A", "Альфа", "100 000", 1))
            rows.append(_row(month, "№B", "Бетаком", "63 000" if month < 9 else "100 000", 10))
            rows.append(_row(month, "№C", "Гамма", "100 000", 5, own="BBC" if month < 8 else "BBCL"))
            rows.append(_row(month, "№D", "Дельта", "50 000" if month == 7 else "100 000", 15))
            if month < 10:
                rows.append(_row(month, "№E", "Эпсилон", "100 000", 20))
        once = _row(9, "БезДог.09-1", "ТОО Новый", "15 000", 3)
        once[10] = ""  # у разовой строки книги «Период по» пуст
        rows.append(once)
        book = _book(rows)

        result = shadow.reconcile(session, workspace, FULL, book)
        kinds = {(result.contracts[p.contract_id].contract.number, p.kind): p for p in result.proposals()}
        assert set(kinds) == {
            ("№A", "period_day"), ("№B", "amount_before"), ("№C", "executor_before"), ("№D", "adjust"), ("№E", "stop"),
        }
        assert kinds[("№B", "amount_before")].value == {"amount": "63000", "until": "2026-09-10", "after": "100000"}
        assert kinds[("№C", "executor_before")].value["until"] == "2026-08-05"
        assert kinds[("№E", "stop")].options == ["end", "pause"]
        assert [item["number"] for item in result.unknown] == ["БезДог.09-1"]
        counts = result.counts()
        assert counts["contracts"] == 5 and counts["unknown"] == 1

        for proposal in result.proposals():
            option = "end" if proposal.kind == "stop" else ""
            changes.apply_proposal(session, workspace, EDITOR, OWNER, proposal.contract_id, proposal.kind, proposal.value, option=option)
        after = shadow.reconcile(session, workspace, FULL, book)
        assert list(after.proposals()) == []
        assert all(item.clean for item in after.contracts.values()), [
            (item.contract.number, [(line.month, line.codes) for line in item.lines if line.codes])
            for item in after.contracts.values()
            if not item.clean
        ]
        # История «до даты» не трогает текущее значение договора.
        session.refresh(b)
        session.refresh(c)
        assert b.amount == Decimal("100000") and c.executor_id == legal
        origins = {row.origin for row in session.scalars(sa.select(ContractAmendment).where(ContractAmendment.contract_id == b.id))}
        assert origins == {"book"}

        # Строка без договора - договор по её строкам; после этого «нет в реестре» пусто.
        key = after.unknown[0]["key"]
        created = changes.create_from_book(session, workspace, FULL, OWNER, book, key)
        assert created.number == "БезДог.09-1" and created.billing == "total" and created.amount == Decimal("15000")
        assert shadow.reconcile(session, workspace, FULL, book).unknown == []


def test_sverka_pachkoy_i_vozvrat_tochkoy(space):
    from app.finance import history
    from app.finance.contracts import restore

    # Автор правок - из контекста запроса, как у маршрута: иначе точка сочла
    # бы записанное пачкой чужой правкой и не вернула его.
    token = history.CONTEXT.set(history.AuditContext(user_id=OWNER.user_id))
    try:
        with finance_session() as session:
            workspace = _ws(session, space)
            a = _make(session, space, customer="ТОО Альфа", amount="100000", signed_at="2025-05-25", number="№A")
            b = _make(session, space, customer="ТОО Бетаком", amount="100000", signed_at="2025-05-27", number="№B")
            book = _book([_row(m, n, c, "100 000", 1) for m in (6, 7, 8) for n, c in (("№A", "Альфа"), ("№B", "Бетаком"))])
            with pytest.raises(PermissionError):
                changes.apply_all(session, workspace, service.Access(view=True), OWNER, book, "period_day")
            out = changes.apply_all(session, workspace, FULL, OWNER, book, "period_day")
            assert out["done"] == 2 and out["point"] is not None
            session.refresh(a)
            assert a.period_day == 1
            restore.restore_point(session, workspace, FULL, OWNER, uuid.UUID(out["point"]["id"]))
            session.refresh(a)
            session.refresh(b)
            assert a.period_day is None and b.period_day is None
    finally:
        history.CONTEXT.reset(token)


def test_lenta_mesyacev_dogovora(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, customer="ТОО Альфа", amount="100000", signed_at="2026-06-01", number="№A")
        book = _book([_row(m, "№A", "Альфа", "100 000" if m != 8 else "90 000", 1) for m in (6, 7, 8)])
        out = shadow.contract_months(session, workspace, FULL, contract, book, money=True, on=date(2026, 10, 25))
        by_month = {row["month"]: row for row in out["months"]}
        assert out["schedule"]["kind"] == "month" and out["schedule"]["period_day"] == 1
        assert by_month["2026-08"]["codes"] == ["amount"] and by_month["2026-07"]["codes"] == []
        assert by_month["2026-10"]["book"] is None and by_month["2026-10"]["codes"] == [], "вне окна книги - не расхождение"
        assert "paid" in by_month["2026-06"]["book"]
        hidden = shadow.contract_months(session, workspace, FULL, contract, book, money=False, on=date(2026, 10, 25))
        assert "paid" not in hidden["months"][-1]["book"]
        assert [item["kind"] for item in out["proposals"]] == ["amount_from"]
        alone = shadow.contract_months(session, workspace, FULL, contract, None, money=True, on=date(2026, 10, 25))
        assert alone["book"] is False and [row["month"] for row in alone["months"]][:2] == ["2026-11", "2026-10"]
