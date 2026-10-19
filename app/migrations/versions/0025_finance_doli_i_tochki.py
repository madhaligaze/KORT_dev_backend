"""finance: доли исполнителей и отделов в договоре, точки восстановления листа

**Доли людей.** `contract_people.share_amount` / `share_percent` - доля
ответственного в договоре суммой или процентом, одно из двух (как ввели). В
реестре ЮО BBC 6 договоров из 421 на двоих-троих («Елжас, Рысбек · 700 000»,
«Тимур, Салтанат, Алтынай · 348 000»), а книги долей не хранили вовсе: сводка
по сотрудникам считала `SUMIFS` по точному имени, и такие договоры из неё
выпадали. Пусто - доля не задана.

**Доли отделов.** `contract_departments` - отделы, работающие над договором
вместе, с долей. Отдел договора (`contracts.department_id`, колонка «Отдел»)
остаётся одним: по нему листы, права и отборы. Здесь - только разделение.

**Точки восстановления.** `restore_points` - что было до изменения, которое
лист называет ломающим (удаление договоров, колонка, шапка, лист, массовая
правка): вернуть его можно из кабинета или Ctrl+Z. Хранится только то, что
изменение задевает, - не снимок всей таблицы: вернуть свою правку не должно
значить стереть правки коллег, сделанные после.

Revision ID: 0025
Revises: 0024
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0025'
down_revision: Union[str, None] = '0024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("contract_people", schema=SCHEMA)}
    if "share_amount" not in columns:
        op.add_column("contract_people", sa.Column("share_amount", sa.Numeric(18, 2), nullable=True), schema=SCHEMA)
    if "share_percent" not in columns:
        op.add_column("contract_people", sa.Column("share_percent", sa.Numeric(9, 4), nullable=True), schema=SCHEMA)
    if not inspector.has_table("contract_departments", schema=SCHEMA):
        op.create_table(
            "contract_departments",
            sa.Column("contract_id", sa.Uuid(), nullable=False),
            sa.Column("department_id", sa.Uuid(), nullable=False),
            sa.Column("share_amount", sa.Numeric(18, 2), nullable=True),
            sa.Column("share_percent", sa.Numeric(9, 4), nullable=True),
            sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.ForeignKeyConstraint(
                ["contract_id"], ["finance.contracts.id"],
                name=op.f("fk_contract_departments_contract_id"), ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["department_id"], ["finance.departments.id"],
                name=op.f("fk_contract_departments_department_id"), ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("contract_id", "department_id", name=op.f("pk_contract_departments")),
            schema=SCHEMA,
        )
        op.create_index(
            op.f("ix_contract_departments_department_id"), "contract_departments", ["department_id"], schema=SCHEMA
        )
    if not inspector.has_table("restore_points", schema=SCHEMA):
        op.create_table(
            "restore_points",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=True),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("book", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("view_key", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column(
                "payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("restored_by", sa.Uuid(), nullable=True),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["finance.workspaces.id"],
                name=op.f("fk_restore_points_workspace_id"), ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], ["finance.users.id"], name=op.f("fk_restore_points_user_id"), ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["restored_by"], ["finance.users.id"], name=op.f("fk_restore_points_restored_by"), ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_restore_points")),
            sa.CheckConstraint(
                "kind IN ('contracts_delete', 'contracts_create', 'values', 'view', 'field_add', 'order')",
                name=op.f("ck_restore_points_restore_point_kind"),
            ),
            schema=SCHEMA,
        )
        op.create_index(
            "ix_restore_points_workspace_created", "restore_points", ["workspace_id", "created_at"], schema=SCHEMA
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_index("ix_restore_points_workspace_created", table_name="restore_points", schema=SCHEMA)
    op.drop_table("restore_points", schema=SCHEMA)
    op.drop_index(op.f("ix_contract_departments_department_id"), table_name="contract_departments", schema=SCHEMA)
    op.drop_table("contract_departments", schema=SCHEMA)
    op.drop_column("contract_people", "share_percent", schema=SCHEMA)
    op.drop_column("contract_people", "share_amount", schema=SCHEMA)
