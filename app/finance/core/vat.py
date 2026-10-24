"""НДС и порог: оборот наших юрлиц по месяцам, переход порога, перевод клиентов (фаза 7).

Решение владельца 24.10.2026: расчёт НДС и порога - полноценная функция ядра,
без оговорок; перевод клиента между нашими ТОО - обычное изменение договора
«исполнитель с даты».

Оборот к порогу - выручка юрлица за календарный год без НДС: начисления
договоров со смыслом «выручка» (не заём, не внутри группы, не расход), у
которых юрлицо - исполнитель на день начала периода. Прошедшие и текущий
месяцы - факт начислений, будущие - прогноз из тех же условий договоров
(соглашения «с даты» учтены, в том числе ещё не вступившие). Рядом - суммы
АВР из книги-сводки: проверка факта, пока правда в книге.

НДС начисления: исполнитель - плательщик НДС на день начала периода
(`EntityTaxPeriod`), ставка - года (`core/tax.py`); сумма договора включает
НДС («в сумме», по умолчанию) или НДС начисляется сверху («сверху»), или
договор без НДС. Не плательщику НДС не начисляется.

Перевод клиентов считается до записи: `moves` - какие договоры с какого дня
переходят к какому юрлицу; оборот обоих юрлиц пересчитывается так, как будто
перевод уже записан. «Перевести» пишет те же изменения «исполнитель с даты»
пачкой с точкой восстановления.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts.models import Contract, GroupEntity
from app.finance.contracts.service import Access, Registry, _economic, people_of, visible_to
from app.finance.core import tax
from app.finance.core.accruals import Ledger, month_key, month_start
from app.finance.core.models import EntityTaxPeriod
from app.finance.models import Counterparty, Workspace

_ZERO = Decimal("0")
_CENT = Decimal("0.01")
#: Как сумма договора относится к НДС у плательщика: пусто - «в сумме».
VAT_MODES = ("included", "on_top", "none")


def vat_of(amount: Decimal, *, payer: bool, mode: str, rate: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Без НДС, НДС, с НДС - для суммы начисления."""
    if not payer or mode == "none" or not rate:
        return amount, _ZERO, amount
    if mode == "on_top":
        vat = (amount * rate / Decimal("100")).quantize(_CENT, rounding=ROUND_HALF_UP)
        return amount, vat, amount + vat
    vat = (amount * rate / (Decimal("100") + rate)).quantize(_CENT, rounding=ROUND_HALF_UP)
    return amount - vat, vat, amount


@dataclass(frozen=True)
class Move:
    """Договор переходит к другому нашему юрлицу с дня - для предпросмотра."""

    contract_id: uuid.UUID
    target: uuid.UUID
    since: date


@dataclass
class Month:
    month: str
    net: Decimal = _ZERO
    vat: Decimal = _ZERO
    acts: Decimal | None = None
    forecast: bool = False
    contracts: int = 0

    def out(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "net": format(self.net, "f"),
            "vat": format(self.vat, "f"),
            "acts": format(self.acts, "f") if self.acts is not None else None,
            "forecast": self.forecast,
            "contracts": self.contracts,
        }


@dataclass
class EntityYear:
    entity_id: uuid.UUID
    code: str
    name: str
    year: int
    months: list[Month] = field(default_factory=list)
    payer_now: bool = False
    payer_since: date | None = None
    regime: str = ""

    @property
    def fact(self) -> Decimal:
        return sum((item.net for item in self.months if not item.forecast), _ZERO)

    @property
    def forecast(self) -> Decimal:
        return sum((item.net for item in self.months), _ZERO)

    @property
    def vat(self) -> Decimal:
        return sum((item.vat for item in self.months), _ZERO)

    def crossing(self, threshold: Decimal) -> str | None:
        """Месяц, в котором оборот с начала года превысит порог."""
        total = _ZERO
        for item in self.months:
            total += item.net
            if total > threshold:
                return item.month
        return None

    def out(self, threshold: Decimal) -> dict[str, Any]:
        return {
            "entity_id": str(self.entity_id),
            "code": self.code,
            "name": self.name,
            "regime": self.regime,
            "payer_now": self.payer_now,
            "payer_since": self.payer_since.isoformat() if self.payer_since else None,
            "fact": format(self.fact, "f"),
            "forecast": format(self.forecast, "f"),
            "vat": format(self.vat, "f"),
            "remaining": format(threshold - self.fact, "f"),
            "crossing": self.crossing(threshold),
            "months": [item.out() for item in self.months],
        }


def _revenue(contract: Contract, registry: Registry) -> bool:
    """Выручка ли договор: смысл записан - по нему, нет - по сторонам, как в реестре."""
    system = registry.meaning(contract.economic_role_id).get("system") if contract.economic_role_id else None
    if not system:
        system, _source = _economic(contract, registry)
    return system == "revenue"


class Turnover:
    """Оборот наших юрлиц за год - начисления, НДС и АВР книги, на один запрос."""

    def __init__(self, session: Session, workspace: Workspace, access: Access, year: int, *, today: date):
        self.session = session
        self.workspace = workspace
        self.year = year
        self.today = today
        self.registry = Registry(session, workspace)
        contracts = list(
            session.scalars(sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None)))
        )
        people = people_of(session, [item.id for item in contracts])
        self.contracts = [
            item
            for item in contracts
            if visible_to(item, self.registry, access, people.get(item.id, [])) and _revenue(item, self.registry)
        ]
        self.ledger = Ledger(session, workspace.id, self.contracts)
        self.entities = {
            row.counterparty_id: row
            for row in session.scalars(
                sa.select(GroupEntity).where(GroupEntity.workspace_id == workspace.id, GroupEntity.archived_at.is_(None))
            )
        }
        self.names = dict(
            session.execute(
                sa.select(Counterparty.id, Counterparty.name).where(Counterparty.id.in_(list(self.entities) or [uuid.uuid4()]))
            ).all()
        )
        self.periods: dict[uuid.UUID, list[EntityTaxPeriod]] = {}
        self.years = tax.years(session, workspace.id)

    @property
    def tax_year(self) -> tax.Year:
        return tax.year_of(self.session, self.workspace.id, self.year, self.years)

    def _payer(self, entity_id: uuid.UUID, day: date) -> bool:
        period = tax.regime_on(self.session, entity_id, day, self.periods)
        return bool(period and period.vat_payer)

    def run(self, moves: Iterable[Move] = (), *, acts: dict[tuple[uuid.UUID, str], Decimal] | None = None) -> dict[uuid.UUID, EntityYear]:
        moved = {item.contract_id: item for item in moves}
        out = {
            entity_id: EntityYear(
                entity_id=entity_id,
                code=row.code or "",
                name=self.names.get(entity_id, ""),
                year=self.year,
                months=[
                    Month(month=f"{self.year:04d}-{index:02d}", forecast=f"{self.year:04d}-{index:02d}" > month_key(self.today))
                    for index in range(1, 13)
                ],
            )
            for entity_id, row in self.entities.items()
        }
        for entity_id, item in out.items():
            period = tax.regime_on(self.session, entity_id, self.today, self.periods)
            item.payer_now = bool(period and period.vat_payer)
            item.regime = tax.REGIME_TITLES.get(period.regime, "") if period else ""
            item.payer_since = next(
                (row.valid_from for row in reversed(self.periods.get(entity_id, [])) if row.vat_payer and row.valid_from <= self.today),
                None,
            )
        since, until = date(self.year, 1, 1), date(self.year, 12, 1)
        rate_of = {self.year: self.tax_year.vat_rate}
        counted: dict[tuple[uuid.UUID, str], set[uuid.UUID]] = {}
        for contract in self.contracts:
            schedule = self.ledger.schedule(contract, since=since, until=until)
            move = moved.get(contract.id)
            for accrual in schedule.accruals:
                amount = _ZERO if accrual.skipped else (accrual.amount or _ZERO)
                if not amount:
                    continue
                executor = accrual.executor_id
                if move is not None and accrual.period_from >= move.since:
                    executor = move.target
                entity = out.get(executor)
                if entity is None:
                    continue
                index = int(accrual.month[5:7]) - 1
                payer = self._payer(executor, accrual.period_from)
                net, vat, _gross = vat_of(amount, payer=payer, mode=contract.vat_mode or "included", rate=rate_of[self.year])
                entity.months[index].net += net
                entity.months[index].vat += vat
                counted.setdefault((executor, accrual.month), set()).add(contract.id)
        for (entity_id, month), ids in counted.items():
            out[entity_id].months[int(month[5:7]) - 1].contracts = len(ids)
        for (entity_id, month), value in (acts or {}).items():
            if entity_id in out and month.startswith(f"{self.year:04d}-"):
                out[entity_id].months[int(month[5:7]) - 1].acts = value
        return out

    def clients(self, entity_id: uuid.UUID) -> list[dict[str, Any]]:
        """Договоры юрлица, которые можно перевести: оборот каждого с текущего месяца до конца года."""
        since, until = month_start(month_key(self.today)), date(self.year, 12, 1)
        if since > until:
            return []
        names = dict(
            self.session.execute(
                sa.select(Counterparty.id, Counterparty.name).where(
                    Counterparty.id.in_([item.customer_id for item in self.contracts if item.customer_id] or [uuid.uuid4()])
                )
            ).all()
        )
        out = []
        for contract in self.contracts:
            schedule = self.ledger.schedule(contract, since=since, until=until)
            rest = [item for item in schedule.accruals if item.executor_id == entity_id and not item.skipped and item.amount]
            if not rest:
                continue
            out.append(
                {
                    "contract_id": str(contract.id),
                    "number": contract.number,
                    "customer_id": str(contract.customer_id) if contract.customer_id else None,
                    "customer": names.get(contract.customer_id, ""),
                    "monthly": format(rest[-1].amount, "f"),
                    "rest": format(sum((item.amount for item in rest), _ZERO), "f"),
                    "next": rest[0].period_from.isoformat(),
                }
            )
        out.sort(key=lambda item: -Decimal(item["rest"]))
        return out


def book_acts(reconciler: Any) -> dict[tuple[uuid.UUID, str], Decimal]:
    """АВР из книги-сводки по юрлицу и месяцу даты АВР - проверка факта оборота."""
    out: dict[tuple[uuid.UUID, str], Decimal] = {}
    for months in reconciler.grouped.values():
        for item in months.values():
            for row in item.rows:
                if item.entity_id is None or row.act_sum is None or row.act_date is None:
                    continue
                key = (item.entity_id, month_key(row.act_date))
                out[key] = out.get(key, _ZERO) + row.act_sum
    return out


def year_out(turnover: Turnover, entities: dict[uuid.UUID, EntityYear]) -> dict[str, Any]:
    year = turnover.tax_year
    return {
        "year": year.out(),
        "today": turnover.today.isoformat(),
        "entities": [
            item.out(year.threshold)
            for item in sorted(entities.values(), key=lambda row: (-row.forecast, row.code or row.name))
        ],
    }


__all__ = ["Move", "Turnover", "VAT_MODES", "book_acts", "vat_of", "year_out"]
