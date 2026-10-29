"""finance: оплата делится между договорами - решение человека с суммой части

Ядро KORT, этап 2, п.10 страницы «Полигон целиком» (29.10.2026). Одна оплата
клиента за два договора («Оплата за сентябрь 2026», 280 000 = 200 000 +
80 000; полигон, О2) была спорной, а решить её человек мог только целиком к
одному договору: второй договор оставался должником, первый - с авансом.
Теперь решение по операции - строка на каждый договор с суммой части.

* `contract_payments.amount` - сумма части; пусто - вся операция (прежние
  решения не меняются).
* Уникальность - операция и договор, а не операция: у разделённой оплаты
  строк столько, сколько частей.

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
    op.drop_constraint("uq_contract_payments_operation", "contract_payments", schema=SCHEMA, type_="unique")
    op.create_unique_constraint(
        "uq_contract_payments_operation_contract", "contract_payments", ["operation_id", "contract_id"], schema=SCHEMA
    )
    op.create_index("ix_contract_payments_operation_id", "contract_payments", ["operation_id"], schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    # Разделённая оплата прежней схеме не по силам: остаётся решение первой частью.
    op.execute(sa.text(
        f"DELETE FROM {SCHEMA}.contract_payments a USING {SCHEMA}.contract_payments b "
        "WHERE a.operation_id = b.operation_id AND a.id > b.id"
    ))
    op.drop_index("ix_contract_payments_operation_id", table_name="contract_payments", schema=SCHEMA)
    op.drop_constraint("uq_contract_payments_operation_contract", "contract_payments", schema=SCHEMA, type_="unique")
    op.create_unique_constraint("uq_contract_payments_operation", "contract_payments", ["operation_id"], schema=SCHEMA)
    op.drop_column("contract_payments", "amount", schema=SCHEMA)
