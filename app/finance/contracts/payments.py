"""Оплаты по договору - из журнала операций.

«Оплачено» и «Остаток» в реестре BBC были колонками, которые вели руками по
выпискам («Потом настроим формулы и свяжем с финансами» - «Настройки реестра»,
26.09.2026). Здесь они считаются из журнала: выписки, загруженные в «Финансы»,
и есть источник.

Разнесение считается при чтении и не хранится - как принадлежность договора к
листам: новая выписка, правка стороны или даты договора сразу меняют ответ, и
старых меток, разъехавшихся с правдой, не бывает. Хранится только решение
человека (`ContractPayment`) - там, где система решить не может.

Какая операция - оплата по договору
───────────────────────────────────
* **направление**: у продажи (наше юрлицо - исполнитель) - поступление от
  заказчика; у покупки (наше юрлицо только заказчик) - списание исполнителю;
* **сторона**: тот же контрагент, его подтверждённое написание, то же имя без
  кавычек, регистра и пробелов (`party_key`) или тот же БИН. Банк пишет
  «ТОО "ТЕПЛОБЕТОНСТРОЙ"», реестр - «ТОО Теплобетонстрой», и выписка заводит
  себе отдельную запись контрагента: по одному идентификатору оплаты не
  нашлись бы никогда. Латиница-двойник - та же буква (`visual_key`): банк
  набрал «ТОО "ЕСО Bazar"» кириллицей, реестр - «ТОО ECO Bazar» латиницей, и 26
  оплат на 14,4 млн на копии прода лежали без договора (25.10.2026);
* **наше юрлицо**: счёт операции привязан к юрлицу договора («Наши юрлица» →
  счета). Счёт без юрлица не мешает - тогда решает сторона.

Подходит один договор - оплата его. Несколько (у клиента старый и новый
договор с тем же ТОО) - берётся тот, чей срок содержит дату платежа: от даты
договора без месяца (предоплата) до окончания плюс два месяца (последний
месяц платят после). Таких несколько - тот, чья сумма (у абонентского -
месячная) ровно равна платежу; нет такого - единственный с известной датой
договора (договор без даты подходит по сроку к любому платежу и иначе делал
бы спорным всё). Нашёлся один - его. Иначе платёж **спорный**:
виден у каждого кандидата с «Отнести к этому договору» и в «Оплачено» не
входит - угадывать нельзя (правило проекта «не угадывать»).

**Номер договора из источника** (`Operation.contract_ref`, «№ЮО/84» в
журнале, 25.10.2026) - довод сильнее плательщика: платит нередко не сам
клиент (Kaspi Pay от физлица, другое ТОО), и по имени такая оплата не
находилась никогда. Номер ищется среди договоров того же направления и, если
счёт привязан к юрлицу, этого юрлица; другая рука («ОБО-62» вместо «ОБО/62») -
тот же номер, если такой договор один. Плательщик не узнан - решает номер.
Плательщик узнан и это сторона договора с этим номером - тем более. Узнан, но
это другой клиент - спорный: номер и плательщик спорят, решает человек.
Номера среди договоров юрлица нет («№ЮО/35-2», которого нет в реестре, или
договор другого юрлица) - оплата ничья: источник назвал другой договор, и
отдать её договору того же клиента по имени значило бы угадать (так оплаты
«ЮО/35-2» ложились в «ЮО/35», 25.10.2026). Её решает человек в «Оплатах без
договора».

«Остаток» - только у договора на всю сумму: сумма минус оплачено. У
абонентских и аренды остаток - это начисления по месяцам; в файле BBC эти
колонки у них пусты, и выдумывать число здесь не лучше.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts.fields import number_key, party_key
from app.finance.contracts.models import Contract, ContractPayment, CounterpartyName
from app.finance.contracts.service import (
    Access,
    Actor,
    NotFound,
    Registry,
    ensure_writable,
    get_contract,
    people_of,
    visible_to,
)
from app.finance.models import Account, Counterparty, Operation, Workspace
from app.finance.onec.match import number_loose, visual_key
from app.finance.service import FinanceError

#: Срок договора для выбора между несколькими: предоплата - за месяц до даты
#: договора, последний месяц - в два месяца после окончания.
WINDOW_BEFORE = timedelta(days=31)
WINDOW_AFTER = timedelta(days=62)


@dataclass(frozen=True)
class Side:
    """Договор глазами выписки: чьи деньги и в какую сторону."""

    contract_id: uuid.UUID
    ours: uuid.UUID
    theirs: uuid.UUID
    kind: str
    start: date | None
    end: date | None
    #: Сумма договора (у абонентского - месячная): платёж ровно на неё - довод.
    amount: Decimal | None = None
    #: Номер договора без оформления (`number_key`): по нему находит оплата с «№» источника.
    number: str = ""

    def holds(self, day: date) -> bool:
        if self.start is not None and day < self.start - WINDOW_BEFORE:
            return False
        if self.end is not None and day > self.end + WINDOW_AFTER:
            return False
        return True


#: Поля операции, которые читают разнесение и его потребители (строка, а не объект ORM).
OPERATION_COLUMNS = (
    Operation.id, Operation.kind, Operation.paid_at, Operation.amount_base, Operation.counterparty_id,
    Operation.account_to_id, Operation.account_from_id, Operation.contract_ref, Operation.comment,
)


@dataclass
class Allocation:
    #: Договор → операции (строки `OPERATION_COLUMNS`), отнесённые к нему, и как:
    #: «auto» - по стороне, «number» - по номеру договора из источника, «manual» - решил человек.
    paid: dict[uuid.UUID, list[tuple[Any, str]]] = field(default_factory=dict)
    #: Договор → спорные операции, для которых он один из кандидатов.
    open: dict[uuid.UUID, list[Any]] = field(default_factory=dict)


def _sides(contracts: Iterable[Contract], registry: Registry) -> list[Side]:
    out = []
    for contract in contracts:
        if contract.deleted_at is not None or contract.executor_id is None or contract.customer_id is None:
            continue
        if registry.is_own(contract.executor_id):
            ours, theirs, kind = contract.executor_id, contract.customer_id, "income"
        elif registry.is_own(contract.customer_id):
            ours, theirs, kind = contract.customer_id, contract.executor_id, "expense"
        else:
            continue
        out.append(
            Side(
                contract.id, ours, theirs, kind, contract.signed_at, contract.end_date, contract.amount,
                number_key(contract.number),
            )
        )
    return out


def _narrow(candidates: dict[uuid.UUID, Side], operation: Any) -> dict[uuid.UUID, Side]:
    """Из нескольких договоров - по сроку, сумме и известной дате (см. описание модуля)."""
    if len(candidates) < 2:
        return candidates
    fitting = {cid: side for cid, side in candidates.items() if side.holds(operation.paid_at)} or candidates
    exact = {cid: side for cid, side in fitting.items() if side.amount == operation.amount_base}
    dated = {cid: side for cid, side in fitting.items() if side.start is not None}
    if len(fitting) == 1:
        return fitting
    if len(exact) == 1:
        return exact
    if len(dated) == 1:
        # Договор без даты «подходит по сроку» к любому платежу: без этой
        # ступени заведённый без даты второй договор клиента делал спорными
        # все его прошлые оплаты.
        return dated
    return fitting


def allocate(
    session: Session,
    workspace: Workspace,
    registry: Registry,
    contracts: Iterable[Contract],
    *,
    operations: Iterable[uuid.UUID] | None = None,
) -> Allocation:
    """Разнести операции журнала по договорам (см. описание модуля).

    `operations` - только эти операции (журнал фактов проводит изменённые):
    куда ляжет одна операция, от других операций не зависит.
    """
    result = Allocation()
    picked = list(operations) if operations is not None else None
    if picked is not None and not picked:
        return result
    sides = _sides(contracts, registry)
    if not sides:
        return result
    theirs_ids = {side.theirs for side in sides}
    by_side: dict[tuple[uuid.UUID, str], list[Side]] = {}
    for side in sides:
        by_side.setdefault((side.theirs, side.kind), []).append(side)

    # Как выписка может назвать сторону договора.
    by_key: dict[str, set[uuid.UUID]] = {}
    by_bin: dict[str, set[uuid.UUID]] = {}
    for party_id, party in registry.parties_for(theirs_ids).items():
        by_key.setdefault(party_key(party.name), set()).add(party_id)
        by_key.setdefault(visual_key(party.name), set()).add(party_id)
        entity = registry.own.get(party_id)
        # БИН - колонка участника (ядро, 24.10.2026); `details.bin` - прежнее место.
        bin_value = str(
            party.bin or (party.details or {}).get("bin") or (entity.bin if entity is not None else "") or ""
        ).strip()
        if bin_value:
            by_bin.setdefault(bin_value, set()).add(party_id)
    for normalized, name, party_id in session.execute(
        sa.select(CounterpartyName.normalized, CounterpartyName.name, CounterpartyName.counterparty_id).where(
            CounterpartyName.workspace_id == workspace.id, CounterpartyName.counterparty_id.in_(theirs_ids)
        )
    ):
        by_key.setdefault(normalized, set()).add(party_id)
        by_key.setdefault(visual_key(name), set()).add(party_id)

    by_number: dict[tuple[str, str], list[Side]] = {}
    by_loose: dict[tuple[str, str], list[Side]] = {}
    for side in sides:
        if side.number:
            by_number.setdefault((side.number, side.kind), []).append(side)
            by_loose.setdefault((number_loose(side.number), side.kind), []).append(side)

    entity_of_account = dict(
        session.execute(
            sa.select(Account.id, Account.group_entity_id).where(Account.workspace_id == workspace.id)
        ).all()
    )
    # Строками нужных колонок, а не объектами: на 6 000 операций объекты ORM
    # с `raw` выписки были половиной проведения журнала (27.10.2026).
    query = sa.select(*OPERATION_COLUMNS).where(
        Operation.workspace_id == workspace.id,
        Operation.deleted_at.is_(None),
        Operation.status == "fact",
        Operation.kind.in_(("income", "expense")),
        sa.or_(Operation.counterparty_id.is_not(None), Operation.contract_ref != ""),
    )
    if picked is not None:
        query = query.where(Operation.id.in_(picked))
    operations = list(session.execute(query))
    if not operations:
        return result
    payer_ids = {operation.counterparty_id for operation in operations if operation.counterparty_id}
    payers = {
        party.id: party
        for party in session.scalars(sa.select(Counterparty).where(Counterparty.id.in_(payer_ids)))
    } if payer_ids else {}
    manual = dict(
        session.execute(
            sa.select(ContractPayment.operation_id, ContractPayment.contract_id).where(
                ContractPayment.workspace_id == workspace.id
            )
        ).all()
    )
    known = {side.contract_id for side in sides}

    for operation in operations:
        if operation.id in manual:
            chosen = manual[operation.id]
            if chosen in known:
                result.paid.setdefault(chosen, []).append((operation, "manual"))
            continue
        payer = payers.get(operation.counterparty_id) if operation.counterparty_id else None
        matches: set[uuid.UUID] = set()
        if payer is not None:
            if payer.id in theirs_ids:
                matches = {payer.id}
            else:
                matches = set(by_key.get(party_key(payer.name), set())) | by_key.get(visual_key(payer.name), set())
                if len(matches) > 1:
                    # Одно написание у нескольких клиентов - посредник, а не сторона:
                    # «Филиал "Южная столица" АО "Kaspi Bank"» было написанием семи
                    # клиентов, и платёж уходил тому, чья сумма совпала (25.10.2026).
                    matches = set()
                payer_bin = str(payer.bin or (payer.details or {}).get("bin") or "").strip()
                if payer_bin:
                    matches |= by_bin.get(payer_bin, set())
        account = operation.account_to_id if operation.kind == "income" else operation.account_from_id
        entity = entity_of_account.get(account) if account else None

        def ours(side: Side) -> bool:
            return entity is None or side.ours == entity

        ref = number_key(operation.contract_ref) if operation.contract_ref else ""
        numbered = [side for side in by_number.get((ref, operation.kind), []) if ours(side)] if ref else []
        if ref and not numbered:
            loose = [side for side in by_loose.get((number_loose(ref), operation.kind), []) if ours(side)]
            numbered = loose if len(loose) == 1 else []
            if not numbered:
                # Источник назвал договор, которого у юрлица нет, - не угадываем по имени.
                continue
        if numbered:
            agreeing = {side.contract_id: side for side in numbered if not matches or side.theirs in matches}
            if not agreeing:
                # Номер - договор одного клиента, плательщик - другой клиент.
                disputed = {side.contract_id for side in numbered}
                disputed |= {
                    side.contract_id
                    for theirs in matches
                    for side in by_side.get((theirs, operation.kind), [])
                    if ours(side)
                }
                for contract_id in disputed:
                    result.open.setdefault(contract_id, []).append(operation)
                continue
            chosen_sides = _narrow(agreeing, operation)
            if len(chosen_sides) == 1:
                result.paid.setdefault(next(iter(chosen_sides)), []).append((operation, "number"))
            else:
                for contract_id in chosen_sides:
                    result.open.setdefault(contract_id, []).append(operation)
            continue

        if not matches:
            continue
        candidates: dict[uuid.UUID, Side] = {}
        for theirs in matches:
            for side in by_side.get((theirs, operation.kind), []):
                if ours(side):
                    candidates[side.contract_id] = side
        if not candidates:
            continue
        candidates = _narrow(candidates, operation)
        if len(candidates) == 1:
            result.paid.setdefault(next(iter(candidates)), []).append((operation, "auto"))
        else:
            for contract_id in candidates:
                result.open.setdefault(contract_id, []).append(operation)
    return result


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(Decimal("0.01")), "f")


def summary_of(contract: Contract, found: list[tuple[Operation, str]], open_count: int) -> dict[str, Any] | None:
    """«Оплачено» и «Остаток» договора. Нет ни одной оплаты - нет и ответа:
    пустая ячейка честнее нуля, если выписка этого счёта ещё не загружена."""
    if not found and not open_count:
        return None
    paid = sum((operation.amount_base for operation, _how in found), Decimal("0"))
    remaining = None
    if found and contract.billing == "total" and contract.amount is not None:
        remaining = contract.amount - paid
    return {
        "paid": _money(paid) if found else None,
        "remaining": _money(remaining),
        "count": len(found),
        "last_at": max(operation.paid_at for operation, _how in found).isoformat() if found else None,
        "open": open_count,
    }


def _check_money(access: Access) -> None:
    if not access.view or "paid" in access.hidden:
        raise PermissionError("Оплаты по договорам вам не открыты - нужен доступ к журналу")


def _visible_contracts(session: Session, workspace: Workspace, registry: Registry, access: Access) -> list[Contract]:
    contracts = list(
        session.scalars(
            sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
        )
    )
    people = people_of(session, [item.id for item in contracts])
    return [item for item in contracts if visible_to(item, registry, access, people.get(item.id, []))]


def summaries(session: Session, workspace: Workspace, access: Access) -> dict[str, Any]:
    """«Оплачено/Остаток по выписке» всех видимых договоров - для листа и списка."""
    _check_money(access)
    registry = Registry(session, workspace)
    # Разносится по всем договорам компании, а показывается по видимым: иначе
    # у сотрудника «своего отдела» спорный платёж стал бы бесспорным только
    # потому, что второй договор клиента ему не виден.
    everything = list(
        session.scalars(
            sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
        )
    )
    allocation = allocate(session, workspace, registry, everything)
    shown = {item.id for item in _visible_contracts(session, workspace, registry, access)}
    out: dict[str, Any] = {}
    for contract in everything:
        if contract.id not in shown:
            continue
        item = summary_of(contract, allocation.paid.get(contract.id, []), len(allocation.open.get(contract.id, [])))
        if item is not None:
            out[str(contract.id)] = item
    return {"contracts": out}


def _operation_out(operation: Operation, how: str, names: dict[uuid.UUID, str], accounts: dict[uuid.UUID, str]) -> dict[str, Any]:
    account = operation.account_to_id if operation.kind == "income" else operation.account_from_id
    return {
        "operation_id": str(operation.id),
        "paid_at": operation.paid_at.isoformat(),
        "amount": _money(operation.amount_base),
        "kind": operation.kind,
        "counterparty": names.get(operation.counterparty_id, "") if operation.counterparty_id else "",
        "account": accounts.get(account, "") if account else "",
        "comment": (operation.comment or "")[:160],
        "contract_ref": operation.contract_ref or "",
        "how": how,
    }


def of_contract(session: Session, workspace: Workspace, access: Access, contract_id: uuid.UUID) -> dict[str, Any]:
    """Оплаты одного договора: отнесённые и спорные - для карточки."""
    _check_money(access)
    registry = Registry(session, workspace)
    contract = get_contract(session, workspace, contract_id)
    if not visible_to(contract, registry, access, people_of(session, [contract.id]).get(contract.id, [])):
        raise NotFound("Договор не найден")
    everything = list(
        session.scalars(
            sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
        )
    )
    allocation = allocate(session, workspace, registry, everything)
    found = allocation.paid.get(contract.id, [])
    disputed = allocation.open.get(contract.id, [])
    party_ids = {operation.counterparty_id for operation, _how in found} | {item.counterparty_id for item in disputed}
    names = {
        party.id: party.name
        for party in session.scalars(sa.select(Counterparty).where(Counterparty.id.in_(party_ids - {None})))
    }
    accounts = dict(session.execute(sa.select(Account.id, Account.name).where(Account.workspace_id == workspace.id)).all())
    items = [_operation_out(operation, how, names, accounts) for operation, how in found]
    items += [_operation_out(operation, "open", names, accounts) for operation in disputed]
    items.sort(key=lambda item: item["paid_at"], reverse=True)
    return {"summary": summary_of(contract, found, len(disputed)), "items": items}


def decide(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    contract_id: uuid.UUID,
    operation_id: uuid.UUID,
    action: str,
) -> dict[str, Any]:
    """Решение по платежу: `link` - к этому договору, `unlink` - ни к одному,
    `auto` - снять решение, пусть снова решает система."""
    _check_money(access)
    if not access.edit:
        raise PermissionError("Разносить оплаты вам не открыто")
    if action not in ("link", "unlink", "auto"):
        raise FinanceError("Ждём «link», «unlink» или «auto»")
    registry = Registry(session, workspace)
    contract = get_contract(session, workspace, contract_id)
    ensure_writable(session, registry, access, contract)
    operation = session.get(Operation, operation_id)
    if operation is None or operation.workspace_id != workspace.id or operation.deleted_at is not None:
        raise FinanceError("Такой операции в журнале нет")
    if operation.kind not in ("income", "expense"):
        raise FinanceError("Перевод между своими счетами - не оплата по договору")
    row = session.scalar(
        sa.select(ContractPayment).where(
            ContractPayment.workspace_id == workspace.id, ContractPayment.operation_id == operation_id
        )
    )
    if action == "auto":
        if row is not None:
            session.delete(row)
    else:
        target = contract.id if action == "link" else None
        if row is None:
            session.add(
                ContractPayment(
                    workspace_id=workspace.id, operation_id=operation_id, contract_id=target, created_by=actor.user_id
                )
            )
        else:
            row.contract_id, row.created_by = target, actor.user_id
    session.flush()
    words = {"link": "отнесена к договору", "unlink": "не по договору", "auto": "разнесение снова по выписке"}
    history.write(
        session,
        workspace,
        kind=f"contract.payment.{action}",
        entity="contract",
        entity_id=contract.id,
        title=(
            f"договор {contract.number or ''} · оплата {operation.paid_at:%d.%m.%Y} на "
            f"{_money(operation.amount_base)} - {words[action]}"
        ).replace("  ", " "),
        after={"operation_id": str(operation_id), "action": action},
        actor=actor.email,
    )
    return of_contract(session, workspace, access, contract.id)


__all__ = ["Allocation", "Side", "allocate", "decide", "of_contract", "summaries", "summary_of"]
