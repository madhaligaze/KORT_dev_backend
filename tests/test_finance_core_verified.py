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

import company_layer


@pytest.fixture(autouse=True)
def _company_layer(monkeypatch):
    """Компания теста - с шапкой таблиц копии прода: её знает словарь компании, а не ядро (п.9 «Пакеты»)."""
    company_layer.everywhere(monkeypatch)


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


def test_status_protiv_tablicy(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        # Как №RENT/74: в реестре «Недействующий» без даты окончания, а таблица начисляет по октябрь.
        rent = _make(session, space, customer="ТОО Арендаком", amount="50000", signed_at="2025-03-01", number="№RENT/74",
                     status="Недействующий")
        # Как №ЮО/145: разовый «Не состоялся», а в таблице начисление за сентябрь.
        once = _make(session, space, customer="ТОО Юрком", amount="340000", signed_at="2026-09-30", number="№ЮО/145",
                     type="Разовая услуга", status="Не состоялся")
        rows = _chain("№RENT/74", "Арендаком", [date(2026, month, 1) for month in (6, 7, 8, 9, 10)], {m: "50 000" for m in range(6, 11)})
        rows.append(_row(9, "№ЮО/145", "Юрком", "340 000", date(2026, 9, 30), date(2026, 10, 29)))
        book = _book(rows)

        result = shadow.reconcile(session, workspace, FULL, book)
        found = {(result.contracts[p.contract_id].contract.number, p.kind): p for p in result.proposals()}
        assert set(found) == {("№RENT/74", "status"), ("№ЮО/145", "status")}
        rent_status = found[("№RENT/74", "status")]
        assert rent_status.text == "статус «Недействующий» - KORT не начисляет, а таблица начисляет: июнь - октябрь"
        assert rent_status.options == ["active"] and rent_status.labels == {"active": "Статус «Действующий»"}
        once_status = found[("№ЮО/145", "status")]
        assert once_status.options == ["in_progress", "fulfilled"]
        assert once_status.labels == {"in_progress": "Статус «На исполнении»", "fulfilled": "Статус «Исполнен»"}
        lines = _lines(session, workspace, book)
        assert lines[("№RENT/74", "2026-07")].hint == "у KORT не начисляется: договор закончился, а дата окончания не указана"
        assert lines[("№ЮО/145", "2026-09")].hint == "у KORT не начисляется: договор не состоялся"

        # Договор действует - статус исправлен, месяцы сходятся.
        text = changes.apply_proposal(session, workspace, FULL, OWNER, rent.id, "status", rent_status.value, option="active")
        assert text == "№RENT/74: статус «Действующий»"
        # Разовый не состоялся, ошиблась таблица - «Верно у KORT» вместо предложения.
        result = changes.verify_proposal(session, workspace, FULL, OWNER, book, once.id, once_status.months)
        assert result["text"] == "№ЮО/145 · сентябрь 2026: верно у KORT" and result["done"] == 1 and result["batch"]
        with pytest.raises(changes.ChangeError, match="уже нет"):
            changes.verify_proposal(session, workspace, FULL, OWNER, book, once.id, once_status.months)
        after = shadow.reconcile(session, workspace, FULL, book)
        assert list(after.proposals()) == []
        assert after.contracts[rent.id].clean and after.contracts[once.id].clean


def test_v_tablice_net_stroki(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        _make(session, space, customer="ТОО Новаком", amount="50000", signed_at="2026-10-21", number="№RENT/113")
        _make(session, space, customer="ТОО Позднеком", amount="50000", signed_at="2026-09-10", number="№RENT/90")
        later = _make(session, space, customer="ТОО Сдвигком", amount="50000", signed_at="2026-09-10", number="№RENT/91")
        late = _row(10, "№RENT/90", "Позднеком", "50 000", date(2026, 10, 10), date(2026, 11, 9))
        late[7] = late[8] = ""  # как в таблице BBC: у октябрьской строки нет периода
        rows = [late, _row(10, "№RENT/91", "Сдвигком", "50 000", date(2026, 10, 10), date(2026, 11, 9))]
        rows += _chain("№БезДоговора", "Прочеком", [date(2026, month, 1) for month in (6, 7, 8, 9, 10)])
        book = _book(rows)

        lines = _lines(session, workspace, book)
        assert lines[("№RENT/113", "2026-10")].hint == "в таблице у договора нет ни одной строки; у KORT начисления с 21.10.2026"
        assert lines[("№RENT/90", "2026-09")].hint == "в таблице договор с октября 2026; у KORT начисления с 10.09.2026"
        # С периодом в таблице - предложение начала, и сентябрь KORT - его месяц.
        start = [p for p in shadow.reconcile(session, workspace, FULL, book).proposals() if p.contract_id == later.id]
        assert [(p.kind, p.months) for p in start] == [("billing_from", ["2026-09", "2026-10"])]


def test_podskazka_do_nachala_ranshe_prichiny_statusa(space):
    with finance_session() as session:
        workspace = _ws(session, space)
        # Начат в августе, в реестре «Недействующий» без даты окончания; таблица ведёт его с июля.
        _make(session, space, customer="ТОО Гаммаком", amount="50000", signed_at="2026-08-01", number="№RENT/77", status="Недействующий")
        rows = _chain("№RENT/77", "Гаммаком", [date(2026, month, 1) for month in (7, 8, 9, 10)])
        rows += _chain("№БезДоговора", "Прочеком", [date(2026, month, 1) for month in (6, 7, 8, 9, 10)])
        lines = _lines(session, workspace, _book(rows))
        assert lines[("№RENT/77", "2026-07")].hint == "у KORT начисления с 01.08.2026"
        assert lines[("№RENT/77", "2026-08")].hint == "у KORT не начисляется: договор закончился, а дата окончания не указана"


def test_segodnya_schitaet_mesyacy_bez_predlozheniya(space, monkeypatch):
    from app.finance.core import today as core_today

    with finance_session() as session:
        workspace = _ws(session, space)
        _make(session, space, customer="ТОО Альфа", amount="150000", signed_at="2025-02-07", number="№ОБО/100")
        rows = _chain("№ОБО/100", "Альфа", [date(2026, 6, 7), date(2026, 7, 7), date(2026, 8, 8), date(2026, 9, 8), date(2026, 10, 8)])
        rows[1][8] = "06.08.2026"
        book = _book(rows)
        monkeypatch.setattr(book_module, "current", lambda *args, **kwargs: (book, ""))
        # Ошибка периода таблицы: предложений нет, а три месяца расходятся - три дела, «совпадают» не пишется.
        card = core_today.shadow_card(session, workspace, FULL, today=date(2026, 10, 26))
        alone = next(line for line in card["lines"] if line["label"] == "месяцев расходится без предложения")
        assert card["count"] == 3 and alone["count"] == 3 and not card.get("empty")
        assert alone["go"] == {"core.shadow.view": "lines", "core.shadow.code": "alone"}
        changes.verify_slips(session, workspace, FULL, OWNER, book)
        card = core_today.shadow_card(session, workspace, FULL, today=date(2026, 10, 26))
        assert card["count"] == 0 and card["empty"] == "Начисления KORT и таблицы совпадают"
