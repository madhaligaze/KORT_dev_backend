"""Счета-фактуры: обязательство появляется раньше денег.

Зачем это в разделе
───────────────────
До сих пор долг в учёте мог появиться только одним способом — человек вручную
отмечал операцию ожиданием. Так работает и Finmap, и это его слабое место:
между «выставили счёт клиенту» и «кто-то вспомнил отметить ожидание» лежит
месяц, и ровно в этот месяц дебиторка показывает неправду.

Счёт закрывает разрыв: выставили — дебиторка появилась в тот же момент, вместе
со сроком. Пришёл счёт от поставщика — появилась кредиторка.

Одна истина на один долг
────────────────────────
Счёт не считает долг сам: он **создаёт ожидание** (`Operation` со статусом
`plan`) и держит на него ссылку. Поэтому:

* в «Долгах» счёт виден наравне с ручными ожиданиями, одним списком;
* оплата закрывается там же, где и остальные — кнопкой «Оплачено»;
* сумма долга не может разойтись с суммой счёта, потому что она одна.

Второй источник истины («счета отдельно, ожидания отдельно») означал бы две
дебиторки, расходящиеся на округлении НДС.

НДС
───
В прибыль идёт сумма **без** НДС, в дебиторку — сумма **к оплате**. Поэтому
хранятся обе, а не одна с пересчётом «когда понадобится»: пересчёт однажды
сделают по другой ставке, и отчёты разъедутся.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.models import (
    Counterparty,
    Invoice,
    InvoiceLine,
    Operation,
    Project,
    Workspace,
)
from app.finance.service import FinanceError, OperationInput, check_money, create_operation

CENT = Decimal("0.01")


def _money(value: Any, *, field: str = "Сумма") -> Decimal:
    """Деньги счёта проверяются тем же правилом, что и деньги операции.

    Иначе цена позиции в двадцать пять знаков доходила до Postgres как есть и
    возвращалась пятисотой ошибкой: столбец `numeric(18, 2)` переполнялся уже
    после того, как запрос ушёл. Человек при этом набирал не двадцать пять
    знаков, а залипшую клавишу.
    """
    return check_money(value or 0, field=field)


def totals(lines: Sequence[dict[str, Any]], vat_rate: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Суммы счёта: без НДС, НДС, к оплате.

    Считается по строкам, а не «введите итог»: в счёте из шести позиций итог,
    набранный руками, однажды не совпадёт с позициями — и спорить будут с
    клиентом, а не с программой.
    """
    net = _money(sum((_money(line.get("amount")) for line in lines), Decimal("0")), field="Сумма счёта")
    vat = _money(net * _number(vat_rate or 0, field="Ставка НДС") / Decimal("100"), field="НДС")
    return net, vat, _money(net + vat, field="Сумма счёта к оплате")


def _number(value: Any, *, field: str) -> Decimal:
    """Не деньги, но число: количество, ставка НДС. Мусор — отказ, не 500."""
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise FinanceError(f"{field}: это не число") from exc
    if not parsed.is_finite():
        raise FinanceError(f"{field}: это не число")
    return parsed


def line_amount(line: dict[str, Any]) -> Decimal:
    quantity = _number(line.get("quantity") or 1, field="Количество")
    price = _money(line.get("price"), field="Цена позиции")
    return _money(quantity * price, field="Сумма позиции")


def next_number(session: Session, workspace_id: uuid.UUID, kind: str) -> str:
    """Следующий номер: «2026-014». Год в номере — чтобы нумерация не росла вечно."""
    year = date.today().year
    prefix = f"{year}-"
    last = session.scalar(
        sa.select(sa.func.max(Invoice.number)).where(
            Invoice.workspace_id == workspace_id,
            Invoice.kind == kind,
            Invoice.number.like(f"{prefix}%"),
        )
    )
    tail = 0
    if last and "-" in last:
        try:
            tail = int(last.split("-", 1)[1])
        except ValueError:
            tail = 0
    return f"{prefix}{tail + 1:03d}"


def create(
    session: Session,
    workspace: Workspace,
    *,
    kind: str,
    issued_at: date,
    due_at: date,
    lines: Sequence[dict[str, Any]],
    vat_rate: Decimal | float | str = 0,
    number: str = "",
    counterparty_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    comment: str = "",
    actor: str = "",
) -> Invoice:
    """Выставить счёт и завести по нему ожидание."""
    if kind not in ("out", "in"):
        raise FinanceError("Счёт бывает исходящим («out») или входящим («in»)")
    prepared = [
        {
            "title": str(line.get("title") or "").strip() or "Без названия",
            "quantity": _number(line.get("quantity") or 1, field="Количество"),
            "price": _money(line.get("price")),
            "amount": line_amount(line),
        }
        for line in lines
    ]
    if not prepared:
        raise FinanceError("В счёте нет ни одной позиции")
    net, vat, gross = totals(prepared, _number(vat_rate or 0, field="Ставка НДС"))
    if gross <= 0:
        raise FinanceError("Сумма счёта должна быть больше нуля")
    if due_at < issued_at:
        raise FinanceError("Срок оплаты не может быть раньше даты счёта")

    invoice = Invoice(
        workspace_id=workspace.id,
        kind=kind,
        number=number.strip() or next_number(session, workspace.id, kind),
        status="sent",
        issued_at=issued_at,
        due_at=due_at,
        counterparty_id=counterparty_id,
        project_id=project_id,
        category_id=category_id,
        currency=workspace.base_currency,
        amount_net=net,
        vat_rate=_number(vat_rate or 0, field="Ставка НДС"),
        vat_amount=vat,
        amount_gross=gross,
        comment=comment.strip(),
        created_by=actor,
    )
    session.add(invoice)
    session.flush()
    for position, line in enumerate(prepared):
        session.add(
            InvoiceLine(
                invoice_id=invoice.id,
                title=line["title"],
                quantity=line["quantity"],
                price=line["price"],
                amount=line["amount"],
                position=position,
            )
        )

    # Ожидание по счёту. Дата платежа — срок: по ней раздел «Долги» считает
    # просрочку. Дата сделки — дата счёта: по ней считается прибыль, и
    # разница между этими двумя датами и есть долг.
    operation = create_operation(
        session,
        workspace,
        OperationInput(
            kind="income" if kind == "out" else "expense",
            status="plan",
            paid_at=due_at,
            accrued_at=issued_at,
            amount=gross,
            account_to_id=account_id if kind == "out" else None,
            account_from_id=account_id if kind == "in" else None,
            category_id=category_id,
            counterparty_id=counterparty_id,
            comment=(f"Счёт {invoice.number}" + (f" · {comment.strip()}" if comment.strip() else "")),
            projects=((project_id, gross),) if project_id else (),
            source="invoice",
        ),
        actor=actor,
    )
    invoice.operation_id = operation.id
    session.flush()
    return invoice


def list_invoices(
    session: Session, workspace_id: uuid.UUID, *, kind: str | None = None
) -> list[dict[str, Any]]:
    query = (
        sa.select(Invoice, Counterparty.name, Project.name, Operation.status)
        .outerjoin(Counterparty, Invoice.counterparty_id == Counterparty.id)
        .outerjoin(Project, Invoice.project_id == Project.id)
        .outerjoin(Operation, Invoice.operation_id == Operation.id)
        .where(Invoice.workspace_id == workspace_id)
        .order_by(Invoice.issued_at.desc(), Invoice.number.desc())
    )
    if kind in ("out", "in"):
        query = query.where(Invoice.kind == kind)

    today = date.today()
    out: list[dict[str, Any]] = []
    for invoice, counterparty, project, operation_status in session.execute(query):
        # Оплаченность берётся у операции, а не хранится дважды: статус счёта
        # «paid» и операция в ожидании — это расхождение, которого не должно
        # существовать в принципе.
        paid = operation_status == "fact" or invoice.status == "paid"
        overdue = (not paid) and invoice.due_at < today
        out.append(
            {
                "id": str(invoice.id),
                "kind": invoice.kind,
                "number": invoice.number,
                "status": "paid" if paid else invoice.status,
                "issued_at": invoice.issued_at.isoformat(),
                "due_at": invoice.due_at.isoformat(),
                "counterparty": counterparty or "",
                "project": project or "",
                "amount_net": str(invoice.amount_net),
                "vat_rate": str(invoice.vat_rate),
                "vat_amount": str(invoice.vat_amount),
                "amount_gross": str(invoice.amount_gross),
                "comment": invoice.comment,
                "paid": paid,
                "overdue_days": (today - invoice.due_at).days if overdue else 0,
                "operation_id": str(invoice.operation_id) if invoice.operation_id else None,
            }
        )
    return out


def read(session: Session, workspace_id: uuid.UUID, invoice_id: uuid.UUID) -> dict[str, Any]:
    invoice = session.get(Invoice, invoice_id)
    if invoice is None or invoice.workspace_id != workspace_id:
        raise FinanceError("Счёт не найден")
    lines = session.scalars(
        sa.select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.position)
    )
    return {
        "id": str(invoice.id),
        "kind": invoice.kind,
        "number": invoice.number,
        "status": invoice.status,
        "issued_at": invoice.issued_at.isoformat(),
        "due_at": invoice.due_at.isoformat(),
        "amount_net": str(invoice.amount_net),
        "vat_rate": str(invoice.vat_rate),
        "vat_amount": str(invoice.vat_amount),
        "amount_gross": str(invoice.amount_gross),
        "comment": invoice.comment,
        "operation_id": str(invoice.operation_id) if invoice.operation_id else None,
        "lines": [
            {
                "title": line.title,
                "quantity": str(line.quantity),
                "price": str(line.price),
                "amount": str(line.amount),
            }
            for line in lines
        ],
    }


def void(session: Session, workspace_id: uuid.UUID, invoice_id: uuid.UUID) -> Invoice:
    """Отменить счёт вместе с его ожиданием.

    Ожидание снимается тоже: счёт, отменённый в одном месте и висящий долгом в
    другом, — это ровно то расхождение, из-за которого учёту перестают верить.
    """
    invoice = session.get(Invoice, invoice_id)
    if invoice is None or invoice.workspace_id != workspace_id:
        raise FinanceError("Счёт не найден")
    if invoice.operation_id:
        operation = session.get(Operation, invoice.operation_id)
        if operation is not None and operation.status == "fact":
            raise FinanceError("Счёт уже оплачен — отменять нечего, поправьте операцию")
        if operation is not None:
            session.delete(operation)
    invoice.status = "void"
    invoice.operation_id = None
    session.flush()
    return invoice


def summary(session: Session, workspace_id: uuid.UUID) -> dict[str, Any]:
    """Сколько выставлено и сколько из этого не оплачено — по обеим сторонам."""
    today = date.today()
    result: dict[str, Any] = {}
    for kind, name in (("out", "receivable"), ("in", "payable")):
        rows = session.execute(
            sa.select(Invoice.amount_gross, Invoice.due_at, Invoice.status, Operation.status)
            .outerjoin(Operation, Invoice.operation_id == Operation.id)
            .where(Invoice.workspace_id == workspace_id, Invoice.kind == kind)
        )
        total = Decimal("0")
        open_total = Decimal("0")
        overdue = Decimal("0")
        for amount, due_at, status, operation_status in rows:
            if status == "void":
                continue
            value = Decimal(str(amount or 0))
            total += value
            paid = operation_status == "fact" or status == "paid"
            if not paid:
                open_total += value
                if due_at < today:
                    overdue += value
        result[name] = {"total": str(total), "open": str(open_total), "overdue": str(overdue)}
    return result


__all__ = ["create", "line_amount", "list_invoices", "next_number", "read", "summary", "totals", "void"]
