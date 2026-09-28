"""finance: номер закрытой учётки освобождается

Учётка сотрудника, которому закрыли доступ или которого убрали в архив,
держала номер навсегда: новый человек с этим номером получал «занят в другой
компании», хотя другой компании не было. Теперь такой номер отпускается — но
у учётки, заведённой по номеру, другого логина нет, а `ck_users_user_login`
требовал почту или телефон.

Учётка без почты и телефона допустима, только если она закрыта
(`status = blocked`): войти по ней нечем, а подписи под её прошлыми
действиями остаются — обезличивать их задним числом нельзя.

Revision ID: 0021
Revises: 0020
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = '0021'
down_revision: Union[str, None] = '0020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции — для Postgres.
        return
    op.drop_constraint(op.f('ck_users_user_login'), 'users', schema=SCHEMA, type_='check')
    op.create_check_constraint(
        op.f('ck_users_user_login'), 'users',
        "email_normalized IS NOT NULL OR phone IS NOT NULL OR status = 'blocked'", schema=SCHEMA,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_constraint(op.f('ck_users_user_login'), 'users', schema=SCHEMA, type_='check')
    op.create_check_constraint(
        op.f('ck_users_user_login'), 'users',
        "email_normalized IS NOT NULL OR phone IS NOT NULL", schema=SCHEMA,
    )
