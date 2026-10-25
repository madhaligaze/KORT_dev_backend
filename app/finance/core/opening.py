"""Входящее сальдо договоров - долг на начало учёта оплат (фаза 4).

KORT ведёт оплаты с дня начала учёта (`core/settings.py`). Сколько клиент был
должен до него, оплатами KORT не узнать: тех платежей в KORT нет. Долг на
этот день - входящее сальдо договора, документ `opening` с периодом - днём
начала учёта. Оплаты гасят его первым, раньше любого начисления
(`core/payments.py`); отрицательное сальдо - аванс, оно гасит начисления,
как оплата в первый день.

Пока учёт в книге-сводке, сальдо берётся из неё: «Сальдо Начало (без АВР)»
строки договора за месяц начала учёта. Перенос - пачкой, с предпросмотром и
точкой восстановления; повторный перенос не двоит (ключ источника - договор
и месяц) и правит только изменившиеся. У договора нет строки в этом месяце -
нет и сальдо: KORT не выводит долг из соседних месяцев.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts import restore
from app.finance.contracts.fields import bump
from app.finance.contracts.models import Contract
from app.finance.contracts.service import Access, Actor
from app.finance.core.accruals import month_key
from app.finance.core.models import Document
from app.finance.models import Workspace
from app.finance.service import FinanceError

_ZERO = Decimal("0")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _key(contract_id: uuid.UUID, since: date) -> str:
    return f"{contract_id}:{month_key(since)}"


def _current(session: Session, workspace: Workspace, since: date) -> dict[uuid.UUID, Document]:
    return {
        doc.contract_id: doc
        for doc in session.scalars(
            sa.select(Document).where(
                Document.workspace_id == workspace.id,
                Document.kind == "opening",
                Document.deleted_at.is_(None),
                Document.period == since,
                Document.contract_id.is_not(None),
            )
        )
    }


def _from_book(session: Session, workspace: Workspace, access: Access, book: Any, since: date) -> dict[uuid.UUID, tuple[Decimal, int]]:
    """Договор → сальдо начала месяца по книге и строка книги."""
    from app.finance.core.shadow import Reconciler

    reconciler = Reconciler(session, workspace, access, book)
    month = month_key(since)
    out: dict[uuid.UUID, tuple[Decimal, int]] = {}
    for contract_id, months in reconciler.grouped.items():
        entry = months.get(month)
        if entry is None:
            continue
        values = [(row.opening_accruals, row.line) for row in entry.rows if row.opening_accruals is not None]
        if values:
            out[contract_id] = (sum((value for value, _line in values), _ZERO), values[0][1])
    return out


def _require(access: Access) -> None:
    if not access.setup:
        raise PermissionError("Входящее сальдо переносит владелец или администратор")


def plan(session: Session, workspace: Workspace, access: Access, book: Any, *, since: date) -> dict[str, Any]:
    """Что запишет перенос: новое сальдо, изменившееся, ставшее нулём."""
    _require(access)
    wanted = _from_book(session, workspace, access, book, since)
    current = _current(session, workspace, since)
    contracts = {
        item.id: item
        for item in session.scalars(sa.select(Contract).where(Contract.id.in_(list(set(wanted) | set(current)))))
    } if (wanted or current) else {}
    items: list[dict[str, Any]] = []
    for contract_id in sorted(set(wanted) | set(current), key=lambda cid: (contracts[cid].number or "") if cid in contracts else ""):
        amount, line = wanted.get(contract_id, (_ZERO, 0))
        doc = current.get(contract_id)
        was = doc.amount if doc is not None else None
        if doc is None and amount == 0:
            continue
        if doc is not None and doc.source == "kort":
            # Сальдо, поставленное человеком, книга не перебивает.
            state = "manual"
        elif doc is not None and was == amount:
            state = "same"
        elif doc is None:
            state = "new"
        elif amount == 0:
            state = "remove"
        else:
            state = "changed"
        contract = contracts.get(contract_id)
        items.append(
            {
                "contract_id": str(contract_id),
                "number": contract.number if contract else "",
                "amount": _money(amount),
                "was": _money(was),
                "line": line,
                "state": state,
            }
        )
    changes = [item for item in items if item["state"] in ("new", "changed", "remove")]
    kept = [item for item in items if item["state"] != "remove"]
    return {
        "since": since.isoformat(),
        "items": changes,
        "counts": {state: sum(1 for item in items if item["state"] == state) for state in ("new", "changed", "remove", "same", "manual")},
        "debt": _money(sum((Decimal(item["amount"]) for item in kept if Decimal(item["amount"]) > 0), _ZERO)),
        "advance": _money(-sum((Decimal(item["amount"]) for item in kept if Decimal(item["amount"]) < 0), _ZERO)),
    }


def apply(session: Session, workspace: Workspace, access: Access, actor: Actor, book: Any, *, since: date) -> dict[str, Any]:
    """Записать сальдо из книги пачкой - с точкой восстановления."""
    _require(access)
    wanted = _from_book(session, workspace, access, book, since)
    current = _current(session, workspace, since)
    stored: list[dict[str, Any]] = []
    touched: list[uuid.UUID] = []
    for contract_id in set(wanted) | set(current):
        amount, line = wanted.get(contract_id, (_ZERO, 0))
        doc = current.get(contract_id)
        if doc is None and amount == 0:
            continue
        if doc is not None and (doc.amount == amount or doc.source == "kort"):
            continue
        if doc is None:
            doc = Document(
                workspace_id=workspace.id, contract_id=contract_id, kind="opening", period=since,
                source="book", source_key=_key(contract_id, since), created_by=actor.user_id,
            )
            # Сальдо, когда-то перенесённое и снятое, лежит удалённым под тем же
            # ключом источника: оно возвращается, а не заводится вторым.
            gone = session.scalar(
                sa.select(Document).where(
                    Document.workspace_id == workspace.id,
                    Document.source == "book",
                    Document.kind == "opening",
                    Document.source_key == _key(contract_id, since),
                )
            )
            if gone is not None:
                doc = gone
                stored.append({"id": str(doc.id), "before": None, "after": _money(amount), "revived": True})
                doc.deleted_at = None
            else:
                session.add(doc)
                session.flush()
                stored.append({"id": str(doc.id), "before": None, "after": _money(amount)})
            doc.amount = amount
        elif amount == 0:
            stored.append({"id": str(doc.id), "before": _money(doc.amount), "after": None})
            doc.deleted_at = _now()
        else:
            stored.append({"id": str(doc.id), "before": _money(doc.amount), "after": _money(amount)})
            doc.amount = amount
        doc.meta = {**(doc.meta or {}), "line": line}
        doc.updated_by, doc.updated_at = actor.user_id, _now()
        touched.append(contract_id)
    if not stored:
        return {"done": 0, "point": None}
    session.flush()
    seq = bump(session, workspace.id, "contracts")
    session.execute(sa.update(Contract).where(Contract.id.in_(touched)).values(seq=seq))
    title = f"входящее сальдо на {since:%d.%m.%Y} из книги: {len(stored)} договоров"
    point = restore._point(session, workspace, actor, "core_opening", title, {"items": stored, "since": since.isoformat()})
    history.write(
        session, workspace, kind="core.opening", entity="documents", entity_id=None,
        title=title, after={"count": len(stored), "since": since.isoformat()}, actor=actor.email,
    )
    return {"done": len(stored), "point": restore._point_ref(point)}


def undo(session: Session, workspace: Workspace, actor: Actor, payload: dict[str, Any], done: list[str], skipped: list[str]) -> None:
    """Вернуть сальдо, как было до переноса. Изменённое с тех пор - не трогается."""
    touched: list[uuid.UUID] = []
    for item in payload.get("items") or []:
        doc = session.get(Document, uuid.UUID(item["id"]))
        if doc is None or doc.workspace_id != workspace.id or doc.kind != "opening":
            skipped.append("сальдо уже нет")
            continue
        now = None if doc.deleted_at is not None else _money(doc.amount)
        if now != item.get("after"):
            skipped.append(f"сальдо договора изменили после переноса - оставлено {now or 'снятым'}")
            continue
        if item.get("before") is None:
            doc.deleted_at = _now()
        else:
            doc.deleted_at = None
            doc.amount = Decimal(item["before"])
        doc.updated_by, doc.updated_at = actor.user_id, _now()
        if doc.contract_id:
            touched.append(doc.contract_id)
        done.append(f"сальдо {item.get('before') or 'снято'}")
    if touched:
        session.flush()
        seq = bump(session, workspace.id, "contracts")
        session.execute(sa.update(Contract).where(Contract.id.in_(touched)).values(seq=seq))


def set_one(
    session: Session, workspace: Workspace, access: Access, actor: Actor, contract_id: uuid.UUID, *, since: date, amount: Decimal | None
) -> Document | None:
    """Сальдо одного договора руками (`None` - снять): поправка человека, источник KORT."""
    _require(access)
    contract = session.get(Contract, contract_id)
    if contract is None or contract.workspace_id != workspace.id or contract.deleted_at is not None:
        raise FinanceError("Договор не найден")
    doc = _current(session, workspace, since).get(contract_id)
    before = _money(doc.amount) if doc is not None else None
    if amount is None:
        if doc is None:
            return None
        doc.deleted_at = _now()
    else:
        if doc is None:
            doc = Document(workspace_id=workspace.id, contract_id=contract_id, kind="opening", period=since, created_by=actor.user_id)
            session.add(doc)
        doc.amount, doc.source, doc.source_key = amount, "kort", ""
    doc.updated_by, doc.updated_at = actor.user_id, _now()
    session.flush()
    seq = bump(session, workspace.id, "contracts")
    contract.seq = seq
    history.write(
        session, workspace, kind="contract.opening", entity="contract", entity_id=contract.id,
        title=f"договор {contract.number or ''} · входящее сальдо на {since:%d.%m.%Y}: {before or '-'} → {_money(amount) or 'снято'}".replace("  ", " "),
        before={"amount": before}, after={"amount": _money(amount)}, actor=actor.email,
    )
    return doc if amount is not None else None


__all__ = ["apply", "plan", "set_one", "undo"]
