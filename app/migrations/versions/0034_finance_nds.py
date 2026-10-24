"""finance: НДС договора и перевод клиентов между юрлицами

Ядро KORT, фаза 7 (25.10.2026). Расчёт НДС и порога - функция ядра (решение
владельца 24.10.2026). У договора появляется `vat_mode` - как сумма договора
относится к НДС, когда исполнитель - плательщик НДС: «в сумме» (пусто - так
же), «сверху», «без НДС». Не плательщику НДС не начисляется вовсе.

Перевод клиентов между нашими юрлицами - пачка изменений «исполнитель с
даты»; вернуть её целиком - новый вид точки восстановления `core_transfer`.

Revision ID: 0034
Revises: 0033
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"

KINDS_OLD = "kind IN ('contracts_delete', 'contracts_create', 'values', 'view', 'field_add', 'order', 'party_merge')"
KINDS_NEW = (
    "kind IN ('contracts_delete', 'contracts_create', 'values', 'view', 'field_add', 'order', 'party_merge', "
    "'core_transfer')"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("contracts", schema=SCHEMA)}
    if "vat_mode" not in columns:
        op.add_column(
            "contracts", sa.Column("vat_mode", sa.Text(), server_default=sa.text("''"), nullable=False), schema=SCHEMA
        )
        op.create_check_constraint(
            op.f("ck_contracts_contract_vat_mode"),
            "contracts",
            "vat_mode = '' OR vat_mode IN ('included', 'on_top', 'none')",
            schema=SCHEMA,
        )
    op.drop_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", KINDS_NEW, schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.execute(sa.text(f"DELETE FROM {SCHEMA}.restore_points WHERE kind = 'core_transfer'"))
    op.drop_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", KINDS_OLD, schema=SCHEMA)
    op.drop_constraint(op.f("ck_contracts_contract_vat_mode"), "contracts", schema=SCHEMA, type_="check")
    op.drop_column("contracts", "vat_mode", schema=SCHEMA)
