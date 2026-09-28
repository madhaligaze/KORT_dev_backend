"""finance: доступ — вход по номеру, роли, права, уведомления, журнал действий

Второй кирпич ERP: сотрудники входят по телефону, права раздаются отделам и
людям галочками, журнал действий пишет каждое действие каждого аккаунта.
Почему модель такая — в `app/finance/accounts_model.py` и `app/finance/access.py`.

Что меняется
────────────
* `users`: + `phone` (уникальный, `+77XXXXXXXXX`), + `pending_until`; почта и
  пароль необязательны, `CHECK` держит, что логин есть всегда (почта или
  телефон); `status` += `pending`.
* `memberships.role` → `owner` / `admin` / `employee`; + `blocked_at` (вход
  закрывается в одной компании, а не везде).
* `access_grants`, `notifications` — новые таблицы.
* `sessions`: + `ip`.
* `action_log` → журнал действий: + `user_id`, `session_id`, `ip`,
  `user_agent`, `category` и индексы под фильтры и очистку просмотров.
* `employees`: одна запись на учётку в компании.

Данные
──────
* У каждого члена компании появляется запись сотрудника: права сотрудника
  пишутся на неё. Имя совпало с чужой записью — к нему дописывается логин, а
  не угадывается, что это тот же человек.
* Бывшие `accountant` и `viewer` становятся `employee` с **личными правами,
  равными прежним способностям**: `viewer` видит всё, кроме людей и журнала
  действий; `accountant` вдобавок правит учёт, договоры, правила и планы, а
  справочники и подключения по-прежнему только видит — счета он и раньше не
  заводил. Список зашит здесь, а не взят из `access.py`: ревизия обязана
  делать то же самое и через год, когда код прав поменяется.
* Старым записям журнала проставляются вид (`contract.export` — выгрузка,
  `contract.import` — загрузка, остальное — данные) и автор по почте.

`access_grants` и `notifications` создаются, только если их ещё нет: до этой
ревизии рабочий стенд с новым кодом мог завести их сам через `create_all`
(`finance/db.py`), и повторное `CREATE TABLE` уронило бы ревизию.

Revision ID: 0019
Revises: 0018
"""
from __future__ import annotations

import re
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0019'
down_revision: Union[str, None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"
JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
POSITION_STEP = 1024

_VIEWABLE = [
    "contracts", "journal", "table", "calendar", "invoices", "recurrences", "import", "sheets",
    "reports.cash", "reports.profit", "reports.debts", "reports.balance", "reports.indicators",
    "reports.statement", "reports.projects", "reports.plan", "integrations", "rules", "dictionaries",
]
_ACCOUNTANT_EDIT = [
    "contracts", "journal", "table", "calendar", "invoices", "recurrences", "import", "sheets",
    "rules", "reports.plan",
]
LEGACY_GRANTS = {
    "viewer": {key: "view" for key in _VIEWABLE},
    "accountant": {key: ("edit" if key in _ACCOUNTANT_EDIT else "view") for key in _VIEWABLE},
}

_SPACES = re.compile(r"[\s   ]+")


def _norm(value: str) -> str:
    """Как `app.finance.layout.norm` — нормализованное имя сотрудника."""
    return _SPACES.sub(" ", str(value or "")).strip().lower().replace("ё", "е")


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name, schema=SCHEMA)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    # ── users ────────────────────────────────────────────────────────────
    op.add_column('users', sa.Column('phone', sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column('users', sa.Column('pending_until', sa.DateTime(timezone=True), nullable=True), schema=SCHEMA)
    op.alter_column('users', 'email', existing_type=sa.TEXT(), nullable=True, schema=SCHEMA)
    op.alter_column('users', 'email_normalized', existing_type=sa.TEXT(), nullable=True, schema=SCHEMA)
    op.alter_column('users', 'password_hash', existing_type=sa.TEXT(), nullable=True, schema=SCHEMA)
    op.create_unique_constraint('uq_users_phone', 'users', ['phone'], schema=SCHEMA)
    op.execute(
        "UPDATE finance.users SET status = 'blocked' "
        "WHERE status NOT IN ('active', 'blocked', 'pending')"
    )
    op.create_check_constraint(
        op.f('ck_users_user_login'), 'users',
        "email_normalized IS NOT NULL OR phone IS NOT NULL", schema=SCHEMA,
    )
    op.create_check_constraint(
        op.f('ck_users_user_status'), 'users',
        "status IN ('active', 'blocked', 'pending')", schema=SCHEMA,
    )

    # ── sessions, memberships ────────────────────────────────────────────
    op.add_column('sessions', sa.Column('ip', sa.Text(), server_default=sa.text("''"), nullable=False), schema=SCHEMA)
    op.add_column('memberships', sa.Column('blocked_at', sa.DateTime(timezone=True), nullable=True), schema=SCHEMA)
    op.drop_constraint(op.f('ck_memberships_membership_role'), 'memberships', schema=SCHEMA, type_='check')

    # ── action_log ───────────────────────────────────────────────────────
    op.add_column('action_log', sa.Column('user_id', sa.Uuid(), nullable=True), schema=SCHEMA)
    op.add_column('action_log', sa.Column('session_id', sa.Uuid(), nullable=True), schema=SCHEMA)
    op.add_column('action_log', sa.Column('ip', sa.Text(), server_default=sa.text("''"), nullable=False), schema=SCHEMA)
    op.add_column('action_log', sa.Column('user_agent', sa.Text(), server_default=sa.text("''"), nullable=False), schema=SCHEMA)
    op.add_column('action_log', sa.Column('category', sa.Text(), server_default=sa.text("'data'"), nullable=False), schema=SCHEMA)
    op.create_foreign_key(
        op.f('fk_action_log_user_id'), 'action_log', 'users', ['user_id'], ['id'],
        source_schema=SCHEMA, referent_schema=SCHEMA, ondelete='SET NULL',
    )
    op.execute("UPDATE finance.action_log SET category = 'export' WHERE kind = 'contract.export'")
    op.execute("UPDATE finance.action_log SET category = 'import' WHERE kind = 'contract.import'")
    # Автор старых записей — по почте, которой они подписаны.
    op.execute(
        "UPDATE finance.action_log AS a SET user_id = u.id FROM finance.users AS u "
        "WHERE a.user_id IS NULL AND a.actor <> '' AND u.email = a.actor"
    )
    op.create_check_constraint(
        op.f('ck_action_log_action_log_category'), 'action_log',
        "category IN ('data', 'auth', 'admin', 'view', 'export', 'import')", schema=SCHEMA,
    )
    op.create_index('ix_action_log_category_at', 'action_log', ['category', 'at'], unique=False, schema=SCHEMA)
    op.create_index('ix_action_log_entity_id', 'action_log', ['entity_id'], unique=False, schema=SCHEMA)
    op.create_index('ix_action_log_workspace_user_at', 'action_log', ['workspace_id', 'user_id', 'at'], unique=False, schema=SCHEMA)

    # ── новые таблицы ────────────────────────────────────────────────────
    if not _has_table(bind, 'access_grants'):
        op.create_table('access_grants',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('workspace_id', sa.Uuid(), nullable=False),
        sa.Column('subject_kind', sa.Text(), nullable=False),
        sa.Column('subject_id', sa.Uuid(), nullable=False),
        sa.Column('resource', sa.Text(), nullable=False),
        sa.Column('level', sa.Text(), server_default=sa.text("'none'"), nullable=False),
        sa.Column('scope', JSONB, server_default=sa.text("'{}'"), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_by', sa.Uuid(), nullable=True),
        sa.CheckConstraint("level IN ('none', 'view', 'edit')", name=op.f('ck_access_grants_access_grant_level')),
        sa.CheckConstraint("subject_kind IN ('department', 'employee')", name=op.f('ck_access_grants_access_grant_subject')),
        sa.ForeignKeyConstraint(['updated_by'], ['finance.users.id'], name=op.f('fk_access_grants_updated_by'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['workspace_id'], ['finance.workspaces.id'], name=op.f('fk_access_grants_workspace_id'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_access_grants')),
        sa.UniqueConstraint('workspace_id', 'subject_kind', 'subject_id', 'resource', name='uq_access_grants_subject_resource'),
        schema=SCHEMA
        )
        op.create_index(op.f('ix_access_grants_workspace_id'), 'access_grants', ['workspace_id'], unique=False, schema=SCHEMA)
    if not _has_table(bind, 'notifications'):
        op.create_table('notifications',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('workspace_id', sa.Uuid(), nullable=False),
        sa.Column('audience', sa.Text(), server_default=sa.text("'admins'"), nullable=False),
        sa.Column('recipient_id', sa.Uuid(), nullable=True),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('subject_user_id', sa.Uuid(), nullable=True),
        sa.Column('payload', JSONB, server_default=sa.text("'{}'"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by', sa.Uuid(), nullable=True),
        sa.CheckConstraint("audience IN ('admins', 'user')", name=op.f('ck_notifications_notification_audience')),
        sa.ForeignKeyConstraint(['recipient_id'], ['finance.users.id'], name=op.f('fk_notifications_recipient_id'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['resolved_by'], ['finance.users.id'], name=op.f('fk_notifications_resolved_by'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['subject_user_id'], ['finance.users.id'], name=op.f('fk_notifications_subject_user_id'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['finance.workspaces.id'], name=op.f('fk_notifications_workspace_id'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications')),
        schema=SCHEMA
        )
        op.create_index('ix_notifications_workspace_open', 'notifications', ['workspace_id', 'resolved_at', 'created_at'], unique=False, schema=SCHEMA)
        op.create_index(op.f('ix_notifications_subject_user_id'), 'notifications', ['subject_user_id'], unique=False, schema=SCHEMA)

    # ── сотрудник у каждой учётки, прежние роли → права ─────────────────
    # Две записи сотрудника на одну учётку (не должно быть, но уникальность
    # ниже иначе не встанет): связь остаётся у первой.
    op.execute(
        "UPDATE finance.employees AS e SET user_id = NULL "
        "WHERE e.user_id IS NOT NULL AND EXISTS ("
        "  SELECT 1 FROM finance.employees AS o WHERE o.workspace_id = e.workspace_id "
        "  AND o.user_id = e.user_id AND (o.created_at, o.id::text) < (e.created_at, e.id::text))"
    )
    _members_to_employees(bind)
    op.create_unique_constraint('uq_employees_workspace_user', 'employees', ['workspace_id', 'user_id'], schema=SCHEMA)

    op.execute("UPDATE finance.memberships SET role = 'employee' WHERE role IN ('accountant', 'viewer')")
    op.alter_column('memberships', 'role', existing_type=sa.TEXT(), server_default=sa.text("'employee'"), schema=SCHEMA)
    op.create_check_constraint(
        op.f('ck_memberships_membership_role'), 'memberships',
        "role IN ('owner', 'admin', 'employee')", schema=SCHEMA,
    )


def _members_to_employees(bind) -> None:
    """Запись сотрудника каждому члену компании и личные права прежней роли."""
    members = bind.execute(sa.text(
        "SELECT m.workspace_id, m.user_id, m.role, u.full_name, u.email "
        "FROM finance.memberships AS m JOIN finance.users AS u ON u.id = m.user_id "
        "ORDER BY m.created_at"
    )).all()
    for workspace_id, user_id, role, full_name, email in members:
        employee_id = bind.execute(sa.text(
            "SELECT id FROM finance.employees WHERE workspace_id = :w AND user_id = :u"
        ), {"w": workspace_id, "u": user_id}).scalar()
        if employee_id is None:
            login = email or ""
            base = (full_name or "").strip() or login or "Сотрудник"
            for candidate in (base, f"{base} · {login}" if login else "", f"{base} · {uuid.uuid4().hex[:4]}"):
                if not candidate:
                    continue
                taken = bind.execute(sa.text(
                    "SELECT 1 FROM finance.employees WHERE workspace_id = :w AND normalized_name = :n"
                ), {"w": workspace_id, "n": _norm(candidate)}).scalar()
                if taken is None:
                    break
            top = bind.execute(sa.text(
                "SELECT COALESCE(MAX(position), 0) FROM finance.employees WHERE workspace_id = :w"
            ), {"w": workspace_id}).scalar()
            employee_id = uuid.uuid4()
            bind.execute(sa.text(
                "INSERT INTO finance.employees (id, workspace_id, full_name, normalized_name, user_id, position) "
                "VALUES (:id, :w, :name, :norm, :u, :pos)"
            ), {
                "id": employee_id, "w": workspace_id, "name": candidate, "norm": _norm(candidate),
                "u": user_id, "pos": int(top or 0) + POSITION_STEP,
            })
        for resource, level in LEGACY_GRANTS.get(role, {}).items():
            bind.execute(sa.text(
                "INSERT INTO finance.access_grants (id, workspace_id, subject_kind, subject_id, resource, level, scope) "
                "VALUES (:id, :w, 'employee', :s, :r, :l, CAST('{}' AS jsonb)) "
                "ON CONFLICT ON CONSTRAINT uq_access_grants_subject_resource DO NOTHING"
            ), {"id": uuid.uuid4(), "w": workspace_id, "s": employee_id, "r": resource, "l": level})


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    # Роли обратно: кто правил журнал — бухгалтер, остальные — наблюдатели.
    op.drop_constraint(op.f('ck_memberships_membership_role'), 'memberships', schema=SCHEMA, type_='check')
    op.execute(
        "UPDATE finance.memberships AS m SET role = CASE WHEN EXISTS ("
        "  SELECT 1 FROM finance.employees AS e JOIN finance.access_grants AS g "
        "  ON g.subject_kind = 'employee' AND g.subject_id = e.id "
        "  WHERE e.workspace_id = m.workspace_id AND e.user_id = m.user_id "
        "  AND g.resource = 'journal' AND g.level = 'edit') THEN 'accountant' ELSE 'viewer' END "
        "WHERE m.role = 'employee'"
    )
    op.alter_column('memberships', 'role', existing_type=sa.TEXT(), server_default=sa.text("'accountant'"), schema=SCHEMA)
    op.create_check_constraint(
        op.f('ck_memberships_membership_role'), 'memberships',
        "role IN ('owner', 'admin', 'accountant', 'viewer')", schema=SCHEMA,
    )
    op.drop_constraint('uq_employees_workspace_user', 'employees', schema=SCHEMA, type_='unique')

    op.drop_index(op.f('ix_notifications_subject_user_id'), table_name='notifications', schema=SCHEMA)
    op.drop_index('ix_notifications_workspace_open', table_name='notifications', schema=SCHEMA)
    op.drop_table('notifications', schema=SCHEMA)
    op.drop_index(op.f('ix_access_grants_workspace_id'), table_name='access_grants', schema=SCHEMA)
    op.drop_table('access_grants', schema=SCHEMA)

    op.drop_index('ix_action_log_workspace_user_at', table_name='action_log', schema=SCHEMA)
    op.drop_index('ix_action_log_entity_id', table_name='action_log', schema=SCHEMA)
    op.drop_index('ix_action_log_category_at', table_name='action_log', schema=SCHEMA)
    op.drop_constraint(op.f('ck_action_log_action_log_category'), 'action_log', schema=SCHEMA, type_='check')
    op.drop_constraint(op.f('fk_action_log_user_id'), 'action_log', schema=SCHEMA, type_='foreignkey')
    op.drop_column('action_log', 'category', schema=SCHEMA)
    op.drop_column('action_log', 'user_agent', schema=SCHEMA)
    op.drop_column('action_log', 'ip', schema=SCHEMA)
    op.drop_column('action_log', 'session_id', schema=SCHEMA)
    op.drop_column('action_log', 'user_id', schema=SCHEMA)

    op.drop_column('memberships', 'blocked_at', schema=SCHEMA)
    op.drop_column('sessions', 'ip', schema=SCHEMA)

    # Учётки без почты и пароля старой схеме не выразить: вход по номеру ей
    # неизвестен. Почта — заглушка на номер, пароль — заведомо неверный хеш,
    # ждущие пароль — заблокированы.
    op.drop_constraint(op.f('ck_users_user_status'), 'users', schema=SCHEMA, type_='check')
    op.drop_constraint(op.f('ck_users_user_login'), 'users', schema=SCHEMA, type_='check')
    op.execute(
        "UPDATE finance.users SET email = phone || '@phone.invalid', "
        "email_normalized = phone || '@phone.invalid' WHERE email_normalized IS NULL"
    )
    op.execute("UPDATE finance.users SET password_hash = '!' WHERE password_hash IS NULL")
    op.execute("UPDATE finance.users SET status = 'blocked' WHERE status = 'pending'")
    op.drop_constraint('uq_users_phone', 'users', schema=SCHEMA, type_='unique')
    op.alter_column('users', 'password_hash', existing_type=sa.TEXT(), nullable=False, schema=SCHEMA)
    op.alter_column('users', 'email_normalized', existing_type=sa.TEXT(), nullable=False, schema=SCHEMA)
    op.alter_column('users', 'email', existing_type=sa.TEXT(), nullable=False, schema=SCHEMA)
    op.drop_column('users', 'pending_until', schema=SCHEMA)
    op.drop_column('users', 'phone', schema=SCHEMA)
