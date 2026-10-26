"""«Верно у KORT»: таблица ошиблась, условия договора под неё не подгоняются.

Сверка сама видит, где таблица не сходится с собой: «Период с» месяца не
продолжает «Период по» прошлого - день пропущен или посчитан дважды, - и
месяцы, которые таблица продолжает с этим сдвигом. Решение человека «верно у
KORT» засчитывает месяц сошедшимся, пока KORT и таблица считают его так же,
как в момент решения: правка таблицы или условий договора после решения его
снимает, а не прячется под ним.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
import sqlalchemy as sa

from app.finance import service as finance_service
from app.finance.contracts import service, setup
from app.finance.core import book as book_module
from app.finance.core import changes, shadow, switch
from app.finance.core.models import Document
from app.finance.db import finance_session
from app.finance.models import ActionLog

FULL = service.Access(view=True, edit=True, setup=True, admin=True)
OWNER = service.Actor(uuid.uuid4(), "owner@test")

HEADER = [
    "Мес", "Техн. 1", "Наша Фирма", "Вид Услуги", "Заказчик (Название Фирмы)", "Сумма Договора", "№ Договора",
    "Период с (нач.)", "Период по (зав.)",
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
        setup.add_entity(session, workspace, name="BBC", code="BBC")
        session.flush()
        return workspace.id


def _ws(session, space_id):
    return finance_service.get_workspace(session, space_id)


def _make(session, space_id, **values):
    values.setdefault("executor", "BBC")
    values.setdefault("type", "Абонентское обслуживание")
    return service.create(session, _ws(session, space_id), FULL, OWNER, values)


def _row(month: int, number: str, customer: str, amount: str, start: date, end: date) -> list[str]:
    return [str(month), f"{MONTHS[month]} 2026", "BBC", "Абон.П.", customer, amount, number, f"{start:%d.%m.%Y}", f"{end:%d.%m.%Y}"]


def _book(rows: list[list[str]]) -> book_module.Book:
    return book_module.build_book([HEADER, *rows], title="Сводка", worksheet="Сводка все ЮР лица")


def _chain(number: str, customer: str, starts: list[date], amounts: dict[int, str] | None = None) -> list[list[str]]:
    """Строки договора с июня: «Период по» - накануне следующего «Период с», последний - на месяц."""
    rows = []
    for index, start in enumerate(starts):
        month = 6 + index
        end = starts[index + 1] - timedelta(days=1) if index + 1 < len(starts) else date(2026, month + 1, start.day) - timedelta(days=1)
        rows.append(_row(month, number, customer, (amounts or {}).get(month, "150 000"), start, end))
    return rows


def _lines(session, workspace, book) -> dict[tuple[str, str], shadow.Line]:
    result = shadow.reconcile(session, workspace, FULL, book)
    return {(item.contract.number, line.month): line for item in result.contracts.values() for line in item.lines}


def test_tablica_propustila_den_i_reshenie_verno_u_kort(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, customer="ТОО Альфа", amount="150000", signed_at="2025-02-07", number="№ОБО/100")
        # Как №ОБО/100 в сводной таблице BBC: июль по 06.08, август уже с 08.08, дальше со сдвигом.
        starts = [date(2026, 6, 7), date(2026, 7, 7), date(2026, 8, 8), date(2026, 9, 8), date(2026, 10, 8)]
        rows = _chain("№ОБО/100", "Альфа", starts)
        rows[1][8] = "06.08.2026"
        book = _book(rows)

        lines = _lines(session, workspace, book)
        august = lines[("№ОБО/100", "2026-08")]
        assert august.codes == ["period"] and august.slip == "gap"
        assert august.hint == "таблица пропустила 07.08: июль по 06.08, август с 08.08"
        for month in ("2026-09", "2026-10"):
            assert lines[("№ОБО/100", month)].slip == "shift"
            assert lines[("№ОБО/100", month)].hint == "таблица идёт со сдвигом с пропуска 07.08"
        assert lines[("№ОБО/100", "2026-07")].codes == []

        with pytest.raises(changes.ChangeError, match="и так совпадают"):
            changes.verify_month(session, workspace, FULL, OWNER, book, contract.id, "2026-07")
        doc, text = changes.verify_month(session, workspace, FULL, OWNER, book, contract.id, "2026-08")
        assert text == "№ОБО/100 · август 2026: верно у KORT" and doc.kind == "verified"
        with pytest.raises(changes.ChangeError, match="уже отмечен"):
            changes.verify_month(session, workspace, FULL, OWNER, book, contract.id, "2026-08")
        result = shadow.reconcile(session, workspace, FULL, book)
        assert result.counts()["verified"] == 1 and result.counts()["period"] == 2
        assert not result.contracts[contract.id].clean

        # Остальное, что таблица продолжила со сдвигом, - одним решением.
        bulk = changes.verify_slips(session, workspace, FULL, OWNER, book)
        assert bulk["done"] == 2 and bulk["contracts"] == 1 and bulk["batch"]
        result = shadow.reconcile(session, workspace, FULL, book)
        assert result.contracts[contract.id].clean and result.counts()["verified"] == 3
        # «Переход»: решённые месяцы - сошедшиеся.
        _window, tally = switch._accruals(session, workspace, FULL, book)
        entity = str(contract.executor_id)
        assert tally[entity]["months"] == tally[entity]["same"] == 5

        changes.unverify_batch(session, workspace, FULL, OWNER, bulk["batch"])
        lines = _lines(session, workspace, book)
        assert lines[("№ОБО/100", "2026-08")].verified is not None
        assert lines[("№ОБО/100", "2026-09")].verified is None and lines[("№ОБО/100", "2026-10")].verified is None
        changes.unverify(session, workspace, FULL, OWNER, lines[("№ОБО/100", "2026-08")].verified.id)
        assert shadow.reconcile(session, workspace, FULL, book).counts()["verified"] == 0

        kinds = [row.kind for row in session.scalars(sa.select(ActionLog).where(ActionLog.kind.like("contract.verified%")))]
        assert sorted(kinds) == ["contract.verified"] * 3 + ["contract.verified.remove"] * 3
        title = session.scalar(sa.select(ActionLog.title).where(ActionLog.kind == "contract.verified").order_by(ActionLog.at))
        assert "период у KORT с 07.08, в таблице с 08.08; таблица пропустила 07.08" in title


def test_reshenie_ne_pryachet_pravku_posle_nego(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        contract = _make(session, space, customer="ТОО Альфа", amount="150000", signed_at="2025-02-07", number="№ОБО/100")
        starts = [date(2026, 6, 7), date(2026, 7, 7), date(2026, 8, 8)]
        rows = _chain("№ОБО/100", "Альфа", starts)
        rows[1][8] = "06.08.2026"
        changes.verify_month(session, workspace, FULL, OWNER, _book(rows), contract.id, "2026-08")

        # В таблице поменяли и сумму августа: решение о прежнем расхождении больше не действует.
        rows[2][5] = "160 000"
        changed = _book(rows)
        august = _lines(session, workspace, changed)[("№ОБО/100", "2026-08")]
        assert august.codes == ["amount", "period"] and august.verified is None
        assert august.stale is not None and august.stale_side == "book"
        assert august.out(money=True)["stale"]["side"] == "book"
        # Решили заново - тот же документ с новыми снимками, второго нет.
        changes.verify_month(session, workspace, FULL, OWNER, changed, contract.id, "2026-08")
        docs = list(session.scalars(sa.select(Document).where(Document.kind == "verified", Document.deleted_at.is_(None))))
        assert len(docs) == 1 and docs[0].meta["book"]["amount"] == "160000.00"
        assert _lines(session, workspace, changed)[("№ОБО/100", "2026-08")].verified is not None

        # Условия договора поменялись - KORT считает август иначе, решение снова не действует.
        service.patch(session, workspace, FULL, OWNER, contract.id, {"amount": "155000"}, known_seq=None, mode=service.Mode(kind="fix"))
        august = _lines(session, workspace, changed)[("№ОБО/100", "2026-08")]
        assert august.verified is None and august.stale_side == "kort"


def test_dvazhdy_den_i_den_perioda_kort(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        twice = _make(session, space, customer="ТОО Бетаком", amount="150000", signed_at="2025-01-30", number="№B")
        day = _make(session, space, customer="ТОО Гаммаком", amount="150000", signed_at="2025-07-31", number="№C")
        # №B: август начат 29.08, хотя июль идёт по 29.08 - день посчитан дважды.
        b = _chain("№B", "Бетаком", [date(2026, 6, 30), date(2026, 7, 30), date(2026, 8, 29), date(2026, 9, 29)])
        b[1][8] = "29.08.2026"
        # №C: подписан 31-го, а таблица считает с 30-го - без разрывов, своим днём.
        c = _chain("№C", "Гаммаком", [date(2026, 6, 30), date(2026, 7, 30), date(2026, 8, 30), date(2026, 9, 30)])
        book = _book(b + c)

        lines = _lines(session, workspace, book)
        assert lines[("№B", "2026-08")].slip == "overlap"
        assert lines[("№B", "2026-08")].hint == "таблица считает 29.08 дважды: июль по 29.08, август с 29.08"
        assert lines[("№B", "2026-09")].hint == "таблица идёт со сдвигом с 29.08, посчитанного дважды"
        assert [month for (number, month), line in lines.items() if number == "№C" and line.codes] == ["2026-07", "2026-08"]
        assert lines[("№C", "2026-07")].slip == ""
        assert lines[("№C", "2026-07")].hint == "у KORT день периода 31 - по началу начислений 31.07.2025"

        # Предложение «день периода 30» держится, пока расходящиеся месяцы не решены; совпавшие решать нечего.
        proposals = lambda: [p.kind for p in shadow.reconcile(session, workspace, FULL, book).proposals() if p.contract_id == day.id]  # noqa: E731
        assert proposals() == ["period_day"]
        changes.verify_month(session, workspace, FULL, OWNER, book, day.id, "2026-07")
        assert proposals() == ["period_day"]
        changes.verify_month(session, workspace, FULL, OWNER, book, day.id, "2026-08")
        assert proposals() == []
        assert shadow.reconcile(session, workspace, FULL, book).contracts[day.id].clean
        # Пачка берёт только ошибки периода таблицы: у №C их нет.
        assert changes.verify_slips(session, workspace, FULL, OWNER, book)["done"] == 2

        # Лента месяцев карточки: решение и подсказка у месяца.
        ribbon = shadow.contract_months(session, workspace, FULL, twice, book, money=True, on=date(2026, 10, 25))
        august = next(row for row in ribbon["months"] if row["month"] == "2026-08")
        assert august["verified"] is not None and august["hint"].startswith("таблица считает 29.08 дважды")
