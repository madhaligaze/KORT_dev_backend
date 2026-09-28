"""Отчёты раздела «Финансы»: что именно они обязаны различать.

Набор проверяет не «числа посчитались», а свойства, из-за которых
управленческий учёт вообще заводят:

* деньги и прибыль — разные отчёты, и разница между ними это дебиторка;
* перевод между своими счетами не создаёт ни дохода, ни расхода;
* месяц без операций стоит в отчёте нулём, а не пропадает из ряда;
* кассовый разрыв назван днём, а не «где-то в следующем месяце».
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.finance import reports, service
from app.finance.db import finance_session


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
def filled(finance_db):
    """Компания с тремя месяцами работы, переводом и отложенной оплатой."""
    with finance_session() as session:
        space = service.ensure_workspace(session)
        accounts = {a.name: a for a in service.list_accounts(session, space.id)}
        bank, cash = accounts["Банковский счёт"], accounts["Касса"]
        bank.starting_balance = Decimal("1000000")
        revenue = service.ensure_category(session, space.id, "income", "Выручка")
        rent = service.ensure_category(session, space.id, "expense", "Аренда")
        client = service.ensure_counterparty(session, space.id, "Клиент Альфа")
        north = service.ensure_project(session, space.id, "Проект Север")

        def income(day, amount, *, month=7, accrued=None, status="fact", project=None):
            service.create_operation(
                session,
                space,
                service.OperationInput(
                    kind="income",
                    status=status,
                    paid_at=date(2026, month, day),
                    accrued_at=accrued,
                    amount=Decimal(str(amount)),
                    account_to_id=bank.id,
                    category_id=revenue.id,
                    counterparty_id=client.id,
                    projects=[(project, Decimal(str(amount)))] if project else (),
                    comment=f"поступление {month}/{day}",
                ),
            )

        def expense(day, amount, *, month=7, status="fact"):
            service.create_operation(
                session,
                space,
                service.OperationInput(
                    kind="expense",
                    status=status,
                    paid_at=date(2026, month, day),
                    amount=Decimal(str(amount)),
                    account_from_id=bank.id,
                    category_id=rent.id,
                    comment=f"списание {month}/{day}",
                ),
            )

        income(5, 500000, month=7, project=north.id)
        expense(7, 200000, month=7)
        # Август: ни одной операции — месяц обязан остаться в отчёте нулём.
        income(3, 800000, month=9)
        expense(7, 300000, month=9)
        # Услуга оказана в сентябре, деньги ждём в октябре: в «Прибыли» это
        # сентябрь, в «Деньгах» — октябрь.
        income(20, 900000, month=10, accrued=date(2026, 9, 25), status="plan")
        # Перевод: деньги переехали, но не появились и не исчезли.
        service.create_operation(
            session,
            space,
            service.OperationInput(
                kind="transfer",
                paid_at=date(2026, 9, 10),
                amount=Decimal("100000"),
                account_from_id=bank.id,
                account_to_id=cash.id,
                comment="инкассация",
            ),
        )
        return space.id


def test_perevod_ne_dohod_i_ne_rashod(filled):
    """Перевод между своими счетами обязан свернуться в ноль."""
    with finance_session() as session:
        report = reports.cash_flow(session, filled, date(2026, 9, 1), date(2026, 9, 30))
    september = report["rows"][0]
    assert september["income"] == "800000.00"
    assert september["expense"] == "300000.00"
    # Общий остаток на конец месяца от перевода не меняется.
    assert Decimal(september["end_balance"]) == Decimal(september["start_balance"]) + Decimal("500000")


def test_mesyats_bez_operatsiy_stoit_nulyom(filled):
    """Пустой август остаётся в ряду. Пропуск читался бы как «данных нет»."""
    with finance_session() as session:
        report = reports.cash_flow(session, filled, date(2026, 7, 1), date(2026, 9, 30))
    months = [row["month"] for row in report["rows"]]
    assert months == ["2026-07", "2026-08", "2026-09"]
    august = report["rows"][1]
    assert august["income"] == "0" or Decimal(august["income"]) == 0
    # Остаток на конец пустого месяца равен остатку на его начало.
    assert Decimal(august["end_balance"]) == Decimal(august["start_balance"])


def test_dengi_i_pribyl_raznyye_otchety(filled):
    """Оплата в октябре, сделка в сентябре: два отчёта видят её в разных месяцах."""
    with finance_session() as session:
        cash = reports.cash_flow(session, filled, date(2026, 9, 1), date(2026, 10, 31))
        profit = reports.profit_and_loss(session, filled, date(2026, 9, 1), date(2026, 10, 31))

    cash_by_month = {row["month"]: row for row in cash["rows"]}
    profit_by_month = {row["month"]: row for row in profit["rows"]}

    # В деньгах сентябрь без этой оплаты, а ожидание стоит отдельной строкой.
    assert cash_by_month["2026-09"]["income"] == "800000.00"
    assert cash_by_month["2026-10"]["income_plan"] == "900000.00"
    # В прибыли она же учтена сентябрём — по дате сделки.
    assert profit_by_month["2026-09"]["income"] == "1700000.00"
    assert profit_by_month["2026-10"]["income"] == "0"


def test_debitorka_znaet_prosrochku(filled):
    """Просроченное ожидание названо просроченным, а не молча забыто."""
    with finance_session() as session:
        debts = reports.debts(session, filled, as_of=date(2026, 11, 1))
    assert debts["receivable"]["total"] == "900000.00"
    assert debts["receivable"]["overdue"] == "900000.00"
    assert debts["receivable"]["items"][0]["overdue_days"] == 12
    assert debts["receivable"]["by_counterparty"][0]["name"] == "Клиент Альфа"


def test_ostatki_po_schetam_uchityvayut_nachalnyy(filled):
    """Начальный остаток входит в остаток счёта, план — нет."""
    with finance_session() as session:
        balances = {item["name"]: item for item in reports.account_balances(session, filled)}
    bank = balances["Банковский счёт"]
    # 1 000 000 начальный + 500 000 + 800 000 − 200 000 − 300 000 − 100 000 перевод
    assert bank["balance"] == "1700000.00"
    assert bank["balance_with_plan"] == "2600000.00"
    assert balances["Касса"]["balance"] == "100000.00"


def test_kassovyy_razryv_nazvan_dnyom(finance_db):
    """День, в который остаток уходит ниже нуля, назван датой."""
    with finance_session() as session:
        space = service.ensure_workspace(session)
        bank = next(a for a in service.list_accounts(session, space.id) if a.name == "Банковский счёт")
        rent = service.ensure_category(session, space.id, "expense", "Аренда")
        service.create_operation(
            session,
            space,
            service.OperationInput(
                kind="expense",
                status="plan",
                paid_at=date(2026, 10, 12),
                amount=Decimal("450000"),
                account_from_id=bank.id,
                category_id=rent.id,
                comment="аренда, платить нечем",
            ),
        )
        month = reports.calendar(session, space.id, 2026, 10)

    assert month["cash_gaps"] == [f"2026-10-{day:02d}" for day in range(12, 32)]
    twelfth = next(day for day in month["days"] if day["date"] == "2026-10-12")
    assert twelfth["negative"] is True
    assert Decimal(twelfth["balance"]) == Decimal("-450000")


def test_kontrolnye_summy_otcheta(filled):
    """Отчёт обязан доказывать свою правоту, а не обещать её."""
    with finance_session() as session:
        report = reports.cash_flow(session, filled, date(2026, 7, 1), date(2026, 10, 31))
    assert all(check["ok"] for check in report["checks"]), report["checks"]


def test_proekty_pokazyvayut_nerazneseennoe(filled):
    """Сумма по проектам не сходится с прибылью — и это видно строкой."""
    with finance_session() as session:
        report = reports.projects_report(session, filled, date(2026, 7, 1), date(2026, 10, 31))
    north = next(item for item in report["items"] if item["name"] == "Проект Север")
    assert north["income"] == "500000.00"
    # Всё остальное не разнесено, и отчёт говорит об этом прямо.
    assert Decimal(report["not_split"]["income"]) == Decimal("1700000")
    assert Decimal(report["not_split"]["expense"]) == Decimal("500000")


def test_plan_fakt_bez_plana_ne_delit_na_nol(filled):
    """Процент выполнения при нулевом плане не считается вовсе."""
    with finance_session() as session:
        space_id = filled
        report = reports.plan_actual(session, space_id, date(2026, 9, 1), date(2026, 9, 30))
    revenue = next(item for item in report["items"] if item["name"] == "Выручка")
    september = revenue["cells"][0]
    assert september["fact"] == "800000.00"
    assert september["plan"] == "0"
    assert september["done_pct"] is None

def test_pervoe_otkrytie_razdela_dvumya_zaprosami_srazu(finance_db):
    """Гонка на создании пространства не роняет запрос.

    Раздел открывается двумя запросами сразу — сводка и справочники. Оба
    вызывают `ensure_workspace`, и до исправления второй падал на уникальном
    ключе `slug`: отвечал 500, а экран оставался пустым без объяснения.
    Поймано сквозным прогоном 18 сентября 2026.

    Гонка воспроизводится честно, но без потоков: первый поиск подменяется на
    «не нашёл», хотя пространство в базе уже есть. Ровно это и видит запрос,
    проигравший гонку.
    """
    import sqlalchemy as sa

    from app.finance.models import Workspace

    with finance_session() as session:
        first = service.ensure_workspace(session)
        first_id = first.id

    with finance_session() as session:
        real_scalar = session.scalar
        calls = {"n": 0}

        def blind_first(statement, *args, **kwargs):
            # Первый поиск пространства отвечает «нет», как будто сосед ещё не
            # зафиксировал вставку. Дальше всё честно.
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return real_scalar(statement, *args, **kwargs)

        session.scalar = blind_first  # type: ignore[method-assign]
        second = service.ensure_workspace(session)
        second_id = second.id
        session.scalar = real_scalar  # type: ignore[method-assign]
        total = session.scalar(sa.select(sa.func.count(Workspace.id)))

    assert second_id == first_id, "проигравший гонку обязан получить чужое пространство"
    assert total == 1, "второго пространства с тем же slug быть не должно"

def test_period_po_umolchaniyu_vklyuchaet_tekushchiy_mesyats(finance_db) -> None:
    """Отчёт без явных дат обязан показывать операции этого месяца.

    Дефект, найденный сквозным прогоном 18 сентября 2026: период по умолчанию
    заканчивался ПЕРВЫМ числом текущего месяца, и всё, что записано позже, в
    отчёты не попадало. Столбец месяца при этом стоял на месте с нулём —
    выглядело как «данных нет», хотя в журнале были одиннадцать операций.

    Проверяется через тот же `_period`, которым пользуются все маршруты
    отчётов: иначе тест проверял бы собственную копию правила.
    """
    from datetime import date as _date

    from app.api.routes.finance import _period

    today = _date.today()
    start, end = _period(None, None)
    assert start <= today <= end, f"сегодня {today} не попало в период {start}…{end}"

    with finance_session() as session:
        space = service.ensure_workspace(session)
        cash = next(a for a in service.list_accounts(session, space.id) if a.name == "Касса")
        revenue = service.ensure_category(session, space.id, "income", "Выручка")
        service.create_operation(
            session,
            space,
            service.OperationInput(
                kind="income",
                paid_at=today,
                amount=Decimal("123456"),
                account_to_id=cash.id,
                category_id=revenue.id,
                comment="операция сегодняшним днём",
            ),
        )
        report = reports.cash_flow(session, space.id, start, end)

    month = f"{today.year:04d}-{today.month:02d}"
    row = next(item for item in report["rows"] if item["month"] == month)
    assert Decimal(row["income"]) == Decimal("123456"), report["rows"]

def test_razbivka_ne_smeshivaet_fakt_i_ozhidanie(filled) -> None:
    """Итог разбивки — факт; ожидание стоит отдельным числом.

    Найдено глазами на живом экране 18 сентября 2026: в шапке отчёта стояло
    «Поступило за период 5 117 777», а в итоге разбивки по категориям —
    6 017 777. Обе цифры были верные (вторая включала запланированный платёж),
    но на экране разница ничем не объяснялась: два ответа на один вопрос.
    """
    with finance_session() as session:
        report = reports.cash_flow(session, filled, date(2026, 9, 1), date(2026, 10, 31))

    months_income = sum((Decimal(row["income"]) for row in report["rows"]), Decimal("0"))
    parts_income = sum((Decimal(item["total"]) for item in report["breakdown"]["income"]), Decimal("0"))
    assert months_income == parts_income, "итог разбивки обязан совпадать с таблицей месяцев"

    # Ожидание при этом не потеряно — оно просто названо своим именем.
    planned = sum((Decimal(item["planned"]) for item in report["breakdown"]["income"]), Decimal("0"))
    assert planned == Decimal("900000")
    assert all(check["ok"] for check in report["checks"]), report["checks"]
