"""Начисления договора: считаются из условий при чтении, не хранятся.

Правило одно для всех компаний (без слов какой-то одной книги):

* **В месяц** - начисление на каждый период. Первый - с даты начала
  начислений (`billing_from`, иначе дата договора), следующие - с дня периода
  (`period_day`, иначе день даты начала) каждого месяца. Месяц начисления -
  месяц начала периода: у договора от 25.06 июньский период - 25.06-24.07.
  Сумма, стороны и статус - на день начала периода (`core/terms.py`):
  клиента перевели на другое ТОО с июля - июльское начисление уже у него.
* **Вся сумма** - одно начисление в день начала.
* **Условие** («20% по разовым») и не заданный вид - не начисляются: сумму
  из текста KORT не придумывает.
* Статус на день периода: приостановлен - месяц пропущен; не состоялся или
  черновик - не начисляется; расторгнут или исполнен - начисляется до даты
  окончания, а без неё - нет: когда договор закончился, неизвестно, и
  угадывать это нельзя (сверка с книгой предложит дату).
* **Поправка месяца** (`Document` вида `adjustment`) - решение человека:
  другая сумма или «не начислять», с причиной. Поправка в месяце, где по
  условиям начисления нет, - доначисление.
"""
from __future__ import annotations

import calendar
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Iterator

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts.models import Contract, ContractAmendment, ListValue
from app.finance.core.models import Document
from app.finance.core.terms import Terms, Timeline

#: Фазы, при которых договор не начисляется вовсе.
NEVER = ("failed", "draft")
#: Фазы конца: начисляется до даты окончания, без неё - нет.
ENDED = ("terminated", "fulfilled")

#: Почему по договору нет начислений - словами для карточки и сверки.
WHY = {
    "terms": "сумма задана условием - начисление не считается",
    "unset": "не задано, как начислять",
    "no_date": "нет даты договора и даты начала начислений",
    "no_amount": "не задана сумма",
    "never": "договор не состоялся",
    "ended_unknown": "договор закончился, а дата окончания не указана",
}


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def month_start(key: str) -> date:
    return date(int(key[:4]), int(key[5:7]), 1)


def _shift(year: int, month: int, by: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + by
    return index // 12, index % 12 + 1


def _on_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def periods(start: date, day: int) -> Iterator[tuple[date, date]]:
    """Периоды «в месяц»: первый - со `start`, дальше - с дня `day` каждого месяца."""
    year, month = start.year, start.month
    border = _on_day(year, month, day)
    if border <= start:
        year, month = _shift(year, month, 1)
        border = _on_day(year, month, day)
    current = start
    while True:
        yield current, border - timedelta(days=1)
        current = border
        year, month = _shift(year, month, 1)
        border = _on_day(year, month, day)


@dataclass
class Accrual:
    contract_id: uuid.UUID
    month: str
    period_from: date
    period_to: date | None
    #: По условиям договора; `None` у доначисления поправкой.
    base: Decimal | None
    #: С поправкой месяца.
    amount: Decimal | None
    executor_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    adjustment: Document | None = None

    @property
    def skipped(self) -> bool:
        return bool(self.adjustment is not None and self.adjustment.skip)

    def out(self) -> dict[str, Any]:
        adjustment = self.adjustment
        return {
            "month": self.month,
            "period_from": self.period_from.isoformat(),
            "period_to": self.period_to.isoformat() if self.period_to else None,
            "base": format(self.base, "f") if self.base is not None else None,
            "amount": format(self.amount, "f") if self.amount is not None and not self.skipped else None,
            "executor_id": str(self.executor_id) if self.executor_id else None,
            "skipped": self.skipped,
            "adjustment": (
                {
                    "id": str(adjustment.id),
                    "amount": format(adjustment.amount, "f") if adjustment.amount is not None else None,
                    "skip": adjustment.skip,
                    "reason": adjustment.reason,
                }
                if adjustment is not None
                else None
            ),
        }


@dataclass
class Schedule:
    """Начисления договора в окне месяцев и почему их нет, если нет."""

    contract_id: uuid.UUID
    #: `month` / `total`; пусто - договор не начисляется.
    kind: str
    start: date | None
    period_day: int | None
    accruals: list[Accrual] = field(default_factory=list)
    #: Месяцы, пропущенные из-за приостановки.
    paused: list[str] = field(default_factory=list)
    #: Код причины (`WHY`), если начислений нет или они обрываются.
    why: str = ""

    def by_month(self) -> dict[str, Accrual]:
        return {item.month: item for item in self.accruals}


class Ledger:
    """Начисления многих договоров: соглашения, поправки и фазы статусов читаются один раз."""

    def __init__(self, session: Session, workspace_id: uuid.UUID, contracts: Iterable[Contract]) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.contracts = {item.id: item for item in contracts}
        ids = list(self.contracts)
        self.amendments: dict[uuid.UUID, list[ContractAmendment]] = defaultdict(list)
        self.adjustments: dict[tuple[uuid.UUID, str], Document] = {}
        if ids:
            for row in session.scalars(sa.select(ContractAmendment).where(ContractAmendment.contract_id.in_(ids))):
                self.amendments[row.contract_id].append(row)
            for doc in session.scalars(
                sa.select(Document).where(
                    Document.workspace_id == workspace_id,
                    Document.kind == "adjustment",
                    Document.deleted_at.is_(None),
                    Document.contract_id.in_(ids),
                )
            ):
                if doc.period is not None and doc.contract_id is not None:
                    self.adjustments[(doc.contract_id, month_key(doc.period))] = doc
        self.phases: dict[uuid.UUID, str] = {
            row.id: str((row.meaning or {}).get("phase") or "")
            for row in session.scalars(
                sa.select(ListValue).where(ListValue.workspace_id == workspace_id, ListValue.field_key == "status")
            )
        }

    def phase_of(self, status_id: uuid.UUID | None) -> str:
        return self.phases.get(status_id, "") if status_id else ""

    def timeline(self, contract: Contract) -> Timeline:
        return Timeline(contract, self.amendments.get(contract.id, []), self.phase_of)

    def terms(self, contract: Contract, day: date) -> Terms:
        return self.timeline(contract).on(day)

    def schedule(self, contract: Contract, *, since: date, until: date) -> Schedule:
        """Начисления договора за месяцы с `since` по `until` включительно."""
        first, last = month_key(since), month_key(until)
        timeline = self.timeline(contract)
        start = contract.billing_from or contract.signed_at
        day = contract.period_day or (start.day if start else None)
        kind = contract.billing if contract.billing in ("month", "total") else ""
        out = Schedule(contract_id=contract.id, kind=kind, start=start, period_day=day)
        if not kind:
            out.why = "terms" if contract.billing == "terms" else "unset"
        elif start is None:
            out.why = "no_date"
        elif kind == "total":
            terms = timeline.on(start)
            if terms.phase in NEVER:
                out.why = "never"
            elif first <= month_key(start) <= last:
                out.accruals.append(self._accrual(contract, start, terms.end_date, terms))
        else:
            for period_from, period_to in periods(start, day or start.day):
                month = month_key(period_from)
                if month > last:
                    break
                terms = timeline.on(period_from)
                if terms.end_date is not None and period_from > terms.end_date:
                    break
                if terms.phase in NEVER:
                    out.why = out.why or "never"
                    continue
                if terms.phase == "suspended":
                    if month >= first:
                        out.paused.append(month)
                    continue
                if terms.phase in ENDED and terms.end_date is None:
                    out.why = out.why or "ended_unknown"
                    continue
                if month < first:
                    continue
                out.accruals.append(self._accrual(contract, period_from, period_to, terms))
        self._adjust(contract, out, first, last, timeline)
        if out.accruals and all(item.base is None and item.adjustment is None for item in out.accruals):
            out.why = out.why or "no_amount"
        return out

    def _accrual(self, contract: Contract, period_from: date, period_to: date | None, terms: Terms) -> Accrual:
        return Accrual(
            contract_id=contract.id,
            month=month_key(period_from),
            period_from=period_from,
            period_to=period_to,
            base=terms.amount,
            amount=terms.amount,
            executor_id=terms.executor_id,
            customer_id=terms.customer_id,
        )

    def _adjust(self, contract: Contract, out: Schedule, first: str, last: str, timeline: Timeline) -> None:
        months = out.by_month()
        for (contract_id, month), doc in self.adjustments.items():
            if contract_id != contract.id or not first <= month <= last:
                continue
            accrual = months.get(month)
            if accrual is None:
                if doc.skip or doc.amount is None:
                    continue
                # Доначисление: по условиям в этом месяце начисления нет.
                day = month_start(month)
                terms = timeline.on(day)
                accrual = Accrual(
                    contract_id=contract.id, month=month, period_from=day, period_to=None, base=None,
                    amount=None, executor_id=terms.executor_id, customer_id=terms.customer_id,
                )
                out.accruals.append(accrual)
            accrual.adjustment = doc
            if not doc.skip and doc.amount is not None:
                accrual.amount = doc.amount
        out.accruals.sort(key=lambda item: item.period_from)


__all__ = ["Accrual", "ENDED", "Ledger", "NEVER", "Schedule", "WHY", "month_key", "month_start", "periods"]
