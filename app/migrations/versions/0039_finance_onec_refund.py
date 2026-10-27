"""finance: возврат покупателю из 1С

Ядро KORT, этап 2, шаг 4 (27.10.2026).

Компания с 1С без таблиц входит в KORT из одной базы: поступления 1С -
источник оплат юрлица. Возврат переплаты клиенту («Платёжное поручение
исходящее» с видом «Возврат денежных средств покупателю») 1С не читался, и
долг такого договора выходил меньше настоящего на сумму возврата (полигон,
О6). Документ 1С вида `refund` - оплата со знаком минус.

Revision ID: 0039
Revises: 0038
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"

KINDS_OLD = "kind IN ('invoice', 'sale', 'sf', 'esf', 'payment')"
KINDS_NEW = "kind IN ('invoice', 'sale', 'sf', 'esf', 'payment', 'refund')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    op.drop_constraint(op.f("ck_onec_documents_onec_document_kind"), "onec_documents", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_onec_documents_onec_document_kind"), "onec_documents", KINDS_NEW, schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.execute(sa.text(f"DELETE FROM {SCHEMA}.onec_documents WHERE kind = 'refund'"))
    op.drop_constraint(op.f("ck_onec_documents_onec_document_kind"), "onec_documents", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_onec_documents_onec_document_kind"), "onec_documents", KINDS_OLD, schema=SCHEMA)
