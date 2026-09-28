"""Уведомления: что ждёт решения администратора и что ему просто стоит знать.

Два вида, и они различаются не цветом, а тем, нужно ли действие:

* **просьбы** (`password_reset_requested`, `login_locked`) — висят, пока их не
  решат: сброс пароля закрывает их сам, остальное — «Решено». Их число —
  «N запросов» в раме администратора;
* **сведения** (`password_set`) — строка «Недавно» за сутки; действия не
  требуют и в число запросов не входят.

Повтор одной и той же просьбы не плодит строк: «забыл пароль» дважды за час
— это одна просьба с новым временем, а не две. Иначе список «Ждут решения»
вырос бы от нетерпения человека, а не от числа дел.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.accounts_model import FinanceUser, Notification

ACTIONABLE = ("password_reset_requested", "login_locked")
INFORMATIONAL = ("password_set",)
#: Сколько «Недавно» держит сведения.
RECENT = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def notify(
    session: Session,
    workspace_id: uuid.UUID,
    kind: str,
    *,
    subject_user_id: uuid.UUID | None,
    payload: dict[str, Any] | None = None,
    audience: str = "admins",
    recipient_id: uuid.UUID | None = None,
) -> Notification:
    """Завести уведомление; открытая просьба того же вида о том же — обновляется."""
    if kind in ACTIONABLE and subject_user_id is not None:
        existing = session.scalar(
            sa.select(Notification)
            .where(
                Notification.workspace_id == workspace_id,
                Notification.kind == kind,
                Notification.subject_user_id == subject_user_id,
                Notification.resolved_at.is_(None),
            )
            .order_by(Notification.created_at.desc())
            .limit(1)
        )
        if existing is not None:
            merged = dict(existing.payload or {})
            merged.update(payload or {})
            merged["repeats"] = int(merged.get("repeats", 1)) + 1
            existing.payload = merged
            existing.created_at = _now()
            session.flush()
            return existing
    item = Notification(
        workspace_id=workspace_id,
        audience=audience,
        recipient_id=recipient_id,
        kind=kind,
        subject_user_id=subject_user_id,
        payload=payload or {},
        created_at=_now(),
    )
    session.add(item)
    session.flush()
    return item


def last_request_at(session: Session, kind: str, subject_user_id: uuid.UUID) -> datetime | None:
    """Когда последний раз просили о том же — для «не чаще раза в 10 минут»."""
    return _aware(
        session.scalar(
            sa.select(sa.func.max(Notification.created_at)).where(
                Notification.kind == kind, Notification.subject_user_id == subject_user_id
            )
        )
    )


def pending_count(session: Session, workspace_id: uuid.UUID) -> int:
    """Число открытых просьб к администраторам — «N запросов» в раме."""
    return int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(Notification)
            .where(
                Notification.workspace_id == workspace_id,
                Notification.audience == "admins",
                Notification.kind.in_(ACTIONABLE),
                Notification.resolved_at.is_(None),
            )
        )
        or 0
    )


def open_about(session: Session, workspace_id: uuid.UUID, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Открытые просьбы по людям — для списка сотрудников одним запросом."""
    if not user_ids:
        return {}
    out: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for item in session.scalars(
        sa.select(Notification)
        .where(
            Notification.workspace_id == workspace_id,
            Notification.subject_user_id.in_(user_ids),
            Notification.kind.in_(ACTIONABLE),
            Notification.resolved_at.is_(None),
        )
        .order_by(Notification.created_at.desc())
    ):
        out.setdefault(item.subject_user_id, []).append(
            {"id": str(item.id), "kind": item.kind, "created_at": _iso(item.created_at)}
        )
    return out


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def listing(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    viewer_id: uuid.UUID,
    include_resolved: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Открытые просьбы, сведения за сутки и адресованное лично смотрящему."""
    from app.finance.contracts.models import Employee

    since = _now() - RECENT
    conditions = [Notification.workspace_id == workspace_id]
    audience = sa.or_(
        Notification.audience == "admins",
        sa.and_(Notification.audience == "user", Notification.recipient_id == viewer_id),
    )
    conditions.append(audience)
    if not include_resolved:
        conditions.append(
            sa.or_(
                sa.and_(Notification.kind.in_(ACTIONABLE), Notification.resolved_at.is_(None)),
                sa.and_(~Notification.kind.in_(ACTIONABLE), Notification.created_at >= since),
            )
        )
    items = list(
        session.scalars(
            sa.select(Notification)
            .where(*conditions)
            .order_by(Notification.created_at.desc())
            .limit(min(max(limit, 1), 500))
        )
    )
    subjects = {item.subject_user_id for item in items if item.subject_user_id}
    users = {
        user.id: user
        for user in session.scalars(sa.select(FinanceUser).where(FinanceUser.id.in_(subjects)))
    } if subjects else {}
    employees = {
        employee.user_id: employee
        for employee in session.scalars(
            sa.select(Employee).where(Employee.workspace_id == workspace_id, Employee.user_id.in_(subjects))
        )
    } if subjects else {}
    out = []
    for item in items:
        user = users.get(item.subject_user_id) if item.subject_user_id else None
        employee = employees.get(item.subject_user_id) if item.subject_user_id else None
        out.append(
            {
                "id": str(item.id),
                "kind": item.kind,
                "actionable": item.kind in ACTIONABLE,
                "created_at": _iso(item.created_at),
                "resolved_at": _iso(item.resolved_at),
                "resolved_by": str(item.resolved_by) if item.resolved_by else None,
                "payload": item.payload or {},
                "subject": (
                    {
                        "user_id": str(user.id),
                        "employee_id": str(employee.id) if employee else None,
                        "name": (employee.full_name if employee else "") or user.full_name or user.email or user.phone or "",
                        "phone": user.phone or "",
                    }
                    if user is not None
                    else None
                ),
            }
        )
    return {"items": out, "pending": pending_count(session, workspace_id)}


class NotFound(LookupError):
    """Уведомления нет в этой компании — ответ тот же, что «нет вовсе»."""


def resolve(session: Session, workspace_id: uuid.UUID, notification_id: uuid.UUID, *, by: uuid.UUID) -> Notification:
    item = session.get(Notification, notification_id)
    if item is None or item.workspace_id != workspace_id:
        raise NotFound("Уведомление не найдено")
    if item.resolved_at is None:
        item.resolved_at = _now()
        item.resolved_by = by
        session.flush()
    return item


def resolve_about(
    session: Session, workspace_id: uuid.UUID, subject_user_id: uuid.UUID, *, by: uuid.UUID | None
) -> int:
    """Закрыть просьбы о человеке — после сброса пароля им решать больше нечего."""
    result = session.execute(
        sa.update(Notification)
        .where(
            Notification.workspace_id == workspace_id,
            Notification.subject_user_id == subject_user_id,
            Notification.kind.in_(ACTIONABLE),
            Notification.resolved_at.is_(None),
        )
        .values(resolved_at=_now(), resolved_by=by)
    )
    return int(result.rowcount or 0)


__all__ = [
    "ACTIONABLE",
    "INFORMATIONAL",
    "NotFound",
    "last_request_at",
    "listing",
    "notify",
    "open_about",
    "pending_count",
    "resolve",
    "resolve_about",
]
