"""Журнал действий компании: чтение, просмотры, очистка.

Запись идёт через `history.write` — одна дорога для правок данных, входов,
администрирования, выгрузок и просмотров (автор, сеанс и адрес — из
контекста запроса, см. `history.py`). Здесь то, что журнал делает сверх
записи:

* **лента с фильтрами** — человек, отдел, вид, запись, даты; курсор по
  `(at, id)`, а не номер страницы: пока человек листает, наверху появляются
  новые события, и страница «2» съехала бы на одну строку;
* **просмотры** — сигнал фронта «открыт раздел» и открытие карточки
  договора, не чаще раза в минуту на раздел в сеансе: иначе переключение
  туда-обратно превращало бы журнал в поток одинаковых строк;
* **очистка** — просмотры старше 180 дней уходят раз в час из фоновой
  задачи договоров (своего вечного цикла у журнала нет). Всё остальное
  хранится всегда, и API на правку или удаление записей нет.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.access import RESOURCE_BY_KEY, Rights
from app.finance.accounts_model import FinanceUser
from app.finance.models import ACTION_CATEGORIES, ActionLog, Workspace

#: Пояс компании — границы дня в фильтре дат.
COMPANY_TZ = timezone(timedelta(hours=5))
VIEW_RETENTION = timedelta(days=180)
VIEW_EVERY = 60.0
PAGE = 100
PAGE_MAX = 500

#: Разделы, открытие которых отмечается. Ключи — ресурсы прав плюс экраны без
#: своего права.
SECTION_TITLES: dict[str, str] = {
    **{key: item.title for key, item in RESOURCE_BY_KEY.items()},
    "overview": "Обзор",
}


class AuditError(ValueError):
    """Фильтр или сигнал записан неверно — 422 с текстом."""


# ── Просмотры ────────────────────────────────────────────────────────────────

_VIEWS: dict[tuple[Any, ...], float] = {}
_VIEWS_LOCK = threading.Lock()
_VIEWS_SWEEP_AT = 4096


def _seen_recently(key: tuple[Any, ...]) -> bool:
    now = time.monotonic()
    with _VIEWS_LOCK:
        last = _VIEWS.get(key)
        if last is not None and now - last < VIEW_EVERY:
            return True
        if len(_VIEWS) >= _VIEWS_SWEEP_AT:
            for stale in [k for k, stamp in _VIEWS.items() if now - stamp >= VIEW_EVERY]:
                del _VIEWS[stale]
        _VIEWS[key] = now
        return False


def section_for_view(section: str) -> str:
    """Проверить ключ раздела из сигнала фронта."""
    if section not in SECTION_TITLES:
        raise AuditError(f"Раздела «{section}» нет")
    return section


def record_view(
    session: Session,
    workspace: Workspace,
    *,
    session_id: uuid.UUID,
    section: str,
    contract_id: uuid.UUID | None = None,
    contract_number: str = "",
) -> bool:
    """Отметить просмотр. `False` — такой же был меньше минуты назад."""
    if _seen_recently((session_id, section, contract_id)):
        return False
    if contract_id is not None:
        number = contract_number.strip()
        title = f"открыт договор {number}".strip() if number else "открыт договор"
        history.write(
            session, workspace, kind="view.contract", entity="contract", entity_id=contract_id,
            title=title, after={"section": section}, category="view",
        )
    else:
        history.write(
            session, workspace, kind="view.section", entity="section",
            title=f"открыт раздел «{SECTION_TITLES[section]}»", after={"section": section},
            category="view",
        )
    return True


def purge_views(session: Session, *, now: datetime | None = None) -> int:
    """Удалить просмотры старше 180 дней. Одно `DELETE` по индексу `(category, at)`."""
    edge = (now or datetime.now(timezone.utc)) - VIEW_RETENTION
    result = session.execute(
        sa.delete(ActionLog).where(ActionLog.category == "view", ActionLog.at < edge)
    )
    return int(result.rowcount or 0)


# ── Откат из журнала ─────────────────────────────────────────────────────────

#: Какое право нужно, чтобы откатить правку этой записи.
_UNDO_RESOURCES = {
    "operation": ("journal", "table", "calendar"),
    "operations": ("rules",),
    "account": ("dictionaries",),
}


def undo_resources(entry: ActionLog) -> tuple[str, ...]:
    return _UNDO_RESOURCES.get(entry.entity, ())


def can_undo(entry: ActionLog, rights: Rights) -> bool:
    if entry.undone_at is not None or not history._undoable(entry):
        return False
    resources = undo_resources(entry)
    return bool(resources) and rights.can_any(resources, "edit")


# ── Лента ────────────────────────────────────────────────────────────────────


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _uuid(value: Any, *, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise AuditError(f"{field}: ждём идентификатор") from exc


def _day_edge(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=COMPANY_TZ)


def _cursor(value: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not value:
        return None
    try:
        stamp, _, raw_id = value.partition("|")
        at = datetime.fromisoformat(stamp)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return at, uuid.UUID(raw_id)
    except ValueError as exc:
        raise AuditError("Курсор ленты записан неверно") from exc


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _people(session: Session, workspace_id: uuid.UUID, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """Автор по идентификатору: имя из записи сотрудника компании, иначе из учётки."""
    from app.finance.contracts.models import Employee
    from app.finance.people import short_name

    ids = {item for item in user_ids if item}
    if not ids:
        return {}
    users = {
        user.id: user for user in session.scalars(sa.select(FinanceUser).where(FinanceUser.id.in_(ids)))
    }
    employees = {
        employee.user_id: employee
        for employee in session.scalars(
            sa.select(Employee).where(Employee.workspace_id == workspace_id, Employee.user_id.in_(ids))
        )
    }
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for user_id in ids:
        user, employee = users.get(user_id), employees.get(user_id)
        name = (employee.full_name if employee else "") or (user.full_name if user else "") or (
            (user.email or user.phone or "") if user else ""
        )
        out[user_id] = {
            "user_id": str(user_id),
            "employee_id": str(employee.id) if employee else None,
            "name": name,
            "short_name": short_name(name) if name and " " in name else name,
        }
    return out


def listing(
    session: Session,
    workspace_id: uuid.UUID,
    rights: Rights,
    *,
    person: uuid.UUID | None = None,
    employee: uuid.UUID | None = None,
    department: uuid.UUID | None = None,
    categories: str | None = None,
    kinds: str | None = None,
    entity_id: uuid.UUID | None = None,
    q: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    cursor: str | None = None,
    limit: int = PAGE,
) -> dict[str, Any]:
    """Лента журнала действий, новые сверху, с курсором на следующую страницу."""
    from app.finance.contracts.models import Employee

    conditions: list[Any] = [ActionLog.workspace_id == workspace_id]
    if person is not None:
        # «Действия человека» — и то, что делал он, и то, что делали с его
        # учёткой (неверный пароль, сброс): автор второго неизвестен.
        conditions.append(
            sa.or_(ActionLog.user_id == person, sa.and_(ActionLog.entity == "user", ActionLog.entity_id == person))
        )
    if employee is not None:
        record = session.get(Employee, employee)
        if record is None or record.workspace_id != workspace_id:
            raise AuditError("Сотрудник не найден")
        about = [ActionLog.entity_id == record.id]
        if record.user_id is not None:
            about += [ActionLog.user_id == record.user_id, ActionLog.entity_id == record.user_id]
        conditions.append(sa.or_(*about))
    if department is not None:
        members = sa.select(Employee.user_id).where(
            Employee.workspace_id == workspace_id,
            Employee.department_id == department,
            Employee.user_id.is_not(None),
        )
        conditions.append(ActionLog.user_id.in_(members))
    wanted = _split(categories)
    unknown = [item for item in wanted if item not in ACTION_CATEGORIES]
    if unknown:
        raise AuditError(f"Вида «{unknown[0]}» нет. Бывают: {', '.join(ACTION_CATEGORIES)}")
    if wanted:
        conditions.append(ActionLog.category.in_(wanted))
    kind_list = _split(kinds)
    if kind_list:
        conditions.append(
            sa.or_(
                *[
                    ActionLog.kind.startswith(item[:-1]) if item.endswith("*") else ActionLog.kind == item
                    for item in kind_list
                ]
            )
        )
    if entity_id is not None:
        conditions.append(ActionLog.entity_id == entity_id)
    if q and q.strip():
        conditions.append(ActionLog.title.ilike(f"%{q.strip()}%"))
    if date_from is not None:
        conditions.append(ActionLog.at >= _day_edge(date_from))
    if date_to is not None:
        conditions.append(ActionLog.at < _day_edge(date_to + timedelta(days=1)))
    position = _cursor(cursor)
    if position is not None:
        at, last_id = position
        conditions.append(sa.or_(ActionLog.at < at, sa.and_(ActionLog.at == at, ActionLog.id < last_id)))

    size = min(max(int(limit or PAGE), 1), PAGE_MAX)
    rows = list(
        session.scalars(
            sa.select(ActionLog)
            .where(*conditions)
            .order_by(ActionLog.at.desc(), ActionLog.id.desc())
            .limit(size + 1)
        )
    )
    more = len(rows) > size
    rows = rows[:size]
    authors = _people(session, workspace_id, [row.user_id for row in rows if row.user_id])
    items = []
    for entry in rows:
        before, after = entry.before or {}, entry.after or {}
        if entry.kind == "autotag.apply":
            # Разметка хранит пары «операция → статья» на тысячи строк; в ленте
            # нужно только сколько.
            before = {"operations": len(before.get("items", []))} if "items" in before else before
            after = {"operations": len(after.get("items", []))} if "items" in after else after
        items.append(
            {
                "id": str(entry.id),
                "at": _iso(entry.at),
                "category": entry.category or "data",
                "kind": entry.kind,
                "title": entry.title,
                "entity": entry.entity,
                "entity_id": str(entry.entity_id) if entry.entity_id else None,
                "before": before,
                "after": after,
                "actor": authors.get(entry.user_id) if entry.user_id else None,
                "actor_text": entry.actor,
                "ip": entry.ip or "",
                "user_agent": entry.user_agent or "",
                "session_id": str(entry.session_id) if entry.session_id else None,
                "undone_at": _iso(entry.undone_at),
                "can_undo": can_undo(entry, rights),
            }
        )
    next_cursor = f"{_iso(rows[-1].at)}|{rows[-1].id}" if more and rows else None
    return {"items": items, "next_cursor": next_cursor}


__all__ = [
    "AuditError",
    "SECTION_TITLES",
    "VIEW_RETENTION",
    "can_undo",
    "listing",
    "purge_views",
    "record_view",
    "section_for_view",
    "undo_resources",
]
