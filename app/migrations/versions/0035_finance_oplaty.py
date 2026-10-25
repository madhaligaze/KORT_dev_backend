"""finance: оплаты в ядре - номер договора у операции, настройки ядра, входящее сальдо

Ядро KORT, фаза 4 (25.10.2026).

`operations.contract_ref` - номер договора, как его написал источник. В
журнале движения денег у оплаты клиента бухгалтер пишет номер («№ЮО/84»), а
платит нередко не сам клиент: через Kaspi Pay от физлица, через другое ТОО.
По одному плательщику такую оплату к договору не отнести, по номеру - можно.
Это факт источника, а не решение: к какому договору оплата относится,
считается при чтении (`contracts/payments.py`), и номер там - довод, а не
адрес.

`operations.period_label` - «за какой период» словами источника, когда это не
месяц и не год («Старые (до Май/Июня 2026)»): такая оплата - не за месяцы
учёта, и разнесение (`core/payments.py`) в месяцы её не кладёт.

`core_settings` - решения компании, которых нет в данных: с какого дня KORT
ведёт оплаты (до него долг - входящее сальдо) и откуда оплаты каждого нашего
юрлица (журнал или 1С).

Входящее сальдо из книги переносится пачкой документов `opening`; вернуть
пачку - новый вид точки восстановления `core_opening`.

Revision ID: 0035
Revises: 0034
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"

KINDS_OLD = (
    "kind IN ('contracts_delete', 'contracts_create', 'values', 'view', 'field_add', 'order', 'party_merge', "
    "'core_transfer')"
)
KINDS_NEW = (
    "kind IN ('contracts_delete', 'contracts_create', 'values', 'view', 'field_add', 'order', 'party_merge', "
    "'core_transfer', 'core_opening')"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("operations", schema=SCHEMA)}
    for name in ("contract_ref", "period_label"):
        if name not in columns:
            op.add_column(
                "operations", sa.Column(name, sa.Text(), server_default=sa.text("''"), nullable=False), schema=SCHEMA
            )
    if not inspector.has_table("core_settings", schema=SCHEMA):
        op.create_table(
            "core_settings",
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
            sa.Column("updated_by", sa.Uuid(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["finance.workspaces.id"], name=op.f("fk_core_settings_workspace_id"), ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["updated_by"], ["finance.users.id"], name=op.f("fk_core_settings_updated_by"), ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("workspace_id", name=op.f("pk_core_settings")),
            schema=SCHEMA,
        )
    op.drop_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", KINDS_NEW, schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.execute(sa.text(f"DELETE FROM {SCHEMA}.restore_points WHERE kind = 'core_opening'"))
    op.drop_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", KINDS_OLD, schema=SCHEMA)
    inspector = sa.inspect(bind)
    if inspector.has_table("core_settings", schema=SCHEMA):
        op.drop_table("core_settings", schema=SCHEMA)
    columns = {column["name"] for column in inspector.get_columns("operations", schema=SCHEMA)}
    for name in ("period_label", "contract_ref"):
        if name in columns:
            op.drop_column("operations", name, schema=SCHEMA)
