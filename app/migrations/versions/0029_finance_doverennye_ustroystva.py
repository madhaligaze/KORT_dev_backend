"""finance: доверенные устройства - слабый пароль пускает только с подтверждённого

Знание уже заданного пароля перестаёт быть достаточным с незнакомого
устройства. Вход слабым паролем (из тех, что подбирают первыми) с нового
устройства не открывает сеанс сразу, а ждёт подтверждения с устройства, где
человек уже вошёл: на нём всплывает «Вход с нового устройства - Разрешить /
Отклонить». Подтверждённое устройство дальше входит прямо.

`login_devices` - по строке на пару «учётка - устройство»: `device_hash` -
SHA-256 куки `fin_device`, `status` - ждёт / доверенное / отклонённое. Строки
заводятся только у учёток, входящих слабым паролем: сильному паролю
устройство не нужно, и у него строк не бывает.

Новых паролей политика (`app/finance/passwords.py`) слабыми быть не даёт;
механизм - для паролей, заданных до политики (в том числе владельца).

Revision ID: 0029
Revises: 0028
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    inspector = sa.inspect(bind)
    if inspector.has_table("login_devices", schema=SCHEMA):
        return
    op.create_table(
        "login_devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("device_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("user_agent", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("ip", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'trusted', 'denied')", name=op.f("ck_login_devices_login_device_status")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["finance.users.id"], name=op.f("fk_login_devices_user_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_devices")),
        sa.UniqueConstraint("user_id", "device_hash", name="uq_login_devices_user_device"),
        schema=SCHEMA,
    )
    op.create_index("ix_login_devices_user_id", "login_devices", ["user_id"], schema=SCHEMA)
    op.create_index(
        "ix_login_devices_user_status", "login_devices", ["user_id", "status"], schema=SCHEMA
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_table("login_devices", schema=SCHEMA)
