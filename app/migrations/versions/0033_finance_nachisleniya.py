"""finance: начисления из условий договора и документы

Ядро KORT, фаза 2 (25.10.2026). Начисления не хранятся - считаются из условий
договора: вида начисления, суммы и сторон по соглашениям, статуса по датам.
Для этого у договора появляются день периода (`period_day`: «Период с»
книги - 25-е у договора от 25.06, но у договора от 25.05.2023 - 1-е) и дата
начала начислений (`billing_from`); пусто - от даты договора.

Соглашение может менять статус с даты (`effect = 'status'`: приостановка,
возобновление), а взятое из книги-сводки помечено `origin = 'book'`.

`documents` - события-документы договора за месяц: поправка начисления
(другая сумма или «не начислять», с причиной), а дальше счёт, АВР, ЭСФ и
входящее сальдо из KORT, 1С и книги. Повторное чтение источника не двоит
документ: ключ источника уникален.

Revision ID: 0033
Revises: 0032
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"

EFFECTS_OLD = "effect IN ('none', 'amount', 'executor', 'customer', 'end_date', 'other')"
EFFECTS_NEW = "effect IN ('none', 'amount', 'executor', 'customer', 'end_date', 'other', 'status')"
ORIGINS_OLD = "origin IN ('change', 'parsed')"
ORIGINS_NEW = "origin IN ('change', 'parsed', 'book')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("contracts", schema=SCHEMA)}
    if "period_day" not in columns:
        op.add_column("contracts", sa.Column("period_day", sa.SmallInteger(), nullable=True), schema=SCHEMA)
        op.create_check_constraint(
            op.f("ck_contracts_contract_period_day"),
            "contracts",
            "period_day IS NULL OR period_day BETWEEN 1 AND 31",
            schema=SCHEMA,
        )
    if "billing_from" not in columns:
        op.add_column("contracts", sa.Column("billing_from", sa.Date(), nullable=True), schema=SCHEMA)

    op.drop_constraint(
        op.f("ck_contract_amendments_contract_amendment_effect"), "contract_amendments", schema=SCHEMA, type_="check"
    )
    op.create_check_constraint(
        op.f("ck_contract_amendments_contract_amendment_effect"), "contract_amendments", EFFECTS_NEW, schema=SCHEMA
    )
    op.drop_constraint(
        op.f("ck_contract_amendments_contract_amendment_origin"), "contract_amendments", schema=SCHEMA, type_="check"
    )
    op.create_check_constraint(
        op.f("ck_contract_amendments_contract_amendment_origin"), "contract_amendments", ORIGINS_NEW, schema=SCHEMA
    )

    if not inspector.has_table("documents", schema=SCHEMA):
        op.create_table(
            "documents",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("contract_id", sa.Uuid(), nullable=True),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("period", sa.Date(), nullable=True),
            sa.Column("number", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("issued_on", sa.Date(), nullable=True),
            sa.Column("amount", sa.Numeric(18, 2), nullable=True),
            sa.Column("skip", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("state", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("reason", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("source", sa.Text(), server_default=sa.text("'kort'"), nullable=False),
            sa.Column("source_key", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
            sa.Column("seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column("created_by", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_by", sa.Uuid(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "kind IN ('adjustment', 'bill', 'act', 'esf', 'opening')", name=op.f("ck_documents_document_kind")
            ),
            sa.CheckConstraint("source IN ('kort', 'book', '1c')", name=op.f("ck_documents_document_source")),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["finance.workspaces.id"], name=op.f("fk_documents_workspace_id"), ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["contract_id"], ["finance.contracts.id"], name=op.f("fk_documents_contract_id"), ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["created_by"], ["finance.users.id"], name=op.f("fk_documents_created_by"), ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["updated_by"], ["finance.users.id"], name=op.f("fk_documents_updated_by"), ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
            schema=SCHEMA,
        )
        op.create_index(op.f("ix_documents_workspace_id"), "documents", ["workspace_id"], schema=SCHEMA)
        op.create_index(
            "ix_documents_contract_period", "documents", ["workspace_id", "contract_id", "period"], schema=SCHEMA
        )
        op.create_index(
            "uq_documents_source_key",
            "documents",
            ["workspace_id", "source", "kind", "source_key"],
            unique=True,
            schema=SCHEMA,
            postgresql_where=sa.text("source_key <> ''"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_table("documents", schema=SCHEMA)
    op.execute(sa.text(f"DELETE FROM {SCHEMA}.contract_amendments WHERE effect = 'status' OR origin = 'book'"))
    op.drop_constraint(
        op.f("ck_contract_amendments_contract_amendment_origin"), "contract_amendments", schema=SCHEMA, type_="check"
    )
    op.create_check_constraint(
        op.f("ck_contract_amendments_contract_amendment_origin"), "contract_amendments", ORIGINS_OLD, schema=SCHEMA
    )
    op.drop_constraint(
        op.f("ck_contract_amendments_contract_amendment_effect"), "contract_amendments", schema=SCHEMA, type_="check"
    )
    op.create_check_constraint(
        op.f("ck_contract_amendments_contract_amendment_effect"), "contract_amendments", EFFECTS_OLD, schema=SCHEMA
    )
    op.drop_constraint(op.f("ck_contracts_contract_period_day"), "contracts", schema=SCHEMA, type_="check")
    op.drop_column("contracts", "billing_from", schema=SCHEMA)
    op.drop_column("contracts", "period_day", schema=SCHEMA)
