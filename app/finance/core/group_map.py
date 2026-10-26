"""Карта группы (фаза 8): наши ТОО и что между ними.

Группа BBC - больше десяти ТОО, и то, что связывает их, нигде не видно целиком:
договоры между своими (займы, аренда, услуги друг другу), переводы клиентов с
одного ТОО на другое ради порога НДС, люди, получающие выплаты сразу из
нескольких ТОО. Карта - один взгляд на это:

* **узлы** - наши юрлица: оборот без НДС с начала года и к концу года, статус
  НДС (`core/vat.py`: плательщик, станет, к порогу, режим не записан), месяц
  порога;
* **договоры между своими** - стрелка от исполнителя к заказчику, сколько
  договоров и начислено за год;
* **переводы клиентов** - стрелка от прежнего исполнителя к новому, сколько
  договоров и с какого месяца: смена исполнителя в начислениях года (и запись
  перевода, и история «до даты исполнитель был»);
* **люди с выплатами из нескольких ТОО** - получатели расходов журнала со
  счетов двух и больше наших юрлиц, у которых вид лица - человек (ИИН) или
  имя с отчеством. Это наблюдение для владельца, а не вывод: KORT ничего с ним
  не делает.
"""
from __future__ import annotations

import re
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts.service import Access, Registry
from app.finance.core import vat
from app.finance.core.accruals import month_key
from app.finance.core.debts import entity_codes, side_of
from app.finance.core.registers import ledger_on
from app.finance.models import Account, Counterparty, Operation, Workspace

_ZERO = Decimal("0")
#: «Ерланович», «Сериковна», «Нурлан кызы», «Асан улы» - отчество: это человек.
_PATRONYMIC = re.compile(r"(ович|евич|овна|евна|ична|инична|\bкызы|\bулы|\bұлы|\bқызы)\b", re.IGNORECASE)
_FORMS = re.compile(r"\b(тоо|ип|ао|оао|зао|ооо|llp|ltd|llc|кх|пк|гу|ксп|потребительский|филиал)\b", re.IGNORECASE)


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _person(party: Counterparty) -> bool:
    if (party.kind or "") == "person":
        return True
    name = party.name or ""
    return bool(_PATRONYMIC.search(name)) and not _FORMS.search(name)


def group_map(session: Session, workspace: Workspace, access: Access, *, year: int, today: date) -> dict[str, Any]:
    from app.finance.core.shadow import _visible

    turnover = vat.Turnover(session, workspace, access, year, today=today)
    years = turnover.run()
    threshold = turnover.tax_year.threshold
    codes = entity_codes(session, workspace)
    registry = Registry(session, workspace)
    nodes = [
        {
            "id": str(item.entity_id),
            "code": item.code or item.name,
            "name": item.name,
            "status": item.status,
            "payer_from": item.payer_from.isoformat() if item.payer_from else None,
            "fact": _money(item.fact),
            "forecast": _money(item.forecast),
            "vat": _money(item.vat),
            "crossing": item.crossing(threshold),
        }
        for item in sorted(years.values(), key=lambda row: -row.forecast)
    ]
    known = {item.entity_id for item in years.values()}

    visible = _visible(session, workspace, access)
    ledger = ledger_on(session, workspace, visible, day=today)
    since, until = date(year, 1, 1), date(year, 12, 1)
    current = month_key(today)
    intra: dict[tuple[uuid.UUID, uuid.UUID], dict[str, Any]] = {}
    moves: dict[tuple[uuid.UUID, uuid.UUID], dict[str, Any]] = {}
    for contract in visible:
        side = side_of(contract, registry.is_own)
        accruals = [item for item in ledger.accruals(contract, since=since, until=until) if not item.skipped and item.amount]
        if side == "intra" and contract.executor_id in known and contract.customer_id in known:
            entry = intra.setdefault(
                (contract.executor_id, contract.customer_id), {"contracts": 0, "accrued": _ZERO, "numbers": []}
            )
            entry["contracts"] += 1
            entry["accrued"] += sum((item.amount for item in accruals if item.month <= current), _ZERO)
            if len(entry["numbers"]) < 5:
                entry["numbers"].append(contract.number)
        if side not in ("receivable", "intra"):
            continue
        previous: uuid.UUID | None = None
        for item in accruals:
            executor = item.executor_id
            if previous is not None and executor is not None and executor != previous and previous in known and executor in known:
                entry = moves.setdefault((previous, executor), {"contracts": set(), "first": item.month, "monthly": _ZERO})
                if contract.id not in entry["contracts"]:
                    entry["contracts"].add(contract.id)
                    entry["monthly"] += item.amount
                entry["first"] = min(entry["first"], item.month)
            previous = executor

    edges = [
        {
            "kind": "intra",
            "from": str(source),
            "to": str(target),
            "contracts": entry["contracts"],
            "amount": _money(entry["accrued"]),
            "numbers": entry["numbers"],
        }
        for (source, target), entry in intra.items()
    ]
    edges += [
        {
            "kind": "transfer",
            "from": str(source),
            "to": str(target),
            "contracts": len(entry["contracts"]),
            "amount": _money(entry["monthly"]),
            "month": entry["first"],
        }
        for (source, target), entry in moves.items()
    ]

    # Люди с выплатами из нескольких наших юрлиц - расходы журнала за год.
    accounts = dict(
        session.execute(
            sa.select(Account.id, Account.group_entity_id).where(Account.workspace_id == workspace.id, Account.group_entity_id.is_not(None))
        ).all()
    )
    paid: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = defaultdict(lambda: defaultdict(lambda: _ZERO))
    if accounts:
        rows = session.execute(
            sa.select(Operation.counterparty_id, Operation.account_from_id, Operation.amount_base).where(
                Operation.workspace_id == workspace.id,
                Operation.deleted_at.is_(None),
                Operation.status == "fact",
                Operation.kind == "expense",
                Operation.counterparty_id.is_not(None),
                Operation.account_from_id.in_(list(accounts)),
                Operation.paid_at >= since,
                Operation.paid_at <= date(year, 12, 31),
            )
        )
        for party_id, account_id, amount in rows:
            paid[party_id][accounts[account_id]] += amount or _ZERO
    several = [party_id for party_id, by_entity in paid.items() if len(by_entity) >= 2]
    parties = {
        party.id: party for party in session.scalars(sa.select(Counterparty).where(Counterparty.id.in_(several or [uuid.uuid4()])))
    }
    people = []
    for party_id in several:
        party = parties.get(party_id)
        if party is None or party.id in known or not _person(party):
            continue
        by_entity = paid[party_id]
        people.append(
            {
                "id": str(party_id),
                "name": party.name,
                "entities": [
                    {"code": codes.get(entity) or "", "amount": _money(amount)}
                    for entity, amount in sorted(by_entity.items(), key=lambda pair: -pair[1])
                ],
                "total": _money(sum(by_entity.values(), _ZERO)),
            }
        )
    people.sort(key=lambda item: -Decimal(item["total"]))
    return {
        "year": year,
        "threshold": _money(threshold),
        "nodes": nodes,
        "edges": edges,
        "people": people[:40],
        "people_total": len(people),
    }


def entity_numbers(session: Session, workspace: Workspace, access: Access, entity_id: uuid.UUID, *, today: date) -> dict[str, Any]:
    """Досье нашего ТОО «в цифрах»: НДС и оборот за год, кто ему должен - те же числа, что в «НДС» и «Долгах»."""
    from app.finance.core.debts import Debts

    turnover = vat.Turnover(session, workspace, access, today.year, today=today)
    item = turnover.run().get(entity_id)
    threshold = turnover.tax_year.threshold
    debts = Debts(session, workspace, access, day=today)
    out: dict[str, Any] = {"year": today.year, "vat": None}
    if item is not None:
        full = item.out(threshold)
        out["vat"] = {key: full[key] for key in ("status", "payer_from", "fact", "forecast", "vat", "vat_fact", "remaining", "crossing")}
    # Долг этому ТОО - его месяцы: переведённый клиент остаётся должен прежнему за свои старые месяцы.
    owed = {debt.contract.id: debt.owners().get(entity_id, {}) for debt in debts.items.values()}
    out["debts"] = {
        "owed": _money(sum((sum(aging.values(), _ZERO) for aging in owed.values()), _ZERO)),
        "d90plus": _money(sum((aging.get("d90plus", _ZERO) for aging in owed.values()), _ZERO)),
        "advance": _money(sum((debt.advance for debt in debts.items.values() if debt.contract.executor_id == entity_id), _ZERO)),
        "contracts": sum(1 for aging in owed.values() if sum(aging.values(), _ZERO) > 0),
    }
    return out


__all__ = ["entity_numbers", "group_map"]
