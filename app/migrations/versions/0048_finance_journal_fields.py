"""finance: журнал финансистов - поля строки журнала у операции

Журнал финансистов (04.11.2026) ведётся по строке выписки. У строки, кроме
даты, счёта, суммы и статьи, есть «ФОТ/Детали», вопрос к строке («Доп.
комменты / Вопросы») и дробление: одна сумма выписки, разбитая на части по
месяцам, договорам или статьям («Общий 400 000» - 100 000 за старые месяцы и
300 000 за июнь). Части - отдельные операции с общей группой и суммой
выписки, чтобы каждая ложилась в свой месяц и свой договор.

Номер договора и «Старые (до …)» у операции уже есть (`contract_ref`,
`period_label`, ревизия 0035); месяц ОПиУ - период операции.

У статей - третья сторона, перемещение между своими счетами («Текущий счёт -
Депозит», «Пэй - Юр счёт»): у перевода своя статья, как в журнале BBC.

Revision ID: 0048
Revises: 0047
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    for name in ("details", "note"):
        op.add_column(
            "operations", sa.Column(name, sa.Text(), server_default=sa.text("''"), nullable=False), schema=SCHEMA
        )
    op.add_column("operations", sa.Column("split_group", sa.Uuid(), nullable=True), schema=SCHEMA)
    op.add_column("operations", sa.Column("split_total", sa.Numeric(18, 2), nullable=True), schema=SCHEMA)
    op.create_index(
        "ix_operations_workspace_id_split_group", "operations", ["workspace_id", "split_group"], schema=SCHEMA
    )
    op.drop_constraint("ck_categories_category_side", "categories", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_categories_category_side", "categories", "side IN ('income', 'expense', 'transfer')", schema=SCHEMA
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    # Статьи перемещений прежней схеме не по силам: снимаются вместе со ссылками.
    op.execute(
        sa.text(
            f"UPDATE {SCHEMA}.operations SET category_id = NULL WHERE category_id IN "
            f"(SELECT id FROM {SCHEMA}.categories WHERE side = 'transfer')"
        )
    )
    op.execute(sa.text(f"DELETE FROM {SCHEMA}.categories WHERE side = 'transfer'"))
    op.drop_constraint("ck_categories_category_side", "categories", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_categories_category_side", "categories", "side IN ('income', 'expense')", schema=SCHEMA
    )
    op.drop_index("ix_operations_workspace_id_split_group", "operations", schema=SCHEMA)
    for column in ("split_total", "split_group", "note", "details"):
        op.drop_column("operations", column, schema=SCHEMA)
