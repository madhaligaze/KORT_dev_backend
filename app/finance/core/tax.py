"""Налоговые числа года и режим наших юрлиц по датам.

Числа года (расчётный показатель - у РК МРП, порог постановки на учёт по НДС
в показателях, ставка НДС) по умолчанию - из пакета страны компании
(`app/finance/packs`). Компания может поправить год у себя (`TaxYear`): закон
о бюджете с новым показателем выходит раньше обновления KORT, а следующий год
до закона - прогноз.

Режим юрлица - с даты (`EntityTaxPeriod`): общеустановленный или специальный,
плательщик НДС или нет. На каких режимах плательщиком НДС бывают, говорит
страна; запись против этого система не примет.

Расчёт оборота к порогу и НДС документов - в `core/vat.py` (фаза 7 плана).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history, packs
from app.finance.contracts.models import GroupEntity
from app.finance.contracts.service import Access, Actor
from app.finance.core.models import TAX_REGIMES, EntityTaxPeriod, TaxYear
from app.finance.models import Counterparty, Workspace
from app.finance.service import FinanceError

#: Подписи режимов страны по умолчанию - для кода без компании.
REGIME_TITLES = dict(packs.country().regimes)


@dataclass(frozen=True)
class Year:
    """Налоговые числа года. `forecast` - число ещё не закон."""

    year: int
    mrp: Decimal
    threshold_mrp: int
    vat_rate: Decimal
    forecast: bool = False
    #: Откуда числа: код страны («KZ»), «компания» или «прогноз».
    source: str = ""

    @property
    def threshold(self) -> Decimal:
        return self.mrp * self.threshold_mrp

    def out(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "mrp": format(self.mrp, "f"),
            "threshold_mrp": self.threshold_mrp,
            "threshold": format(self.threshold, "f"),
            "vat_rate": format(self.vat_rate, "f"),
            "forecast": self.forecast,
            "source": self.source,
        }


def country_years(country: packs.Country) -> dict[int, Year]:
    """Годы по закону страны."""
    return {
        year: Year(year, item.unit, item.threshold_units, item.vat_rate, source=country.code)
        for year, item in country.years.items()
    }


class TaxError(FinanceError):
    """Отказ с причиной для человека."""


def _forecast(year: int, known: dict[int, Year]) -> Year:
    """Год за пределами известных: МРП растёт средней индексацией последних четырёх лет."""
    last = max(known)
    window = [known[y] for y in sorted(known) if y > last - 4]
    growth = Decimal("0")
    if len(window) > 1:
        steps = [window[i].mrp / window[i - 1].mrp - 1 for i in range(1, len(window))]
        growth = sum(steps, Decimal("0")) / len(steps)
    base = known[last]
    mrp = base.mrp
    for _ in range(year - last):
        mrp = (mrp * (1 + growth)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return Year(year, mrp, base.threshold_mrp, base.vat_rate, forecast=True, source="прогноз")


def years(session: Session, workspace_id: uuid.UUID) -> dict[int, Year]:
    """Известные годы: страна компании с поправками компании."""
    out = country_years(packs.country_of(session.get(Workspace, workspace_id)))
    for row in session.scalars(sa.select(TaxYear).where(TaxYear.workspace_id == workspace_id)):
        out[row.year] = Year(
            row.year, row.mrp, row.vat_threshold_mrp, row.vat_rate, forecast=row.forecast, source="компания"
        )
    return out


def year_of(session: Session, workspace_id: uuid.UUID, year: int, known: dict[int, Year] | None = None) -> Year:
    known = known or years(session, workspace_id)
    if year in known:
        return known[year]
    if year < min(known):
        return known[min(known)]
    return _forecast(year, known)


def set_year(
    session: Session, workspace: Workspace, access: Access, actor: Actor, year: int, data: dict[str, Any]
) -> dict[str, Any]:
    if not access.setup:
        raise PermissionError("Налоговые числа года меняет владелец или администратор")
    if not 2000 <= year <= 2100:
        raise TaxError("Год - от 2000 до 2100")
    try:
        mrp = Decimal(str(data["mrp"]))
        threshold = int(data["threshold_mrp"])
        rate = Decimal(str(data["vat_rate"]))
    except (KeyError, ValueError, ArithmeticError) as exc:
        raise TaxError("Нужны МРП, порог в МРП и ставка НДС числами") from exc
    if mrp <= 0 or threshold <= 0 or not Decimal("0") <= rate < Decimal("100"):
        raise TaxError("МРП и порог - больше нуля, ставка - от 0 до 100")
    row = session.get(TaxYear, (workspace.id, year))
    before = None
    if row is None:
        row = TaxYear(workspace_id=workspace.id, year=year, mrp=mrp, vat_threshold_mrp=threshold, vat_rate=rate)
        session.add(row)
    else:
        before = {"mrp": str(row.mrp), "threshold_mrp": row.vat_threshold_mrp, "vat_rate": str(row.vat_rate)}
        row.mrp, row.vat_threshold_mrp, row.vat_rate = mrp, threshold, rate
    row.forecast = bool(data.get("forecast"))
    row.note = str(data.get("note") or "").strip()
    row.updated_by = actor.user_id
    session.flush()
    history.write(
        session, workspace, kind="tax.year", entity="tax_year",
        title=f"налоговый {year} год: МРП {mrp}, порог {threshold} МРП, НДС {rate}%",
        before=before or {}, after={"mrp": str(mrp), "threshold_mrp": threshold, "vat_rate": str(rate)}, actor=actor.email,
    )
    return year_of(session, workspace.id, year).out()


def periods(session: Session, entity_id: uuid.UUID) -> list[EntityTaxPeriod]:
    return list(
        session.scalars(sa.select(EntityTaxPeriod).where(EntityTaxPeriod.entity_id == entity_id).order_by(EntityTaxPeriod.valid_from))
    )


def regime_on(session: Session, entity_id: uuid.UUID, day: date, cache: dict[uuid.UUID, list[EntityTaxPeriod]] | None = None) -> EntityTaxPeriod | None:
    """Режим юрлица на дату: последний период, начавшийся не позже неё."""
    items = cache.get(entity_id) if cache is not None and entity_id in cache else periods(session, entity_id)
    if cache is not None:
        cache[entity_id] = items
    current = None
    for item in items:
        if item.valid_from <= day:
            current = item
    return current


def regime_title(regime: str, country: packs.Country | None = None) -> str:
    return (country or packs.country()).regimes.get(regime, regime)


def period_out(item: EntityTaxPeriod) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "valid_from": item.valid_from.isoformat(),
        "regime": item.regime,
        "regime_title": regime_title(item.regime),
        "vat_payer": item.vat_payer,
        "note": item.note,
    }


def add_period(
    session: Session, workspace: Workspace, access: Access, actor: Actor, entity_id: uuid.UUID, data: dict[str, Any]
) -> dict[str, Any]:
    if not access.setup:
        raise PermissionError("Режим юрлица меняет владелец или администратор")
    entity = session.get(GroupEntity, entity_id)
    if entity is None or entity.workspace_id != workspace.id:
        raise TaxError("Такого нашего юрлица нет")
    try:
        valid_from = date.fromisoformat(str(data["valid_from"]))
    except (KeyError, ValueError) as exc:
        raise TaxError("Нужна дата начала режима") from exc
    country = packs.country_of(workspace)
    regime = str(data.get("regime") or "general")
    if regime not in TAX_REGIMES or regime not in country.regimes:
        raise TaxError("Режим: " + ", ".join(country.regimes[key] for key in TAX_REGIMES if key in country.regimes))
    vat_payer = bool(data.get("vat_payer"))
    if vat_payer and regime not in country.vat_regimes:
        raise TaxError(country.vat_regime_refusal)
    existing = session.scalar(
        sa.select(EntityTaxPeriod).where(EntityTaxPeriod.entity_id == entity_id, EntityTaxPeriod.valid_from == valid_from)
    )
    before = period_out(existing) if existing is not None else None
    if existing is None:
        existing = EntityTaxPeriod(workspace_id=workspace.id, entity_id=entity_id, valid_from=valid_from, created_by=actor.user_id)
        session.add(existing)
    existing.regime, existing.vat_payer = regime, vat_payer
    existing.note = str(data.get("note") or "").strip()
    session.flush()
    name = session.get(Counterparty, entity_id).name
    history.write(
        session, workspace, kind="tax.period", entity="group_entity", entity_id=entity_id,
        title=(
            f"{entity.code or name}: с {valid_from:%d.%m.%Y} - {country.regimes[regime]}"
            + (", плательщик НДС" if vat_payer else ", без НДС")
        ),
        before=before or {}, after=period_out(existing), actor=actor.email,
    )
    return {"periods": [period_out(item) for item in periods(session, entity_id)]}


def remove_period(
    session: Session, workspace: Workspace, access: Access, actor: Actor, entity_id: uuid.UUID, period_id: uuid.UUID
) -> dict[str, Any]:
    if not access.setup:
        raise PermissionError("Режим юрлица меняет владелец или администратор")
    item = session.get(EntityTaxPeriod, period_id)
    if item is None or item.entity_id != entity_id or item.workspace_id != workspace.id:
        raise TaxError("Такой записи режима нет")
    before = period_out(item)
    entity = session.get(GroupEntity, entity_id)
    name = (entity.code if entity is not None else "") or session.get(Counterparty, entity_id).name
    session.delete(item)
    session.flush()
    history.write(
        session, workspace, kind="tax.period.remove", entity="group_entity", entity_id=entity_id,
        title=f"{name}: режим с {item.valid_from:%d.%m.%Y} убран", before=before, actor=actor.email,
    )
    return {"periods": [period_out(row) for row in periods(session, entity_id)]}


__all__ = [
    "REGIME_TITLES",
    "TaxError",
    "Year",
    "add_period",
    "country_years",
    "regime_title",
    "period_out",
    "periods",
    "regime_on",
    "remove_period",
    "set_year",
    "year_of",
    "years",
]
