"""finance: реестр договоров

Верхнее звено учёта: договор, его стороны и соглашения, ответственные,
справочники реестра (свои поля, значения списков со смыслом, листы-отборы),
наши юрлица и псевдонимы контрагентов, партии загрузки из Excel и счётчик
изменений для живого режима. Почему модель такая — в
`app/finance/contracts/models.py`.

Две колонки в таблицах денег, экраны которых не меняются:
`accounts.group_entity_id` — чьё это ТОО, `invoices.contract_id` — по какому
договору счёт. Обе необязательны и пусты до своей очереди; без них «оплата
нашла договор» и порог НДС потом переписали бы все таблицы денег.

Revision ID: 0018
Revises: 0016  (в PDF-CONVERTER между ними стояла 0017 «Таблиц»)
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0018'
down_revision: Union[str, None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.create_table('contract_imports',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('file_name', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('status', sa.Text(), server_default=sa.text("'preview'"), nullable=False),
    sa.Column('report', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('decisions', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('staged', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('preview', 'applied', 'cancelled')", name=op.f('ck_contract_imports_contract_import_status')),
    sa.ForeignKeyConstraint(['created_by'], ['finance.users.id'], name=op.f('fk_contract_imports_created_by'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['finance.workspaces.id'], name=op.f('fk_contract_imports_workspace_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contract_imports')),
    schema='finance'
    )
    op.create_index(op.f('ix_contract_imports_workspace_id'), 'contract_imports', ['workspace_id'], unique=False, schema='finance')
    op.create_table('counters',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('value', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['finance.workspaces.id'], name=op.f('fk_counters_workspace_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('workspace_id', 'name', name=op.f('pk_counters')),
    schema='finance'
    )
    op.create_table('departments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.Text(), nullable=False),
    sa.Column('normalized_name', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['workspace_id'], ['finance.workspaces.id'], name=op.f('fk_departments_workspace_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_departments')),
    sa.UniqueConstraint('workspace_id', 'normalized_name', name='uq_departments_name'),
    schema='finance'
    )
    op.create_index(op.f('ix_departments_workspace_id'), 'departments', ['workspace_id'], unique=False, schema='finance')
    op.create_table('entity_fields',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('entity', sa.Text(), server_default=sa.text("'contract'"), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('system', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('type', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('names', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('required', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('hidden', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("type IN ('text', 'number', 'money', 'date', 'bool', 'list', 'multi_list', 'url', 'person', 'party', 'department', 'choice')", name=op.f('ck_entity_fields_entity_field_type')),
    sa.ForeignKeyConstraint(['workspace_id'], ['finance.workspaces.id'], name=op.f('fk_entity_fields_workspace_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_entity_fields')),
    sa.UniqueConstraint('workspace_id', 'entity', 'key', name='uq_entity_fields_key'),
    schema='finance'
    )
    op.create_index(op.f('ix_entity_fields_workspace_id'), 'entity_fields', ['workspace_id'], unique=False, schema='finance')
    op.create_table('entity_views',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('entity', sa.Text(), server_default=sa.text("'contract'"), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('main', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('blocks', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('sort', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('style', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['finance.workspaces.id'], name=op.f('fk_entity_views_workspace_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_entity_views')),
    sa.UniqueConstraint('workspace_id', 'entity', 'key', name='uq_entity_views_key'),
    schema='finance'
    )
    op.create_index(op.f('ix_entity_views_workspace_id'), 'entity_views', ['workspace_id'], unique=False, schema='finance')
    op.create_table('list_values',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('field_key', sa.Text(), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('normalized', sa.Text(), nullable=False),
    sa.Column('meaning', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['finance.workspaces.id'], name=op.f('fk_list_values_workspace_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_list_values')),
    sa.UniqueConstraint('workspace_id', 'field_key', 'normalized', name='uq_list_values_value'),
    schema='finance'
    )
    op.create_index('ix_list_values_workspace_field', 'list_values', ['workspace_id', 'field_key'], unique=False, schema='finance')
    op.create_table('contracts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('number_key', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('signed_at', sa.Date(), nullable=True),
    sa.Column('executor_id', sa.Uuid(), nullable=True),
    sa.Column('customer_id', sa.Uuid(), nullable=True),
    sa.Column('type_id', sa.Uuid(), nullable=True),
    sa.Column('subject_id', sa.Uuid(), nullable=True),
    sa.Column('status_id', sa.Uuid(), nullable=True),
    sa.Column('economic_role_id', sa.Uuid(), nullable=True),
    sa.Column('department_id', sa.Uuid(), nullable=True),
    sa.Column('billing', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('amount_terms', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('currency', sa.Text(), server_default=sa.text("'KZT'"), nullable=False),
    sa.Column('planned_end_at', sa.Date(), nullable=True),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('end_kind', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('folder_url', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('amendments_text', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('amendments_summary_text', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('note', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('attrs', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('file_snapshot', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('provenance', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('acknowledged', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('field_seq', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('position', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('seq', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('source', sa.Text(), server_default=sa.text("'app'"), nullable=False),
    sa.Column('import_id', sa.Uuid(), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('updated_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("billing = '' OR billing IN ('month', 'total', 'terms')", name=op.f('ck_contracts_contract_billing')),
    sa.CheckConstraint("end_kind = '' OR end_kind IN ('terminated', 'fulfilled', 'unknown')", name=op.f('ck_contracts_contract_end_kind')),
    sa.CheckConstraint("source IN ('app', 'grid', 'import', 'api')", name=op.f('ck_contracts_contract_source')),
    sa.ForeignKeyConstraint(['created_by'], ['finance.users.id'], name=op.f('fk_contracts_created_by'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['customer_id'], ['finance.counterparties.id'], name=op.f('fk_contracts_customer_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['department_id'], ['finance.departments.id'], name=op.f('fk_contracts_department_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['economic_role_id'], ['finance.list_values.id'], name=op.f('fk_contracts_economic_role_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['executor_id'], ['finance.counterparties.id'], name=op.f('fk_contracts_executor_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['import_id'], ['finance.contract_imports.id'], name=op.f('fk_contracts_import_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['status_id'], ['finance.list_values.id'], name=op.f('fk_contracts_status_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['subject_id'], ['finance.list_values.id'], name=op.f('fk_contracts_subject_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['type_id'], ['finance.list_values.id'], name=op.f('fk_contracts_type_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by'], ['finance.users.id'], name=op.f('fk_contracts_updated_by'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['finance.workspaces.id'], name=op.f('fk_contracts_workspace_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contracts')),
    schema='finance'
    )
    op.create_index(op.f('ix_contracts_workspace_id'), 'contracts', ['workspace_id'], unique=False, schema='finance')
    op.create_index('ix_contracts_workspace_number_key', 'contracts', ['workspace_id', 'number_key'], unique=False, schema='finance')
    op.create_index('ix_contracts_workspace_seq', 'contracts', ['workspace_id', 'seq'], unique=False, schema='finance')
    op.create_table('counterparty_names',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('counterparty_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('normalized', sa.Text(), nullable=False),
    sa.Column('source', sa.Text(), server_default=sa.text("'manual'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('registry', 'bank', '1c', 'manual')", name=op.f('ck_counterparty_names_counterparty_name_source')),
    sa.ForeignKeyConstraint(['counterparty_id'], ['finance.counterparties.id'], name=op.f('fk_counterparty_names_counterparty_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['finance.workspaces.id'], name=op.f('fk_counterparty_names_workspace_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_counterparty_names')),
    sa.UniqueConstraint('counterparty_id', 'normalized', name='uq_counterparty_names_alias'),
    schema='finance'
    )
    op.create_index(op.f('ix_counterparty_names_counterparty_id'), 'counterparty_names', ['counterparty_id'], unique=False, schema='finance')
    op.create_index('ix_counterparty_names_workspace_normalized', 'counterparty_names', ['workspace_id', 'normalized'], unique=False, schema='finance')
    op.create_table('employees',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('full_name', sa.Text(), nullable=False),
    sa.Column('normalized_name', sa.Text(), nullable=False),
    sa.Column('job_title', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('department_id', sa.Uuid(), nullable=True),
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['department_id'], ['finance.departments.id'], name=op.f('fk_employees_department_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['finance.users.id'], name=op.f('fk_employees_user_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['finance.workspaces.id'], name=op.f('fk_employees_workspace_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_employees')),
    sa.UniqueConstraint('workspace_id', 'normalized_name', name='uq_employees_name'),
    schema='finance'
    )
    op.create_index(op.f('ix_employees_workspace_id'), 'employees', ['workspace_id'], unique=False, schema='finance')
    op.create_table('group_entities',
    sa.Column('counterparty_id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('full_name', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('bin', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('vat_payer', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['counterparty_id'], ['finance.counterparties.id'], name=op.f('fk_group_entities_counterparty_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['finance.workspaces.id'], name=op.f('fk_group_entities_workspace_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('counterparty_id', name=op.f('pk_group_entities')),
    schema='finance'
    )
    op.create_index(op.f('ix_group_entities_workspace_id'), 'group_entities', ['workspace_id'], unique=False, schema='finance')
    op.create_table('contract_amendments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('contract_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('signed_at', sa.Date(), nullable=True),
    sa.Column('summary', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('effect', sa.Text(), server_default=sa.text("'none'"), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=True),
    sa.Column('before', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('after', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('origin', sa.Text(), server_default=sa.text("'change'"), nullable=False),
    sa.Column('piece', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("effect IN ('none', 'amount', 'executor', 'customer', 'end_date', 'other')", name=op.f('ck_contract_amendments_contract_amendment_effect')),
    sa.CheckConstraint("origin IN ('change', 'parsed')", name=op.f('ck_contract_amendments_contract_amendment_origin')),
    sa.ForeignKeyConstraint(['contract_id'], ['finance.contracts.id'], name=op.f('fk_contract_amendments_contract_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by'], ['finance.users.id'], name=op.f('fk_contract_amendments_created_by'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['finance.workspaces.id'], name=op.f('fk_contract_amendments_workspace_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contract_amendments')),
    schema='finance'
    )
    op.create_index(op.f('ix_contract_amendments_contract_id'), 'contract_amendments', ['contract_id'], unique=False, schema='finance')
    op.create_index('ix_contract_amendments_due', 'contract_amendments', ['applied_at', 'effective_from'], unique=False, schema='finance')
    op.create_table('contract_people',
    sa.Column('contract_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.ForeignKeyConstraint(['contract_id'], ['finance.contracts.id'], name=op.f('fk_contract_people_contract_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['employee_id'], ['finance.employees.id'], name=op.f('fk_contract_people_employee_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('contract_id', 'employee_id', name=op.f('pk_contract_people')),
    schema='finance'
    )
    op.create_index(op.f('ix_contract_people_employee_id'), 'contract_people', ['employee_id'], unique=False, schema='finance')
    op.add_column('accounts', sa.Column('group_entity_id', sa.Uuid(), nullable=True), schema='finance')
    op.create_foreign_key(op.f('fk_accounts_group_entity_id'), 'accounts', 'group_entities', ['group_entity_id'], ['counterparty_id'], source_schema='finance', referent_schema='finance', ondelete='SET NULL')
    op.add_column('invoices', sa.Column('contract_id', sa.Uuid(), nullable=True), schema='finance')
    op.create_foreign_key(op.f('fk_invoices_contract_id'), 'invoices', 'contracts', ['contract_id'], ['id'], source_schema='finance', referent_schema='finance', ondelete='SET NULL')


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_constraint(op.f('fk_invoices_contract_id'), 'invoices', schema='finance', type_='foreignkey')
    op.drop_column('invoices', 'contract_id', schema='finance')
    op.drop_constraint(op.f('fk_accounts_group_entity_id'), 'accounts', schema='finance', type_='foreignkey')
    op.drop_column('accounts', 'group_entity_id', schema='finance')
    op.drop_index(op.f('ix_contract_people_employee_id'), table_name='contract_people', schema='finance')
    op.drop_table('contract_people', schema='finance')
    op.drop_index('ix_contract_amendments_due', table_name='contract_amendments', schema='finance')
    op.drop_index(op.f('ix_contract_amendments_contract_id'), table_name='contract_amendments', schema='finance')
    op.drop_table('contract_amendments', schema='finance')
    op.drop_index(op.f('ix_group_entities_workspace_id'), table_name='group_entities', schema='finance')
    op.drop_table('group_entities', schema='finance')
    op.drop_index(op.f('ix_employees_workspace_id'), table_name='employees', schema='finance')
    op.drop_table('employees', schema='finance')
    op.drop_index('ix_counterparty_names_workspace_normalized', table_name='counterparty_names', schema='finance')
    op.drop_index(op.f('ix_counterparty_names_counterparty_id'), table_name='counterparty_names', schema='finance')
    op.drop_table('counterparty_names', schema='finance')
    op.drop_index('ix_contracts_workspace_seq', table_name='contracts', schema='finance')
    op.drop_index('ix_contracts_workspace_number_key', table_name='contracts', schema='finance')
    op.drop_index(op.f('ix_contracts_workspace_id'), table_name='contracts', schema='finance')
    op.drop_table('contracts', schema='finance')
    op.drop_index('ix_list_values_workspace_field', table_name='list_values', schema='finance')
    op.drop_table('list_values', schema='finance')
    op.drop_index(op.f('ix_entity_views_workspace_id'), table_name='entity_views', schema='finance')
    op.drop_table('entity_views', schema='finance')
    op.drop_index(op.f('ix_entity_fields_workspace_id'), table_name='entity_fields', schema='finance')
    op.drop_table('entity_fields', schema='finance')
    op.drop_index(op.f('ix_departments_workspace_id'), table_name='departments', schema='finance')
    op.drop_table('departments', schema='finance')
    op.drop_table('counters', schema='finance')
    op.drop_index(op.f('ix_contract_imports_workspace_id'), table_name='contract_imports', schema='finance')
    op.drop_table('contract_imports', schema='finance')
