"""finance: очередь правок журнала и регистры итогов

Ядро KORT, этап 2, шаг 2 (27.10.2026).

`fact_inputs` - что именно правил писатель (операция, договор или вся часть
журнала): читатель проводит в журнал фактов только это, а не весь журнал.
Правка, которая денег не трогает (примечание, статус договора), стоила
каждому экрану итогов 1,2 с проведения всех операций и документов 1С.

Регистры итогов (`register_*`, `core/registers.py`) - хранимый итог журнала и
условий договоров: начисления по периодам, расчёты по месяцам, итог договора и
состояние компании. Вывод, руками не правится: договор пересчитывается, когда
сменилась подпись его входов. Наполняются при первом чтении - миграция
только заводит таблицы.

Revision ID: 0038
Revises: 0037
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"
MONEY = sa.Numeric(18, 2)
TABLES = ("fact_inputs", "register_state", "register_contracts", "register_months", "register_accruals")


def _workspace(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["workspace_id"], ["finance.workspaces.id"], name=op.f(f"fk_{table}_workspace_id"), ondelete="CASCADE"
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("fact_inputs", schema=SCHEMA):
        op.create_table(
            "fact_inputs",
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("inputs", sa.BigInteger(), nullable=False),
            sa.Column("part", sa.Text(), nullable=False),
            sa.Column("ref", sa.Uuid(), nullable=False),
            _workspace("fact_inputs"),
            sa.PrimaryKeyConstraint("workspace_id", "inputs", "part", "ref", name=op.f("pk_fact_inputs")),
            schema=SCHEMA,
        )
    if not inspector.has_table("register_state", schema=SCHEMA):
        op.create_table(
            "register_state",
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("signature", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("day", sa.Date(), nullable=True),
            sa.Column("since", sa.Date(), nullable=True),
            sa.Column("facts_seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            _workspace("register_state"),
            sa.PrimaryKeyConstraint("workspace_id", name=op.f("pk_register_state")),
            schema=SCHEMA,
        )
    if not inspector.has_table("register_contracts", schema=SCHEMA):
        op.create_table(
            "register_contracts",
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("contract_id", sa.Uuid(), nullable=False),
            sa.Column("signature", sa.Text(), nullable=False),
            sa.Column("valid_until", sa.Date(), nullable=True),
            sa.Column("accrues", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("opening", MONEY, nullable=True),
            sa.Column("opening_paid", MONEY, server_default=sa.text("0"), nullable=False),
            sa.Column("opening_cash", MONEY, server_default=sa.text("0"), nullable=False),
            sa.Column("opening_executor", sa.Uuid(), nullable=True),
            sa.Column("over", MONEY, server_default=sa.text("0"), nullable=False),
            sa.Column("paid", MONEY, server_default=sa.text("0"), nullable=False),
            _workspace("register_contracts"),
            sa.PrimaryKeyConstraint("workspace_id", "contract_id", name=op.f("pk_register_contracts")),
            schema=SCHEMA,
        )
    if not inspector.has_table("register_months", schema=SCHEMA):
        op.create_table(
            "register_months",
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("contract_id", sa.Uuid(), nullable=False),
            sa.Column("month", sa.Date(), nullable=False),
            sa.Column("bucket", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("accrued", MONEY, server_default=sa.text("0"), nullable=False),
            sa.Column("starts", sa.Date(), nullable=True),
            sa.Column("executor_id", sa.Uuid(), nullable=True),
            sa.Column("paid", MONEY, server_default=sa.text("0"), nullable=False),
            sa.Column("cash", MONEY, server_default=sa.text("0"), nullable=False),
            sa.Column("attributed", MONEY, server_default=sa.text("0"), nullable=False),
            sa.Column("received", MONEY, server_default=sa.text("0"), nullable=False),
            _workspace("register_months"),
            sa.PrimaryKeyConstraint("workspace_id", "contract_id", "month", name=op.f("pk_register_months")),
            schema=SCHEMA,
        )
    if not inspector.has_table("register_accruals", schema=SCHEMA):
        op.create_table(
            "register_accruals",
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("contract_id", sa.Uuid(), nullable=False),
            sa.Column("period_from", sa.Date(), nullable=False),
            sa.Column("month", sa.Date(), nullable=False),
            sa.Column("period_to", sa.Date(), nullable=True),
            sa.Column("base", MONEY, nullable=True),
            sa.Column("amount", MONEY, nullable=True),
            sa.Column("executor_id", sa.Uuid(), nullable=True),
            sa.Column("customer_id", sa.Uuid(), nullable=True),
            sa.Column("adjustment_id", sa.Uuid(), nullable=True),
            sa.Column("skipped", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            _workspace("register_accruals"),
            sa.PrimaryKeyConstraint("workspace_id", "contract_id", "period_from", name=op.f("pk_register_accruals")),
            schema=SCHEMA,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TABLE IF EXISTS {SCHEMA}.{table}"))
