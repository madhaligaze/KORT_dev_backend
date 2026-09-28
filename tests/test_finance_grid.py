"""Табличный вид журнала: поверхностей две, поведение одно.

Главное свойство набора — правка ячейки обязана вести себя ровно так же, как
форма, и разбирать значения теми же функциями, что импорт. Иначе «1 500,50» в
таблице однажды станет числом 150050, а в файле останется 1500.50, и объяснить
расхождение будет нечем.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.finance import grid, service
from app.finance.db import finance_session
from app.finance.service import FinanceError, VersionConflict


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
def one_income(finance_db):
    """Одно поступление в кассу — то, что дальше правят ячейками."""
    with finance_session() as session:
        space = service.ensure_workspace(session)
        cash = next(a for a in service.list_accounts(session, space.id) if a.name == "Касса")
        revenue = service.ensure_category(session, space.id, "income", "Выручка")
        operation = service.create_operation(
            session,
            space,
            service.OperationInput(
                kind="income",
                paid_at=date(2026, 9, 3),
                amount=Decimal("100000"),
                account_to_id=cash.id,
                category_id=revenue.id,
                comment="первая",
            ),
        )
        return space.id, operation.id


def test_list_pokazyvaet_operatsii_i_spravochniki(one_income):
    space_id, _op = one_income
    with finance_session() as session:
        space = service.ensure_workspace(session)
        operations, _total = service.list_operations(session, space_id, limit=100)
        payload = grid.build_grid(session, space, operations)

    assert [column["key"] for column in payload["columns"]][:3] == ["paid_at", "kind_label", "amount"]
    row = payload["rows"][0]
    assert row["cells"]["kind_label"] == "Поступление"
    assert row["cells"]["account_to"] == "Касса"
    assert "Касса" in payload["options"]["accounts"]


def test_summa_v_yacheike_razbiraetsya_kak_v_faile(one_income):
    """«1 500,50» — это тысяча пятьсот, а не сто пятьдесят тысяч."""
    space_id, operation_id = one_income
    with finance_session() as session:
        space = service.ensure_workspace(session)
        operation = grid.apply_cell(session, space, operation_id, "amount", "1 500,50")
        # Значения снимаются внутри сессии: после выхода из блока объект
        # отвязан от неё, и обращение к полю уже не читается из базы.
        amount, amount_base = operation.amount, operation.amount_base
    assert amount == Decimal("1500.50")
    assert amount_base == Decimal("1500.50")


def test_data_v_yacheike_prinimaet_privychnuyu_zapis(one_income):
    space_id, operation_id = one_income
    with finance_session() as session:
        space = service.ensure_workspace(session)
        operation = grid.apply_cell(session, space, operation_id, "paid_at", "05.10.2026")
        paid_at = operation.paid_at
    assert paid_at == date(2026, 10, 5)


def test_neponyatnoe_znachenie_eto_otkaz_a_ne_nol(one_income):
    """Нераспознанная сумма не превращается в ноль: ноль выглядит как данные."""
    space_id, operation_id = one_income
    with finance_session() as session:
        space = service.ensure_workspace(session)
        with pytest.raises(FinanceError) as exc:
            grid.apply_cell(session, space, operation_id, "amount", "примерно сто тысяч")
    assert "Не понял сумму" in str(exc.value)

    with finance_session() as session:
        operations, _ = service.list_operations(session, space_id, limit=10)
        amount = operations[0].amount
    assert amount == Decimal("100000")


def test_minus_v_yacheike_ne_menyaet_vid_operatsii(one_income):
    """Смена вида — не правка ячейки: от вида зависят обязательные поля."""
    space_id, operation_id = one_income
    with finance_session() as session:
        space = service.ensure_workspace(session)
        with pytest.raises(FinanceError) as exc:
            grid.apply_cell(session, space, operation_id, "amount", "-5000")
    assert "вид операции меняется в карточке" in str(exc.value)


def test_vid_operatsii_yacheikoy_ne_pravitsya(one_income):
    space_id, operation_id = one_income
    with finance_session() as session:
        space = service.ensure_workspace(session)
        with pytest.raises(FinanceError):
            grid.apply_cell(session, space, operation_id, "kind_label", "Списание")


def test_neizvestnyy_schet_v_yacheike_otkaz(one_income):
    """Счёт не создаётся ни импортом, ни таблицей."""
    space_id, operation_id = one_income
    with finance_session() as session:
        space = service.ensure_workspace(session)
        with pytest.raises(FinanceError) as exc:
            grid.apply_cell(session, space, operation_id, "account_to", "Kaspi Gold")
    text = str(exc.value)
    # Отказ называет, что есть, — иначе за названием счёта шли в справочник.
    assert "Kaspi Gold" in text and "Касса" in text and "Справочник" in text


def test_schet_po_nachalu_nazvaniya_esli_on_odin(one_income):
    """«Кас» + Enter — это «Касса»: выпадающий список Univer не дополняет набор."""
    space_id, operation_id = one_income
    with finance_session() as session:
        space = service.ensure_workspace(session)
        grid.apply_cell(session, space, operation_id, "account_to", "Банк")
        row = grid.row_of(session, space, session.get(grid.Operation, operation_id))
    assert row["cells"]["account_to"] == "Банковский счёт"


def test_neodnoznachnoe_nachalo_scheta_otkaz(one_income):
    """Два кандидата — не угадываем, а отказываем."""
    space_id, operation_id = one_income
    with finance_session() as session:
        space = service.ensure_workspace(session)
        service.create_account(session, space, name="Касса офис")
        with pytest.raises(FinanceError):
            grid.apply_cell(session, space, operation_id, "account_to", "Кас")


def test_novaya_stroka_minus_pri_postuplenii_otkaz(one_income):
    """Минус при счёте в «На счёт» — не повод молча завести поступление."""
    with finance_session() as session:
        space = service.ensure_workspace(session)
        with pytest.raises(FinanceError) as exc:
            grid.append_row(
                session, space,
                {"paid_at": "05.09.2026", "amount": "-5000", "account_to": "Касса"},
            )
    assert "минус" in str(exc.value).lower()


def test_novaya_stroka_minus_pri_raskhode_prinyat(one_income):
    """Минус при счёте в «Со счёта» с видом согласен — это расход на 5 000."""
    with finance_session() as session:
        space = service.ensure_workspace(session)
        operation = grid.append_row(
            session, space,
            {"paid_at": "05.09.2026", "amount": "-5000", "account_from": "Касса"},
        )
        assert operation.kind == "expense"
        assert operation.amount == Decimal("5000")


def test_kategoriya_po_nachalu_ne_plodit_dvoynika(one_income):
    """«Арен» в новой строке — это существующая «Аренда», а не вторая статья."""
    with finance_session() as session:
        space = service.ensure_workspace(session)
        before = len(service.list_categories(session, space.id))
        operation = grid.append_row(
            session, space,
            {"paid_at": "05.09.2026", "amount": "1000", "account_from": "Касса", "category": "Арен"},
        )
        category = session.get(grid.Category, operation.category_id)
        assert category.name == "Аренда"
        assert len(service.list_categories(session, space.id)) == before


def test_kategoriya_v_yacheike_zavoditsya_srazu(one_income):
    """Категория, наоборот, появляется в работе постоянно — создаём на месте."""
    space_id, operation_id = one_income
    with finance_session() as session:
        space = service.ensure_workspace(session)
        grid.apply_cell(session, space, operation_id, "category", "Новая статья")
        names = [c.name for c in service.list_categories(session, space_id, "income")]
    assert "Новая статья" in names


def test_pravka_po_ustarevshey_versii_otklonyaetsya(one_income):
    """Двое правят одну строку — второй получает отказ, а не тихую перезапись."""
    space_id, operation_id = one_income
    with finance_session() as session:
        space = service.ensure_workspace(session)
        grid.apply_cell(session, space, operation_id, "comment", "правка первого", version=1)
    with finance_session() as session:
        space = service.ensure_workspace(session)
        with pytest.raises(VersionConflict):
            grid.apply_cell(session, space, operation_id, "comment", "правка второго", version=1)


def test_novaya_stroka_snizu_stanovitsya_operatsiey(finance_db):
    """Заполнили строку внизу листа — завелась операция.

    Вид определяется по заполненным счетам, как в кассовой книге: сумма в графе
    «приход» и есть приход.
    """
    with finance_session() as session:
        space = service.ensure_workspace(session)
        operation = grid.append_row(
            session,
            space,
            {
                "paid_at": "03.09.2026",
                "amount": "250 000",
                "account_to": "Касса",
                "category": "Выручка",
                "counterparty": "Клиент Бета",
                "comment": "продажа",
            },
        )
        kind, amount, source = operation.kind, operation.amount, operation.source
    assert kind == "income"
    assert amount == Decimal("250000")
    assert source == "grid"


def test_stroka_s_dvumya_schetami_eto_perevod(finance_db):
    with finance_session() as session:
        space = service.ensure_workspace(session)
        operation = grid.append_row(
            session,
            space,
            {"paid_at": "04.09.2026", "amount": "50000", "account_from": "Касса", "account_to": "Банковский счёт"},
        )
        kind, category_id = operation.kind, operation.category_id
    assert kind == "transfer"
    assert category_id is None


def test_stroka_s_datoy_v_budushchem_eto_plan(finance_db):
    """Платёж с датой вперёд — ожидание, а не факт."""
    future = (date.today() + timedelta(days=30)).strftime("%d.%m.%Y")
    with finance_session() as session:
        space = service.ensure_workspace(session)
        operation = grid.append_row(
            session, space, {"paid_at": future, "amount": "10000", "account_to": "Касса"}
        )
        status = operation.status
    assert status == "plan"


def test_stroka_bez_scheta_ne_stanovitsya_operatsiey(finance_db):
    with finance_session() as session:
        space = service.ensure_workspace(session)
        with pytest.raises(FinanceError) as exc:
            grid.append_row(session, space, {"paid_at": "04.09.2026", "amount": "1000"})
    assert "приход это или расход" in str(exc.value)
