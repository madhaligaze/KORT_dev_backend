"""Таблицы ядра: налоговый профиль наших юрлиц по датам и налоговые годы.

Что здесь правда, а что производное
───────────────────────────────────
**Правда** - режим нашего юрлица с даты (`entity_tax_periods`) и поправки
компании к налоговым числам года (`tax_years`): МРП, порог постановки на учёт
по НДС в МРП, ставка. Без поправки действуют числа РК из `core/tax.py`.

**Производное** - оборот юрлица к порогу, НДС в документе, прогноз по
месяцам: считаются из событий и условий договоров при чтении.

Почему режим - по датам, а не флажок «плательщик НДС» у юрлица: юрлицо встаёт
на учёт по НДС с числа, и обороты, документы и счета до этого числа
считаются без НДС, после - с ним. Флажок без даты переписал бы прошлое.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.finance.db import FinanceBase
from app.finance.models import MONEY, _in

#: Налоговый режим юрлица: общеустановленный, упрощённая декларация, другой
#: специальный (патент, розничный налог и т.п.). На специальном режиме
#: плательщиком НДС не бывают (НК РК с 2026 года) - проверяет `core/tax.py`.
TAX_REGIMES = ("general", "simplified", "other")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class EntityTaxPeriod(FinanceBase):
    """Режим нашего юрлица с даты: какой и плательщик ли НДС."""

    __tablename__ = "entity_tax_periods"
    __table_args__ = (
        sa.CheckConstraint(_in("regime", TAX_REGIMES), name="entity_tax_period_regime"),
        sa.UniqueConstraint("entity_id", "valid_from", name="uq_entity_tax_periods_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("group_entities.counterparty_id", ondelete="CASCADE"), index=True
    )
    valid_from: Mapped[date] = mapped_column(sa.Date)
    regime: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'general'"))
    vat_payer: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    note: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class TaxYear(FinanceBase):
    """Поправка компании к налоговым числам года.

    Числа года меняет закон о бюджете (МРП) и Налоговый кодекс (порог, ставка).
    Умолчания РК лежат в коде (`core/tax.py`); строка здесь нужна, когда
    компания знает число раньше, чем вышло обновление KORT, или ведёт учёт
    по-своему. `forecast` - год, числа которого ещё прогноз (МРП следующего
    года до закона о бюджете).
    """

    __tablename__ = "tax_years"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    year: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    mrp: Mapped[Decimal] = mapped_column(MONEY)
    vat_threshold_mrp: Mapped[int] = mapped_column(sa.Integer)
    vat_rate: Mapped[Decimal] = mapped_column(sa.Numeric(5, 2))
    forecast: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    note: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


__all__ = ["EntityTaxPeriod", "TAX_REGIMES", "TaxYear"]
