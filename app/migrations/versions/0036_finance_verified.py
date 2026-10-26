"""finance: «верно у KORT» - решение по сверке с таблицей

Ядро KORT, фаза 10 (26.10.2026).

Переход звена юрлица на KORT ждёт двух закрытых месяцев без расхождений с
таблицей. Но таблица ошибается сама: у части договоров «Период с» в одном
месяце сдвинут на день, и между периодами остаётся непокрытый день. KORT
считает по договору без разрыва, и подгонять его под ошибку таблицы нельзя.
Документ `verified` - решение человека «месяц договора у KORT верен»:
со снимком значений KORT, чтобы решение не прикрывало расхождение, которое
появится после правки условий договора.

Revision ID: 0036
Revises: 0035
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"

KINDS_OLD = "kind IN ('adjustment', 'bill', 'act', 'esf', 'opening')"
KINDS_NEW = "kind IN ('adjustment', 'bill', 'act', 'esf', 'opening', 'verified')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    op.drop_constraint(op.f("ck_documents_document_kind"), "documents", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_documents_document_kind"), "documents", KINDS_NEW, schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.execute(sa.text(f"DELETE FROM {SCHEMA}.documents WHERE kind = 'verified'"))
    op.drop_constraint(op.f("ck_documents_document_kind"), "documents", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_documents_document_kind"), "documents", KINDS_OLD, schema=SCHEMA)
