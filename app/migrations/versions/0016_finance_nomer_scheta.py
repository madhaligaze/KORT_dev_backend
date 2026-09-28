"""finance: номер счёта в банке

`accounts.number` — IBAN счёта. По нему выписка сама находит свой счёт, а
перевод на свой депозит отличается от расхода: в выписке юрлица рядом с
контрагентом напечатан его счёт, и если это наш счёт, операция — перевод.

Пустая строка, а не NULL: у кассы и сейфа номера нет, и сравнение «номер
совпал» не должно спотыкаться о NULL.

Revision ID: 0016
Revises: 0015
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0016'
down_revision: Union[str, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.add_column(
        'accounts',
        sa.Column('number', sa.Text(), server_default=sa.text("''"), nullable=False),
        schema='finance',
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_column('accounts', 'number', schema='finance')
