"""finance: ожидания платёжного календаря - плановые платежи и обещания по долгам

Платёжный календарь ядра (30.10.2026). Остаток, поступления по договорам,
долги и факт календарь выводит сам; хранить нужно только то, чего в данных нет:
плановый платёж (приход или расход, юрлицо, день, сумма, строка календаря) и
обещание клиента заплатить долг договора к дате. Из таблицы ожидание приходит
с ключом строки - повторная загрузка не двоит. Сделан ли платёж, сводится при
чтении; хранится только решение человека («оплачено этой операцией», «не
будет»).

Revision ID: 0047
Revises: 0046
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"
TABLE = "cash_plans"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), server_default=sa.text("'payment'"), nullable=False),
        sa.Column("direction", sa.Text(), server_default=sa.text("'out'"), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("day", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("line", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("counterparty_id", sa.Uuid(), nullable=True),
        sa.Column("party", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=True),
        sa.Column("note", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("source", sa.Text(), server_default=sa.text("'manual'"), nullable=False),
        sa.Column("source_key", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("source_name", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('payment', 'promise')", name=op.f("ck_cash_plans_cash_plan_kind")),
        sa.CheckConstraint("direction IN ('in', 'out')", name=op.f("ck_cash_plans_cash_plan_direction")),
        sa.CheckConstraint("source IN ('manual', 'table', 'history')", name=op.f("ck_cash_plans_cash_plan_source")),
        sa.CheckConstraint("amount IS NULL OR amount >= 0", name=op.f("ck_cash_plans_cash_plan_amount_sign")),
        sa.ForeignKeyConstraint(["workspace_id"], [f"{SCHEMA}.workspaces.id"], name=op.f("fk_cash_plans_workspace_id"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["entity_id"], [f"{SCHEMA}.group_entities.counterparty_id"], name=op.f("fk_cash_plans_entity_id"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["category_id"], [f"{SCHEMA}.categories.id"], name=op.f("fk_cash_plans_category_id"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["counterparty_id"], [f"{SCHEMA}.counterparties.id"], name=op.f("fk_cash_plans_counterparty_id"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["contract_id"], [f"{SCHEMA}.contracts.id"], name=op.f("fk_cash_plans_contract_id"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["operation_id"], [f"{SCHEMA}.operations.id"], name=op.f("fk_cash_plans_operation_id"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], [f"{SCHEMA}.users.id"], name=op.f("fk_cash_plans_created_by"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], [f"{SCHEMA}.users.id"], name=op.f("fk_cash_plans_updated_by"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cash_plans")),
        schema=SCHEMA,
    )
    op.create_index(op.f("ix_cash_plans_workspace_id"), TABLE, ["workspace_id"], schema=SCHEMA)
    op.create_index("ix_cash_plans_workspace_day", TABLE, ["workspace_id", "day"], schema=SCHEMA)
    op.create_index("ix_cash_plans_contract", TABLE, ["workspace_id", "contract_id"], schema=SCHEMA)
    op.create_index(
        "uq_cash_plans_source_key", TABLE, ["workspace_id", "source_key"], unique=True, schema=SCHEMA,
        postgresql_where=sa.text("source_key <> ''"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    for name in ("uq_cash_plans_source_key", "ix_cash_plans_contract", "ix_cash_plans_workspace_day", op.f("ix_cash_plans_workspace_id")):
        op.drop_index(name, table_name=TABLE, schema=SCHEMA)
    op.drop_table(TABLE, schema=SCHEMA)
