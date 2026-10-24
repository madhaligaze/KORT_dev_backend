"""Таблицы ядра: документы договора, налоговый профиль наших юрлиц по датам, налоговые годы.

Что здесь правда, а что производное
───────────────────────────────────
**Правда** - документы договора за месяц (`documents`: поправка начисления,
счёт, АВР, ЭСФ, входящее сальдо), режим нашего юрлица с даты
(`entity_tax_periods`) и поправки компании к налоговым числам года
(`tax_years`): МРП, порог постановки на учёт по НДС в МРП, ставка. Без
поправки действуют числа РК из `core/tax.py`.

**Производное** - начисления (`core/accruals.py` - из условий договора),
оборот юрлица к порогу, НДС в документе, прогноз по месяцам: считаются из
событий и условий договоров при чтении.

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
from app.finance.models import JSONB, MONEY, _in

#: Налоговый режим юрлица: общеустановленный, упрощённая декларация, другой
#: специальный (патент, розничный налог и т.п.). На специальном режиме
#: плательщиком НДС не бывают (НК РК с 2026 года) - проверяет `core/tax.py`.
TAX_REGIMES = ("general", "simplified", "other")
#: Документы договора: поправка начисления, счёт, АВР, ЭСФ, входящее сальдо.
DOCUMENT_KINDS = ("adjustment", "bill", "act", "esf", "opening")
#: Откуда документ: записан в KORT, прочитан из книги или из 1С.
DOCUMENT_SOURCES = ("kort", "book", "1c")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Document(FinanceBase):
    """Документ договора за месяц - событие, а не клетка книги.

    `period` - первый день месяца начисления, к которому документ относится:
    счёт за октябрь, выставленный 28 сентября, - всё равно октябрьский.

    Поправка (`adjustment`) - решение человека о начислении месяца: другая
    сумма или «не начислять» (`skip`), с причиной. Само начисление не хранится
    (`core/accruals.py`), поэтому поправка - единственный способ сказать «в
    этом месяце не так, как по договору», и он виден в истории.

    Из книги и 1С документ приходит с ключом источника: повторное чтение
    находит свой документ, а не заводит второй.
    """

    __tablename__ = "documents"
    __table_args__ = (
        sa.CheckConstraint(_in("kind", DOCUMENT_KINDS), name="document_kind"),
        sa.CheckConstraint(_in("source", DOCUMENT_SOURCES), name="document_source"),
        sa.Index("ix_documents_contract_period", "workspace_id", "contract_id", "period"),
        sa.Index(
            "uq_documents_source_key",
            "workspace_id",
            "source",
            "kind",
            "source_key",
            unique=True,
            postgresql_where=sa.text("source_key <> ''"),
            sqlite_where=sa.text("source_key <> ''"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("contracts.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(sa.Text)
    period: Mapped[date | None] = mapped_column(sa.Date)
    number: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    issued_on: Mapped[date | None] = mapped_column(sa.Date)
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    skip: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    state: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    reason: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    source: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'kort'"))
    source_key: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    meta: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    seq: Mapped[int] = mapped_column(sa.BigInteger, server_default=sa.text("0"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


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


__all__ = ["DOCUMENT_KINDS", "DOCUMENT_SOURCES", "Document", "EntityTaxPeriod", "TAX_REGIMES", "TaxYear"]
