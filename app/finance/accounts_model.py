"""Учётки и компании раздела «Финансы» — модель.

Почему у раздела своя авторизация, а не общая с дашбордом
────────────────────────────────────────────────────────
Первая версия пускала в раздел по праву `finance` учётки BBC Dashboard. Это
было неверно по существу: в «Финансах» каждая компания регистрируется сама,
ведёт свой учёт и к BBC отношения не имеет. Более того, BBC сам может однажды
переехать сюда — и тогда зависимость смотрела бы в обратную сторону, от общего
к частному.

Поэтому здесь свои `users`, `memberships`, `sessions`, а `workspaces` из
`models.py` становится компанией: у неё появляется владелец. Пакет
`app.finance` после этого не импортирует `app.bbc` ни одной строкой — это
проверяется тестом `test_finance_isolated`.

Три решения, которые стоит объяснить
────────────────────────────────────
**1. Пользователь и компания связаны таблицей, а не колонкой.** Один человек
ведёт несколько компаний (бухгалтер на подряде — десяток), и в Finmap это
устроено так же: переключатель компаний в шапке. Колонка `company_id` у
пользователя заставила бы завести ему вторую учётку с тем же паролем.

**2. Сессия живёт на сервере, в cookie уходит только случайный токен.** В базе
лежит его SHA-256: дамп базы не должен давать работающих сессий. Тот же принцип,
что в дашборде, — он проверен и правильный.

**3. Роль хранится на членстве, а не на пользователе.** Один и тот же человек —
владелец своей компании и приглашённый бухгалтер в чужой. Роль на пользователе
означала бы, что права в одной компании тянутся в другую.

Доступ (ревизия 0019)
─────────────────────
**Вход сотрудника — по телефону.** Администратор заводит учётку с номером и
без пароля (`status = pending`, окно `pending_until` — 72 часа); человек сам
задаёт пароль по номеру и дальше входит номером и паролем. Поэтому почта и
пароль у учётки необязательны, а `CHECK` держит главное: логин есть всегда —
почта или телефон.

**Блокировка — на членстве, а не на человеке.** Один человек бывает в
нескольких компаниях, и администратор одной не должен закрывать ему вход в
другую. `users.status = blocked` остаётся для команды сервера.

**Права — строками `access_grants`, а не ролью.** Роли три: владелец и
администратор видят всё, сотрудник — то, что записано ему лично или его
отделу (`app/finance/access.py`).
"""
from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.finance.db import FinanceBase
from app.finance.models import JSONB, _in

#: Роли в компании:
#:
#:   owner    — создал компанию: всё, включая счета, людей и название;
#:   admin    — всё, кроме названия компании, назначения администраторов и
#:              сброса пароля другим администраторам;
#:   employee — то, что открыто ему или его отделу в `access_grants`.
#:
#: До 0019 были ещё `accountant` и `viewer`; ревизия перевела их в `employee`
#: с личными правами, равными прежним способностям (`access.LEGACY_GRANTS`).
MEMBER_ROLES = ("owner", "admin", "employee")
#: `pending` — учётка заведена, пароль ещё не задан (или сброшен).
USER_STATUSES = ("active", "blocked", "pending")
#: Кому уведомление: администраторам компании или одному человеку.
NOTIFICATION_AUDIENCES = ("admins", "user")
#: Чьи права: отдела или человека.
GRANT_SUBJECTS = ("department", "employee")
GRANT_LEVELS = ("none", "view", "edit")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class FinanceUser(FinanceBase):
    """Человек. Один на все компании, где он участвует."""

    __tablename__ = "users"
    __table_args__ = (
        # Почта — логин, поэтому уникальна в нормализованном виде.
        sa.UniqueConstraint("email_normalized", name="uq_users_email_normalized"),
        # Телефон — второй логин. Номер, занятый в другой компании, не
        # заводится: вход по номеру не спрашивает компанию.
        sa.UniqueConstraint("phone", name="uq_users_phone"),
        # Без логина — только закрытая учётка: номер ушёл новому человеку
        # (ревизия 0021), а подписи под прошлыми действиями остаются.
        sa.CheckConstraint(
            "email_normalized IS NOT NULL OR phone IS NOT NULL OR status = 'blocked'", name="user_login"
        ),
        sa.CheckConstraint(_in("status", USER_STATUSES), name="user_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    email: Mapped[str | None] = mapped_column(sa.Text)
    #: Почта в нижнем регистре и без обрамляющих пробелов. Отдельной колонкой, а
    #: не функциональным индексом: на SQLite в тестах функциональные индексы
    #: ведут себя иначе, а поиск по логину обязан быть одинаковым везде.
    email_normalized: Mapped[str | None] = mapped_column(sa.Text)
    #: Казахстанский мобильный в одном виде: `+77011234567`.
    phone: Mapped[str | None] = mapped_column(sa.Text)
    #: Пусто, пока человек не задал пароль сам (`status = pending`).
    password_hash: Mapped[str | None] = mapped_column(sa.Text)
    full_name: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: `active` | `blocked` | `pending`. Блокировка не удаляет: за человеком
    #: остаются подписи под операциями, и их нельзя обезличить задним числом.
    status: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'active'"))
    #: До какого момента можно задать пароль по номеру. Окно, а не бессрочно:
    #: кто первым введёт номер, тот и задаст пароль, и держать эту дверь
    #: открытой неделями нельзя.
    pending_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: Пароль выдан владельцем и должен быть сменён при первом входе.
    must_change_password: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))


class FinanceMembership(FinanceBase):
    """Участие человека в компании и его роль там."""

    __tablename__ = "memberships"
    __table_args__ = (
        sa.CheckConstraint(_in("role", MEMBER_ROLES), name="membership_role"),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_memberships_workspace_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'employee'"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    #: Кто пригласил. Нужно, чтобы владелец понимал, откуда в его компании
    #: взялся человек, которого он не помнит.
    invited_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, sa.ForeignKey("users.id"))
    #: Вход в эту компанию закрыт администратором. Членство остаётся: права,
    #: отдел и подписи под записями никуда не деваются, «Разблокировать»
    #: возвращает всё как было.
    blocked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class FinanceSession(FinanceBase):
    """Серверная сессия. В cookie уходит токен, здесь лежит его SHA-256."""

    __tablename__ = "sessions"
    __table_args__ = (
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        sa.Index("ix_sessions_user_id_expires_at", "user_id", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(sa.Text)
    #: Компания, выбранная в этой сессии. Переключатель компаний меняет её, а не
    #: заводит вторую сессию: человек остаётся тем же, меняется только контекст.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    #: Чем зашли — чтобы человек узнал свои сессии в списке и отозвал чужую.
    user_agent: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: Откуда. Значение справочное: его ставит прокси Next заголовком
    #: `x-client-ip`, и доказательством оно не служит.
    ip: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))


class AccessGrant(FinanceBase):
    """Право отдела или человека на раздел.

    Нет записи — нет доступа (кроме полей договора: там отсутствие записи
    значит «как у договоров»). Личная запись перекрывает отдельскую целиком,
    вместе с областью. Как это складывается — `app/finance/access.py`.
    """

    __tablename__ = "access_grants"
    __table_args__ = (
        sa.CheckConstraint(_in("subject_kind", GRANT_SUBJECTS), name="access_grant_subject"),
        sa.CheckConstraint(_in("level", GRANT_LEVELS), name="access_grant_level"),
        sa.UniqueConstraint(
            "workspace_id", "subject_kind", "subject_id", "resource", name="uq_access_grants_subject_resource"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    #: `department` — `departments.id`; `employee` — `employees.id`. Без
    #: внешнего ключа: он указывал бы в одну из двух таблиц, а удаление отдела
    #: или человека снимает его права само (`people.py`).
    subject_kind: Mapped[str] = mapped_column(sa.Text)
    subject_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    #: `journal`, `contracts`, `reports.debts`, `contracts.field.amount`…
    resource: Mapped[str] = mapped_column(sa.Text)
    level: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'none'"))
    #: Область — только у договоров: `{"rows": "all|department|own",
    #: "entities": [id наших юрлиц]}`.
    scope: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )


class Notification(FinanceBase):
    """Уведомление: «забыл пароль», «пять неверных паролей», «пароль задан».

    Общая очередь ERP: позже сюда же лягут замечания данных. Решённое не
    удаляется — `resolved_at/_by` помнят, кто и когда закрыл просьбу.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        sa.CheckConstraint(_in("audience", NOTIFICATION_AUDIENCES), name="notification_audience"),
        sa.Index("ix_notifications_workspace_open", "workspace_id", "resolved_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    audience: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'admins'"))
    #: Адресат, когда `audience = user`.
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE")
    )
    #: `password_reset_requested` | `login_locked` | `password_set` | …
    kind: Mapped[str] = mapped_column(sa.Text)
    #: О ком.
    subject_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )


__all__ = [
    "GRANT_LEVELS",
    "GRANT_SUBJECTS",
    "MEMBER_ROLES",
    "NOTIFICATION_AUDIENCES",
    "USER_STATUSES",
    "AccessGrant",
    "FinanceMembership",
    "FinanceSession",
    "FinanceUser",
    "Notification",
]
