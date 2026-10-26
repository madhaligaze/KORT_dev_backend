"""finance: журнал фактов

Ядро KORT, этап 2, шаг 1 (27.10.2026). Факты о договоре лежали в шести
таблицах своей формы: операции журнала денег, документы 1С, документы ядра,
соглашения, решения по оплатам и по документам 1С. `facts` - одна форма для
всех: оплата, счёт, АВР, ЭСФ, соглашение, поправка, сальдо, решение человека.
Таблицы-источники остаются хранилищами подключений; факты из них проводятся
(`core/journal.py`), и итоги ядра читают только журнал.

Журнал не правится: изменение источника - новая версия факта со ссылкой на
прежнюю, снятие - пометка «снят». Номер проведения (`seq`) - курсор для
«что изменилось после N».

Revision ID: 0037
Revises: 0036
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    inspector = sa.inspect(bind)
    if inspector.has_table("facts", schema=SCHEMA):
        return
    op.create_table(
        "facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=True),
        sa.Column("period", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("contract_id", sa.Uuid(), nullable=True),
        sa.Column("party_id", sa.Uuid(), nullable=True),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("author", sa.Uuid(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("replaces", sa.Uuid(), nullable=True),
        sa.Column("void", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("ended_seq", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('payment', 'bill', 'act', 'esf', 'agreement', 'adjustment', 'opening', 'decision')",
            name=op.f("ck_facts_fact_kind"),
        ),
        sa.CheckConstraint("source IN ('kort', 'journal', '1c', 'book')", name=op.f("ck_facts_fact_source")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["finance.workspaces.id"], name=op.f("fk_facts_workspace_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_facts")),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_facts_current_key",
        "facts",
        ["workspace_id", "key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("ended_seq IS NULL"),
    )
    op.create_index(
        "ix_facts_current_kind",
        "facts",
        ["workspace_id", "kind", "contract_id"],
        schema=SCHEMA,
        postgresql_where=sa.text("ended_seq IS NULL"),
    )
    op.create_index("ix_facts_workspace_seq", "facts", ["workspace_id", "seq"], schema=SCHEMA)
    op.create_index("ix_facts_contract", "facts", ["workspace_id", "contract_id", "seq"], schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_table("facts", schema=SCHEMA)
    op.execute(sa.text(f"DELETE FROM {SCHEMA}.counters WHERE name LIKE 'facts%'"))
