"""Подключения к базам 1С и документы, прочитанные из них.

Что здесь правда, а что производное
───────────────────────────────────
**Правда 1С** - документы базы: счета на оплату, реализации (АВР),
счета-фактуры, ЭСФ, поступления. Здесь лежит их снимок с последнего чтения:
номер, дата, сумма, контрагент и договор - как написано в 1С. В KORT он не
правится; меняет его только следующее чтение базы.

**Решение человека** - «этот документ по такому договору» или «не по
договору» (`manual_*`). Хранится на строке документа, чтение 1С его не
трогает.

**Производное** - к какому договору реестра документ относится и в каком
месяце лежит. Считается при показе (`onec/match.py`), как оплаты по выписке:
поменяли номер договора в реестре - ответ меняется сразу, без перепривязки.

Одна база - одно юрлицо
───────────────────────
У группы BBC у каждого ТОО своя база 1С, и база привязывается к «нашему
юрлицу» реестра (`group_entity_id`): её реализации - продажи этого юрлица,
договор ищется только среди договоров, где оно исполнитель. База, где
организаций несколько, сначала просит выбрать организацию
(`organization_ref`) - иначе документы чужого ТОО легли бы в договоры этого.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.finance.db import FinanceBase
from app.finance.models import JSONB, MONEY, _in


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


BASE_STATES = ("active", "off")
#: Виды документов 1С: счёт на оплату, реализация (АВР), счёт-фактура,
#: электронный счёт-фактура, поступление от покупателя.
DOCUMENT_KINDS = ("invoice", "sale", "sf", "esf", "payment")
#: Решение человека: `link` - по договору `manual_contract_id`, `none` - не по
#: договору реестра. Пусто - решает сопоставление.
MANUAL = ("", "link", "none")


class OnecBase(FinanceBase):
    """База 1С компании, из которой KORT читает документы."""

    __tablename__ = "onec_bases"
    __table_args__ = (
        sa.CheckConstraint(_in("state", BASE_STATES), name="onec_base_state"),
        sa.UniqueConstraint("workspace_id", "url", name="uq_onec_bases_url"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    #: Как базу называют люди: «ИБ ТОО "Big Business Consulting"».
    title: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: Адрес базы без хвостов веб-клиента: https://1cfresh.kz/a/ea201/284171.
    url: Mapped[str] = mapped_column(sa.Text)
    #: Пользователь автоматического REST-сервиса (обычно `odata.user`).
    login: Mapped[str] = mapped_column(sa.Text)
    #: Пароль, зашифрованный ключом окружения (`secretbox.py`). Открытым не лежит.
    secret: Mapped[str] = mapped_column(sa.Text)
    #: Наше юрлицо реестра, чья это база (`counterparties.id` юрлица группы).
    group_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("counterparties.id", ondelete="SET NULL")
    )
    #: Организация 1С, чьи документы читаются (`Ref_Key`).
    organization_ref: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    organization_name: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    organization_bin: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    state: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'active'"))
    #: С какой даты читать документы.
    sync_from: Mapped[date] = mapped_column(sa.Date)
    last_sync_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: Последний отказ чтения - человеческим текстом; пусто - прочиталось.
    last_error: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: Сколько документов каждого вида прочитано в последний раз.
    counts: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    #: Что прочитать не удалось, но чтение не остановило: вид документа не
    #: открыт в составе REST-сервиса и т. п.
    warnings: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'"))
    #: Организации базы на последнем чтении: [{ref, name, bin}].
    organizations: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'"))
    created_by: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class OnecDocument(FinanceBase):
    """Документ базы 1С - снимок с последнего чтения."""

    __tablename__ = "onec_documents"
    __table_args__ = (
        sa.CheckConstraint(_in("kind", DOCUMENT_KINDS), name="onec_document_kind"),
        sa.CheckConstraint(_in("manual", MANUAL), name="onec_document_manual"),
        sa.UniqueConstraint("base_id", "kind", "ref", name="uq_onec_documents_ref"),
        sa.Index("ix_onec_documents_workspace_date", "workspace_id", "doc_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    base_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("onec_bases.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(sa.Text)
    #: `Ref_Key` документа 1С; у строки поступления - `Ref_Key:номер строки`.
    ref: Mapped[str] = mapped_column(sa.Text)
    #: Номер как в 1С («00000000444»).
    number: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    doc_date: Mapped[date] = mapped_column(sa.Date)
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    posted: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    #: Помечен на удаление в 1С.
    deleted: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    #: В последнем чтении документа уже не было - удалён в 1С насовсем.
    gone: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    party_ref: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    party_name: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    party_full_name: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    party_bin: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    contract_ref: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    contract_number: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    contract_date: Mapped[date | None] = mapped_column(sa.Date)
    #: Основание: у счёта-фактуры - реализация, у ЭСФ - счёт-фактура, у
    #: поступления - счёт на оплату из расшифровки платежа.
    basis_ref: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: Реализация: дата подписания акта покупателем.
    signed_at: Mapped[date | None] = mapped_column(sa.Date)
    #: ЭСФ: состояние в ИС ЭСФ; счёт-фактура - способ выставления.
    status: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: ЭСФ: регистрационный номер в ИС ЭСФ.
    reg_number: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: Период услуги: отчётный период акта, «за период с … по …» счёта.
    period_from: Mapped[date | None] = mapped_column(sa.Date)
    period_to: Mapped[date | None] = mapped_column(sa.Date)
    #: Содержание первой строки услуг или назначение платежа.
    content: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    data_version: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    synced_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    manual: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    manual_contract_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("contracts.id", ondelete="SET NULL")
    )
    manual_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    manual_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


__all__ = ["BASE_STATES", "DOCUMENT_KINDS", "MANUAL", "OnecBase", "OnecDocument"]
