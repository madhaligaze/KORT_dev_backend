"""finance: поддержка - переписка учётки с командой продукта через Telegram

**Переписка.** `support_threads` - одна на учётку: «больше не показывать»
под объявлением о поддержке и день, когда объявление уже показано (раз в
день), и последний пост переписки в канале - новое сообщение встаёт ответом
на него. `support_messages` - сообщения человека и ответы поддержки;
`support_files` - вложения (картинки, голосовые, файлы) байтами в базе:
история переписки в KORT не зависит от Telegram.

**Доставка.** Сообщение человека уходит в закрытый канал Telegram отдельным
проходом с повторами (`delivered_at`, `sending_at`, `attempts`,
`next_try_at`): не дошло - не потерялось. Пост с шапкой и посты вложений
помечаются сразу (`tg_message_id`, `tg_parts`), и повтор не шлёт их второй раз.

**Ответ.** `support_reply_sessions` - кому сотрудник поддержки отвечает в
личке бота (заводится кнопкой «Отправить ответ» под обращением);
`support_updates` - разобранные события Telegram, чтобы перезапуск API не
задвоил ответ.

Revision ID: 0028
Revises: 0027
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0028'
down_revision: Union[str, None] = '0027'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("support_threads", schema=SCHEMA):
        op.create_table(
            "support_threads",
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("notice_off", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("notice_seen_on", sa.Date(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("tg_last_post_id", sa.BigInteger(), nullable=True),
            sa.ForeignKeyConstraint(
                ["user_id"], ["finance.users.id"], name=op.f("fk_support_threads_user_id"), ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("user_id", name=op.f("pk_support_threads")),
            schema=SCHEMA,
        )
    if not inspector.has_table("support_messages", schema=SCHEMA):
        op.create_table(
            "support_messages",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("workspace_id", sa.Uuid(), nullable=True),
            sa.Column("author", sa.Text(), nullable=False),
            sa.Column("body", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sending_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("next_try_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("tg_message_id", sa.BigInteger(), nullable=True),
            sa.Column("tg_parts", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("reply_to_id", sa.Uuid(), nullable=True),
            sa.Column("support_name", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("tg_media_group", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.CheckConstraint(
                "author IN ('user', 'support')", name=op.f("ck_support_messages_support_message_author")
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], ["finance.users.id"], name=op.f("fk_support_messages_user_id"), ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["finance.workspaces.id"],
                name=op.f("fk_support_messages_workspace_id"), ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["reply_to_id"], ["finance.support_messages.id"],
                name=op.f("fk_support_messages_reply_to_id"), ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_support_messages")),
            schema=SCHEMA,
        )
        op.create_index(
            "ix_support_messages_user_created", "support_messages", ["user_id", "created_at"], schema=SCHEMA
        )
        op.create_index(
            "ix_support_messages_user_unread", "support_messages", ["user_id", "author", "read_at"], schema=SCHEMA
        )
        op.create_index(
            "ix_support_messages_undelivered", "support_messages", ["author", "delivered_at"], schema=SCHEMA
        )
    if not inspector.has_table("support_files", schema=SCHEMA):
        op.create_table(
            "support_files",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("message_id", sa.Uuid(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("mime", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("size", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("data", sa.LargeBinary(), nullable=False),
            sa.Column("duration", sa.Float(), nullable=True),
            sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("tg_message_id", sa.BigInteger(), nullable=True),
            sa.CheckConstraint(
                "kind IN ('image', 'voice', 'file')", name=op.f("ck_support_files_support_file_kind")
            ),
            sa.ForeignKeyConstraint(
                ["message_id"], ["finance.support_messages.id"],
                name=op.f("fk_support_files_message_id"), ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_support_files")),
            schema=SCHEMA,
        )
        op.create_index(op.f("ix_support_files_message_id"), "support_files", ["message_id"], schema=SCHEMA)
    if not inspector.has_table("support_reply_sessions", schema=SCHEMA):
        op.create_table(
            "support_reply_sessions",
            sa.Column("tg_user_id", sa.BigInteger(), autoincrement=False, nullable=False),
            sa.Column("message_id", sa.Uuid(), nullable=False),
            sa.Column("tg_name", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("touched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(
                ["message_id"], ["finance.support_messages.id"],
                name=op.f("fk_support_reply_sessions_message_id"), ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("tg_user_id", name=op.f("pk_support_reply_sessions")),
            schema=SCHEMA,
        )
    if not inspector.has_table("support_updates", schema=SCHEMA):
        op.create_table(
            "support_updates",
            sa.Column("update_id", sa.BigInteger(), autoincrement=False, nullable=False),
            sa.Column("seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("update_id", name=op.f("pk_support_updates")),
            schema=SCHEMA,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_table("support_updates", schema=SCHEMA)
    op.drop_table("support_reply_sessions", schema=SCHEMA)
    op.drop_index(op.f("ix_support_files_message_id"), table_name="support_files", schema=SCHEMA)
    op.drop_table("support_files", schema=SCHEMA)
    op.drop_index("ix_support_messages_undelivered", table_name="support_messages", schema=SCHEMA)
    op.drop_index("ix_support_messages_user_unread", table_name="support_messages", schema=SCHEMA)
    op.drop_index("ix_support_messages_user_created", table_name="support_messages", schema=SCHEMA)
    op.drop_table("support_messages", schema=SCHEMA)
    op.drop_table("support_threads", schema=SCHEMA)
