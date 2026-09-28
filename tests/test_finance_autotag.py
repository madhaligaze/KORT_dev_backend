"""Авторазметка: что по тексту ясно наверняка — размечается, остальное нет.

Ловушки взяты из прогона таксономии «Анализа выписок» по настоящей выписке
Kaspi Gold 21 сентября 2026: там подстроки давали уверенные и неверные статьи.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.finance import autotag, history, service
from app.finance.autotag import classify
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


@pytest.mark.parametrize(
    ("comment", "kind", "expected"),
    [
        # подстроки, которые таксономия «Анализа выписок» читала неверно
        ("Перевод · Аяна Ж.", "expense", "Переводы людям"),
        ("Перевод · Нургазы А.", "expense", "Переводы людям"),
        ("Покупка · Магазин Окей", "expense", "Продукты"),
        ("Покупка · ИП НАЗАРАЛИЕВА", "expense", "Покупки без уточнения"),
        ("Покупка · ТОО \"АЛИЕВ-АРИФ\"", "expense", "Покупки без уточнения"),
        ("Покупка · Кафедра физики", "expense", "Покупки без уточнения"),
        ("Покупка · ИП Boltaev", "expense", "Покупки без уточнения"),
        # продавцы, которые уходили в безликое «Покупки»
        ("Покупка · TTP*ANYTIME.KZ", "expense", "Такси и каршеринг"),
        ("Покупка · YANDEX.GO", "expense", "Такси и каршеринг"),
        ("Покупка · Bolt Food", "expense", "Доставка еды"),
        ("Покупка · ONAY. Пополнение баланса", "expense", "Общественный транспорт"),
        # тип операции банка главнее текста
        ("Снятие · Банкомат Royal Petrol", "expense", "Снятие наличных"),
        ("Покупка · Anytime.kz", "income", "Возвраты покупок"),
        ("Пополнение · В Kaspi Банкомате", "income", "Внесение наличных"),
        ("Пополнение · Əділет О.", "income", "Переводы от людей"),
        ("Перевод · Fotima Abdubakirovna T.", "expense", "Переводы людям"),
        # юр. счёт: назначение платежа одной строкой
        ("Перечисление ИПН за август", "expense", "Налоги и сборы"),
        ("Заработная плата за сентябрь", "expense", "Зарплата"),
        ("Разное · Комиссия за перевод на карту др. банка", "expense", "Комиссии банка"),
    ],
)
def test_statya_po_tekstu(comment, kind, expected):
    verdict = classify(comment, kind)
    assert verdict is not None and verdict.category == expected


@pytest.mark.parametrize(
    ("comment", "kind"),
    [
        ("Оплата за продукцию по счёту 15", "expense"),  # не «Продукты»
        ("Возврат налога", "income"),  # налог на поступлении — не угадываем
        ("Kaspi Business · оплата", "expense"),  # «bus» — не автобус
        ("Перевод между своими счетами", "transfer"),
    ],
)
def test_neyasno_ne_ugadyvaem(comment, kind):
    assert classify(comment, kind) is None


def _op(session, space, comment, kind="expense", category_id=None):
    cash = next(a for a in service.list_accounts(session, space.id) if a.name == "Касса")
    return service.create_operation(
        session,
        space,
        service.OperationInput(
            kind=kind,
            paid_at=date(2026, 9, 3),
            amount=Decimal("1000"),
            account_from_id=cash.id if kind == "expense" else None,
            account_to_id=cash.id if kind == "income" else None,
            category_id=category_id,
            comment=comment,
        ),
    )


def test_razmetka_i_otmena_celikom(finance_db):
    with finance_session() as session:
        space = service.ensure_workspace(session)
        taxi = _op(session, space, "Покупка · YANDEX.GO")
        _op(session, space, "Покупка · TTP*ANYTIME.KZ")
        vague = _op(session, space, "Покупка · ДиА")
        rent = service.ensure_category(session, space.id, "expense", "Аренда")
        already = _op(session, space, "Покупка · Magnum", category_id=rent.id)

        seen = autotag.preview(session, space)
        groups = {item["category"]: item for item in seen["groups"]}
        assert groups["Такси и каршеринг"]["count"] == 2
        assert groups["Покупки без уточнения"]["broad"] is True
        # размеченное не пересматривается
        assert "Продукты" not in groups

        done = autotag.apply(session, space, [("expense", "Такси и каршеринг")])
        assert done["updated"] == 2
        # новая статья расходов — операционная, это обычная трата
        taxi_category = session.get(type(rent), session.get(type(taxi), taxi.id).category_id)
        assert taxi_category.nature == "operating"
        assert session.get(type(vague), vague.id).category_id is None
        assert session.get(type(already), already.id).category_id == rent.id

        entry = history.write(
            session, space, kind="autotag.apply", entity="operations", after={"items": done["items"]}
        )
        # человек поправил одну строку после разметки — отмена её не трогает
        service.update_operation(session, space, taxi.id, {"category_id": rent.id})
        undone = history.undo(session, space, entry.id)
        assert undone["cleared"] == 1
        assert session.get(type(taxi), taxi.id).category_id == rent.id
        listed = history.listing(session, space.id)
        assert listed[0]["title"].startswith("отменено")
        assert all("items" not in item["after"] for item in listed)


def test_postupleniya_razmetki_ne_vyruchka(finance_db):
    """Внесение наличных и переводы от людей — не выручка в «Показателях»."""
    from app.finance import reports

    with finance_session() as session:
        space = service.ensure_workspace(session)
        _op(session, space, "Пополнение · В Kaspi Банкомате", kind="income")
        _op(session, space, "Пополнение · Ергалий С.", kind="income")
        autotag.apply(session, space, [("income", "Внесение наличных"), ("income", "Переводы от людей")])
        natures = {c.name: c.nature for c in service.list_categories(session, space.id)}
        assert natures["Внесение наличных"] == "capital"
        assert natures["Переводы от людей"] == "other"
        totals = reports._by_nature(session, space.id, date(2026, 9, 1), date(2026, 9, 30))
        assert totals.get("revenue", Decimal("0")) == Decimal("0")
