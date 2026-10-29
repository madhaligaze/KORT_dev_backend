"""Строки реестра против договоров, которые KORT уже знает.

Компания входит не одним файлом: сначала 1С заводит наши юрлица, участников с
БИН и договоры по актам, потом отдел приносит свой реестр - с условиями,
которых в 1С нет: отдел, ответственные, примечание. Реестр тогда не заводит
договоры второй раз, а находит их и дополняет пустое (полигон целиком: до
этого реестр поверх 1С двоил 57 договоров из 59).

Как строка находит договор:

* наше юрлицо - из колонки «Наше ТОО» (коротким именем тоже), вторая
  сторона - по БИН, иначе по написанию; спорная сторона - не совпадение;
* договор - по номеру у этого юрлица, с прежними исполнителями (клиента
  перевели на другое ТОО, а реестр пишет старое). Номер сравнивается без
  знаков: «ТС 15-2026» в реестре - это «ТС-15/2026» в 1С (Д1);
* номера нет или он не нашёлся, а у стороны ровно один договор с этим
  юрлицом, и тот без номера, и строка у стороны одна - это он: 1С вела его
  «Основным договором», реестр даёт номер (Д3).

Найденный договор получает только пустое. Сумма, исполнитель и статус,
которые расходятся с KORT, - строки сверки, а не перезапись. Вопрос один:
сумма реестра равна нынешней сумме KORT, но в учётном периоде KORT считал
иначе (по актам 1С до июля 150 000, в реестре 180 000) - с какого дня эта
сумма, KORT не знает (Д5).
"""
from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import packs
from app.finance.contracts.models import (
    Contract,
    ContractAmendment,
    ContractDepartment,
    ContractPerson,
)
from app.finance.contracts.service import Registry, read_money
from app.finance.models import Counterparty, Workspace
from app.finance.service import FinanceError

_JUNK = re.compile(r"[^0-9a-zа-яёәғқңөұүһі]")
#: Поля, которые реестр дописывает найденному договору, если у того пусто.
FILL = ("number", "signed_at", "amount", "department", "people", "note", "subject")


def loose_number(number: Any) -> str:
    """Номер без знаков и регистра: «ТС 15-2026» и «ТС-15/2026» - один номер."""
    return _JUNK.sub("", str(number or "").lower())


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _words(value: Decimal | None) -> str:
    """Сумма для текста вопроса: «180 000», «1 234,50»."""
    if value is None:
        return "-"
    whole, cents = divmod(value.quantize(Decimal("0.01")), 1)
    text = f"{int(whole):,}".replace(",", " ")
    return text + (f",{int(cents * 100):02d}" if cents else "")


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


@dataclass
class Match:
    """Строка реестра → договор KORT: как найден и что с ним сделает загрузка."""

    ref: str
    contract_id: uuid.UUID
    how: str
    number: str
    name: str
    fill: dict[str, Any] = field(default_factory=dict)
    compare: list[dict[str, Any]] = field(default_factory=list)
    question: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref, "contract_id": str(self.contract_id), "how": self.how, "number": self.number or None,
            "name": self.name, "fill": sorted(self.fill), "compare": self.compare, "question": self.question,
        }


class Known:
    """Что KORT уже знает о договорах компании - индексами, на один разбор файла."""

    def __init__(self, session: Session, workspace: Workspace, registry: Registry):
        from app.finance.contracts.payments import former_executors

        self.session, self.workspace, self.registry = session, workspace, registry
        self.ids = packs.country_of(workspace).ids
        self.contracts = {
            item.id: item for item in session.scalars(
                sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
            )
        }
        former = former_executors(session, workspace.id)
        #: (участник, наше юрлицо) → договоры: обе стороны и прежние исполнители.
        self.by_pair: dict[tuple[uuid.UUID, uuid.UUID], list[Contract]] = {}
        for contract in self.contracts.values():
            sides = {contract.executor_id, contract.customer_id} - {None}
            ours = {side for side in sides | former.get(contract.id, set()) if registry.is_own(side)}
            for party in sides:
                for entity in ours - {party}:
                    self.by_pair.setdefault((party, entity), []).append(contract)
        self.by_bin = {
            number: party_id
            for party_id, number in session.execute(
                sa.select(Counterparty.id, Counterparty.bin).where(
                    Counterparty.workspace_id == workspace.id, Counterparty.archived_at.is_(None), Counterparty.bin != ""
                )
            )
            if number
        }
        self._departments: set[uuid.UUID] | None = None
        self._people: set[uuid.UUID] | None = None

    def sides(self, values: dict[str, Any], bin_raw: Any) -> tuple[uuid.UUID, uuid.UUID] | None:
        """(вторая сторона, наше юрлицо) строки - или ничего, если одна из них не ясна."""
        executor, customer = values.get("executor"), values.get("customer")
        mine = {slot: self._own(raw) for slot, raw in (("executor", executor), ("customer", customer))}
        if mine["executor"] is not None:
            entity, other = mine["executor"], customer
        elif mine["customer"] is not None:
            entity, other = mine["customer"], executor
        else:
            return None
        if not other:
            return None
        checked = self.ids.check(bin_raw, str(other)) if bin_raw not in (None, "") else None
        if checked is not None and checked.ok and checked.number in self.by_bin:
            return self.by_bin[checked.number], entity
        resolved = self.registry.resolve_party(other, slot="customer", create=False)
        if resolved.party is None:
            return None
        return resolved.party.id, entity

    def _own(self, raw: Any) -> uuid.UUID | None:
        if not raw:
            return None
        resolved = self.registry.resolve_party(raw, slot="executor", create=False)
        return resolved.party.id if resolved.party is not None and self.registry.is_own(resolved.party.id) else None

    def match_rows(self, rows: list[dict[str, Any]], bin_key: str) -> dict[str, tuple[Contract, str]]:
        """Строка → (договор, как найден). Не нашлось однозначно - строки нет."""
        sides = {row["ref"]: self.sides(row["values"], row["values"].get(bin_key)) for row in rows}
        per_pair = Counter(pair for pair in sides.values() if pair is not None)
        out: dict[str, tuple[Contract, str]] = {}
        taken: Counter = Counter()
        for row in rows:
            pair = sides[row["ref"]]
            if pair is None:
                continue
            mine = self.by_pair.get(pair) or []
            number = loose_number(row["values"].get("number"))
            hits = [contract for contract in mine if number and loose_number(contract.number) == number]
            if len(hits) == 1:
                out[row["ref"]] = (hits[0], "number")
            elif not hits and len(mine) == 1 and not mine[0].number and per_pair[pair] == 1:
                out[row["ref"]] = (mine[0], "only")
            else:
                continue
            taken[out[row["ref"]][0].id] += 1
        # Две строки на один договор - не совпадение ни для одной: решает человек.
        return {ref: found for ref, found in out.items() if taken[found[0].id] == 1}

    # ── Что найденному договору дописать и где файл расходится с KORT ──

    def _filled_departments(self) -> set[uuid.UUID]:
        if self._departments is None:
            self._departments = set(self.session.scalars(
                sa.select(ContractDepartment.contract_id).where(ContractDepartment.contract_id.in_(list(self.contracts)))
            ))
        return self._departments

    def _filled_people(self) -> set[uuid.UUID]:
        if self._people is None:
            self._people = set(self.session.scalars(
                sa.select(ContractPerson.contract_id).where(ContractPerson.contract_id.in_(list(self.contracts)))
            ))
        return self._people

    def amounts(self, contract_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[tuple[date | None, Decimal | None]]]:
        """Сумма договора по времени: (с какого дня, сумма) по соглашениям «с даты»; первая - с начала."""
        rows = self.session.execute(
            sa.select(ContractAmendment.contract_id, ContractAmendment.effective_from, ContractAmendment.before,
                      ContractAmendment.after)
            .where(ContractAmendment.contract_id.in_(contract_ids), ContractAmendment.effect == "amount",
                   ContractAmendment.applied_at.is_not(None))
            .order_by(ContractAmendment.effective_from, ContractAmendment.position)
        ).all()
        out: dict[uuid.UUID, list[tuple[date | None, Decimal | None]]] = {}
        for contract_id, since, before, after in rows:
            line = out.setdefault(contract_id, [(None, _decimal((before or {}).get("amount")))])
            line.append((since, _decimal((after or {}).get("amount"))))
        return out

    def review(self, ref: str, row: dict[str, Any], contract: Contract, how: str, *, since: date | None, today: date,
               timeline: list[tuple[date | None, Decimal | None]] | None, name: str) -> Match:
        values = row["values"]
        found = Match(ref, contract.id, how, contract.number or "", name)
        # Сумму решает `_review_amount`: у неё кроме «пусто» есть история.
        empty = {
            "number": not contract.number,
            "signed_at": contract.signed_at is None,
            "department": contract.id not in self._filled_departments(),
            "people": contract.id not in self._filled_people(),
            "note": not (contract.note or "").strip(),
            "subject": contract.subject_id is None,
        }
        for key, blank in empty.items():
            raw = values.get(key)
            if blank and raw not in (None, ""):
                found.fill[key] = str(raw).strip() if key == "number" else raw
        self._review_amount(found, values.get("amount"), contract, since=since, today=today, timeline=timeline)
        own = self._own(values.get("executor")) or self._own(values.get("customer"))
        ours = {contract.executor_id, contract.customer_id}
        if own is not None and own not in ours:
            parties = self.registry.parties_for([own, contract.executor_id])
            found.compare.append({
                "field": "executor", "file": parties[own].name if own in parties else "",
                "kort": parties[contract.executor_id].name if contract.executor_id in parties else "",
                "text": "в файле прежнее наше юрлицо - KORT считает по соглашению «с даты»",
            })
        self._review_status(found, values.get("status"), contract)
        return found

    def _review_amount(self, found: Match, raw: Any, contract: Contract, *, since: date | None, today: date,
                       timeline: list[tuple[date | None, Decimal | None]] | None) -> None:
        if raw in (None, ""):
            return
        try:
            amount, _terms = read_money(raw, field="Сумма")
        except FinanceError:
            return
        if amount is None:
            return
        if contract.amount is None:
            if contract.billing != "documents":
                found.fill["amount"] = format(amount, "f")
            return
        if contract.billing == "documents":
            # Начисление по счетам и актам: сумма договора - не то, что начисляется.
            return
        line = timeline or [(None, contract.amount)]
        start = since or today
        # Куски суммы, которые задевают учётный период: [с какого дня, до следующего).
        within: list[tuple[date | None, Decimal | None]] = []
        for index, (day, value) in enumerate(line):
            nxt = line[index + 1][0] if index + 1 < len(line) else None
            if nxt is not None and nxt <= start:
                continue
            within.append((day, value))
        if amount == contract.amount:
            earlier = [(day, value) for day, value in within if value != amount]
            if not earlier:
                return
            since_day = next((day for day, value in reversed(within) if value == amount and day is not None), None)
            prior = earlier[-1][1]
            found.question = {
                "kind": "amount_since", "key": f"amount_since:{found.ref}",
                "text": (f"«{found.name}»: в файле {_words(amount)}, у KORT до {since_day:%d.%m.%Y} - {_words(prior)}. "
                         f"С какого дня {_words(amount)}?") if since_day else
                        f"«{found.name}»: в файле {_words(amount)}, у KORT в учётном периоде было {_words(prior)}. С какого дня?",
                "options": [
                    {"value": "kort", "label": f"с {since_day:%d.%m.%Y}, как у KORT" if since_day else "как у KORT"},
                    {"value": "file", "label": f"{_words(amount)} весь срок - у KORT прежняя сумма неверна"},
                ],
                "file": _money(amount),
            }
            return
        earlier_values = {value for _day, value in line[:-1]}
        found.compare.append({
            "field": "amount", "file": _words(amount), "kort": _words(contract.amount),
            "text": ("в файле прежняя сумма - KORT считает новую с " + line[-1][0].strftime("%d.%m.%Y"))
            if amount in earlier_values and line[-1][0] is not None else "сумма в файле другая - KORT считает свою",
        })

    def _review_status(self, found: Match, raw: Any, contract: Contract) -> None:
        if raw in (None, "") or contract.status_id is None:
            return
        value = self.registry.resolve_value("status", raw, create=False)
        phase = (value.meaning or {}).get("phase") if value is not None else None
        current = self.registry.values.get(contract.status_id)
        kort_phase = (current.meaning or {}).get("phase") if current is not None else None
        if phase and kort_phase and phase != kort_phase:
            found.compare.append({
                "field": "status", "file": str(raw).strip(), "kort": current.value if current is not None else "",
                "text": "статус в файле другой - KORT считает свой",
            })


__all__ = ["FILL", "Known", "Match", "loose_number"]
