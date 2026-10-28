"""finance: точка восстановления пачки «применить»

Ядро KORT, этап 2, шаг 7 (28.10.2026) - API модели. «Применить» пишет пачку
команд одной транзакцией и одной точкой восстановления: вид `batch`, шаги -
прежние виды точек и шаги `restore.STEP_KINDS`, обратно - в обратном порядке
(`app/finance/model/apply.py`). Схема данных не меняется - только список
видов точки.

Revision ID: 0042
Revises: 0041
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"

KINDS_OLD = (
    "kind IN ('contracts_delete', 'contracts_create', 'values', 'view', 'field_add', 'order', 'party_merge', "
    "'core_transfer', 'core_opening')"
)
KINDS_NEW = (
    "kind IN ('contracts_delete', 'contracts_create', 'values', 'view', 'field_add', 'order', 'party_merge', "
    "'core_transfer', 'core_opening', 'batch')"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    op.drop_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", KINDS_NEW, schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.execute(sa.text(f"DELETE FROM {SCHEMA}.restore_points WHERE kind = 'batch'"))
    op.drop_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", KINDS_OLD, schema=SCHEMA)
