"""Документы месяца договора руками: счёт, АВР, ЭСФ.

Компания без 1С и без сводной таблицы ведёт документы в самом KORT. Документ -
`Document` источника KORT на договор и месяц начисления, и читают его те же
итоги, что документы из 1С: счёт гасит «Не выставлены» (`core/billing.kort_board`),
АВР - долг по актам и списки «не подписан», «без ЭСФ» (`core/acts.py`, источник
KORT там первый по приоритету), ЭСФ - отметку у АВР месяца.

Один документ вида на месяц договора: запись того же месяца правит его, а не
заводит второй, - так и `core/acts.py` выбирает один АВР месяца. Сумма счёта и
АВР, если её не назвали, - начисление месяца по условиям договора: так их и
выписывают; месяц без начисления требует суммы - придумывать её KORT не будет.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts.service import (
    Access,
    Actor,
    Registry,
    ensure_writable,
    get_contract,
    read_money,
)
from app.finance.core.accruals import Ledger, month_key, month_start
from app.finance.core.models import Document
from app.finance.models import Workspace
from app.finance.service import FinanceError

KINDS = ("bill", "act", "esf")
KIND_TITLES = {"bill": "счёт", "act": "АВР", "esf": "ЭСФ"}


class DocumentError(FinanceError):
    """Документ записан неверно - словами."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _day(value: date | None) -> str | None:
    return value.isoformat() if value else None


def state(doc: Document | None) -> dict[str, Any]:
    if doc is None:
        return {}
    return {
        "number": doc.number, "day": _day(doc.issued_on), "amount": _money(doc.amount),
        "signed": (doc.meta or {}).get("signed"), "note": (doc.meta or {}).get("note") or "",
    }


def out(doc: Document) -> dict[str, Any]:
    return {
        "id": str(doc.id), "kind": doc.kind, "contract_id": str(doc.contract_id) if doc.contract_id else None,
        "month": month_key(doc.period) if doc.period else None, **state(doc),
    }


def find(session: Session, workspace_id: uuid.UUID, contract_id: uuid.UUID, kind: str, period: date) -> Document | None:
    return session.scalar(
        sa.select(Document)
        .where(
            Document.workspace_id == workspace_id, Document.contract_id == contract_id, Document.kind == kind,
            Document.period == period, Document.source == "kort", Document.deleted_at.is_(None),
        )
        .order_by(Document.updated_at.desc())
        .limit(1)
    )


def _accrued(session: Session, workspace: Workspace, contract: Any, period: date) -> Decimal | None:
    """Начисление месяца по условиям договора - сумма счёта и АВР, если её не назвали."""
    item = next(iter(Ledger(session, workspace.id, [contract]).accruals(contract, since=period, until=period)), None)
    if item is None or item.skipped or not item.amount:
        return None
    return item.amount


def record(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    contract_id: uuid.UUID,
    *,
    kind: str,
    month: str,
    number: str = "",
    day: date | None = None,
    amount: Any = None,
    signed: date | None = None,
    note: str = "",
) -> Document:
    """Записать счёт, АВР или ЭСФ месяца договора (см. описание модуля)."""
    if kind not in KINDS:
        raise DocumentError("Документ месяца - счёт, АВР или ЭСФ")
    if not access.edit:
        raise PermissionError("Правка реестра вам не открыта - только просмотр")
    registry = Registry(session, workspace)
    contract = get_contract(session, workspace, contract_id, for_update=True)
    ensure_writable(session, registry, access, contract)
    try:
        period = month_start(month)
    except (ValueError, IndexError) as exc:
        raise DocumentError("Месяц документа - в виде 2026-07") from exc
    value: Decimal | None = None
    if amount not in (None, ""):
        value, terms = read_money(amount, field="Сумма документа")
        if value is None:
            raise DocumentError(f"Сумма документа: «{terms}» не похоже на число")
        if value < 0:
            raise DocumentError("Сумма документа не бывает меньше нуля")
    elif kind in ("bill", "act"):
        value = _accrued(session, workspace, contract, period)
        if value is None:
            raise DocumentError(f"За {period:%m.%Y} по договору начисления нет - назовите сумму документа")
    if signed is not None and kind != "act":
        raise DocumentError("Дата подписания клиентом - у АВР")
    doc = find(session, workspace.id, contract.id, kind, period)
    before = state(doc)
    if doc is None:
        doc = Document(
            workspace_id=workspace.id, contract_id=contract.id, kind=kind, period=period, source="kort",
            source_key=f"{kind}:{contract.id}:{month_key(period)}", created_by=actor.user_id,
        )
        session.add(doc)
    doc.number = " ".join(str(number or "").split())
    doc.issued_on = day
    if value is not None or kind == "esf":
        doc.amount = value
    meta = dict(doc.meta or {})
    if kind == "act":
        meta["signed"] = signed.isoformat() if signed is not None else None
    meta["note"] = " ".join(str(note or "").split())
    doc.meta = {key: item for key, item in meta.items() if item not in (None, "")}
    doc.deleted_at = None
    doc.updated_by, doc.updated_at = actor.user_id, _now()
    session.flush()
    label = f"№ {doc.number}" if doc.number else "без номера"
    history.write(
        session, workspace, kind=f"core.document.{kind}", entity="contract", entity_id=contract.id,
        title=f"договор {contract.number or 'без номера'} · {KIND_TITLES[kind]} за {period:%m.%Y}: {label}"
        + (f" от {doc.issued_on:%d.%m.%Y}" if doc.issued_on else "")
        + (f", подписан {signed:%d.%m.%Y}" if signed is not None else ""),
        before=before, after=state(doc), actor=actor.email,
    )
    return doc


def remove(session: Session, workspace: Workspace, access: Access, actor: Actor, document_id: uuid.UUID) -> Document:
    """Снять документ месяца, записанный в KORT: факт журнала станет «снят», итоги - без него."""
    if not access.edit:
        raise PermissionError("Правка реестра вам не открыта - только просмотр")
    doc = session.get(Document, document_id)
    if doc is None or doc.workspace_id != workspace.id or doc.kind not in KINDS or doc.deleted_at is not None:
        raise DocumentError("Такого документа нет")
    if doc.source != "kort":
        raise DocumentError("Документ из 1С или таблицы снимается там, откуда пришёл")
    registry = Registry(session, workspace)
    contract = get_contract(session, workspace, doc.contract_id) if doc.contract_id else None
    if contract is not None:
        ensure_writable(session, registry, access, contract)
    before = state(doc)
    doc.deleted_at = _now()
    doc.updated_by, doc.updated_at = actor.user_id, _now()
    session.flush()
    history.write(
        session, workspace, kind=f"core.document.{doc.kind}.remove", entity="contract",
        entity_id=doc.contract_id or doc.id,
        title=f"{KIND_TITLES[doc.kind]} за {doc.period:%m.%Y} снят" if doc.period else f"{KIND_TITLES[doc.kind]} снят",
        before=before, actor=actor.email,
    )
    return doc


def revive(session: Session, workspace: Workspace, document_id: uuid.UUID, before: dict[str, Any]) -> None:
    """Обратный шаг точки восстановления: вернуть документ таким, каким он был до команды (пусто - снять)."""
    doc = session.get(Document, document_id)
    if doc is None or doc.workspace_id != workspace.id:
        return
    if not before:
        doc.deleted_at = _now()
    else:
        doc.deleted_at = None
        doc.number = before.get("number") or ""
        doc.issued_on = date.fromisoformat(before["day"]) if before.get("day") else None
        doc.amount = Decimal(before["amount"]) if before.get("amount") else None
        meta = {key: before.get(key) for key in ("signed", "note") if before.get(key)}
        doc.meta = meta
    doc.updated_at = _now()
    session.flush()


__all__ = ["KINDS", "KIND_TITLES", "DocumentError", "find", "out", "record", "remove", "revive", "state"]
