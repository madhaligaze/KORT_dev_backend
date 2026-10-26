"""Выручка по договорам по месяцам (фаза 6): «Прибыль» и «Деньги» - виды тех же итогов.

Отчёты журнала считают доходы по дате начисления операции («Прибыль») и по
дню платежа («Деньги»). Ядро даёт то же по договорам - из того, что KORT
знает о договоре сам:

* **начислено** - начисления по условиям (`core/accruals.py`): что заработано
  за месяц; юрлицо - исполнитель на день периода (после перевода клиента -
  новое);
* **АВР** - сумма акта месяца по приоритету источника (`core/acts.py`);
* **оплачено за месяц** - оплаты, которые разнесение отдало этому месяцу
  (`core/payments.py`), как «Сумма Факт Поступ.» строки сводной таблицы;
* **поступило** - те же оплаты по дню платежа: деньги месяца.

Группы - как у долгов (`core/debts.py`): наше юрлицо, клиент, отдел и человек
по долям. Сторона - продажи клиентам или договоры между своими: в итог продаж
они не входят, иначе группа зарабатывала бы сама на себе. Оплат до начала
учёта KORT не знает - у этих месяцев «оплачено» и «поступило» пусты, а не ноль.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

import sqlalchemy as sa

from app.finance.contracts.models import Contract
from app.finance.contracts.service import Access, Registry
from app.finance.core.accruals import month_key
from app.finance.core.acts import Acts
from app.finance.core.debts import NOBODY, _SPECIAL, entity_codes, fractions, group_titles, side_of
from app.finance.core.registers import totals
from app.finance.models import Counterparty, Workspace

_ZERO = Decimal("0")
_ONE = Decimal("1")
MEASURES = ("accrued", "acts", "paid", "received")
GROUPS = ("entity", "client", "department", "person")
SIDES = ("receivable", "intra")


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def year_months(year: int) -> list[str]:
    return [f"{year:04d}-{month:02d}" for month in range(1, 13)]


class Revenue:
    """Выручка видимых договоров по месяцам года - на запрос."""

    def __init__(
        self,
        session: Session,
        workspace: Workspace,
        access: Access,
        *,
        year: int,
        today: date,
        book: Any | None = None,
        side: str = "receivable",
    ) -> None:
        if side not in SIDES:
            raise ValueError("выручка - продажи клиентам или между своими")
        from app.finance.core.shadow import _visible

        self.session = session
        self.workspace = workspace
        self.access = access
        self.year = year
        self.months = year_months(year)
        self.side = side
        self.today = today
        registry = Registry(session, workspace)
        # Подписи групп - те же, что у долгов.
        self.names = dict(
            session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace.id)).all()
        )
        self.codes = entity_codes(session, workspace)
        visible = [item for item in _visible(session, workspace, access) if side_of(item, registry.is_own) == side]
        self.contracts: dict[uuid.UUID, Contract] = {item.id: item for item in visible}
        # Начисления и оплаты - из регистра итогов на сегодня (`core/registers.py`).
        found = totals(session, workspace, visible, day=today)
        self.since = found.since
        acts = Acts(session, workspace, access, book, today=today)
        first = month_key(self.since) if self.since else None
        start, end = date(year, 1, 1), date(year, 12, 1)
        #: Договор → месяц → мера → сумма; юрлицо месяца - отдельно.
        self.cells: dict[uuid.UUID, dict[str, dict[str, Decimal]]] = {}
        self.entity_of: dict[tuple[uuid.UUID, str], uuid.UUID | None] = {}
        for contract in visible:
            months: dict[str, dict[str, Decimal]] = defaultdict(dict)
            for accrual in found.ledger.accruals(contract, since=start, until=end):
                if accrual.skipped or not accrual.amount:
                    continue
                months[accrual.month]["accrued"] = months[accrual.month].get("accrued", _ZERO) + accrual.amount
                self.entity_of[(contract.id, accrual.month)] = accrual.executor_id
            for month, slot in acts.months.get(contract.id, {}).items():
                act = slot.chosen
                if act is not None and act.amount is not None and month in self.months:
                    months[month]["acts"] = months[month].get("acts", _ZERO) + act.amount
            item = found.settlements.get(contract.id)
            if first is not None and item is not None:
                # Оплачено за месяц - как держит таблица; поступило - по дню платежа.
                for month, amount in item.attributed.items():
                    if month in self.months:
                        months[month]["paid"] = months[month].get("paid", _ZERO) + amount
                for month, amount in item.received.items():
                    if month in self.months:
                        months[month]["received"] = months[month].get("received", _ZERO) + amount
            if months:
                self.cells[contract.id] = months
        #: С какого месяца KORT знает оплаты: раньше - «оплачено» пусто, а не ноль.
        self.paid_from = first

    def _known(self, month: str, measure: str) -> bool:
        if measure in ("paid", "received"):
            return self.paid_from is not None and month >= self.paid_from
        return True

    def rows(self, group: str) -> list[dict[str, Any]]:
        if group not in GROUPS:
            raise ValueError("группа - юрлицо, клиент, отдел или человек")
        titles = group_titles(self.session, self.workspace, group, self.names, self.codes)
        special = _SPECIAL[group]
        shares = (
            fractions(self.session, self.workspace, self.access, {cid: self.contracts[cid] for cid in self.cells}, group)
            if group in ("department", "person")
            else {}
        )
        table: dict[str, dict[str, Any]] = {}
        for contract_id, months in self.cells.items():
            contract = self.contracts[contract_id]
            for month, values in months.items():
                if group == "entity":
                    party = self.entity_of.get((contract_id, month)) or contract.executor_id
                    owners = [(str(party) if party else NOBODY, _ONE)]
                elif group == "client":
                    owners = [(str(contract.customer_id) if contract.customer_id else NOBODY, _ONE)]
                else:
                    owners = shares.get(contract_id, [(NOBODY, _ONE)])
                for owner, fraction in owners:
                    entry = table.setdefault(
                        owner,
                        {
                            "key": owner,
                            "title": special.get(owner) or titles.get(owner) or "-",
                            "special": owner in special,
                            "months": defaultdict(lambda: defaultdict(lambda: _ZERO)),
                            "contracts": set(),
                        },
                    )
                    entry["contracts"].add(contract_id)
                    for measure, amount in values.items():
                        entry["months"][month][measure] += amount * fraction
        out = []
        for entry in table.values():
            total = {measure: sum((cells.get(measure, _ZERO) for cells in entry["months"].values()), _ZERO) for measure in MEASURES}
            out.append(
                {
                    "key": entry["key"],
                    "title": entry["title"],
                    "special": entry["special"],
                    "contracts": len(entry["contracts"]),
                    "months": {month: self._cell(month, entry["months"].get(month, {})) for month in self.months},
                    "total": {measure: _money(value) for measure, value in total.items()},
                }
            )
        out.sort(key=lambda row: (row["special"], -Decimal(row["total"]["accrued"] or "0"), row["title"]))
        return out

    def _cell(self, month: str, values: dict[str, Decimal]) -> dict[str, str | None]:
        return {
            measure: _money(values.get(measure, _ZERO)) if self._known(month, measure) else None for measure in MEASURES
        }

    def totals(self) -> dict[str, Any]:
        months: dict[str, dict[str, Decimal]] = {month: defaultdict(lambda: _ZERO) for month in self.months}
        for values in self.cells.values():
            for month, cells in values.items():
                for measure, amount in cells.items():
                    months[month][measure] += amount
        return {
            "months": {month: self._cell(month, cells) for month, cells in months.items()},
            "total": {
                measure: _money(sum((cells.get(measure, _ZERO) for cells in months.values()), _ZERO)) for measure in MEASURES
            },
        }

    @property
    def forecast_from(self) -> str | None:
        """Первый месяц, чьё начисление - ещё прогноз по условиям договоров."""
        later = [month for month in self.months if month > month_key(self.today)]
        return later[0] if later else None

    def out(self, group: str) -> dict[str, Any]:
        return {
            "year": self.year,
            "months": self.months,
            "side": self.side,
            "group": group,
            "since": self.since.isoformat() if self.since else None,
            "forecast_from": self.forecast_from,
            "rows": self.rows(group),
            "totals": self.totals(),
        }


__all__ = ["GROUPS", "MEASURES", "Revenue", "SIDES", "year_months"]
