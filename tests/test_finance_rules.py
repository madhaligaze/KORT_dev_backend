"""Автоправила и чтение банковских выписок.

Два куска одной задачи: выписка приносит две тысячи строк «Покупка · Magnum»,
правила раскладывают их по статьям. Без второго первое бесполезно — отчёт
покажет один столбец «Без категории».
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.finance import rules as rules_module, service, statements
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
def workspace_id(finance_db):
    with finance_session() as session:
        return service.ensure_workspace(session).id


def make_rule(workspace_id, **kwargs):
    with finance_session() as session:
        rule = rules_module.create_rule(
            session,
            workspace_id,
            name=kwargs.pop("name", "Правило"),
            conditions=kwargs.pop("conditions"),
            actions=kwargs.pop("actions"),
            match=kwargs.pop("match", "all"),
        )
        return rule.id


# ── Сами правила ─────────────────────────────────────────────────────────────


def test_pravilo_razmechaet_po_slovu_v_kommentarii(workspace_id) -> None:
    make_rule(
        workspace_id,
        name="Magnum",
        conditions=[{"field": "comment", "op": "contains", "value": "Magnum"}],
        actions={"category": "Продукты"},
    )
    rows = [
        {"comment": "Покупка · Magnum Cash&Carry", "amount": "3200"},
        {"comment": "Покупка · Аптека", "amount": "1500"},
    ]
    with finance_session() as session:
        hits = rules_module.preview_rows(rules_module.list_rules(session, workspace_id), rows)
    assert rows[0]["category"] == "Продукты"
    assert "category" not in rows[1]
    assert hits == {"Magnum": 1}


def test_pravilo_ne_perepisyvaet_uzhe_zapolnennoe(workspace_id) -> None:
    """Разметка из файла важнее нашей догадки: её поставил человек или банк."""
    make_rule(
        workspace_id,
        conditions=[{"field": "comment", "op": "contains", "value": "Magnum"}],
        actions={"category": "Продукты"},
    )
    rows = [{"comment": "Покупка · Magnum", "category": "Представительские"}]
    with finance_session() as session:
        rules_module.preview_rows(rules_module.list_rules(session, workspace_id), rows)
    assert rows[0]["category"] == "Представительские"


def test_pervoe_srabotavshee_pravilo_pobezhdaet(workspace_id) -> None:
    """Порядок задают правила, а не порядок строк в таблице."""
    make_rule(
        workspace_id,
        name="Первое",
        conditions=[{"field": "comment", "op": "contains", "value": "такси"}],
        actions={"category": "Транспорт"},
    )
    make_rule(
        workspace_id,
        name="Второе",
        conditions=[{"field": "comment", "op": "contains", "value": "яндекс"}],
        actions={"category": "Прочее"},
    )
    rows = [{"comment": "Покупка · Яндекс Такси"}]
    with finance_session() as session:
        hits = rules_module.preview_rows(rules_module.list_rules(session, workspace_id), rows)
    assert rows[0]["category"] == "Транспорт"
    assert hits == {"Первое": 1}


def test_uslovija_soedinyayutsya_i_ili(workspace_id) -> None:
    make_rule(
        workspace_id,
        name="Крупные покупки",
        match="all",
        conditions=[
            {"field": "comment", "op": "contains", "value": "покупка"},
            {"field": "amount", "op": "gt", "value": "50000"},
        ],
        actions={"category": "Крупное"},
    )
    small = {"comment": "Покупка · Магазин", "amount": "1000"}
    big = {"comment": "Покупка · Техника", "amount": "150000"}
    with finance_session() as session:
        active = rules_module.list_rules(session, workspace_id)
        rules_module.preview_rows(active, [small, big])
    assert "category" not in small
    assert big["category"] == "Крупное"


def test_schet_beretsya_iz_lyuboy_iz_dvuh_kolonok(workspace_id) -> None:
    """Правило спрашивает «какой счёт», а не «какая колонка заполнена»."""
    make_rule(
        workspace_id,
        conditions=[{"field": "account", "op": "equals", "value": "Kaspi Gold"}],
        actions={"project": "Личное"},
    )
    income = {"account_to": "Kaspi Gold", "comment": "перевод"}
    expense = {"account_from": "Kaspi Gold", "comment": "покупка"}
    with finance_session() as session:
        active = rules_module.list_rules(session, workspace_id)
        rules_module.preview_rows(active, [income, expense])
    assert income["project"] == "Личное" and expense["project"] == "Личное"


def test_slomannoe_vyrazhenie_ne_ronyaet_import(workspace_id) -> None:
    make_rule(
        workspace_id,
        conditions=[{"field": "comment", "op": "regex", "value": "(((("}],
        actions={"category": "Что-то"},
    )
    rows = [{"comment": "Покупка"}]
    with finance_session() as session:
        rules_module.preview_rows(rules_module.list_rules(session, workspace_id), rows)
    assert "category" not in rows[0]


def test_pravilo_bez_deystviya_ne_zavoditsya(workspace_id) -> None:
    with finance_session() as session:
        with pytest.raises(rules_module.RuleError):
            rules_module.create_rule(
                session,
                workspace_id,
                name="Пустое",
                conditions=[{"field": "comment", "op": "contains", "value": "x"}],
                actions={},
            )


# ── Применение к заведённым операциям ────────────────────────────────────────


def _operation(session, workspace, *, comment: str, amount: str = "1000"):
    cash = next(a for a in service.list_accounts(session, workspace.id) if a.name == "Касса")
    return service.create_operation(
        session,
        workspace,
        service.OperationInput(
            kind="expense",
            paid_at=date(2026, 9, 10),
            amount=Decimal(amount),
            account_from_id=cash.id,
            comment=comment,
        ),
    )


def test_pravila_razmechayut_uzhe_zavedennye_operatsii(workspace_id) -> None:
    """Случай «загрузили выписку за год, потом придумали правило»."""
    with finance_session() as session:
        workspace = service.get_workspace(session, workspace_id)
        _operation(session, workspace, comment="Покупка · Magnum Cash&Carry")
        _operation(session, workspace, comment="Покупка · Magnum Sofia")
        _operation(session, workspace, comment="Покупка · Аптека")

    make_rule(
        workspace_id,
        name="Magnum",
        conditions=[{"field": "comment", "op": "contains", "value": "Magnum"}],
        actions={"category": "Продукты"},
    )
    with finance_session() as session:
        result = rules_module.apply_to_operations(session, workspace_id)
    assert result["updated"] == 2
    assert result["by_rule"] == {"Magnum": 2}

    with finance_session() as session:
        operations, _total = service.list_operations(session, workspace_id, limit=10)
        categories = sorted(
            (op.category_id is not None) for op in operations
        )
    assert categories.count(True) == 2


def test_povtornoe_primenenie_ne_menyaet_razmechennoe(workspace_id) -> None:
    """Переразметка задним числом меняла бы отчёты, которые уже показали."""
    with finance_session() as session:
        workspace = service.get_workspace(session, workspace_id)
        _operation(session, workspace, comment="Покупка · Magnum")

    make_rule(
        workspace_id,
        name="Первое",
        conditions=[{"field": "comment", "op": "contains", "value": "Magnum"}],
        actions={"category": "Продукты"},
    )
    with finance_session() as session:
        rules_module.apply_to_operations(session, workspace_id)

    make_rule(
        workspace_id,
        name="Второе",
        conditions=[{"field": "comment", "op": "contains", "value": "Magnum"}],
        actions={"category": "Совсем другое"},
    )
    with finance_session() as session:
        again = rules_module.apply_to_operations(session, workspace_id)
    assert again["updated"] == 0


def test_podskazki_nazyvayut_mesto_tselikom(workspace_id) -> None:
    """С чего начать разметку: частотность, и она себя не выдаёт за разум.

    Подсказка называет место целиком («Magnum Cash&Carry»), а не одно слово из
    комментария. Первая версия брала самое длинное слово и на «Пополнение · С
    карты другого банка» предлагала правило по слову «другого» — правило по
    случайному слову ловит что попало.
    """
    with finance_session() as session:
        workspace = service.get_workspace(session, workspace_id)
        for _ in range(4):
            _operation(session, workspace, comment="Покупка · Magnum Cash&Carry", amount="3000")
        for _ in range(2):
            _operation(session, workspace, comment="Покупка · Аптека Европейская", amount="1500")
        _operation(session, workspace, comment="Покупка · Разовая лавка", amount="100")

    with finance_session() as session:
        found = rules_module.suggest(session, workspace_id, min_count=2)
    keywords = [item["keyword"] for item in found]
    assert "Magnum Cash&Carry" in keywords, keywords
    assert all(item["count"] >= 2 for item in found), "предложения из одной операции — шум"


# ── Перевод банковской выписки в строки импорта ──────────────────────────────


def _fake_statement(monkeypatch, transactions):
    """Подделываем разбор: свой перевод проверяем, чужой разбор — не наш код."""
    statement = SimpleNamespace(
        metadata=SimpleNamespace(parser_key="kaspi_gold_statement"),
        transactions=transactions,
    )
    matches = [SimpleNamespace(key="kaspi_gold_statement", label="Kaspi Gold", score=1.0)]
    import app.services.document_service as document_service

    monkeypatch.setattr(
        document_service, "parse_statement_with_diagnostics", lambda *a, **k: (statement, matches)
    )


def _tx(**kwargs):
    base = dict(
        date="17.09.26",
        amount=-1850.0,
        direction="outflow",
        operation="Покупка",
        detail='ТОО "АЛИЕВ-АРИФ"',
        currency_op=None,
        raw_counterparty=None,
        document_number=None,
        category=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_vypiska_prevrashchaetsya_v_stroki_so_znakom_i_schetom(monkeypatch) -> None:
    _fake_statement(
        monkeypatch,
        [
            _tx(),
            _tx(amount=250000.0, direction="inflow", operation="Пополнение", detail="Зарплата"),
        ],
    )
    parsed = statements.read_statement(b"%PDF-", "vypiska.pdf", account="Kaspi Gold")
    first, second = parsed["rows"]
    assert first["values"]["kind"] == "expense"
    assert first["values"]["amount"] == "1850.0"
    assert first["values"]["account_from"] == "Kaspi Gold"
    assert first["values"]["account_to"] is None
    # Комментарий склеен из операции и детали: «Покупка» без магазина не
    # говорит ничего, а по детали потом работают правила.
    assert first["values"]["comment"] == 'Покупка · ТОО "АЛИЕВ-АРИФ"'
    assert second["values"]["kind"] == "income"
    assert second["values"]["account_to"] == "Kaspi Gold"
    assert parsed["parser_key"] == "kaspi_gold_statement"


def test_dvuznachnyy_god_v_vypiske_chitaetsya(monkeypatch) -> None:
    _fake_statement(monkeypatch, [_tx(date="05.01.25")])
    parsed = statements.read_statement(b"%PDF-", "vypiska.pdf", account="Kaspi Gold")
    assert parsed["rows"][0]["values"]["paid_at"] == "2025-01-05"


def test_vypiska_bez_operatsiy_otkazyvaetsya_ponyatno(monkeypatch) -> None:
    _fake_statement(monkeypatch, [])
    with pytest.raises(statements.StatementError) as exc:
        statements.read_statement(b"%PDF-", "spravka.pdf", account="Kaspi Gold")
    assert "справка об остатке" in str(exc.value)


def test_bez_vybrannogo_scheta_import_sprashivaet_a_ne_ugadyvaet(monkeypatch, workspace_id) -> None:
    """В выписке счёта нет: файл сам и есть счёт. Спрашиваем один раз на файл."""
    from app.finance.importing import analyze

    _fake_statement(monkeypatch, [_tx()])
    preview = analyze(b"%PDF-", "vypiska.pdf", ["Касса", "Kaspi Gold"])
    assert preview.question is not None
    assert preview.question["kind"] == "account"
    assert [option["value"] for option in preview.question["options"]] == ["Касса", "Kaspi Gold"]
    assert preview.counts["ready"] == 0

    chosen = analyze(b"%PDF-", "vypiska.pdf", ["Касса", "Kaspi Gold"], default_account="Kaspi Gold")
    assert chosen.question is None
    assert chosen.counts["ready"] == 1


def test_neizvestnyy_schet_v_vybore_otkaz(monkeypatch) -> None:
    from app.finance.importing import ImportError_, analyze

    _fake_statement(monkeypatch, [_tx()])
    with pytest.raises(ImportError_) as exc:
        analyze(b"%PDF-", "vypiska.pdf", ["Касса"], default_account="Halyk")
    assert "Заведите его" in str(exc.value)
