"""Ручной ввод: компания заводит данные сама, без файлов и 1С.

Входов в KORT три: 1С (`core/onec_start.py`), таблицы (`core/excel.py`,
реестр и выписки) и руки. Ручной путь - не третий набор правил, а те же
хранилища и те же функции ядра: наше юрлицо и его счёт, участник по БИН
страны компании (`core/participants.create`), договор (`contracts/service`),
оплата - операция журнала денег с решением «эта оплата по этому договору»,
документ месяца (`core/documents.py`), сальдо на начало учёта
(`core/opening.set_one`). Записанное руками проводится в журнал фактов тем же
путём, что пришедшее из 1С и выписок: итоги не знают, как факт попал в KORT.

Каждая функция отдаёт то, что сделала, и обратный шаг (`undo`): «Отменить»
пачки API модели возвращает и ручной ввод.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance import service as finance_service
from app.finance.contracts.models import ContractPayment, GroupEntity
from app.finance.contracts.service import (
    Access,
    Actor,
    Registry,
    ensure_writable,
    get_contract,
    read_money,
)
from app.finance.core import settings as core_settings
from app.finance.core.accruals import month_start
from app.finance.models import Account, Counterparty, Operation, Workspace
from app.finance.service import FinanceError


class EntryError(FinanceError):
    """Ручной ввод записан неверно - словами."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _require_setup(access: Access) -> None:
    if not access.setup:
        raise PermissionError("Это делает владелец или администратор")


# ── Начало учёта и источник оплат ────────────────────────────────────────────


def start(session: Session, workspace: Workspace, access: Access, actor: Actor, since: date | None) -> dict[str, Any]:
    """Начало учёта оплат: с этого дня оплаты гасят начисления, а что было раньше - входящее сальдо."""
    before = core_settings.payments(session, workspace).since
    value = core_settings.set_payments(session, workspace, access, actor, since=since)
    return {"since": value.since.isoformat() if value.since else None, "before": before.isoformat() if before else None}


def use_source(session: Session, workspace: Workspace, access: Access, actor: Actor, entity_id: uuid.UUID, source: str) -> dict[str, Any]:
    """Откуда оплаты нашего юрлица: журнал денег (выписки, руками) или 1С."""
    before = core_settings.payments(session, workspace).sources.get(str(entity_id)) or "journal"
    core_settings.set_payments(session, workspace, access, actor, entity_id=entity_id, source=source)
    return {"entity_id": str(entity_id), "source": source, "before": before}


# ── Наше юрлицо и его счёт ───────────────────────────────────────────────────


def add_entity(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *, name: str, code: str = "", bin: str = "",
    full_name: str = "", vat_payer: bool = False,
) -> dict[str, Any]:
    """Наше юрлицо руками: участник с этим именем или БИН становится нашим, иначе заводится."""
    from app.finance.contracts import setup
    from app.finance.core import participants

    _require_setup(access)
    target, number = name, ""
    if str(bin or "").strip():
        # БИН - сильнее имени: участник с этим номером уже есть - он и становится нашим.
        found = participants.create(session, workspace, access, actor, name=name, bin=bin, supplier=True)
        target, number = found["id"], found["bin"]
    entity = setup.add_entity(session, workspace, name=target, code=code, full_name=full_name, bin=number, vat_payer=vat_payer)
    party = session.get(Counterparty, entity.counterparty_id)
    title = party.name if party is not None else name
    history.write(
        session, workspace, kind="entity.add", entity="counterparty", entity_id=entity.counterparty_id,
        title=f"наше юрлицо «{title}»" + (f" ({entity.code})" if entity.code else ""),
        after={"name": title, "code": entity.code, "bin": entity.bin}, actor=actor.email,
    )
    return {"id": str(entity.counterparty_id), "name": title, "code": entity.code, "bin": entity.bin}


def add_account(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *, entity_id: uuid.UUID, name: str = "",
    currency: str | None = None, number: str = "",
) -> Account:
    """Счёт нашего юрлица: оплата на него - оплата договоров этого юрлица (`contracts/payments.allocate`)."""
    entity = session.get(GroupEntity, entity_id)
    if entity is None or entity.workspace_id != workspace.id or entity.archived_at is not None:
        raise EntryError("Такого нашего юрлица нет")
    party = session.get(Counterparty, entity_id)
    clean = " ".join(str(name or "").split()) or f"Счёт {party.name if party else entity.code}"
    account = finance_service.create_account(
        session, workspace, name=clean, currency=currency, number=str(number or "").strip(), group_entity_id=entity_id,
    )
    history.write(
        session, workspace, kind="account.create", entity="account", entity_id=account.id,
        title=f"счёт «{account.name}» юрлица «{party.name if party else ''}»", after={"name": account.name}, actor=actor.email,
    )
    return account


def _account(session: Session, workspace: Workspace, access: Access, actor: Actor, account_id: uuid.UUID | None,
             entity_id: uuid.UUID | None) -> Account:
    """Счёт оплаты: названный, иначе единственный счёт нашего юрлица; счетов нет - заводится на его имя."""
    if account_id is not None:
        account = session.get(Account, account_id)
        if account is None or account.workspace_id != workspace.id or account.archived_at is not None:
            raise EntryError("Такого счёта нет")
        return account
    if entity_id is None:
        raise EntryError("На какой счёт - укажите счёт или наше юрлицо")
    accounts = list(
        session.scalars(
            sa.select(Account).where(Account.workspace_id == workspace.id, Account.group_entity_id == entity_id, Account.archived_at.is_(None))
        )
    )
    if len(accounts) == 1:
        return accounts[0]
    if accounts:
        names = ", ".join(f"«{item.name}»" for item in accounts[:4])
        raise EntryError(f"У юрлица {len(accounts)} счетов ({names}) - укажите, на какой")
    # Юрлицо названо, а счёта у него нет: оплата руками должна куда-то лечь, и
    # счёт заводится один, на его имя, - не угадывая между несколькими.
    return add_account(session, workspace, access, actor, entity_id=entity_id)


# ── Оплаты ───────────────────────────────────────────────────────────────────


def _amount(raw: Any) -> Decimal:
    value, terms = read_money(raw, field="Сумма оплаты")
    if value is None:
        raise EntryError(f"Сумма оплаты: «{terms or raw}» не похоже на число")
    if value <= 0:
        raise EntryError("Сумма оплаты - больше нуля; возврат - направлением «out» у договора продажи")
    return value


def _decisions(session: Session, workspace_id: uuid.UUID, operation_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        {"contract_id": str(row.contract_id) if row.contract_id else None, "amount": _money(row.amount)}
        for row in session.scalars(
            sa.select(ContractPayment).where(ContractPayment.workspace_id == workspace_id, ContractPayment.operation_id == operation_id)
        )
    ]


def record_payment(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    *,
    direction: str,
    day: date,
    amount: Any,
    account_id: uuid.UUID | None = None,
    entity_id: uuid.UUID | None = None,
    party: Any = None,
    contract_id: uuid.UUID | None = None,
    month: str | None = None,
    comment: str = "",
    key: str | None = None,
) -> dict[str, Any]:
    """Оплата руками: поступление или списание на счёт нашего юрлица, с договором - решением человека.

    Договор назван - наше юрлицо и плательщик берутся из него, а оплата сразу
    ложится на договор (решение «эта оплата по этому договору», как в «Оплатах
    без договора»). Без договора оплата разносится тем же правилом, что строка
    выписки (`contracts/payments.allocate`). `key` - код клиента: повтор команды
    не заводит вторую оплату.
    """
    from app.finance.core import payments as payments_module

    if direction not in ("in", "out"):
        raise EntryError("Направление оплаты - «in» (поступление) или «out» (списание)")
    value = _amount(amount)
    external = f"model:{key}" if key else None
    if external is not None:
        existing = session.scalar(sa.select(Operation).where(Operation.workspace_id == workspace.id, Operation.external_key == external))
        if existing is not None:
            return {"operation_id": str(existing.id), "created": False}
    registry = Registry(session, workspace)
    contract = None
    ours: uuid.UUID | None = entity_id
    payer: Counterparty | None = None
    if contract_id is not None:
        contract = get_contract(session, workspace, contract_id)
        ensure_writable(session, registry, access, contract)
        if registry.is_own(contract.executor_id):
            ours, theirs = contract.executor_id, contract.customer_id
        elif registry.is_own(contract.customer_id):
            ours, theirs = contract.customer_id, contract.executor_id
        else:
            raise EntryError("У договора нет нашего юрлица - оплату не к чему отнести")
        if entity_id is not None and entity_id != ours:
            raise EntryError("Наше юрлицо оплаты - не сторона договора")
        payer = registry.parties_for([theirs]).get(theirs) if theirs else None
    if party not in (None, ""):
        resolved = registry.resolve_party(party, slot="customer" if direction == "in" else "executor", create=True)
        if resolved.ambiguous:
            names = ", ".join(f"«{item.name}»" for item in resolved.ambiguous[:3])
            raise EntryError(f"Под «{party}» несколько участников: {names} - укажите одного")
        payer = resolved.party
    account = _account(session, workspace, access, actor, account_id, ours)
    if contract is not None and account.group_entity_id not in (None, ours):
        raise EntryError(f"Счёт «{account.name}» - другого нашего юрлица, чем у договора")
    period = None
    if month:
        try:
            period = month_start(month)
        except (ValueError, IndexError) as exc:
            raise EntryError("«За какой месяц» - в виде 2026-07") from exc
    data = finance_service.OperationInput(
        kind="income" if direction == "in" else "expense",
        paid_at=day,
        amount=value,
        account_to_id=account.id if direction == "in" else None,
        account_from_id=account.id if direction == "out" else None,
        counterparty_id=payer.id if payer is not None else None,
        period_start=period,
        period_end=period,
        comment=" ".join(str(comment or "").split()),
        source="app",
        external_key=external,
    )
    operation = finance_service.create_operation(session, workspace, data, actor=actor.email)
    history.write(
        session, workspace, kind="operation.create", entity="operation", entity_id=operation.id,
        after=history.snapshot(operation), actor=actor.email,
    )
    if contract is not None:
        payments_module.decide(session, workspace, access, actor, operation.id, action="link", contract_id=contract.id)
    return {
        "operation_id": str(operation.id), "created": True, "account_id": str(account.id), "account": account.name,
        "party_id": str(payer.id) if payer is not None else None, "contract_id": str(contract.id) if contract is not None else None,
    }


#: Что правит «изменить оплату»: поле команды → поле операции.
PAYMENT_FIELDS = {"day": "paid_at", "amount": "amount", "comment": "comment"}


def _operation(session: Session, workspace: Workspace, operation_id: uuid.UUID) -> Operation:
    operation = session.get(Operation, operation_id)
    if operation is None or operation.workspace_id != workspace.id or operation.deleted_at is not None:
        raise EntryError("Такой оплаты нет")
    if operation.kind not in ("income", "expense"):
        raise EntryError("Перевод между своими счетами - не оплата")
    return operation


def payment_state(operation: Operation) -> dict[str, Any]:
    return {
        "day": operation.paid_at.isoformat(), "amount": _money(operation.amount), "comment": operation.comment or "",
        "party_id": str(operation.counterparty_id) if operation.counterparty_id else None,
        "month": operation.period_start.isoformat()[:7] if operation.period_start else None,
    }


def update_payment(
    session: Session, workspace: Workspace, access: Access, actor: Actor, operation_id: uuid.UUID, values: dict[str, Any],
) -> dict[str, Any]:
    """Правка оплаты: день, сумма, плательщик, «за какой месяц», комментарий."""
    operation = _operation(session, workspace, operation_id)
    before = payment_state(operation)
    changes: dict[str, Any] = {}
    if "day" in values:
        changes["paid_at"] = values["day"]
    if "amount" in values:
        changes["amount"] = _amount(values["amount"])
    if "comment" in values:
        changes["comment"] = " ".join(str(values["comment"] or "").split())
    if "month" in values:
        period = month_start(values["month"]) if values["month"] else None
        changes["period_start"] = changes["period_end"] = period
    if "party" in values:
        if values["party"] in (None, ""):
            changes["counterparty_id"] = None
        else:
            resolved = Registry(session, workspace).resolve_party(values["party"], slot="customer" if operation.kind == "income" else "executor")
            if resolved.ambiguous or resolved.party is None:
                raise EntryError(f"Плательщик «{values['party']}» не однозначен - укажите участника")
            changes["counterparty_id"] = resolved.party.id
    if not changes:
        raise EntryError("Нечего менять")
    snapshot = history.snapshot(operation)
    finance_service.update_operation(session, workspace, operation.id, changes, actor=actor.email)
    history.write(
        session, workspace, kind="operation.update", entity="operation", entity_id=operation.id,
        before=snapshot, after=history.snapshot(operation), actor=actor.email,
    )
    return {"operation_id": str(operation.id), "before": before, "after": payment_state(operation)}


def delete_payment(session: Session, workspace: Workspace, access: Access, actor: Actor, operation_id: uuid.UUID) -> dict[str, Any]:
    operation = _operation(session, workspace, operation_id)
    snapshot = history.snapshot(operation)
    finance_service.delete_operation(session, workspace, operation.id, actor=actor.email)
    history.write(session, workspace, kind="operation.delete", entity="operation", entity_id=operation.id, before=snapshot, actor=actor.email)
    return {"operation_id": str(operation.id), "deleted": True}


# ── Обратные шаги пачки («Отменить» API модели) ─────────────────────────────


def undo(session: Session, workspace: Workspace, access: Access, actor: Actor, kind: str, payload: dict[str, Any],
         done: list[str], skipped: list[str]) -> None:
    """Вернуть ручной ввод: шаг пачки `entry_*` - обратно (см. `contracts/restore._restore_step`)."""
    if kind == "entry_operation":
        operation = session.get(Operation, uuid.UUID(payload["operation_id"]))
        if operation is None or operation.workspace_id != workspace.id:
            skipped.append("оплата удалена насовсем - вернуть нечего")
            return
        if payload.get("created"):
            operation.deleted_at = _now()
            operation.version += 1
            done.append(f"оплата {operation.paid_at:%d.%m.%Y} убрана")
        elif payload.get("deleted"):
            operation.deleted_at = None
            operation.version += 1
            done.append(f"оплата {operation.paid_at:%d.%m.%Y} возвращена")
        else:
            before = payload.get("before") or {}
            operation.paid_at = date.fromisoformat(before["day"])
            operation.amount = Decimal(before["amount"])
            operation.amount_base = finance_service.check_money(operation.amount * Decimal(str(operation.rate or 1)))
            operation.comment = before.get("comment") or ""
            operation.counterparty_id = uuid.UUID(before["party_id"]) if before.get("party_id") else None
            operation.period_start = operation.period_end = month_start(before["month"]) if before.get("month") else None
            operation.version += 1
            done.append(f"оплата {operation.paid_at:%d.%m.%Y} - как была")
        session.flush()
    elif kind == "entry_decision":
        operation_id = uuid.UUID(payload["operation_id"])
        for row in session.scalars(
            sa.select(ContractPayment).where(ContractPayment.workspace_id == workspace.id, ContractPayment.operation_id == operation_id)
        ):
            session.delete(row)
        session.flush()
        for item in payload.get("before") or []:
            session.add(ContractPayment(
                workspace_id=workspace.id, operation_id=operation_id,
                contract_id=uuid.UUID(item["contract_id"]) if item.get("contract_id") else None,
                amount=Decimal(item["amount"]) if item.get("amount") else None, created_by=actor.user_id,
            ))
        session.flush()
        done.append("решение по оплате - как было")
    elif kind == "entry_document":
        from app.finance.core import documents

        documents.revive(session, workspace, uuid.UUID(payload["document_id"]), payload.get("before") or {})
        done.append("документ месяца - как был")
    elif kind == "entry_opening":
        from app.finance.core import opening

        before = payload.get("before")
        opening.set_one(
            session, workspace, access, actor, uuid.UUID(payload["contract_id"]), since=date.fromisoformat(payload["since"]),
            amount=Decimal(before) if before is not None else None, basis=payload.get("basis") or "accruals",
        )
        done.append("входящее сальдо - как было")
    elif kind == "entry_since":
        before = payload.get("before")
        core_settings.set_payments(session, workspace, access, actor, since=date.fromisoformat(before) if before else None)
        done.append("начало учёта - как было")
    elif kind == "entry_source":
        core_settings.set_payments(
            session, workspace, access, actor, entity_id=uuid.UUID(payload["entity_id"]), source=payload.get("before") or "journal",
        )
        done.append("источник оплат - как был")
    elif kind == "entry_summary":
        from app.finance.contracts import summary

        _require_setup(access)
        summary.restore(session, workspace.id, payload.get("before"), actor.user_id)
        done.append("сводная таблица - как была" if payload.get("before") else "сводная таблица отключена")
    elif kind == "entry_link":
        from app.finance.core import switch

        switch.set_source(
            session, workspace, access, actor, link=payload["link"], entity_id=uuid.UUID(payload["entity_id"]),
            source=payload.get("before") or "book",
        )
        switch.forget(workspace.id)
        done.append(f"{switch.LINK_TITLES[payload['link']]} - как было")
    elif kind == "entry_import":
        # Выписка, заведённая командой, - назад целиком: её операции в корзину.
        rows = list(
            session.scalars(
                sa.select(Operation).where(
                    Operation.workspace_id == workspace.id, Operation.import_batch_id == uuid.UUID(payload["batch_id"]),
                    Operation.deleted_at.is_(None),
                )
            )
        )
        for operation in rows:
            operation.deleted_at = _now()
            operation.version += 1
        session.flush()
        done.append(f"выписка убрана: {len(rows)} операций")
    elif kind == "entry_party":
        undo_party(session, workspace, payload, done, skipped)
    elif kind == "entry_formula":
        from app.finance.core import formulas

        _require_setup(access)
        formulas.restore(session, workspace, actor, payload.get("before") or [])
        done.append("формулы компании - как были")
    else:
        skipped.append("шаг, который эта версия KORT вернуть не умеет")


def party_state(party: Counterparty) -> dict[str, Any]:
    """Что команда участника может поменять - для шага `entry_party`: БИН, вид лица, полное имя, пометки."""
    return {
        "id": str(party.id),
        "bin": party.bin or "",
        "kind": party.kind or "",
        "full_name": party.full_name or "",
        "details": dict(party.details or {}),
    }


def undo_party(session: Session, workspace: Workspace, payload: dict[str, Any], done: list[str], skipped: list[str]) -> None:
    """Вернуть участника, как был до команды: БИН, полное имя, «не тот номер», «это разные»; новые написания - убрать."""
    from app.finance.contracts.models import Contract, CounterpartyName
    from app.finance.core import participants

    for item in payload.get("parties") or []:
        party = session.get(Counterparty, uuid.UUID(item["id"]))
        if party is None or party.workspace_id != workspace.id or party.merged_into_id is not None:
            skipped.append("участник сведён или удалён - вернуть нечего")
            continue
        bin_changed = (party.bin or "") != item.get("bin", "")
        if bin_changed and item.get("bin") and participants.owner_of_bin(session, workspace, item["bin"], but=party.id) is not None:
            skipped.append(f"«{party.name}»: БИН {item['bin']} уже у другого участника")
            continue
        party.bin, party.kind = item.get("bin", ""), item.get("kind", "")
        party.full_name = item.get("full_name", "")
        party.details = dict(item.get("details") or {})
        entity = session.get(GroupEntity, party.id)
        if entity is not None:
            entity.bin = party.bin
        if bin_changed:
            # БИН решает, чья оплата: договоры стороны - с новым номером, журнал проведёт их заново.
            participants._touch(
                session, workspace,
                session.scalars(sa.select(Contract.id).where(sa.or_(Contract.customer_id == party.id, Contract.executor_id == party.id))),
            )
        done.append(f"«{party.name}» - как был")
    for raw in payload.get("aliases") or []:
        alias = session.get(CounterpartyName, uuid.UUID(raw))
        if alias is not None and alias.workspace_id == workspace.id:
            session.delete(alias)
            done.append(f"написание «{alias.name}» убрано")
    session.flush()


__all__ = [
    "PAYMENT_FIELDS",
    "EntryError",
    "add_account",
    "add_entity",
    "delete_payment",
    "party_state",
    "payment_state",
    "record_payment",
    "start",
    "undo",
    "undo_party",
    "update_payment",
    "use_source",
]
