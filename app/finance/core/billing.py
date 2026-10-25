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
from app.finance.core.shadow import BookMonth, EntityTally, Reconciler, month_owner
from app.finance.models import Workspace

#: Чем списки расходятся.
CODES = ("group", "no_kort", "no_book", "kort_issued")
CODE_TITLES = {
    "group": "срок другой",
    "no_kort": "в таблице ждут счёт, у KORT начисления нет",
    "no_book": "KORT ждёт счёт, строки в таблице нет",
    "kort_issued": "счёт выставлен в KORT, таблица ещё не знает",
}
GROUP_TITLES = {
    bills_module.OVERDUE: "просрочен",
    bills_module.SOON: "скоро",
    bills_module.EARLY: "ещё рано",
    bills_module.UNDATED: "без срока",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bill_of_book(month: BookMonth | None, words: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Счёт месяца по книге: «№ Счета» заполнен или «Счет» = TRUE (или слово компании со смыслом «выставлен»)."""
    if month is None:
        return None
    row = month.rows[0]
    if row.bill_no or bills_module.state_of(row.bill_state, words) is None:
        return {"source": "book", "number": row.bill_no, "date": row.bill_date.isoformat() if row.bill_date else None}
    return None


def _book_group(month: BookMonth | None, words: dict[str, str] | None = None) -> str | None:
    """Группа строки по словам книги и словам компании; `None` - счёт выставлен или строки нет."""
    if month is None:
        return None
    return bills_module.state_of(month.rows[0].bill_state, words)


def kort_bills(session: Session, workspace_id: uuid.UUID) -> dict[tuple[uuid.UUID, str], Document]:
    """Выставленные счета KORT по договору и месяцу. Без номера - ещё не счёт (комментарий к будущему)."""
    return {
        (doc.contract_id, month_key(doc.period)): doc
        for doc in session.scalars(
            sa.select(Document).where(
                Document.workspace_id == workspace_id,
                Document.kind == "bill",
                Document.deleted_at.is_(None),
                Document.contract_id.is_not(None),
                Document.period.is_not(None),
                Document.number != "",
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
    session: Session, workspace: Workspace, access: Access, book: book_module.Book, *, today: date,
    entity: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Список «Не выставлены» KORT против списка книги - по договору и месяцу.

    `entity` - только месяцы, где это наше юрлицо исполнитель (как считает «Переход»).
    """
    from app.finance.core import settings as core_settings

    reconciler = Reconciler(session, workspace, access, book)
    window = [month for month in reconciler.window if month <= month_key(today)]
    docs = kort_bills(session, workspace.id)
    words = core_settings.table_words(session, workspace.id).get("bill", {})
    lines: list[BillLine] = []
    same = 0
    waiting = {"kort": 0, "book": 0}
    tally = EntityTally()
    if not window:
        return {"window": [], "counts": {"same": 0}, "lines": [], "contracts": {}, "by_entity": {}}
    for contract in reconciler.contracts:
        schedule = reconciler.ledger.schedule(contract, since=month_start(window[0]), until=month_start(window[-1]))
        accruals = schedule.by_month()
        book_months = reconciler.grouped.get(contract.id, {})
        for month in window:
            accrual, row = accruals.get(month), book_months.get(month)
            if accrual is None and row is None:
                continue
            owner = month_owner(contract, accrual, row)
            if entity is not None and owner != entity:
                continue
            doc = docs.get((contract.id, month))
            book_bill = _bill_of_book(row, words)
            bill = (
                {"source": "kort", "number": doc.number, "date": doc.issued_on.isoformat() if doc.issued_on else None}
                if doc is not None
                else book_bill
            )
            kort = _kort_side(accrual, bill, today)
            book_group = _book_group(row, words)
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
                tally.add(owner, contract.id, True, month)
                continue
            if kort_waits and book_waits:
                if kort["group"] == book_group:
                    same += 1
                    tally.add(owner, contract.id, True, month)
                    continue
                code = "group"
            elif book_waits:
                code = "kort_issued" if doc is not None else "no_kort"
            else:
                code = "no_book" if row is None else "group"
            tally.add(owner, contract.id, False, month)
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
        #: По нашим юрлицам месяца: сверено и совпало - для «Перехода».
        "by_entity": tally.out(),
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


# ── Счёт без строки в таблице ────────────────────────────────────────────────
#
# Юрлицо ведёт счета в KORT (`core/switch.py`), и месяц, который KORT
# начисляет, есть в списке, даже если строки в таблице нет. Записать такой
# счёт можно только в KORT - документом месяца договора; поля и проверка
# «уже поменяли» - те же, что у записи в таблицу (`contracts/bill_book.py`).

_FIELDS = ("bill_no", "bill_date", "bill_period", "note")
#: Где счёт: строки таблицы нет. Объектом, а не пусто - карточка прежних
#: версий десктопа читает `place.book` и без строки упала бы; новая смотрит на `kort`.
_PLACE = {"book": "KORT", "tab": "", "row": 0, "url": ""}


def _address(bill: Any) -> tuple[uuid.UUID, date]:
    month = book_module.month_of(bill.month, bill.period_from)
    if not bill.contract_id or not month:
        raise ValueError("у счёта KORT нет договора или месяца")
    return uuid.UUID(bill.contract_id), month_start(month)


def _doc_of(session: Session, workspace_id: uuid.UUID, contract_id: uuid.UUID, period: date) -> Document | None:
    return session.scalar(
        sa.select(Document)
        .where(
            Document.workspace_id == workspace_id,
            Document.kind == "bill",
            Document.contract_id == contract_id,
            Document.period == period,
            Document.deleted_at.is_(None),
        )
        .order_by(Document.updated_at.desc())
        .limit(1)
    )


def _fields_of(doc: Document | None) -> dict[str, str]:
    meta = (doc.meta or {}) if doc is not None else {}
    return {
        "bill_no": doc.number if doc is not None else "",
        "bill_date": f"{doc.issued_on:%d.%m.%Y}" if doc is not None and doc.issued_on else "",
        "bill_period": str(meta.get("bill_period") or ""),
        "note": str(meta.get("note") or ""),
    }


def kort_card(session: Session, workspace_id: uuid.UUID, bill: Any) -> dict[str, Any]:
    """Поля счёта KORT для карточки - в том же виде, что строка таблицы юрлица (`bill_book.card`)."""
    contract_id, period = _address(bill)
    fields = _fields_of(_doc_of(session, workspace_id, contract_id, period))
    return {"place": _PLACE, "fields": fields, "state": "TRUE" if fields["bill_no"] else bill.text, "blocked": {}, "kort": True}


def write_kort(
    session: Session,
    workspace: Workspace,
    bill: Any,
    expect: dict[str, Any],
    values: dict[str, Any],
    defaults: dict[str, Any] | None,
    *,
    actor: str,
) -> dict[str, Any]:
    """Записать счёт без строки в таблице документом KORT - ответом как у записи в таблицу.

    `expect` - поля такими, какими их показала карточка: другие в KORT - отказ
    (409), а не затёртая чужая правка; уже ровно то, что пишем, - повтор
    записи, не отказ. Пустой № - счёт снова не выставлен: комментарий и
    период остаются, а в «Выставлены» и долгах счёта нет (`kort_bills`).
    """
    from app.finance import history
    from app.finance.contracts import bill_book

    contract_id, period = _address(bill)
    doc = _doc_of(session, workspace.id, contract_id, period)
    before = _fields_of(doc)
    wanted: dict[str, str] = {}
    if "bill_no" in values:
        number = bill_book._clean_text("bill_no", values["bill_no"])
        if number[:1] in ("=", "+", "@"):
            raise bill_book.BookError("«№ Счета» не может начинаться с =, + или @", status=422)
        wanted["bill_no"] = number
    if "bill_date" in values:
        parsed = bill_book._parse_date(values["bill_date"])
        wanted["bill_date"] = f"{parsed:%d.%m.%Y}" if parsed else ""
    for name in ("bill_period", "note"):
        if name in values:
            wanted[name] = bill_book._clean_text(name, values[name])
    if not wanted:
        raise bill_book.BookError("Нечего записывать", status=422)
    for name, value in (defaults or {}).items():
        if name in ("bill_period", "note") and name not in wanted and not before[name]:
            wanted[name] = bill_book._clean_text(name, value)
    for name, value in wanted.items():
        seen = " ".join(str(expect.get(name, before[name]) or "").split())
        if before[name] != seen and before[name] != value:
            raise bill_book.BookError(
                f"«{bill_book.TITLES[name]}» в KORT уже поменяли: сейчас «{before[name] or 'пусто'}». Откройте счёт заново",
                status=409,
            )
    after = {**before, **wanted}
    changed = [name for name in _FIELDS if after[name] != before[name]]
    if changed:
        if doc is None:
            doc = Document(workspace_id=workspace.id, kind="bill", source="kort", source_key=bill.key)
            session.add(doc)
        doc.contract_id, doc.period = contract_id, period
        doc.number = after["bill_no"]
        doc.issued_on = bills_module.read_date(after["bill_date"]) if after["bill_no"] else None
        doc.amount = bill.amount if isinstance(bill.amount, Decimal) else None
        doc.meta = {**(doc.meta or {}), "bill_period": after["bill_period"], "note": after["note"]}
        doc.updated_at = _now()
        session.flush()
        if not after["bill_no"]:
            after["bill_date"] = ""
        where = f"{bill.customer} {bill.number}, {bill.month}".strip()
        if after["bill_no"] and not before["bill_no"]:
            title = f"счёт выставлен в KORT: {where} - № {after['bill_no']}" + (f" от {after['bill_date']}" if after["bill_date"] else "")
        elif before["bill_no"] and not after["bill_no"]:
            title = f"отметка «выставлен» снята в KORT: {where}"
        else:
            title = f"счёт исправлен в KORT: {where}"
        history.write(
            session,
            workspace,
            kind="bill.kort",
            entity="bill",
            entity_id=doc.id,
            title=title,
            before={bill_book.TITLES[name]: before[name] for name in changed},
            after={bill_book.TITLES[name]: after[name] for name in changed},
            actor=actor,
        )
    return {
        "book": {"place": _PLACE, "fields": after, "state": "TRUE" if after["bill_no"] else bill.text, "blocked": {}, "kort": True},
        "changed": changed,
        "warning": "",
    }


__all__ = ["CODES", "CODE_TITLES", "GROUP_TITLES", "kort_bills", "kort_card", "reconcile_bills", "record", "write_kort"]
