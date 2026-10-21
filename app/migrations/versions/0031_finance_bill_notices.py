"""finance: о каких невыставленных счетах бот счетов уже написал

`bill_notices` - строка на счёт книги-сводки и группу («подошёл срок» /
«срок прошёл»), о которых бот уже сообщил в канал: новое он пишет сразу, в
любое время, и только один раз. Ключ - юрлицо, клиент, № договора и месяц
строки, а не номер строки листа: лист сортирует формула. `message_id` и
`line` - где счёт назван; `done_at` - в книге поставили галочку, и строки в
сообщении больше нет (выставлено всё - сообщение удалено вместе с записями).

Revision ID: 0031
Revises: 0030
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("bill_notices", schema=SCHEMA):
        op.create_table(
            "bill_notices",
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("key", sa.Text(), nullable=False),
            sa.Column("bill_group", sa.Text(), nullable=False),
            sa.Column("notified_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("message_id", sa.BigInteger(), nullable=True),
            sa.Column("line", sa.Text(), server_default=sa.text("''"), nullable=False),
            sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["finance.workspaces.id"], name=op.f("fk_bill_notices_workspace_id"), ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("workspace_id", "key", "bill_group", name=op.f("pk_bill_notices")),
            schema=SCHEMA,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_table("bill_notices", schema=SCHEMA)
