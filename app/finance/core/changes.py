"""Изменения условий договора от ядра: статус с даты и поправка месяца.

Начисления считаются из условий (`core/accruals.py`), поэтому сказать «в
июле договор стоял», «в августе начислить 50 000 вместо 100 000» можно только
условием: статусом с даты или поправкой месяца. Оба пишутся здесь - с правом
правки договора и событием журнала; сверка с книгой (`core/shadow.py`)
предлагает их, записывает человек.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts.models import Contract, ContractAmendment, ListValue
from app.finance.contracts.service import (
    Access,
    Actor,
    NotFound,
    Registry,
    _finish,
    _next_amendment_position,
    ensure_writable,
    get_contract,
    read_money,
    today,
)
from app.finance.core.accruals import Ledger, month_key, month_start
from app.finance.core.models import Document
from app.finance.models import Workspace
from app.finance.service import FinanceError

#: Откуда изменение: человек в карточке или предложение сверки с книгой.
ORIGINS = ("change", "book")


class ChangeError(FinanceError):
    """Отказ с причиной для человека."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_edit(access: Access) -> None:
    if not access.edit:
        raise PermissionError("Правка реестра вам не открыта - только просмотр")


def _label(contract: Contract | None) -> str:
    """«№ ОБО-45» - номер договора словами; «№» в номере часто уже есть."""
    number = (contract.number or "").strip() if contract is not None else ""
    if not number:
        return "договор без номера"
    return number if number.startswith("№") else f"№ {number}"


def _contract(session: Session, workspace: Workspace, access: Access, contract_id: uuid.UUID) -> tuple[Contract, Registry]:
    _require_edit(access)
    registry = Registry(session, workspace)
    contract = get_contract(session, workspace, contract_id, for_update=True)
    ensure_writable(session, registry, access, contract)
    return contract, registry


def status_by_phase(session: Session, workspace_id: uuid.UUID, phase: str) -> ListValue | None:
    """Первое значение статуса с этой фазой - по порядку списка компании."""
    rows = session.scalars(
        sa.select(ListValue)
        .where(
            ListValue.workspace_id == workspace_id,
            ListValue.field_key == "status",
            ListValue.archived_at.is_(None),
        )
        .order_by(ListValue.position, ListValue.created_at)
    )
    return next((row for row in rows if (row.meaning or {}).get("phase") == phase), None)


def status_from(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    contract_id: uuid.UUID,
    status_id: uuid.UUID,
    day: date,
    *,
    origin: str = "change",
    summary: str = "",
) -> ContractAmendment:
    """Статус договора с даты: приостановка, возобновление, расторжение.

    Как изменение с даты стороны и суммы: дата прошла - соглашение применено
    сразу, и статус договора становится новым, если это последнее по дате
    изменение статуса; дата впереди - соглашение ждёт своего дня
    (`service.apply_due`). «Было» - статус, действовавший накануне даты, а не
    текущий: пауза в прошлом не переписывает то, что было до неё.
    """
    if origin not in ORIGINS:
        raise ChangeError("Откуда изменение: из карточки или из таблицы")
    contract, registry = _contract(session, workspace, access, contract_id)
    value = registry.values.get(status_id)
    if value is None or value.field_key != "status":
        raise ChangeError("Такого статуса в реестре нет")
    ledger = Ledger(session, workspace.id, [contract])
    timeline = ledger.timeline(contract)
    before = timeline.value("status", date.fromordinal(day.toordinal() - 1))
    if before == str(status_id):
        raise ChangeError(f"С {day:%d.%m.%Y} статус и так «{value.value}»")
    amendment = ContractAmendment(
        workspace_id=workspace.id,
        contract_id=contract.id,
        summary=summary.strip(),
        effect="status",
        effective_from=day,
        before={"status": before},
        after={"status": str(status_id)},
        origin=origin,
        position=_next_amendment_position(session, contract.id),
        created_by=actor.user_id,
    )
    session.add(amendment)
    changed: list[str] = []
    if day <= today():
        amendment.applied_at = _now()
        later = session.scalar(
            sa.select(sa.func.count())
            .select_from(ContractAmendment)
            .where(
                ContractAmendment.contract_id == contract.id,
                ContractAmendment.effect == "status",
                ContractAmendment.effective_from > day,
                ContractAmendment.effective_from <= today(),
                ContractAmendment.id != amendment.id,
            )
        )
        if not later:
            contract.status_id = status_id
            changed.append("status")
    session.flush()
    _finish(session, registry, contract, actor, changed)
    before_title = registry.values.get(uuid.UUID(before)).value if before and uuid.UUID(before) in registry.values else "-"
    history.write(
        session, workspace, kind="contract.status_from", entity="contract", entity_id=contract.id,
        title=(
            f"договор {contract.number or ''} · статус с {day:%d.%m.%Y}: {before_title} → {value.value}"
            + (" (по сводной таблице)" if origin == "book" else "")
        ).replace("  ", " "),
        before={"status": before}, after={"status": str(status_id), "effective_from": day.isoformat(), "origin": origin},
        actor=actor.email,
    )
    return amendment


def set_adjustment(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    contract_id: uuid.UUID,
    month: str,
    *,
    amount: Any = None,
    skip: bool = False,
    reason: str = "",
    source: str = "kort",
) -> Document:
    """Поправка начисления месяца: другая сумма или «не начислять». Одна на договор и месяц."""
    contract, registry = _contract(session, workspace, access, contract_id)
    try:
        period = month_start(month)
    except (ValueError, IndexError) as exc:
        raise ChangeError("Месяц поправки - в виде 2026-07") from exc
    value: Decimal | None = None
    if not skip:
        value, terms = read_money(amount, field="Сумма поправки")
        if value is None:
            raise ChangeError(f"Сумма поправки: «{terms}» не похоже на число" if terms else "Укажите сумму или «не начислять»")
        if value < 0:
            raise ChangeError("Сумма поправки не бывает меньше нуля - для возврата есть свой документ")
    text = " ".join(str(reason or "").split())
    if not text:
        raise ChangeError("Поправка - решение человека: напишите причину")
    doc = session.scalar(
        sa.select(Document).where(
            Document.workspace_id == workspace.id,
            Document.contract_id == contract.id,
            Document.kind == "adjustment",
            Document.period == period,
            Document.deleted_at.is_(None),
        )
    )
    before = (
        {"amount": format(doc.amount, "f") if doc.amount is not None else None, "skip": doc.skip, "reason": doc.reason}
        if doc is not None
        else {}
    )
    if doc is None:
        doc = Document(
            workspace_id=workspace.id, contract_id=contract.id, kind="adjustment", period=period,
            source=source, created_by=actor.user_id,
        )
        session.add(doc)
    doc.amount, doc.skip, doc.reason = value, bool(skip), text
    doc.updated_by, doc.updated_at = actor.user_id, _now()
    session.flush()
    _finish(session, registry, contract, actor, [])
    label = "не начислять" if skip else f"{value:,.2f}".replace(",", " ").replace(".00", "")
    history.write(
        session, workspace, kind="contract.adjustment", entity="contract", entity_id=contract.id,
        title=f"договор {contract.number or ''} · поправка {period:%m.%Y}: {label} - {text}".replace("  ", " "),
        before=before, after={"month": month_key(period), "amount": format(value, "f") if value is not None else None,
                              "skip": bool(skip), "reason": text},
        actor=actor.email,
    )
    return doc


def remove_adjustment(
    session: Session, workspace: Workspace, access: Access, actor: Actor, contract_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    contract, registry = _contract(session, workspace, access, contract_id)
    doc = session.get(Document, document_id)
    if doc is None or doc.contract_id != contract.id or doc.kind != "adjustment" or doc.deleted_at is not None:
        raise NotFound("Поправки нет")
    doc.deleted_at = _now()
    doc.updated_by = actor.user_id
    session.flush()
    _finish(session, registry, contract, actor, [])
    history.write(
        session, workspace, kind="contract.adjustment.remove", entity="contract", entity_id=contract.id,
        title=f"договор {contract.number or ''} · поправка {doc.period:%m.%Y} убрана - месяц по договору".replace("  ", " "),
        before={"month": month_key(doc.period), "amount": format(doc.amount, "f") if doc.amount is not None else None,
                "skip": doc.skip, "reason": doc.reason},
        actor=actor.email,
    )


def history_change(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    contract_id: uuid.UUID,
    key: str,
    *,
    before: Any,
    after: Any,
    until: date,
    origin: str = "book",
) -> ContractAmendment:
    """«До даты было другое»: изменение, которое уже случилось, а договор о нём не знает.

    Клиента перевели на другое ТОО с августа, и в договоре стоит уже новое
    ТОО - но июль, по договору, тоже его. Соглашение с «было» и «стало»
    возвращает договору прошлое; текущее значение не меняется.
    """
    if key not in ("amount", "executor", "customer"):
        raise ChangeError("История «до даты» - для суммы и сторон")
    contract, registry = _contract(session, workspace, access, contract_id)
    if key == "amount":
        amount, _terms = read_money(after, field="Сумма")
        old, _old_terms = read_money(before, field="Сумма")
        before_value = format(old, "f") if old is not None else None
        after_value: dict[str, Any] = {"amount": format(amount, "f") if amount is not None else None, "amount_terms": ""}
        label = f"{old:,.2f}".replace(",", " ").replace(".00", "") if old is not None else "-"
    else:
        ids = [uuid.UUID(str(value)) for value in (before, after) if value]
        found = registry.parties_for(ids)
        if any(item not in found for item in ids):
            raise ChangeError("Такой стороны нет")
        before_value = str(before) if before else None
        after_value = {key: str(after) if after else None}
        label = found[uuid.UUID(before_value)].name if before_value else "-"
    amendment = ContractAmendment(
        workspace_id=workspace.id,
        contract_id=contract.id,
        summary="по сводной таблице" if origin == "book" else "",
        effect=key,
        effective_from=until,
        before={key: before_value},
        after=after_value,
        origin=origin,
        position=_next_amendment_position(session, contract.id),
        created_by=actor.user_id,
        # Уже случилось и уже стоит в договоре: применять нечего.
        applied_at=_now(),
    )
    session.add(amendment)
    session.flush()
    _finish(session, registry, contract, actor, [])
    history.write(
        session, workspace, kind="contract.history", entity="contract", entity_id=contract.id,
        title=(
            f"договор {contract.number or ''} · до {until:%d.%m.%Y} "
            + ("сумма была " if key == "amount" else "исполнитель был " if key == "executor" else "заказчик был ")
            + label
            + (" (по сводной таблице)" if origin == "book" else "")
        ).replace("  ", " "),
        before={key: before_value}, after={**after_value, "until": until.isoformat()}, actor=actor.email,
    )
    return amendment


def apply_proposal(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    contract_id: uuid.UUID,
    kind: str,
    value: dict[str, Any],
    *,
    option: str = "",
) -> str:
    """Записать предложение сверки (`core/shadow.py`) - обычным изменением договора.

    Возвращает, что сделано, словами.
    """
    from app.finance.contracts.service import Mode, patch

    contract, registry = _contract(session, workspace, access, contract_id)
    seq = contract.seq
    number = _label(contract)

    def day(key: str) -> date:
        try:
            return date.fromisoformat(str(value[key]))
        except (KeyError, ValueError) as exc:
            raise ChangeError("В предложении нет даты") from exc

    def dated(field: str, raw: Any, since: date) -> None:
        mode = Mode(kind="from_date", effective_from=since, summary="по сводной таблице", origin="book")
        patch(session, workspace, access, actor, contract.id, {field: raw}, known_seq=None, mode=mode)

    if kind in ("billing", "period_day", "billing_from"):
        patch(session, workspace, access, actor, contract.id, {kind: value.get(kind)}, known_seq=seq)
        return f"{number}: записано"
    if kind == "amount_fix":
        patch(session, workspace, access, actor, contract.id, {"amount": value.get("amount")}, known_seq=seq, mode=Mode(kind="fix"))
        return f"{number}: сумма договора исправлена"
    if kind == "amount_from":
        if option == "month":
            since = day("from")
            set_adjustment(
                session, workspace, access, actor, contract.id, month_key(since),
                amount=value.get("amount"), reason="как в сводной таблице",
            )
            return f"{number}: поправка месяца"
        dated("amount", value.get("amount"), day("from"))
        return f"{number}: сумма с {day('from'):%d.%m.%Y}"
    if kind == "executor_from":
        dated("executor", {"id": value.get("executor")}, day("from"))
        return f"{number}: исполнитель с {day('from'):%d.%m.%Y}"
    if kind in ("amount_before", "executor_before"):
        key = "amount" if kind == "amount_before" else "executor"
        history_change(
            session, workspace, access, actor, contract.id, key,
            before=value.get(key), after=value.get("after"), until=day("until"),
        )
        return f"{number}: история до {day('until'):%d.%m.%Y}"
    if kind == "adjust":
        set_adjustment(
            session, workspace, access, actor, contract.id, str(value.get("month") or ""),
            amount=value.get("amount"), skip=bool(value.get("skip")), reason="как в сводной таблице",
        )
        return f"{number}: поправка месяца"
    if kind in ("pause", "stop") and (kind == "pause" or option == "pause"):
        since = day("from")
        suspended = status_by_phase(session, workspace.id, "suspended")
        if suspended is None:
            raise ChangeError("В статусах реестра нет приостановки - заведите статус со смыслом «приостановлен»")
        timeline = Ledger(session, workspace.id, [contract]).timeline(contract)
        was = timeline.value("status", date.fromordinal(since.toordinal() - 1))
        status_from(session, workspace, access, actor, contract.id, suspended.id, since, origin="book", summary="по сводной таблице")
        if kind == "pause" and value.get("to") and was:
            status_from(session, workspace, access, actor, contract.id, uuid.UUID(was), day("to"), origin="book", summary="по сводной таблице")
        return f"{number}: приостановка с {since:%d.%m.%Y}"
    if kind in ("stop", "end"):
        end = day("end_date")
        phase = Ledger(session, workspace.id, [contract]).phase_of(contract.status_id)
        end_kind = "fulfilled" if phase == "fulfilled" else "terminated"
        patch(session, workspace, access, actor, contract.id, {"end_date": end.isoformat(), "end_kind": end_kind}, known_seq=None)
        ended = status_by_phase(session, workspace.id, "terminated")
        if kind == "stop" and ended is not None and phase not in ("terminated", "fulfilled"):
            status_from(
                session, workspace, access, actor, contract.id, ended.id, date.fromordinal(end.toordinal() + 1),
                origin="book", summary="по сводной таблице",
            )
        return f"{number}: окончание {end:%d.%m.%Y}"
    raise ChangeError("Такого предложения нет")


def apply_all(
    session: Session, workspace: Workspace, access: Access, actor: Actor, book: Any, kind: str
) -> dict[str, Any]:
    """Все предложения одного простого вида разом - с точкой восстановления.

    Простые - поле договора без даты (вид начисления, день периода, начало
    начислений): «Восстановление» вернёт прежние значения тем же механизмом,
    что массовую правку листа. Изменения с даты и поправки месяцев - по одному:
    у каждого свой ответ.
    """
    from app.finance.contracts import restore
    from app.finance.core import shadow as shadow_module

    _require_edit(access)
    if kind not in shadow_module.BULK_KINDS:
        raise ChangeError("Разом применяются только вид начисления, день периода и начало начислений")
    result = shadow_module.reconcile(session, workspace, access, book)
    proposals = [item for item in result.proposals() if item.kind == kind]
    if not proposals:
        return {"done": 0, "skipped": [], "point": None}
    titles = {"billing": "вид начисления", "period_day": "день периода", "billing_from": "начало начислений"}
    point = restore.values_point(
        session, workspace, access, actor,
        items=[{"id": str(item.contract_id), "keys": [kind]} for item in proposals],
        title=f"сверка с таблицей: {titles[kind]} у {len(proposals)} договоров",
    )
    done = 0
    skipped: list[str] = []
    for item in proposals:
        try:
            with session.begin_nested():
                apply_proposal(session, workspace, access, actor, item.contract_id, kind, item.value)
            done += 1
        except (FinanceError, PermissionError) as exc:
            skipped.append(f"{_label(session.get(Contract, item.contract_id))}: {exc}")
    return {"done": done, "skipped": skipped, "point": point.get("point")}


def create_from_book(
    session: Session, workspace: Workspace, access: Access, actor: Actor, book: Any, key: str
) -> Contract:
    """Договор книги, которого нет в реестре, - завести по его строкам.

    Из строк берётся то, что в них есть: номер, клиент, наше юрлицо по коду,
    сумма последнего месяца, дата договора, «Период с» первого месяца и вид
    начисления по числу месяцев. Вида и предмета услуги из сокращений книги
    не угадываем - их дописывают в карточке.
    """
    from collections import Counter

    from app.finance.contracts.service import create
    from app.finance.core.book import Matcher
    from app.finance.core.shadow import Reconciler
    from app.finance.layout import norm

    _require_edit(access)
    reconciler = Reconciler(session, workspace, access, book)
    rows = reconciler.unknown_rows(key)
    if not rows:
        raise ChangeError("Таких строк в таблице уже нет - сверка обновилась")
    first, last = rows[0], rows[-1]
    months = sorted({row.month for row in rows})
    spans = [(row.period_to - row.period_from).days for row in rows if row.period_from and row.period_to]
    monthly = len(months) >= 2 or bool(spans and all(27 <= span <= 31 for span in spans))
    values: dict[str, Any] = {"number": first.number, "customer": first.customer, "billing": "month" if monthly else "total"}
    entity = Matcher(session, workspace.id).entity_by_code.get(norm(first.own))
    if entity is not None:
        values["executor"] = {"id": str(entity)}
    if last.amount is not None:
        values["amount"] = format(last.amount, "f")
    if first.signed_at:
        values["signed_at"] = first.signed_at.isoformat()
    if first.period_from:
        values["billing_from"] = first.period_from.isoformat()
        days = Counter(row.period_from.day for row in rows if row.period_from)
        if monthly and days:
            values["period_day"] = days.most_common(1)[0][0]
    contract = create(session, workspace, access, actor, values)
    history.write(
        session, workspace, kind="contract.from_book", entity="contract", entity_id=contract.id,
        title=f"договор {contract.number or ''} заведён по сводной таблице: {first.customer}, {len(months)} мес.".replace("  ", " "),
        after={"lines": [row.line for row in rows], "months": months}, actor=actor.email,
    )
    return contract


def transfer(
    session: Session, workspace: Workspace, access: Access, actor: Actor, moves: list[Any]
) -> dict[str, Any]:
    """Перевод договоров к другому нашему юрлицу с дня - пачкой, с точкой восстановления.

    Каждый перевод - обычное изменение «исполнитель с даты» (`service.patch`
    с режимом `from_date`): видно в «Соглашениях» договора, день настал -
    исполнитель меняется, впереди - ждёт своего дня. Точка `core_transfer`
    помнит, что заведено, и «Восстановление» разносит всё обратно.
    """
    from app.finance.contracts import restore
    from app.finance.contracts.service import Mode, patch

    _require_edit(access)
    if not access.setup:
        raise PermissionError("Переводит клиентов между юрлицами владелец или администратор")
    registry = Registry(session, workspace)
    items: list[dict[str, Any]] = []
    skipped: list[str] = []
    targets: set[uuid.UUID] = set()
    for move in moves:
        if not registry.is_own(move.target):
            raise ChangeError("Переводят только к нашему юрлицу")
        try:
            contract = get_contract(session, workspace, move.contract_id)
        except NotFound:
            skipped.append("договор удалён")
            continue
        label = _label(contract)
        if contract.executor_id == move.target:
            skipped.append(f"{label}: исполнитель и так этот")
            continue
        before = contract.executor_id
        try:
            with session.begin_nested():
                mode = Mode(kind="from_date", effective_from=move.since, summary="перевод клиента между юрлицами")
                patch(session, workspace, access, actor, contract.id, {"executor": {"id": str(move.target)}}, known_seq=None, mode=mode)
        except (FinanceError, PermissionError) as exc:
            skipped.append(f"{label}: {exc}")
            continue
        amendment = session.scalars(
            sa.select(ContractAmendment)
            .where(ContractAmendment.contract_id == contract.id, ContractAmendment.effect == "executor")
            .order_by(ContractAmendment.position.desc())
        ).first()
        items.append(
            {
                "contract_id": str(contract.id),
                "amendment_id": str(amendment.id) if amendment else None,
                "executor_before": str(before) if before else None,
                "label": label,
            }
        )
        targets.add(move.target)
    if not items:
        return {"done": 0, "skipped": skipped, "point": None}
    names = ", ".join(sorted(registry.own[item].code or "" for item in targets if item in registry.own)) or "наше юрлицо"
    title = f"перевод к {names}: {len(items)} договор(ов) с {min(move.since for move in moves):%d.%m.%Y}"
    point = restore._point(session, workspace, actor, "core_transfer", title, {"items": items})
    history.write(
        session, workspace, kind="core.transfer", entity="contract", entity_id=uuid.UUID(items[0]["contract_id"]),
        title=title, after={"items": [item["label"] for item in items], "skipped": skipped}, actor=actor.email,
    )
    return {"done": len(items), "skipped": skipped, "point": {"id": str(point.id), "title": point.title, "kind": point.kind}}


def undo_transfer(
    session: Session, workspace: Workspace, access: Access, actor: Actor, payload: dict[str, Any], done: list[str], skipped: list[str]
) -> None:
    """Вернуть перевод: соглашения «исполнитель с даты» убрать, применённые - вернуть исполнителя."""
    from app.finance.contracts.service import Mode, patch

    for item in payload.get("items") or []:
        label = item.get("label") or "договор"
        contract = session.get(Contract, uuid.UUID(item["contract_id"]))
        if contract is None or contract.deleted_at is not None:
            skipped.append(f"{label}: договор удалён")
            continue
        amendment = session.get(ContractAmendment, uuid.UUID(item["amendment_id"])) if item.get("amendment_id") else None
        moved_to = (amendment.after or {}).get("executor") if amendment is not None else None
        applied = amendment is not None and amendment.applied_at is not None
        if amendment is not None:
            session.delete(amendment)
            session.flush()
        if applied and moved_to and str(contract.executor_id) == moved_to:
            before = item.get("executor_before")
            try:
                with session.begin_nested():
                    patch(
                        session, workspace, access, actor, contract.id, {"executor": {"id": before} if before else None},
                        known_seq=None, mode=Mode(kind="fix"),
                    )
            except (FinanceError, PermissionError) as exc:
                skipped.append(f"{label}: {exc}")
                continue
        done.append(f"{label}: исполнитель прежний")


__all__ = [
    "ChangeError",
    "ORIGINS",
    "apply_all",
    "transfer",
    "undo_transfer",
    "apply_proposal",
    "create_from_book",
    "history_change",
    "remove_adjustment",
    "set_adjustment",
    "status_by_phase",
    "status_from",
]
