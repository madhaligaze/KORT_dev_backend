"""Переход с таблицы на KORT (фаза 10): сверки по юрлицам и счета юрлица из KORT.

Переключает человек, и окно перехода показывает, что станет со списком. У
юрлица, переведённого на KORT, «Счета → Не выставлены», число у пункта и бот
берут состояние счёта из начислений KORT; строка таблицы без договора в KORT
не пропадает, а счёт без строки в таблице пишется документом KORT.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from app.finance import history
from app.finance import service as finance_service
from app.finance.contracts import bill_book
from app.finance.contracts import bills as bills_module
from app.finance.contracts import service, setup
from app.finance.contracts.models import GroupEntity
from app.finance.core import billing, switch
from app.finance.core import book as book_module
from app.finance.core.models import Document
from app.finance.db import finance_session
from app.finance.models import ActionLog
from app.finance.service import FinanceError

FULL = service.Access(view=True, edit=True, setup=True, admin=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")
TODAY = date(2026, 10, 20)
HEADER = [
    "Мес", "Техн. 1", "Наша Фирма", "Вид Услуги", "Заказчик (Название Фирмы)", "Сумма Договора", "№ Договора",
    "Период с (нач.)", "Период по (зав.)", "Счет", "№ Счета", "Дата выстав. Счета",
]
MONTHS = {9: "СЕНТЯБРЬ", 10: "ОКТЯБРЬ"}


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
        setup.add_entity(session, workspace, name="BBC legal support", code="BBCL")
        return workspace.id


@pytest.fixture
def audit():
    token = history.CONTEXT.set(history.AuditContext(user_id=OWNER.user_id))
    yield
    history.CONTEXT.reset(token)


def _row(month: int, own: str, number: str, customer: str, state: str, bill_no: str = "") -> list[str]:
    start = date(2026, month, 1)
    return [
        str(month), f"{MONTHS[month]} 2026", own, "Абон.П.", customer, "100 000", number,
        f"{start:%d.%m.%Y}", f"{start + timedelta(days=29):%d.%m.%Y}", state, bill_no, "",
    ]


ROWS = [
    _row(9, "BBC", "№1", "Альфаком", "TRUE", "101"),
    _row(10, "BBC", "№1", "Альфаком", "Еще рано"),  # у KORT октябрь просрочен: период с 01.10
    _row(9, "BBCL", "№3", "Гаммаком", "TRUE", "103"),
    _row(10, "BBCL", "№3", "Гаммаком", "Просрочка более 5 дней"),
    _row(9, "BBC", "№4", "Дельтаком", "TRUE", "104"),
    _row(10, "BBC", "№4", "Дельтаком", "Просрочка 1 день"),  # договор окончен 30.09 - KORT счёт не ждёт
    _row(10, "BBC", "№9", "Омегаком", "Просрочка 2 дня"),  # договора в KORT нет
]


def _contracts(session, workspace) -> None:
    def make(executor: str, customer: str, number: str):
        return service.create(
            session, workspace, FULL, OWNER,
            {"executor": executor, "customer": customer, "number": number, "amount": "100000",
             "type": "Абонентское обслуживание", "signed_at": "2026-09-01"},
        )

    make("BBC", "ТОО Альфаком", "№1")
    make("BBC", "ТОО Бетаком", "№2")  # в таблице строк нет - счета есть только у KORT
    make("BBCL", "ТОО Гаммаком", "№3")
    ended = make("BBC", "ТОО Дельтаком", "№4")
    service.patch(session, workspace, FULL, OWNER, ended.id, {"end_date": "2026-09-30"}, known_seq=None)


def _entity(session, code: str) -> uuid.UUID:
    return session.scalar(sa.select(GroupEntity).where(GroupEntity.code == code)).counterparty_id


def _board() -> bills_module.Board:
    return bills_module.build_board([HEADER, *ROWS], title="Сводка", worksheet="Сводка все ЮР лица")


def test_scheta_yurlica_iz_kort(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        _contracts(session, workspace)
        board = _board()
        assert switch.bills_board(session, workspace.id, board, today=TODAY) is board, "никто не переведён - та же сводка"
        out = switch.build_bills(session, workspace.id, board, {_entity(session, "BBC")}, TODAY)
        items = {(bill.number, bill.month): bill for bill in out.bills}
        assert set(items) == {
            ("№1", "ОКТЯБРЬ 2026"), ("№2", "СЕНТЯБРЬ 2026"), ("№2", "ОКТЯБРЬ 2026"), ("№3", "ОКТЯБРЬ 2026"), ("№9", "ОКТЯБРЬ 2026"),
        }, "№4 за октябрь ушёл: по договору счёт не нужен"
        first = items[("№1", "ОКТЯБРЬ 2026")]
        assert (first.source, first.group, first.text, first.line) == ("kort", "overdue", "Просрочка более 5 дней", 3)
        made = items[("№2", "ОКТЯБРЬ 2026")]
        assert (made.source, made.line, made.own, made.customer, made.kind, made.amount, made.period_from) == (
            "kort", 0, "BBC", "ТОО Бетаком", "Абонентское обслуживание", Decimal("100000"), date(2026, 10, 1),
        )
        assert bills_module.state_of(made.text) == made.group == "overdue", "слова KORT читаются так же, как слова таблицы"
        other = items[("№3", "ОКТЯБРЬ 2026")]
        assert (other.source, other.text) == ("", "Просрочка более 5 дней"), "BBCL на таблице - словами таблицы"
        assert items[("№9", "ОКТЯБРЬ 2026")].source == "", "строка без договора в KORT остаётся"
        assert {(bill.number, bill.month) for bill in out.done} == {
            ("№1", "СЕНТЯБРЬ 2026"), ("№3", "СЕНТЯБРЬ 2026"), ("№4", "СЕНТЯБРЬ 2026"),
        }
        assert out.kort == ("BBC",)
        assert switch.words("soon", date(2026, 10, 22), TODAY) == "2 дня до выставления"
        assert switch.words("soon", TODAY, TODAY) == "Сегодня день выставления"
        assert switch.words("overdue", date(2026, 10, 19), TODAY) == "Просрочка 1 день"


def test_perehod_okno_zapis_i_vozvrat(space, audit):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        _contracts(session, workspace)
        board = _board()
        bbc = _entity(session, "BBC")
        with pytest.raises(PermissionError):
            switch.set_source(session, workspace, service.Access(view=True, edit=True), OWNER, link="bills", entity_id=bbc, source="kort")
        with pytest.raises(FinanceError, match="только в таблице"):
            switch.set_source(session, workspace, FULL, OWNER, link="payments", entity_id=bbc, source="kort")

        # Окно перехода: что станет со списком BBC.
        ahead = switch.preview_bills(session, workspace.id, bbc, "kort", today=TODAY, board=board)
        assert {(item["number"], item["month"]) for item in ahead["appear"]} == {("№2", "СЕНТЯБРЬ 2026"), ("№2", "ОКТЯБРЬ 2026")}
        assert {(item["number"], item["month"]) for item in ahead["vanish"]} == {("№4", "ОКТЯБРЬ 2026")}
        assert [(item["bill"]["number"], item["before"], item["bill"]["group"]) for item in ahead["regroup"]] == [("№1", "early", "overdue")]
        assert ahead["counts"]["before"] == 3 and ahead["counts"]["after"] == 4

        switch.set_source(session, workspace, FULL, OWNER, link="bills", entity_id=bbc, source="kort")
        assert switch.kort_entities(session, workspace.id, "bills") == {bbc}
        titles = [row.title for row in session.scalars(sa.select(ActionLog).where(ActionLog.kind == "core.settings").order_by(ActionLog.at))]
        assert titles[-1] == "Счета к выставлению BBC - из KORT"
        shown = switch.bills_board(session, workspace.id, board, today=TODAY)
        assert shown.kort == ("BBC",) and switch.bills_board(session, workspace.id, board, today=TODAY) is shown, "посчитанное держится до правки"

        # Счёт без строки в таблице пишется документом KORT - с журналом и проверкой чужой правки.
        made = next(bill for bill in shown.bills if (bill.number, bill.month) == ("№2", "ОКТЯБРЬ 2026"))
        card = billing.kort_card(session, workspace.id, made)
        assert card["kort"] and card["place"]["row"] == 0 and card["fields"]["bill_no"] == ""
        saved = billing.write_kort(session, workspace, made, card["fields"], {"bill_no": "301", "bill_date": "2026-10-20"}, None, actor="owner@test")
        assert saved["changed"] == ["bill_no", "bill_date"] and saved["book"]["fields"]["bill_date"] == "20.10.2026"
        again = billing.write_kort(session, workspace, made, card["fields"], {"bill_no": "301", "bill_date": "2026-10-20"}, None, actor="owner@test")
        assert again["changed"] == [], "повтор записи - не отказ и не второй документ"
        with pytest.raises(bill_book.BookError) as conflict:
            billing.write_kort(session, workspace, made, {"bill_no": ""}, {"bill_no": "302"}, None, actor="owner@test")
        assert conflict.value.status == 409
        docs = session.scalars(sa.select(Document).where(Document.kind == "bill", Document.deleted_at.is_(None))).all()
        assert [(doc.number, doc.period) for doc in docs] == [("301", date(2026, 10, 1))]
        assert session.scalar(sa.select(ActionLog).where(ActionLog.kind == "bill.kort")).title.startswith("счёт выставлен в KORT")
        after = switch.bills_board(session, workspace.id, board, today=TODAY)
        assert after is not shown, "записанный счёт меняет список сразу"
        issued = next(bill for bill in after.done if (bill.number, bill.month) == ("№2", "ОКТЯБРЬ 2026"))
        assert (issued.bill_no, issued.bill_date) == ("301", "20.10.2026")
        assert ("№2", "ОКТЯБРЬ 2026") not in {(bill.number, bill.month) for bill in after.bills}

        # Снять отметку: счёт снова ждёт, документа-счёта нет.
        billing.write_kort(session, workspace, made, {"bill_no": "301", "bill_date": "20.10.2026"}, {"bill_no": "", "bill_date": ""}, None, actor="owner@test")
        assert billing.kort_bills(session, workspace.id) == {}

        # Обратно на таблицу - окно говорит, что вернётся, и список снова словами таблицы.
        back = switch.preview_bills(session, workspace.id, bbc, "book", today=TODAY, board=board)
        assert {(item["number"], item["month"]) for item in back["appear"]} == {("№4", "ОКТЯБРЬ 2026")}
        switch.set_source(session, workspace, FULL, OWNER, link="bills", entity_id=bbc, source="book")
        assert switch.bills_board(session, workspace.id, board, today=TODAY) is board


def test_svodka_zvena_po_yurlicam(space, audit, monkeypatch):
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space)
        _contracts(session, workspace)
        book = book_module.build_book([HEADER, *ROWS], title="Сводка", worksheet="Сводка все ЮР лица")
        monkeypatch.setattr(book_module, "current", lambda *args, **kwargs: (book, ""))
        out = switch.link_tally(session, workspace, FULL, "bills", today=TODAY)
        rows = {item["code"]: item for item in out["entities"]}
        # BBC: №1 сентябрь совпал, октябрь - срок другой; №2 - строк в таблице нет;
        # №4 сентябрь совпал, октябрь - KORT не ждёт; строка №9 - без договора.
        assert (rows["BBC"]["months"], rows["BBC"]["same"], rows["BBC"]["unknown"], rows["BBC"]["clean"]) == (6, 2, 1, False)
        assert (rows["BBCL"]["months"], rows["BBCL"]["same"], rows["BBCL"]["unknown"], rows["BBCL"]["clean"]) == (2, 2, 0, True)
        assert rows["BBC"]["source"] == "book" and out["switchable"]
        # Готово - когда два последних закрытых месяца без расхождений: 20.10 закрыт один сентябрь.
        assert rows["BBCL"]["closed"] == [{"month": "2026-09", "checked": 1, "same": 1}] and not rows["BBCL"]["ready"]
        later = {item["code"]: item for item in switch.link_tally(session, workspace, FULL, "bills", today=date(2026, 11, 5))["entities"]}
        assert later["BBCL"]["ready"], "сентябрь и октябрь BBCL совпали"
        assert later["BBC"]["closed"] == [
            {"month": "2026-09", "checked": 3, "same": 2},
            {"month": "2026-10", "checked": 4, "same": 0},
        ] and not later["BBC"]["ready"], "строка без договора - тоже расхождение месяца"
        accruals = {item["code"]: item for item in switch.link_tally(session, workspace, FULL, "accruals", today=TODAY)["entities"]}
        assert accruals["BBCL"]["clean"] and accruals["BBC"]["unknown"] == 1

        # Сверка звена «по ТОО» - те же месяцы, что считает «Переход».
        from app.finance.core.shadow import Reconciler

        bbc, bbcl = _entity(session, "BBC"), _entity(session, "BBCL")
        only = billing.reconcile_bills(session, workspace, FULL, book, today=TODAY, entity=bbcl)
        assert list(only["by_entity"]) == [str(bbcl)] and only["by_entity"][str(bbcl)]["months"] == 2 and not only["lines"]
        mine = Reconciler(session, workspace, FULL, book).run().of_entity(bbc, "BBC")
        assert sorted(item.contract.number for item in mine.contracts.values()) == ["№1", "№2", "№4"]
        assert [group["number"] for group in mine.unknown] == ["№9"]
        assert (mine.counts()["months"], mine.counts()["same"]) == (accruals["BBC"]["months"], accruals["BBC"]["same"])


@pytest.fixture
def app(finance_db, monkeypatch) -> FastAPI:
    from app.core.config import settings
    from app.finance import audit as audit_module
    from app.finance import auth
    from finance_routes import finance_app

    monkeypatch.setattr(settings, "environment", "test")
    auth._ATTEMPTS.clear()
    audit_module._VIEWS.clear()
    return finance_app()


def test_perehod_po_pravam(app: FastAPI) -> None:
    from test_finance_access import BASE, activate, employee, grant, register

    owner = register(app)
    entity = owner.post(f"{BASE}/contracts/setup/entities", json={"name": "BBC", "code": "BBC"}).json()["id"]
    listing = owner.get(f"{BASE}/core/switch").json()
    assert [item["key"] for item in listing["links"]] == list(switch.LINKS) and listing["sources"] == {}
    assert owner.get(f"{BASE}/core/switch/bills").json()["error"], "сводная таблица не подключена - словами"
    assert owner.get(f"{BASE}/core/switch/nothing").status_code == 400
    assert owner.put(f"{BASE}/core/switch/payments/{entity}", json={"source": "kort"}).status_code == 400
    moved = owner.put(f"{BASE}/core/switch/bills/{entity}", json={"source": "kort"})
    assert moved.status_code == 200 and moved.json()["sources"] == {"bills": {entity: "kort"}}, moved.text
    person = employee(owner, "Юристов Рысбек", "+77051140001")
    grant(owner, "employee", person["id"], {"contracts": "edit"})
    lawyer = activate(app, "+77051140001", ip="10.0.4.1")
    assert lawyer.get(f"{BASE}/core/switch").status_code == 403
    assert lawyer.put(f"{BASE}/core/switch/bills/{entity}", json={"source": "book"}).status_code == 403
