"""finance: «Разовые», сводка оплат и личный вид листа

**Книга листа.** `entity_views.book`: `""` — реестр (карточки и «Таблица»),
`oneoff` — «Разовые». Договоры те же, наборы листов разные: у финансистов
разовые услуги жили отдельной книгой со своими отборами («до 2 мес»,
«Остатки»), и в общий реестр эти листы не просятся.

**Сводка оплат.** `contract_summary_sources` — книга Google, откуда берётся
«Оплачено (сводка)»: у BBC — «Осн.Общая сводка BBC 2026», лист «Сводка все
ЮР лица». Одна на компанию. Сами суммы не хранятся — читаются из книги и
держатся в памяти (`contracts/summary.py`).

**Личный вид листа.** `sheet_looks` — ширины, перенос, цвета, скрытые
колонки: у каждой учётки свои, по ключу листа. Данных не меняет.

Revision ID: 0023
Revises: 0022
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0023'
down_revision: Union[str, None] = '0022'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции — для Postgres.
        return
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("entity_views", schema=SCHEMA)}
    if "book" not in columns:
        op.add_column(
            "entity_views",
            sa.Column("book", sa.Text(), server_default=sa.text("''"), nullable=False),
            schema=SCHEMA,
        )
    if not inspector.has_table("contract_summary_sources", schema=SCHEMA):
        op.create_table(
            "contract_summary_sources",
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("spreadsheet_id", sa.Text(), nullable=False),
            sa.Column("worksheet", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("updated_by", sa.Uuid(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["finance.workspaces.id"],
                name=op.f("fk_contract_summary_sources_workspace_id"), ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["updated_by"], ["finance.users.id"],
                name=op.f("fk_contract_summary_sources_updated_by"), ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("workspace_id", name=op.f("pk_contract_summary_sources")),
            schema=SCHEMA,
        )
    if not inspector.has_table("sheet_looks", schema=SCHEMA):
        op.create_table(
            "sheet_looks",
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("key", sa.Text(), nullable=False),
            sa.Column(
                "look", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(
                ["user_id"], ["finance.users.id"], name=op.f("fk_sheet_looks_user_id"), ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["finance.workspaces.id"],
                name=op.f("fk_sheet_looks_workspace_id"), ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("user_id", "workspace_id", "key", name=op.f("pk_sheet_looks")),
            schema=SCHEMA,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_table("sheet_looks", schema=SCHEMA)
    op.drop_table("contract_summary_sources", schema=SCHEMA)
    op.drop_column("entity_views", "book", schema=SCHEMA)
