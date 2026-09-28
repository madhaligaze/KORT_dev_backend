"""Счета-фактуры, повторения, история с отменой, дробление, баланс, показатели.

Набор проверяет не «функции работают», а то, ради чего они появились: долг
должен возникать в момент выставления счёта, повторение — не удваиваться при
повторном продлении, отмена — возвращать состояние, дробление — не терять и не
удваивать платёж, показатели — не показывать красивый ноль там, где данных нет.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.finance import history, integrations, invoices, recurring, reports, service
from app.finance.db import finance_session
from app.finance.models import Category, Operation


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    """Своя база на прогон — см. пояснение в test_finance_import.py."""
    from sqlalchemy import create_engine

    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    yield engine


@pytest.fixture
def space(finance_db):
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        return workspace.id


def _workspace(session, space_id):
    return service.get_workspace(session, space_id)


def _account(session, workspace, name="Касса"):
    for account in service.list_accounts(session, workspace.id):
        if account.name == name:
            return account
    return service.create_account(session, workspace, name=name, kind="cash")


# ── Счета-фактуры ───────────────────────────────────────────────────────────


def test_schet_srazu_stanovitsya_dolgom(space):
    """Дебиторка обязана появиться в момент выставления, а не когда вспомнят."""
    with finance_session() as session:
        workspace = _workspace(session, space)
        invoice = invoices.create(
            session,
            workspace,
            kind="out",
            issued_at=date(2026, 3, 1),
            due_at=date(2026, 3, 15),
            lines=[{"title": "Сопровождение", "quantity": 2, "price": "150000"}],
            vat_rate="12",
            comment="март",
            actor="тест",
        )
        assert str(invoice.amount_net) == "300000.00"
        assert str(invoice.vat_amount) == "36000.00"
        assert str(invoice.amount_gross) == "336000.00"
        assert invoice.operation_id is not None

        operation = session.get(Operation, invoice.operation_id)
        assert operation.status == "plan", "счёт должен создавать ожидание, а не факт"
        assert operation.paid_at == date(2026, 3, 15), "дата платежа ожидания — это срок счёта"
        assert operation.accrued_at == date(2026, 3, 1), "дата сделки — дата счёта"

        debt = reports.debts(session, workspace.id, as_of=date(2026, 3, 20))
        assert debt["receivable"]["total"] == "336000.00"
        assert Decimal(debt["receivable"]["overdue"]) > 0, "срок прошёл — значит просрочено"


def test_summa_schyota_schitaetsya_po_pozitsiyam(space):
    """Итог, набранный руками, однажды не совпадёт с позициями."""
    net, vat, gross = invoices.totals(
        [{"amount": "100000"}, {"amount": "23456.78"}], Decimal("12")
    )
    assert str(net) == "123456.78"
    assert str(vat) == "14814.81"
    assert str(gross) == "138271.59"


def test_otmena_schyota_snimaet_i_dolg(space):
    """Счёт, отменённый в одном месте и висящий долгом в другом, — расхождение."""
    with finance_session() as session:
        workspace = _workspace(session, space)
        invoice = invoices.create(
            session,
            workspace,
            kind="out",
            issued_at=date(2026, 4, 1),
            due_at=date(2026, 4, 10),
            lines=[{"title": "Услуга", "price": "50000"}],
            actor="тест",
        )
        invoices.void(session, workspace.id, invoice.id)
        debt = reports.debts(session, workspace.id, as_of=date(2026, 4, 20))
        assert debt["receivable"]["total"] == "0"


def test_oplachennyy_schyot_ne_otmenyaetsya_molcha(space):
    """Оплата — это факт движения денег, и он не отменяется отменой счёта."""
    with finance_session() as session:
        workspace = _workspace(session, space)
        account = _account(session, workspace)
        invoice = invoices.create(
            session,
            workspace,
            kind="out",
            issued_at=date(2026, 5, 1),
            due_at=date(2026, 5, 10),
            lines=[{"title": "Услуга", "price": "70000"}],
            account_id=account.id,
            actor="тест",
        )
        service.update_operation(
            session, workspace, invoice.operation_id, {"status": "fact"}, actor="тест"
        )
        with pytest.raises(service.FinanceError) as exc:
            invoices.void(session, workspace.id, invoice.id)
        assert "оплачен" in str(exc.value)


# ── Повторения ──────────────────────────────────────────────────────────────


def test_povtorenie_ne_dvoitsya_pri_povtornom_prodlenii(space):
    with finance_session() as session:
        workspace = _workspace(session, space)
        account = _account(session, workspace)
        recurring.create(
            session,
            workspace,
            title="Аренда",
            kind="expense",
            amount=Decimal("250000"),
            period="month",
            day=5,
            start_at=date.today().replace(day=1),
            account_from_id=account.id,
            actor="тест",
        )
        first = recurring.materialize(session, workspace)
        second = recurring.materialize(session, workspace)
        assert first > 0, "первое продление обязано создать ожидания"
        assert second == 0, "второе продление не должно создавать ничего"

        total = session.scalar(
            sa.select(sa.func.count())
            .select_from(Operation)
            .where(Operation.workspace_id == workspace.id, Operation.status == "plan")
        )
        assert total == first


def test_tridtsat_pervoe_v_fevrale_eto_konets_fevralya(space):
    """«Платим 31-го» означает конец месяца, а не первое число следующего."""
    assert recurring.first_date(date(2026, 1, 15), "month", 31) == date(2026, 1, 31)
    assert recurring._shift(date(2026, 1, 31), "month", 31) == date(2026, 2, 28)
    assert recurring._shift(date(2026, 2, 28), "month", 31) == date(2026, 3, 31)


def test_udalenie_povtoreniya_ne_trogaet_oplachennoe(space):
    with finance_session() as session:
        workspace = _workspace(session, space)
        account = _account(session, workspace)
        rule = recurring.create(
            session,
            workspace,
            title="Подписка",
            kind="expense",
            amount=Decimal("5000"),
            period="month",
            day=1,
            start_at=date.today() - timedelta(days=40),
            account_from_id=account.id,
            actor="тест",
        )
        recurring.materialize(session, workspace)
        paid = session.scalars(
            sa.select(Operation).where(
                Operation.recurrence_id == rule.id, Operation.paid_at < date.today()
            )
        ).first()
        if paid is not None:
            service.update_operation(session, workspace, paid.id, {"status": "fact"}, actor="тест")

        recurring.remove(session, workspace.id, rule.id)
        left = session.scalars(
            sa.select(Operation).where(
                Operation.workspace_id == workspace.id, Operation.deleted_at.is_(None)
            )
        ).all()
        assert all(item.status == "fact" or item.paid_at < date.today() for item in left)


# ── История и отмена ────────────────────────────────────────────────────────


def test_otmena_vozvrashchaet_sostoyanie_a_ne_delaet_obratnoe(space):
    """Обратная операция создала бы в журнале платёж, которого не было."""
    with finance_session() as session:
        workspace = _workspace(session, space)
        account = _account(session, workspace)
        operation = service.create_operation(
            session,
            workspace,
            service.OperationInput(
                kind="expense", paid_at=date(2026, 6, 1), amount=Decimal("10000"),
                account_from_id=account.id, comment="было"
            ),
            actor="тест",
        )
        before = history.snapshot(operation)
        service.update_operation(
            session, workspace, operation.id, {"amount": Decimal("99000"), "comment": "стало"}, actor="тест"
        )
        entry = history.write(
            session,
            workspace,
            kind="operation.update",
            entity="operation",
            entity_id=operation.id,
            before=before,
            after=history.snapshot(session.get(Operation, operation.id)),
            actor="тест",
        )

        history.undo(session, workspace, entry.id, actor="тест")
        restored = session.get(Operation, operation.id)
        assert Decimal(str(restored.amount)) == Decimal("10000")
        assert restored.comment == "было"
        assert Decimal(str(restored.amount_base)) == Decimal(
            str(restored.amount)
        ), "оценка в валюте компании пересчитана вместе с суммой"

        count = session.scalar(
            sa.select(sa.func.count())
            .select_from(Operation)
            .where(Operation.workspace_id == workspace.id, Operation.deleted_at.is_(None))
        )
        assert count == 1, "отмена не должна плодить операций"

        with pytest.raises(service.FinanceError) as exc:
            history.undo(session, workspace, entry.id, actor="тест")
        assert "уже отменено" in str(exc.value)


# ── Дробление по статьям ────────────────────────────────────────────────────


def test_droblenie_ne_teryaet_i_ne_udvaivaet_platyozh(space):
    with finance_session() as session:
        workspace = _workspace(session, space)
        account = _account(session, workspace)
        товар = service.ensure_category(session, workspace.id, "expense", "Товар")
        доставка = service.ensure_category(session, workspace.id, "expense", "Доставка")
        прочее = service.ensure_category(session, workspace.id, "expense", "Прочее")

        service.create_operation(
            session,
            workspace,
            service.OperationInput(
                kind="expense",
                paid_at=date(2026, 7, 10),
                amount=Decimal("100000"),
                account_from_id=account.id,
                category_id=прочее.id,
                categories=((товар.id, Decimal("70000")), (доставка.id, Decimal("20000"))),
                comment="поставка",
            ),
            actor="тест",
        )
        flow = reports.cash_flow(session, workspace.id, date(2026, 7, 1), date(2026, 7, 31))
        by_name = {row["name"]: Decimal(row["total"]) for row in flow["breakdown"]["expense"]}
        assert by_name["Товар"] == Decimal("70000.00")
        assert by_name["Доставка"] == Decimal("20000.00")
        assert by_name["Прочее"] == Decimal("10000.00"), "неразнесённый остаток остаётся на статье операции"
        assert sum(by_name.values()) == Decimal("100000.00"), "платёж не удвоился и не потерялся"


def test_chasti_bolshe_summy_ne_prinimayutsya(space):
    with finance_session() as session:
        workspace = _workspace(session, space)
        account = _account(session, workspace)
        one = service.ensure_category(session, workspace.id, "expense", "Раз")
        with pytest.raises(service.FinanceError) as exc:
            service.create_operation(
                session,
                workspace,
                service.OperationInput(
                    kind="expense",
                    paid_at=date(2026, 7, 10),
                    amount=Decimal("1000"),
                    account_from_id=account.id,
                    categories=((one.id, Decimal("1500")),),
                ),
                actor="тест",
            )
        assert "больше суммы" in str(exc.value)


# ── Показатели и баланс ─────────────────────────────────────────────────────


def test_pokazateli_schitayut_ebitda_po_prirode_statey(space):
    with finance_session() as session:
        workspace = _workspace(session, space)
        account = _account(session, workspace)

        def category(name: str, side: str, nature: str):
            entry = service.ensure_category(session, workspace.id, side, name)
            session.get(Category, entry.id).nature = nature
            session.flush()
            return entry

        выручка = category("Выручка", "income", "revenue")
        закуп = category("Закуп", "expense", "cogs")
        аренда = category("Аренда", "expense", "operating")
        проценты = category("Проценты", "expense", "financial")
        амортизация = category("Амортизация", "expense", "depreciation")

        def op(kind, amount, category_id):
            service.create_operation(
                session,
                workspace,
                service.OperationInput(
                    kind=kind,
                    paid_at=date(2026, 8, 10),
                    accrued_at=date(2026, 8, 10),
                    amount=Decimal(amount),
                    account_from_id=account.id if kind == "expense" else None,
                    account_to_id=account.id if kind == "income" else None,
                    category_id=category_id,
                ),
                actor="тест",
            )

        op("income", "1000000", выручка.id)
        op("expense", "400000", закуп.id)
        op("expense", "200000", аренда.id)
        op("expense", "50000", проценты.id)
        op("expense", "30000", амортизация.id)

        data = reports.indicators(session, workspace.id, date(2026, 8, 1), date(2026, 8, 31))
        assert Decimal(data["revenue"]) == Decimal("1000000.00")
        assert Decimal(data["gross_profit"]) == Decimal("600000.00")
        # EBITDA не включает ни проценты, ни амортизацию — в этом весь смысл.
        assert Decimal(data["ebitda"]) == Decimal("400000.00")
        assert Decimal(data["operating_profit"]) == Decimal("370000.00")
        assert Decimal(data["net_profit"]) == Decimal("320000.00")
        assert data["gross_margin"] == "60.0"


def test_balans_ne_podstavlyaet_nuli_vmesto_neizvestnogo(space):
    """Баланс с молчаливыми нулями сходится идеально и не значит ничего."""
    with finance_session() as session:
        workspace = _workspace(session, space)
        account = _account(session, workspace)
        service.create_operation(
            session,
            workspace,
            service.OperationInput(
                kind="income", paid_at=date(2026, 9, 1), amount=Decimal("500000"),
                account_to_id=account.id
            ),
            actor="тест",
        )
        invoices.create(
            session,
            workspace,
            kind="out",
            issued_at=date(2026, 9, 2),
            due_at=date(2026, 9, 30),
            lines=[{"title": "Услуга", "price": "100000"}],
            actor="тест",
        )
        data = reports.balance(session, workspace.id, as_of=date(2026, 9, 5))
        assert Decimal(data["assets"]["total"]) == Decimal("600000.00")
        assert Decimal(data["liabilities"]["total"]) == Decimal("0")
        assert Decimal(data["equity"]) == Decimal("600000.00")
        assert "основные средства" in data["not_counted"]


# ── Интеграции ──────────────────────────────────────────────────────────────


def test_priyom_po_adresu_chitaet_znak_kak_vypiska(space):
    """Одна и та же строка, присланная и загруженная, обязана дать одно и то же."""
    rows = integrations.rows_of(
        [
            {"date": "01.03.2026", "amount": "-15000", "comment": "Magnum"},
            {"date": "2026-03-02", "amount": "240000", "comment": "оплата"},
        ]
    )
    assert [row["kind"] for row in rows] == ["expense", "income"]
    assert rows[0]["amount"] == Decimal("15000")
    assert rows[1]["paid_at"] == date(2026, 3, 2)


def test_token_hranitsya_heshem_i_vyklyuchennoe_podklyuchenie_otkazyvaet(space):
    with finance_session() as session:
        workspace = _workspace(session, space)
        integration, token = integrations.create(
            session, workspace, slug="kaspi", kind="api", actor="тест"
        )
        assert token and integration.token_hash and token not in integration.token_hash

        found = integrations.resolve_token(session, token)
        assert found.id == integration.id

        integrations.set_state(session, workspace.id, integration.id, "off")
        with pytest.raises(service.FinanceError) as exc:
            integrations.resolve_token(session, token)
        assert "выключено" in str(exc.value)

        new = integrations.rotate_token(session, workspace.id, integration.id)
        assert new != token


def test_nelzya_podklyuchit_bank_sposobom_kotorogo_u_nego_net(space):
    with finance_session() as session:
        workspace = _workspace(session, space)
        with pytest.raises(service.FinanceError) as exc:
            integrations.create(session, workspace, slug="sheets", kind="api", actor="тест")
        assert "подключается" in str(exc.value)


# ── Выписка по счёту ────────────────────────────────────────────────────────


def test_vypiska_po_schyotu_daet_ostatok_posle_kazhdoy_stroki(space):
    with finance_session() as session:
        workspace = _workspace(session, space)
        account = _account(session, workspace)
        for day, kind, amount in (
            (1, "income", "100000"),
            (3, "expense", "30000"),
            (5, "expense", "20000"),
        ):
            service.create_operation(
                session,
                workspace,
                service.OperationInput(
                    kind=kind,
                    paid_at=date(2026, 10, day),
                    amount=Decimal(amount),
                    account_to_id=account.id if kind == "income" else None,
                    account_from_id=account.id if kind == "expense" else None,
                ),
                actor="тест",
            )
        data = reports.account_statement(
            session, workspace.id, account.id, date(2026, 10, 1), date(2026, 10, 31)
        )
        assert [row["balance"] for row in data["rows"]] == ["100000.00", "70000.00", "50000.00"]
        assert data["closing_balance"] == "50000.00"
        assert data["income"] == "100000.00"
        assert data["expense"] == "50000.00"


def test_kredit_ne_vyruchka_i_pogashenie_ne_rashod(space):
    """Полученный кредит, посчитанный выручкой, делает месяц «прибыльным» на сумму долга."""
    with finance_session() as session:
        workspace = service.create_workspace(session, title="Кредитная проверка")
        account = service.list_accounts(session, workspace.id)[0]
        by_name = {item.name: item for item in service.list_categories(session, workspace.id)}
        assert by_name["Получение кредита"].nature == "capital"
        assert by_name["Выручка"].nature == "revenue"
        assert by_name["Закуп товара"].nature == "cogs"

        def op(kind, amount, name):
            service.create_operation(
                session,
                workspace,
                service.OperationInput(
                    kind=kind,
                    paid_at=date(2026, 11, 5),
                    amount=Decimal(amount),
                    account_to_id=account.id if kind == "income" else None,
                    account_from_id=account.id if kind == "expense" else None,
                    category_id=by_name[name].id,
                ),
                actor="тест",
            )

        op("income", "5000000", "Получение кредита")
        op("income", "800000", "Выручка")
        op("expense", "300000", "Закуп товара")
        op("expense", "1000000", "Погашение кредита")

        data = reports.indicators(session, workspace.id, date(2026, 11, 1), date(2026, 11, 30))
        assert Decimal(data["revenue"]) == Decimal("800000.00")
        assert Decimal(data["gross_profit"]) == Decimal("500000.00")
        assert Decimal(data["net_profit"]) == Decimal("500000.00")
