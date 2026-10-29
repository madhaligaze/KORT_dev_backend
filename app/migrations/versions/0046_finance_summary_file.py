"""finance: сводная таблица компании - листом Google или файлом

Ядро KORT под любой фронт (30.10.2026). Сводную таблицу, с которой сверяется и
переходит на KORT компания, можно было подключить только листом Google.
Компания, которая ведёт её в Excel, не могла ни сверяться с ней, ни
переключать звенья («Переход»). Теперь источник бывает файлом: строки таблицы,
разобранной приёмником, лежат в `values` под названиями колонок ядра, новая
версия - новой загрузкой. Подключённые листы Google - `kind = google`, как были.

Revision ID: 0046
Revises: 0045
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"
TABLE = "contract_summary_sources"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    op.add_column(TABLE, sa.Column("kind", sa.Text(), server_default=sa.text("'google'"), nullable=False), schema=SCHEMA)
    op.add_column(TABLE, sa.Column("file_name", sa.Text(), server_default=sa.text("''"), nullable=False), schema=SCHEMA)
    op.add_column(TABLE, sa.Column("digest", sa.Text(), server_default=sa.text("''"), nullable=False), schema=SCHEMA)
    op.add_column(TABLE, sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), nullable=True), schema=SCHEMA)
    op.create_check_constraint(op.f("ck_contract_summary_sources_summary_source_kind"), TABLE, "kind IN ('google', 'file')", schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    # Таблица файлом прежней схеме не по силам: такие источники снимаются.
    op.execute(sa.text(f"DELETE FROM {SCHEMA}.{TABLE} WHERE kind = 'file'"))
    op.drop_constraint(op.f("ck_contract_summary_sources_summary_source_kind"), TABLE, schema=SCHEMA, type_="check")
    for column in ("values", "digest", "file_name", "kind"):
        op.drop_column(TABLE, column, schema=SCHEMA)
