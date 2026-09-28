"""finance: живой режим журнала — номер изменения операции и удалённые насовсем

Лист «Таблица» опрашивает сервер, как реестр договоров: «что изменилось после
номера N». Для этого у операции появляется номер изменения, а удалённые
`DELETE` (план из счёта и из повторения) оставляют запись: иначе открытый
лист не узнал бы, что строки больше нет.

* `operations.seq` — номер изменения (счётчик компании «operations»),
  индекс `(workspace_id, seq)` под опрос;
* `operation_removals` — удалённые насовсем: операция, номер, когда.

Старым операциям номер не ставится (0): лист, открытый после этой ревизии,
читает журнал целиком и дальше спрашивает только новое.

Revision ID: 0020
Revises: 0019
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0020'
down_revision: Union[str, None] = '0019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции — для Postgres.
        return
    inspector = sa.inspect(bind)

    columns = {column["name"] for column in inspector.get_columns("operations", schema=SCHEMA)}
    if "seq" not in columns:
        op.add_column(
            "operations",
            sa.Column("seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
            schema=SCHEMA,
        )
    indexes = {index["name"] for index in inspector.get_indexes("operations", schema=SCHEMA)}
    if "ix_operations_workspace_id_seq" not in indexes:
        op.create_index(
            "ix_operations_workspace_id_seq", "operations", ["workspace_id", "seq"], unique=False, schema=SCHEMA
        )

    # Стенд с новым кодом мог завести таблицу сам (`create_all` в finance/db.py).
    if not inspector.has_table("operation_removals", schema=SCHEMA):
        op.create_table(
            "operation_removals",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("operation_id", sa.Uuid(), nullable=False),
            sa.Column("seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column(
                "removed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id"],
                [f"{SCHEMA}.workspaces.id"],
                name=op.f("fk_operation_removals_workspace_id_workspaces"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_operation_removals")),
            schema=SCHEMA,
        )
        op.create_index(
            "ix_operation_removals_workspace_id_seq",
            "operation_removals",
            ["workspace_id", "seq"],
            unique=False,
            schema=SCHEMA,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_index("ix_operation_removals_workspace_id_seq", table_name="operation_removals", schema=SCHEMA)
    op.drop_table("operation_removals", schema=SCHEMA)
    op.drop_index("ix_operations_workspace_id_seq", table_name="operations", schema=SCHEMA)
    op.drop_column("operations", "seq", schema=SCHEMA)
