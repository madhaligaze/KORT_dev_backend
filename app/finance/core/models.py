"""Таблицы ядра: журнал фактов, документы договора, налоговый профиль наших юрлиц, налоговые годы.

Что здесь правда, а что производное
───────────────────────────────────
**Журнал фактов** (`facts`, `core/journal.py`) - одна форма для всего, что
случилось с договором: оплаты, счета, АВР, ЭСФ, соглашения, поправки, сальдо,
решения. Факты проводятся из хранилищ - операций журнала денег, документов
1С, документов ядра, соглашений - и не правятся: изменение - новая версия.
Итоги ядра читают журнал.

**Правда** - документы договора за месяц (`documents`: поправка начисления,
счёт, АВР, ЭСФ, входящее сальдо), режим нашего юрлица с даты
(`entity_tax_periods`), поправки компании к налоговым числам года
(`tax_years`): МРП, порог постановки на учёт по НДС в МРП, ставка (без
поправки действуют числа РК из `core/tax.py`), и настройки ядра компании
(`core_settings`): с какого дня ведутся оплаты и откуда они у каждого юрлица.

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
#: `verified` - решение человека по сверке с таблицей: месяц договора у KORT
#: верен, таблица в нём ошиблась или ещё не дописана (`core/changes.verify_month`).
DOCUMENT_KINDS = ("adjustment", "bill", "act", "esf", "opening", "verified")
#: Откуда документ: записан в KORT, прочитан из книги или из 1С.
DOCUMENT_SOURCES = ("kort", "book", "1c")
#: Виды фактов журнала (`core/journal.py`): оплата, счёт, АВР, ЭСФ,
#: соглашение, поправка начисления, входящее сальдо, решение человека.
FACT_KINDS = ("payment", "bill", "act", "esf", "agreement", "adjustment", "opening", "decision")
#: Откуда факт: записан человеком в KORT, журнал движения денег (выписки),
#: 1С, таблица Google.
FACT_SOURCES = ("kort", "journal", "1c", "book")


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


class Fact(FinanceBase):
    """Факт журнала: что случилось с договором, в одной форме для всех источников.

    Журнал не правится. Источник изменился - пишется новая версия факта с тем
    же ключом (`replaces` - прежняя, у прежней ставится `ended_seq`); источник
    убрал факт - пишется пометка «снят» (`void`). Так у любого числа есть
    ответ «из каких фактов и с какой записи», а у подписчика - «что изменилось
    после номера N» (`seq`). Единственная правка строки - `ended_seq`, и она
    выводима из `replaces`; хранится, чтобы «действующие» выбирались индексом.

    `key` - кто этот факт в своём хранилище: `op:<операция>`, `1c:<документ
    1С>`, `doc:<документ KORT>`, `amend:<соглашение>`, `cp:<операция>`
    (решение по оплате), `1cm:<документ 1С>` (решение по документу 1С).
    Ссылки на договор и стороны - без внешних ключей: журнал помнит и то, чего
    в реестре уже нет.
    """

    __tablename__ = "facts"
    __table_args__ = (
        sa.CheckConstraint(_in("kind", FACT_KINDS), name="fact_kind"),
        sa.CheckConstraint(_in("source", FACT_SOURCES), name="fact_source"),
        sa.Index(
            "uq_facts_current_key",
            "workspace_id",
            "key",
            unique=True,
            postgresql_where=sa.text("ended_seq IS NULL"),
            sqlite_where=sa.text("ended_seq IS NULL"),
        ),
        sa.Index(
            "ix_facts_current_kind",
            "workspace_id",
            "kind",
            "contract_id",
            postgresql_where=sa.text("ended_seq IS NULL"),
            sqlite_where=sa.text("ended_seq IS NULL"),
        ),
        sa.Index("ix_facts_workspace_seq", "workspace_id", "seq"),
        sa.Index("ix_facts_contract", "workspace_id", "contract_id", "seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"))
    #: Номер проведения, в котором факт записан: один на всё проведение.
    seq: Mapped[int] = mapped_column(sa.BigInteger)
    key: Mapped[str] = mapped_column(sa.Text)
    kind: Mapped[str] = mapped_column(sa.Text)
    source: Mapped[str] = mapped_column(sa.Text)
    #: Когда случилось: день платежа, дата документа, «с какого дня» соглашения.
    day: Mapped[date | None] = mapped_column(sa.Date)
    #: За какой месяц (первое число): месяц АВР, поправки, сальдо.
    period: Mapped[date | None] = mapped_column(sa.Date)
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    contract_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    #: Вторая сторона: плательщик, клиент.
    party_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    #: Наше юрлицо: чей счёт, чья база 1С.
    entity_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    #: Остальное своего вида: № и состояние, «за какие месяцы», как найден договор…
    data: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    #: Кто записал: человек у фактов KORT, пусто у фактов источника.
    author: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    recorded_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    #: Прежняя версия того же факта.
    replaces: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    #: Источник факт убрал: строка - пометка «снят», без суммы.
    void: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    #: Номер проведения, в котором факт сменила новая версия или пометка «снят».
    ended_seq: Mapped[int | None] = mapped_column(sa.BigInteger)


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


class CoreSettings(FinanceBase):
    """Настройки ядра компании - решения человека, которых нет в данных.

    `data` - словарь по разделам (`core/settings.py` знает ключи и умолчания):
    `payments.since` - с какого дня KORT ведёт оплаты (до него долг - входящее
    сальдо, а не неоплаченные начисления), `payments.sources` - откуда оплаты
    нашего юрлица: журнал движения денег или 1С (один источник на юрлицо,
    второй - для сверки).
    """

    __tablename__ = "core_settings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


__all__ = [
    "CoreSettings",
    "DOCUMENT_KINDS",
    "DOCUMENT_SOURCES",
    "Document",
    "EntityTaxPeriod",
    "FACT_KINDS",
    "FACT_SOURCES",
    "Fact",
    "TAX_REGIMES",
    "TaxYear",
]
