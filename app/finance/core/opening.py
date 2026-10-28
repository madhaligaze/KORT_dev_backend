"""Входящее сальдо договоров - долг на начало учёта оплат (фазы 4 и 6).

KORT ведёт оплаты с дня начала учёта (`core/settings.py`). Сколько клиент был
должен до него, оплатами KORT не узнать: тех платежей в KORT нет. Долг на
этот день - входящее сальдо договора, документ `opening` с периодом - днём
начала учёта. Оплаты гасят его первым, раньше любого начисления
(`core/payments.py`); отрицательное сальдо - аванс, оно гасит начисления,
как оплата в первый день.

Сальдо два, как и долг (`core/debts.py`): по начислениям (`basis` пусто или
`accruals`) - что клиент должен по договору, и по АВР (`basis = acts`) - что
должен по выставленным актам. Разносятся оплаты только по первому; второе -
начало долга по актам.

Пока учёт в книге-сводке, сальдо берётся из неё: «Сальдо Начало (без АВР)» и
«Сальдо Начало (АВР)» строки договора за месяц начала учёта. Перенос - пачкой,
с предпросмотром и точкой восстановления; повторный перенос не двоит (ключ
источника - договор, месяц и основание) и правит только изменившиеся. У
договора нет строки в этом месяце - нет и сальдо: KORT не выводит долг из
соседних месяцев. Поставленное человеком книга не перебивает.
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
#: Основания сальдо: по начислениям и по АВР - и колонка книги каждого.
BASES = ("accruals", "acts")
_COLUMNS = {"accruals": "opening_accruals", "acts": "opening_acts"}
BASIS_TITLES = {"accruals": "по начислениям", "acts": "по АВР"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def basis_of(doc: Document) -> str:
    """Основание документа сальдо; без пометки - по начислениям (так писала фаза 4)."""
    return str((doc.meta or {}).get("basis") or "accruals")


def _key(contract_id: uuid.UUID, since: date, basis: str) -> str:
    base = f"{contract_id}:{month_key(since)}"
    return base if basis == "accruals" else f"{base}:{basis}"


def current(session: Session, workspace: Workspace, since: date, basis: str = "accruals") -> dict[uuid.UUID, Document]:
    """Действующее сальдо договоров на начало учёта - одного основания."""
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
        if basis_of(doc) == basis
    }


def _from_book(
    session: Session, workspace: Workspace, access: Access, book: Any, since: date
) -> dict[str, dict[uuid.UUID, tuple[Decimal, int]]]:
    """Основание → договор → сальдо начала месяца по книге и строка книги."""
    from app.finance.core.shadow import Reconciler

    reconciler = Reconciler(session, workspace, access, book)
    month = month_key(since)
    out: dict[str, dict[uuid.UUID, tuple[Decimal, int]]] = {basis: {} for basis in BASES}
    for contract_id, months in reconciler.grouped.items():
        entry = months.get(month)
        if entry is None:
            continue
        for basis, column in _COLUMNS.items():
            values = [(getattr(row, column), row.line) for row in entry.rows if getattr(row, column) is not None]
            if values:
                out[basis][contract_id] = (sum((value for value, _line in values), _ZERO), values[0][1])
    return out


def _require(access: Access) -> None:
    if not access.setup:
        raise PermissionError("Входящее сальдо переносит владелец или администратор")


def _state(doc: Document | None, amount: Decimal) -> str | None:
    if doc is None and amount == 0:
        return None
    if doc is not None and doc.source == "kort":
        # Сальдо, поставленное человеком, книга не перебивает.
        return "manual"
    if doc is not None and doc.amount == amount:
        return "same"
    if doc is None:
        return "new"
    return "remove" if amount == 0 else "changed"


def plan(session: Session, workspace: Workspace, access: Access, book: Any, *, since: date) -> dict[str, Any]:
    """Что запишет перенос: новое сальдо, изменившееся, ставшее нулём - по обоим основаниям."""
    _require(access)
    wanted = _from_book(session, workspace, access, book, since)
    items: list[dict[str, Any]] = []
    numbers: dict[uuid.UUID, str] = {}
    for basis in BASES:
        existing = current(session, workspace, since, basis)
        ids = set(wanted[basis]) | set(existing)
        missing = ids - set(numbers)
        if missing:
            numbers.update(dict(session.execute(sa.select(Contract.id, Contract.number).where(Contract.id.in_(list(missing)))).all()))
        for contract_id in ids:
            amount, line = wanted[basis].get(contract_id, (_ZERO, 0))
            doc = existing.get(contract_id)
            state = _state(doc, amount)
            if state is None:
                continue
            items.append(
                {
                    "contract_id": str(contract_id),
                    "number": numbers.get(contract_id) or "",
                    "basis": basis,
                    "amount": _money(amount),
                    "was": _money(doc.amount) if doc is not None else None,
                    "line": line,
                    "state": state,
                }
            )
    items.sort(key=lambda item: (item["number"], item["basis"]))
    changes = [item for item in items if item["state"] in ("new", "changed", "remove")]

    def total(basis: str, sign: int) -> str | None:
        kept = [Decimal(item["amount"]) for item in items if item["basis"] == basis and item["state"] != "remove"]
        return _money(sum((value for value in kept if value * sign > 0), _ZERO) * sign)

    return {
        "since": since.isoformat(),
        "items": changes,
        "counts": {state: sum(1 for item in items if item["state"] == state) for state in ("new", "changed", "remove", "same", "manual")},
        "debt": total("accruals", 1),
        "advance": total("accruals", -1),
        "acts_debt": total("acts", 1),
        "acts_advance": total("acts", -1),
    }


def apply(session: Session, workspace: Workspace, access: Access, actor: Actor, book: Any, *, since: date) -> dict[str, Any]:
    """Записать сальдо из книги пачкой - с точкой восстановления."""
    _require(access)
    wanted = _from_book(session, workspace, access, book, since)
    return apply_wanted(
        session, workspace, access, actor, wanted, since=since, source="book",
        title=f"входящее сальдо на {since:%d.%m.%Y} из таблицы",
    )


def apply_wanted(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    wanted: dict[str, dict[uuid.UUID, Any]],
    *,
    since: date,
    source: str,
    title: str,
    only: set[uuid.UUID] | None = None,
) -> dict[str, Any]:
    """Записать сальдо любого источника пачкой: основание → договор → сумма или (сумма, строка).

    Один путь для сводной таблицы (`book`), ОСВ 1С (`1c`) и таблиц отделов
    (`file`): сальдо, поставленное человеком, источник не перебивает; снятое
    раньше возвращается под тем же ключом; пачка - одна точка восстановления
    `core_opening`. Источник целиком (сводная, ОСВ) снимает сальдо договоров,
    которых в нём нет; `only` - только эти договоры: таблица одного отдела не
    знает о сальдо других.
    """
    _require(access)
    wanted = {
        basis: {key: (value if isinstance(value, tuple) else (value, 0)) for key, value in (wanted.get(basis) or {}).items()}
        for basis in BASES
    }
    stored: list[dict[str, Any]] = []
    touched: set[uuid.UUID] = set()
    for basis in BASES:
        existing = current(session, workspace, since, basis)
        if only is not None:
            existing = {key: doc for key, doc in existing.items() if key in only}
        for contract_id in set(wanted[basis]) | set(existing):
            amount, line = wanted[basis].get(contract_id, (_ZERO, 0))
            doc = existing.get(contract_id)
            if _state(doc, amount) in (None, "same", "manual"):
                continue
            key = _key(contract_id, since, basis)
            if doc is None:
                # Сальдо, когда-то перенесённое и снятое, лежит удалённым под тем же
                # ключом источника: оно возвращается, а не заводится вторым.
                doc = session.scalar(
                    sa.select(Document).where(
                        Document.workspace_id == workspace.id,
                        Document.source == source,
                        Document.kind == "opening",
                        Document.source_key == key,
                    )
                )
                if doc is not None:
                    doc.deleted_at = None
                    stored.append({"id": str(doc.id), "before": None, "after": _money(amount), "revived": True})
                else:
                    doc = Document(
                        workspace_id=workspace.id, contract_id=contract_id, kind="opening", period=since,
                        source=source, source_key=key, created_by=actor.user_id,
                    )
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
            doc.meta = {**(doc.meta or {}), "line": line, "basis": basis}
            doc.updated_by, doc.updated_at = actor.user_id, _now()
            touched.add(contract_id)
    if not stored:
        return {"done": 0, "documents": 0, "point": None}
    session.flush()
    seq = bump(session, workspace.id, "contracts")
    session.execute(sa.update(Contract).where(Contract.id.in_(list(touched))).values(seq=seq))
    title = f"{title}: {len(touched)} договоров"
    point = restore._point(session, workspace, actor, "core_opening", title, {"items": stored, "since": since.isoformat()})
    history.write(
        session, workspace, kind="core.opening", entity="documents", entity_id=None,
        title=title, after={"count": len(stored), "contracts": len(touched), "since": since.isoformat()}, actor=actor.email,
    )
    return {"done": len(touched), "documents": len(stored), "point": restore._point_ref(point)}


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
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    contract_id: uuid.UUID,
    *,
    since: date,
    amount: Decimal | None,
    basis: str = "accruals",
) -> Document | None:
    """Сальдо одного договора руками (`None` - снять): поправка человека, источник KORT."""
    _require(access)
    if basis not in BASES:
        raise FinanceError("Сальдо - по начислениям или по АВР")
    contract = session.get(Contract, contract_id)
    if contract is None or contract.workspace_id != workspace.id or contract.deleted_at is not None:
        raise FinanceError("Договор не найден")
    doc = current(session, workspace, since, basis).get(contract_id)
    before = _money(doc.amount) if doc is not None else None
    if amount is None:
        if doc is None:
            return None
        doc.deleted_at = _now()
    else:
        if doc is None:
            doc = Document(
                workspace_id=workspace.id, contract_id=contract_id, kind="opening", period=since,
                created_by=actor.user_id, meta={"basis": basis},
            )
            session.add(doc)
        doc.amount, doc.source, doc.source_key = amount, "kort", ""
        doc.meta = {**(doc.meta or {}), "basis": basis}
    doc.updated_by, doc.updated_at = actor.user_id, _now()
    session.flush()
    seq = bump(session, workspace.id, "contracts")
    contract.seq = seq
    history.write(
        session, workspace, kind="contract.opening", entity="contract", entity_id=contract.id,
        title=(
            f"договор {contract.number or ''} · входящее сальдо {BASIS_TITLES[basis]} на {since:%d.%m.%Y}: "
            f"{before or '-'} → {_money(amount) or 'снято'}"
        ).replace("  ", " "),
        before={"amount": before, "basis": basis}, after={"amount": _money(amount), "basis": basis}, actor=actor.email,
    )
    return doc if amount is not None else None


__all__ = ["BASES", "BASIS_TITLES", "apply", "apply_wanted", "basis_of", "current", "plan", "set_one", "undo"]
