"""Слои настройки (фаза 9, R15 и R17): шаблон → компания → отдел → человек.

Шаблон «Компания услуг по договорам» - то, с чего начинается каждая компания
(засев реестра, `contracts/fields.py`): поля договора, статусы с фазами, виды
договоров с тем, как начислять, хозяйственные смыслы, книга «Разовые».
Слой компании - её слова и решения поверх шаблона: переименованные и свои
значения списков, другие смыслы, свои поля и листы, словарь колонок и слов
таблицы, начало учёта оплат, звенья на KORT. Слой отдела - права отдела
(потолок для его людей). Слой человека - личные права внутри потолка, личный
вид листов и закреплённые разделы колонки.

BBC - слой компании на шаблоне: всё, чем BBC отличается от любой новой
компании, видно здесь списком, а не размазано по коду. Смысл значения,
который компания поменяла, возвращается к шаблонному обычной правкой
значения (`PATCH /contracts/setup/values/{id}`, с журналом) - `template_meaning`
у каждого значения; остальное компания правит там же, где задавала.
"""
from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.accounts_model import AccessGrant
from app.finance.contracts.fields import SEED_ECONOMIC_ROLES, SEED_STATUSES, SEED_TYPES, SYSTEM_FIELDS
from app.finance.contracts.models import Department, Employee, EntityField, EntityView, ListValue
from app.finance.contracts.service import Access
from app.finance.core import settings as core_settings
from app.finance.layout import norm
from app.finance.models import SheetLook, Workspace

TEMPLATE_TITLE = "Компания услуг по договорам"
#: Списки шаблона: поле → (подпись значения, смысл); смысл - то, по чему значение узнаётся.
TEMPLATE_LISTS: dict[str, tuple[tuple[str, dict[str, Any]], ...]] = {
    "status": SEED_STATUSES,
    "type": SEED_TYPES,
    "economic_role": tuple((value, {"system": system}) for value, system in SEED_ECONOMIC_ROLES),
}
LIST_TITLES = {"status": "Статусы", "type": "Виды договоров", "economic_role": "Хозяйственный смысл"}
#: Ключ смысла, по которому значение компании узнаётся как шаблонное.
_MEANING_KEY = {"status": "phase", "economic_role": "system"}


def _require(access: Access) -> None:
    if not access.setup:
        raise PermissionError("Слои настройки видит владелец или администратор")


def _template_list(field_key: str, rows: list[ListValue]) -> dict[str, Any]:
    """Список компании против шаблона: как в шаблоне, переименовано, смысл другой, своё, убрано."""
    template = TEMPLATE_LISTS[field_key]
    key = _MEANING_KEY.get(field_key)
    live = [row for row in rows if row.archived_at is None]
    used: set[uuid.UUID] = set()
    items: list[dict[str, Any]] = []
    for title, meaning in template:
        if key is not None:
            # Статус и хоз. смысл узнаются по смыслу: подпись компания меняет как хочет.
            match = next((row for row in live if (row.meaning or {}).get(key) == meaning[key] and row.id not in used), None)
        else:
            match = next((row for row in live if row.normalized == norm(title) and row.id not in used), None)
        if match is None:
            items.append({"template": title, "value": None, "id": None, "state": "removed", "meaning": meaning, "template_meaning": meaning})
            continue
        used.add(match.id)
        own = {name: value for name, value in (match.meaning or {}).items() if name != "distinct"}
        if key is None and own != meaning:
            state = "meaning"
        elif match.value != title:
            state = "renamed"
        else:
            state = "template"
        items.append(
            {"template": title, "value": match.value, "id": str(match.id), "state": state, "meaning": own, "template_meaning": meaning}
        )
    for row in live:
        if row.id in used:
            continue
        items.append(
            {
                "template": None,
                "value": row.value,
                "id": str(row.id),
                "state": "added",
                "meaning": {name: value for name, value in (row.meaning or {}).items() if name != "distinct"},
                "template_meaning": None,
            }
        )
    counts = {state: sum(1 for item in items if item["state"] == state) for state in ("template", "renamed", "meaning", "added", "removed")}
    return {"key": field_key, "title": LIST_TITLES[field_key], "counts": counts, "items": items}


def layers(session: Session, workspace: Workspace, access: Access) -> dict[str, Any]:
    """Что задаёт каждый слой настройки и чем компания отличается от шаблона."""
    _require(access)
    values = list(
        session.scalars(
            sa.select(ListValue).where(ListValue.workspace_id == workspace.id, ListValue.field_key.in_(list(TEMPLATE_LISTS)))
        )
    )
    lists = [_template_list(field_key, [row for row in values if row.field_key == field_key]) for field_key in TEMPLATE_LISTS]
    system = {item.key for item in SYSTEM_FIELDS}
    fields = list(session.scalars(sa.select(EntityField).where(EntityField.workspace_id == workspace.id)))
    own_fields = sorted((row.title for row in fields if row.key not in system and not row.system), key=str.lower)
    hidden = sorted((row.title for row in fields if row.key in system and row.hidden), key=str.lower)
    views = list(
        session.scalars(sa.select(EntityView).where(EntityView.workspace_id == workspace.id, EntityView.archived_at.is_(None)))
    )
    books: dict[str, int] = {}
    for view in views:
        books[view.book or "registry"] = books.get(view.book or "registry", 0) + 1
    columns = core_settings.book_columns(session, workspace.id)
    words = core_settings.table_words(session, workspace.id)
    payments = core_settings.payments(session, workspace)
    from app.finance.core import switch

    sources = switch.sources(session, workspace.id)
    departments = list(session.scalars(sa.select(Department).where(Department.workspace_id == workspace.id, Department.archived_at.is_(None))))
    grants = list(session.scalars(sa.select(AccessGrant).where(AccessGrant.workspace_id == workspace.id)))
    by_department: dict[uuid.UUID, int] = {}
    for grant in grants:
        if grant.subject_kind == "department":
            by_department[grant.subject_id] = by_department.get(grant.subject_id, 0) + 1
    people_grants = len({grant.subject_id for grant in grants if grant.subject_kind == "employee"})
    employees = session.scalar(
        sa.select(sa.func.count()).select_from(Employee).where(Employee.workspace_id == workspace.id, Employee.archived_at.is_(None))
    ) or 0
    looks = list(session.scalars(sa.select(SheetLook).where(SheetLook.workspace_id == workspace.id)))
    return {
        "template": {
            "title": TEMPLATE_TITLE,
            "fields": len(SYSTEM_FIELDS),
            "lists": {key: len(items) for key, items in TEMPLATE_LISTS.items()},
        },
        "company": {
            "lists": lists,
            "fields": own_fields,
            "hidden_fields": hidden,
            "views": books,
            "columns": sum(len(names) for names in columns.values()),
            "words": sum(len(items) for items in words.values()),
            "since": payments.since.isoformat() if payments.since else None,
            "kort": {link: sorted(items) for link, items in sources.items() if items},
        },
        "departments": [
            {"id": str(row.id), "code": row.code, "title": row.title, "grants": by_department.get(row.id, 0)}
            for row in sorted(departments, key=lambda item: (item.position, item.code))
        ],
        "people": {
            "employees": employees,
            "with_own_rights": people_grants,
            "looks": len({look.user_id for look in looks if not look.key.startswith("nav")}),
            "pins": len({look.user_id for look in looks if look.key.startswith("nav")}),
        },
    }


__all__ = ["LIST_TITLES", "TEMPLATE_LISTS", "TEMPLATE_TITLE", "layers"]
