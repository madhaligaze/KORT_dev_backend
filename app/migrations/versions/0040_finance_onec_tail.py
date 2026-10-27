"""finance: начисление по документам и покупки из 1С

Ядро KORT, этап 2, хвост шага 4 (27.10.2026).

Аренда с коммунальными по показаниям счётчиков: сумма месяца - аренда плюс
коммунальные, и она разная каждый месяц (полигон, Д11). Договору «в месяц» с
одной суммой такой месяц не выразить: заведённый из 1С договор брал сумму
первого акта, и долг расходился с правдой на коммунальные каждого месяца.
Вид начисления `documents` - сумма месяца по документам 1С: АВР месяца, а
пока его нет - счёт на оплату (`core/accruals.py`).

Покупки («мы должны») 1С не читала: только продажи. Документ 1С `purchase` -
«Поступление товаров и услуг» (акт поставщика), `supplier_payment` -
«Платёжное поручение исходящее» с видом «Оплата поставщику» (полигон, Д17).

Revision ID: 0040
Revises: 0039
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"

BILLING_OLD = "billing = '' OR billing IN ('month', 'total', 'terms')"
BILLING_NEW = "billing = '' OR billing IN ('month', 'total', 'terms', 'documents')"
KINDS_OLD = "kind IN ('invoice', 'sale', 'sf', 'esf', 'payment', 'refund')"
KINDS_NEW = "kind IN ('invoice', 'sale', 'sf', 'esf', 'payment', 'refund', 'purchase', 'supplier_payment')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    op.drop_constraint(op.f("ck_contracts_contract_billing"), "contracts", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_contracts_contract_billing"), "contracts", BILLING_NEW, schema=SCHEMA)
    op.drop_constraint(op.f("ck_onec_documents_onec_document_kind"), "onec_documents", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_onec_documents_onec_document_kind"), "onec_documents", KINDS_NEW, schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.execute(sa.text(f"DELETE FROM {SCHEMA}.onec_documents WHERE kind IN ('purchase', 'supplier_payment')"))
    op.drop_constraint(op.f("ck_onec_documents_onec_document_kind"), "onec_documents", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_onec_documents_onec_document_kind"), "onec_documents", KINDS_OLD, schema=SCHEMA)
    op.execute(sa.text(f"UPDATE {SCHEMA}.contracts SET billing = 'month' WHERE billing = 'documents'"))
    op.drop_constraint(op.f("ck_contracts_contract_billing"), "contracts", schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f("ck_contracts_contract_billing"), "contracts", BILLING_OLD, schema=SCHEMA)
