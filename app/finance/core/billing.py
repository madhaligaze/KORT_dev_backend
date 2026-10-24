"""Счета в ядре: «Не выставлены» из начислений и сверка со списком книги (фаза 3).

Счёт - документ месяца договора (`Document` вида `bill`). Пока правда в
книге-сводке, счёт известен из двух мест: из KORT (карточка счёта, записав
№ и дату в книгу юрлица, пишет и документ - `record`) и из строки книги
(«№ Счета»). «Не выставлены» по KORT - начисления, у которых счёта нет ни там,
ни там; срок - «Период с», «скоро» - за три дня до него, как формула книги
(`bills._group_by_date`): одно правило на два списка, иначе они расходились
бы на день.

Сверка сравнивает два списка по договору и месяцу: совпало; срок другой;
в книге счёт ждут, а у KORT начисления нет; KORT ждёт счёт, а строки в книге
нет; KORT уже знает счёт, а книга ещё нет. Когда расхождений нет, список и
бот можно переключить на KORT (фаза 10) - до тех пор они читают книгу.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts import bills as bills_module
from app.finance.contracts.service import Access
from app.finance.core import book as book_module
from app.finance.core.accruals import Accrual, month_key, month_start
from app.finance.core.models import Document
from app.finance.core.shadow import BookMonth, Reconciler
from app.finance.models import Workspace

#: Чем списки расходятся.
CODES = ("group", "no_kort", "no_book", "kort_issued")
CODE_TITLES = {
    "group": "срок другой",
    "no_kort": "в книге ждут счёт, у KORT начисления нет",
    "no_book": "KORT ждёт счёт, строки в книге нет",
    "kort_issued": "счёт выставлен в KORT, книга ещё не знает",
}
GROUP_TITLES = {
    bills_module.OVERDUE: "просрочен",
    bills_module.SOON: "скоро",
    bills_module.EARLY: "ещё рано",
    bills_module.UNDATED: "без срока",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bill_of_book(month: BookMonth | None) -> dict[str, Any] | None:
    """Счёт месяца по книге: «№ Счета» заполнен или «Счет» = TRUE."""
    if month is None:
        return None
    row = month.rows[0]
    if row.bill_no or bills_module.state_of(row.bill_state) is None:
        return {"source": "book", "number": row.bill_no, "date": row.bill_date.isoformat() if row.bill_date else None}
    return None


def _book_group(month: BookMonth | None) -> str | None:
    """Группа строки по словам книги; `None` - счёт выставлен или строки нет."""
    if month is None:
        return None
    return bills_module.state_of(month.rows[0].bill_state)


def kort_bills(session: Session, workspace_id: uuid.UUID) -> dict[tuple[uuid.UUID, str], Document]:
    return {
        (doc.contract_id, month_key(doc.period)): doc
        for doc in session.scalars(
            sa.select(Document).where(
                Document.workspace_id == workspace_id,
                Document.kind == "bill",
                Document.deleted_at.is_(None),
                Document.contract_id.is_not(None),
                Document.period.is_not(None),
            )
        )
    }


@dataclass
class BillLine:
    contract_id: uuid.UUID
    month: str
    code: str
    kort: dict[str, Any] | None
    book: dict[str, Any] | None

    def out(self) -> dict[str, Any]:
        return {"contract_id": str(self.contract_id), "month": self.month, "code": self.code, "kort": self.kort, "book": self.book}


def _kort_side(accrual: Accrual | None, bill: dict[str, Any] | None, today: date) -> dict[str, Any] | None:
    if accrual is None or accrual.skipped or not (accrual.amount or 0):
        return None
    group = None if bill else bills_module._group_by_date(accrual.period_from, today)
    return {
        "group": group,
        "due": accrual.period_from.isoformat(),
        "amount": format(accrual.amount, "f") if accrual.amount is not None else None,
        "bill": bill,
    }


def reconcile_bills(
    session: Session, workspace: Workspace, access: Access, book: book_module.Book, *, today: date
) -> dict[str, Any]:
    """Список «Не выставлены» KORT против списка книги - по договору и месяцу."""
    reconciler = Reconciler(session, workspace, access, book)
    window = [month for month in reconciler.window if month <= month_key(today)]
    docs = kort_bills(session, workspace.id)
    lines: list[BillLine] = []
    same = 0
    waiting = {"kort": 0, "book": 0}
    if not window:
        return {"window": [], "counts": {"same": 0}, "lines": [], "contracts": {}}
    for contract in reconciler.contracts:
        schedule = reconciler.ledger.schedule(contract, since=month_start(window[0]), until=month_start(window[-1]))
        accruals = schedule.by_month()
        book_months = reconciler.grouped.get(contract.id, {})
        for month in window:
            accrual, row = accruals.get(month), book_months.get(month)
            if accrual is None and row is None:
                continue
            doc = docs.get((contract.id, month))
            book_bill = _bill_of_book(row)
            bill = (
                {"source": "kort", "number": doc.number, "date": doc.issued_on.isoformat() if doc.issued_on else None}
                if doc is not None
                else book_bill
            )
            kort = _kort_side(accrual, bill, today)
            book_group = _book_group(row)
            book_side = (
                {"group": book_group, "text": row.rows[0].bill_state, "bill": book_bill, "amount": format(row.amount, "f") if row.amount is not None else None,
                 "due": row.period_from.isoformat() if row.period_from else None}
                if row is not None
                else None
            )
            kort_waits = kort is not None and kort["group"] is not None
            book_waits = book_group is not None
            waiting["kort"] += kort_waits
            waiting["book"] += book_waits
            if not kort_waits and not book_waits:
                same += 1
                continue
            if kort_waits and book_waits:
                if kort["group"] == book_group:
                    same += 1
                    continue
                code = "group"
            elif book_waits:
                code = "kort_issued" if doc is not None else "no_kort"
            else:
                code = "no_book" if row is None else "group"
            lines.append(BillLine(contract.id, month, code, kort, book_side))
    counts = {"same": same, "kort_waits": waiting["kort"], "book_waits": waiting["book"]}
    for code in CODES:
        counts[code] = sum(1 for line in lines if line.code == code)
    by_id = {contract.id: contract for contract in reconciler.contracts}
    contracts = {
        str(contract_id): {
            "id": str(contract_id),
            "number": by_id[contract_id].number,
            "customer": reconciler.names.get(by_id[contract_id].customer_id, ""),
            "executor": reconciler.party_label(by_id[contract_id].executor_id),
        }
        for contract_id in {line.contract_id for line in lines}
    }
    return {
        "window": window,
        "counts": counts,
        "lines": [line.out() for line in sorted(lines, key=lambda item: (CODES.index(item.code), item.month))],
        "contracts": contracts,
        "codes": CODE_TITLES,
        "groups": GROUP_TITLES,
    }


def record(session: Session, workspace: Workspace, bill: Any, values: dict[str, str]) -> Document | None:
    """Счёт, записанный карточкой в книгу юрлица, - документом KORT.

    Строка сводки (`bills.Bill`) находит договор тем же сопоставителем, что
    сверка; не нашла однозначно - документа нет, но в книгу записано, и
    сверка покажет строку как «нет в реестре». Пустой «№ Счета» - отметку
    сняли: документ уходит в корзину.
    """
    row = book_module.BookRow(
        line=getattr(bill, "line", 0), own=bill.own, customer=bill.customer, number=bill.number,
        month=book_module.month_of(bill.month, bill.period_from), month_label=bill.month,
        onec_name=getattr(bill, "onec_name", ""), period_from=bill.period_from,
    )
    match = book_module.Matcher(session, workspace.id).match(row)
    if match.state != "found" or match.contract_id is None or not row.month:
        return None
    key = bills_module.bill_key(bill.own, bill.customer, bill.number, bill.month)
    doc = session.scalar(
        sa.select(Document).where(
            Document.workspace_id == workspace.id, Document.source == "kort", Document.kind == "bill",
            Document.source_key == key,
        )
    )
    number = (values.get("bill_no") or "").strip()
    if not number:
        if doc is not None and doc.deleted_at is None:
            doc.deleted_at = _now()
            session.flush()
        return None
    issued_on = None
    raw = (values.get("bill_date") or "").strip()
    if raw:
        issued_on = bills_module.read_date(raw) or _iso(raw)
    if doc is None:
        doc = Document(workspace_id=workspace.id, kind="bill", source="kort", source_key=key)
        session.add(doc)
    doc.contract_id = match.contract_id
    doc.period = month_start(row.month)
    doc.number, doc.issued_on = number, issued_on
    doc.amount = bill.amount if isinstance(bill.amount, Decimal) else None
    doc.deleted_at = None
    doc.updated_at = _now()
    session.flush()
    return doc


def _iso(text: str) -> date | None:
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


__all__ = ["CODES", "CODE_TITLES", "GROUP_TITLES", "kort_bills", "reconcile_bills", "record"]
