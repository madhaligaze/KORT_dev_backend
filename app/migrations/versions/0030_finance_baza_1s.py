"""finance: базы 1С и снимок их документов

`onec_bases` - подключённая база 1С компании: адрес, вход в автоматический
REST-сервис (пароль - зашифрованным, `app/finance/secretbox.py`), наше юрлицо
реестра и организация базы, итог последнего чтения.

`onec_documents` - документы базы с последнего чтения: счета на оплату,
реализации (АВР) с датой подписания акта, счета-фактуры, ЭСФ, поступления.
На строке - решение человека «по такому договору / не по договору»
(`manual*`), которое чтение 1С не трогает. Договор и месяц документа не
хранятся - считаются при показе (`app/finance/onec/match.py`).

Revision ID: 0030
Revises: 0029
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("onec_bases", schema=SCHEMA):
        op.create_table(
            "onec_bases",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("login", sa.Text(), nullable=False),
            sa.Column("secret", sa.Text(), nullable=False),
            sa.Column("group_entity_id", sa.Uuid(), nullable=True),
            sa.Column("organization_ref", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("organization_name", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("organization_bin", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("state", sa.Text(), server_default=sa.text("'active'"), nullable=False),
            sa.Column("sync_from", sa.Date(), nullable=False),
            sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("counts", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
            sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False),
            sa.Column(
                "organizations", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False
            ),
            sa.Column("created_by", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.CheckConstraint("state IN ('active', 'off')", name=op.f("ck_onec_bases_onec_base_state")),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["finance.workspaces.id"], name=op.f("fk_onec_bases_workspace_id"), ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["group_entity_id"],
                ["finance.counterparties.id"],
                name=op.f("fk_onec_bases_group_entity_id"),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_onec_bases")),
            sa.UniqueConstraint("workspace_id", "url", name="uq_onec_bases_url"),
            schema=SCHEMA,
        )
        op.create_index("ix_onec_bases_workspace_id", "onec_bases", ["workspace_id"], schema=SCHEMA)
    if not inspector.has_table("onec_documents", schema=SCHEMA):
        op.create_table(
            "onec_documents",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("base_id", sa.Uuid(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("ref", sa.Text(), nullable=False),
            sa.Column("number", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("doc_date", sa.Date(), nullable=False),
            sa.Column("amount", sa.Numeric(18, 2), nullable=True),
            sa.Column("posted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("gone", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("party_ref", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("party_name", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("party_full_name", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("party_bin", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("contract_ref", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("contract_number", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("contract_date", sa.Date(), nullable=True),
            sa.Column("basis_ref", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("signed_at", sa.Date(), nullable=True),
            sa.Column("status", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("reg_number", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("period_from", sa.Date(), nullable=True),
            sa.Column("period_to", sa.Date(), nullable=True),
            sa.Column("content", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("data_version", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("manual", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("manual_contract_id", sa.Uuid(), nullable=True),
            sa.Column("manual_by", sa.Uuid(), nullable=True),
            sa.Column("manual_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "kind IN ('invoice', 'sale', 'sf', 'esf', 'payment')", name=op.f("ck_onec_documents_onec_document_kind")
            ),
            sa.CheckConstraint("manual IN ('', 'link', 'none')", name=op.f("ck_onec_documents_onec_document_manual")),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["finance.workspaces.id"], name=op.f("fk_onec_documents_workspace_id"), ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["base_id"], ["finance.onec_bases.id"], name=op.f("fk_onec_documents_base_id"), ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["manual_contract_id"],
                ["finance.contracts.id"],
                name=op.f("fk_onec_documents_manual_contract_id"),
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["manual_by"], ["finance.users.id"], name=op.f("fk_onec_documents_manual_by"), ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_onec_documents")),
            sa.UniqueConstraint("base_id", "kind", "ref", name="uq_onec_documents_ref"),
            schema=SCHEMA,
        )
        op.create_index("ix_onec_documents_workspace_id", "onec_documents", ["workspace_id"], schema=SCHEMA)
        op.create_index("ix_onec_documents_base_id", "onec_documents", ["base_id"], schema=SCHEMA)
        op.create_index(
            "ix_onec_documents_workspace_date", "onec_documents", ["workspace_id", "doc_date"], schema=SCHEMA
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_table("onec_documents", schema=SCHEMA)
    op.drop_table("onec_bases", schema=SCHEMA)
