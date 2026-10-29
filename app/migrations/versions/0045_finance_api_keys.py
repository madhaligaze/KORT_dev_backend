"""finance: ключи API модели для чужого сервиса

Ядро KORT под любой фронт (29.10.2026). До ключей в API модели входили только
сеансом учётки: чужой сервис, бот у клиента или интеграционная шина не могли
спросить ядро, не храня пароль человека. Ключ выдаёт владелец или
администратор, действует он правами выдавшего и открывает только
`/finance/model/*`; уровень `read` - описать, спросить, объяснить,
подписаться, `write` - ещё предложить, применить, отменить. Токен - хешем.

Revision ID: 0045
Revises: 0044
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), server_default=sa.text("'read'"), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("level IN ('read', 'write')", name=op.f("ck_api_keys_api_key_level")),
        sa.ForeignKeyConstraint(["workspace_id"], [f"{SCHEMA}.workspaces.id"], name=op.f("fk_api_keys_workspace_id"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.users.id"], name=op.f("fk_api_keys_user_id"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_keys_token_hash")),
        schema=SCHEMA,
    )
    op.create_index(op.f("ix_api_keys_workspace_id"), "api_keys", ["workspace_id"], schema=SCHEMA)
    op.create_index(op.f("ix_api_keys_user_id"), "api_keys", ["user_id"], schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_index(op.f("ix_api_keys_user_id"), table_name="api_keys", schema=SCHEMA)
    op.drop_index(op.f("ix_api_keys_workspace_id"), table_name="api_keys", schema=SCHEMA)
    op.drop_table("api_keys", schema=SCHEMA)
