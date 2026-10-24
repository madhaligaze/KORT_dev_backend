"""finance: участник узнаётся по БИН; режим наших юрлиц по датам

Ядро KORT (24.10.2026). У участника (`counterparties`) появляются БИН/ИИН,
вид лица, полное наименование и «сведён в» - ключом становится номер, а не
имя: на проде 24.10 было 17 клиентов, заведённых дважды («ТОО Х» и «Х»), и
ни одного БИН. БИН, записанный раньше в `details.bin`, переезжает в колонку;
у наших юрлиц - из `group_entities.bin`.

`entity_tax_periods` - режим нашего юрлица с даты (общеустановленный или
упрощённый, плательщик НДС или нет): по нему считаются НДС документов и
оборот к порогу. `tax_years` - поправки компании к МРП, порогу и ставке года.

Revision ID: 0032
Revises: 0031
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("counterparties", schema=SCHEMA)}
    if "bin" not in columns:
        op.add_column("counterparties", sa.Column("bin", sa.Text(), server_default=sa.text("''"), nullable=False), schema=SCHEMA)
    if "kind" not in columns:
        op.add_column("counterparties", sa.Column("kind", sa.Text(), server_default=sa.text("''"), nullable=False), schema=SCHEMA)
    if "full_name" not in columns:
        op.add_column(
            "counterparties", sa.Column("full_name", sa.Text(), server_default=sa.text("''"), nullable=False), schema=SCHEMA
        )
    if "merged_into_id" not in columns:
        op.add_column("counterparties", sa.Column("merged_into_id", sa.Uuid(), nullable=True), schema=SCHEMA)
        op.create_foreign_key(
            op.f("fk_counterparties_merged_into_id"),
            "counterparties",
            "counterparties",
            ["merged_into_id"],
            ["id"],
            source_schema=SCHEMA,
            referent_schema=SCHEMA,
            ondelete="SET NULL",
        )
    indexes = {index["name"] for index in inspector.get_indexes("counterparties", schema=SCHEMA)}
    if "ix_counterparties_workspace_bin" not in indexes:
        op.create_index("ix_counterparties_workspace_bin", "counterparties", ["workspace_id", "bin"], schema=SCHEMA)

    # БИН, записанный раньше в реквизитах или у нашего юрлица, - в колонку.
    op.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.counterparties
               SET bin = regexp_replace(details->>'bin', '\\D', '', 'g')
             WHERE bin = '' AND coalesce(details->>'bin', '') <> ''
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.counterparties AS c
               SET bin = regexp_replace(g.bin, '\\D', '', 'g')
              FROM {SCHEMA}.group_entities AS g
             WHERE g.counterparty_id = c.id AND c.bin = '' AND coalesce(g.bin, '') <> ''
            """
        )
    )

    # Точка восстановления «сведены участники» - новый вид точки.
    op.drop_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        op.f("ck_restore_points_restore_point_kind"),
        "restore_points",
        "kind IN ('contracts_delete', 'contracts_create', 'values', 'view', 'field_add', 'order', 'party_merge')",
        schema=SCHEMA,
    )

    if not inspector.has_table("entity_tax_periods", schema=SCHEMA):
        op.create_table(
            "entity_tax_periods",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("entity_id", sa.Uuid(), nullable=False),
            sa.Column("valid_from", sa.Date(), nullable=False),
            sa.Column("regime", sa.Text(), server_default=sa.text("'general'"), nullable=False),
            sa.Column("vat_payer", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("note", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("created_by", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.CheckConstraint(
                "regime IN ('general', 'simplified', 'other')", name=op.f("ck_entity_tax_periods_entity_tax_period_regime")
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["finance.workspaces.id"], name=op.f("fk_entity_tax_periods_workspace_id"), ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["entity_id"],
                ["finance.group_entities.counterparty_id"],
                name=op.f("fk_entity_tax_periods_entity_id"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["created_by"], ["finance.users.id"], name=op.f("fk_entity_tax_periods_created_by"), ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_tax_periods")),
            sa.UniqueConstraint("entity_id", "valid_from", name="uq_entity_tax_periods_from"),
            schema=SCHEMA,
        )
        op.create_index(
            op.f("ix_entity_tax_periods_workspace_id"), "entity_tax_periods", ["workspace_id"], schema=SCHEMA
        )
        op.create_index(op.f("ix_entity_tax_periods_entity_id"), "entity_tax_periods", ["entity_id"], schema=SCHEMA)
        # Плательщик НДС, отмеченный у юрлица раньше флажком, - режимом с начала
        # учёта: дата постановки неизвестна, её поправит человек.
        op.execute(
            sa.text(
                f"""
                INSERT INTO {SCHEMA}.entity_tax_periods (id, workspace_id, entity_id, valid_from, regime, vat_payer, note)
                SELECT gen_random_uuid(), g.workspace_id, g.counterparty_id, DATE '2000-01-01', 'general', true,
                       'перенесено из отметки «плательщик НДС»; дату постановки на учёт уточнить'
                  FROM {SCHEMA}.group_entities AS g
                 WHERE g.vat_payer
                """
            )
        )

    if not inspector.has_table("tax_years", schema=SCHEMA):
        op.create_table(
            "tax_years",
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("year", sa.Integer(), nullable=False),
            sa.Column("mrp", sa.Numeric(18, 2), nullable=False),
            sa.Column("vat_threshold_mrp", sa.Integer(), nullable=False),
            sa.Column("vat_rate", sa.Numeric(5, 2), nullable=False),
            sa.Column("forecast", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("note", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("updated_by", sa.Uuid(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["finance.workspaces.id"], name=op.f("fk_tax_years_workspace_id"), ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["updated_by"], ["finance.users.id"], name=op.f("fk_tax_years_updated_by"), ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("workspace_id", "year", name=op.f("pk_tax_years")),
            schema=SCHEMA,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_table("tax_years", schema=SCHEMA)
    op.drop_table("entity_tax_periods", schema=SCHEMA)
    op.execute(sa.text(f"DELETE FROM {SCHEMA}.restore_points WHERE kind = 'party_merge'"))
    op.drop_constraint(op.f("ck_restore_points_restore_point_kind"), "restore_points", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        op.f("ck_restore_points_restore_point_kind"),
        "restore_points",
        "kind IN ('contracts_delete', 'contracts_create', 'values', 'view', 'field_add', 'order')",
        schema=SCHEMA,
    )
    op.drop_index("ix_counterparties_workspace_bin", table_name="counterparties", schema=SCHEMA)
    op.drop_constraint(op.f("fk_counterparties_merged_into_id"), "counterparties", schema=SCHEMA, type_="foreignkey")
    for column in ("merged_into_id", "full_name", "kind", "bin"):
        op.drop_column("counterparties", column, schema=SCHEMA)
