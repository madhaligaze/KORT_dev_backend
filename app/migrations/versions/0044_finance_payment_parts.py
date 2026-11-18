"""finance: оплата делится между договорами - решение человека с суммой части

Ядро KORT, этап 2, п.10 страницы «Полигон целиком» (29.10.2026). Одна оплата
клиента за два договора («Оплата за сентябрь 2026», 280 000 = 200 000 +
80 000; полигон, О2) была спорной, а решить её человек мог только целиком к
одному договору: второй договор оставался должником, первый - с авансом.
Теперь решение по операции - строка на каждый договор с суммой части.

* `contract_payments.amount` - сумма части; пусто - вся операция (прежние
  решения не меняются).

Уникальность остаётся прежней - одна строка на операцию (18.11.2026). Ядро
спит, а код, который работает на базе, решение по операции ищет и заводит
сам (`contracts/payments.py`), и только запрет базы не даёт двум разом
нажатым «отнести к договору» отнести одну оплату к двум договорам. Прежняя
редакция ревизии меняла его на «операция и договор»; это делается ревизией,
которая будит ядро, вместе с кодом, который делит оплату на части.

Revision ID: 0044
Revises: 0043
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    op.add_column("contract_payments", sa.Column("amount", sa.Numeric(18, 2), nullable=True), schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_column("contract_payments", "amount", schema=SCHEMA)
