"""finance: способ заполнения поля реестра и оплаты по договору

**Способ заполнения.** Колонка статуса, отдела, ответственного и исполнителя
принимала любой текст: напечатанное заводило новое значение справочника («им»
среди статусов, «Взыскание» среди видов — оба ни в одном договоре). Теперь у
поля-списка есть способ заполнения: только из списка, из списка или своё, у
стороны — только наши юрлица. По нему лист ставит выпадающий список, карточка
решает, предлагать ли «+ Завести», а сервер — заводить новое или отказать.
Пусто — умолчание поля (`fields.fill_of`): существующие поля не
переписываются, и смена умолчания в коде доезжает до всех, кто его не трогал.

**Оплаты по договору.** «Оплачено» и «Остаток» считаются из журнала операций
при чтении (`contracts/payments.py`); хранится только решение человека по
платежу, который подходит к нескольким договорам (`contract_payments`).

Revision ID: 0022
Revises: 0021
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0022'
down_revision: Union[str, None] = '0021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции — для Postgres.
        return
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("entity_fields", schema=SCHEMA)}
    if "fill" not in columns:
        op.add_column(
            "entity_fields",
            sa.Column("fill", sa.Text(), server_default=sa.text("''"), nullable=False),
            schema=SCHEMA,
        )
    if not inspector.has_table("contract_payments", schema=SCHEMA):
        op.create_table(
            "contract_payments",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("operation_id", sa.Uuid(), nullable=False),
            sa.Column("contract_id", sa.Uuid(), nullable=True),
            sa.Column("created_by", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["finance.workspaces.id"],
                name=op.f("fk_contract_payments_workspace_id"), ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["operation_id"], ["finance.operations.id"],
                name=op.f("fk_contract_payments_operation_id"), ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["contract_id"], ["finance.contracts.id"],
                name=op.f("fk_contract_payments_contract_id"), ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["created_by"], ["finance.users.id"],
                name=op.f("fk_contract_payments_created_by"), ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_contract_payments")),
            sa.UniqueConstraint("operation_id", name="uq_contract_payments_operation"),
            schema=SCHEMA,
        )
        op.create_index(
            op.f("ix_contract_payments_workspace_id"), "contract_payments", ["workspace_id"], unique=False, schema=SCHEMA
        )
        op.create_index(
            op.f("ix_contract_payments_contract_id"), "contract_payments", ["contract_id"], unique=False, schema=SCHEMA
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_index(op.f("ix_contract_payments_contract_id"), table_name="contract_payments", schema=SCHEMA)
    op.drop_index(op.f("ix_contract_payments_workspace_id"), table_name="contract_payments", schema=SCHEMA)
    op.drop_table("contract_payments", schema=SCHEMA)
    op.drop_column("entity_fields", "fill", schema=SCHEMA)
