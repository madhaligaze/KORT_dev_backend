"""Права «Финансов»: кто что видит и правит.

Как складывается право
──────────────────────
* Владелец и администратор видят и правят всё. Записей прав у них нет и не
  нужно: иначе новый раздел оказался бы закрыт владельцу, пока кто-то не
  поставит ему галочку.
* Сотрудник — то, что записано в `access_grants` ему лично или его отделу.
  **Личное поверх отдельского**: запись человека по разделу перекрывает
  запись отдела целиком, вместе с областью договоров. **Нет записи — нет
  доступа.**
* Исключение — поля договора (`contracts.field.<ключ>`): нет записи — поле
  «как у договоров». Иначе каждое новое поле реестра пряталось бы от всех,
  пока его не откроют, а прятать — исключение («юристу скрыть „Оплачено“»),
  а не правило.
* Уровень ограничен разделом: у отчётов и журнала действий правки нет, и
  записанное «правит» читается как «видит». Единственный отчёт с правкой —
  «План/Факт»: там ставят планы.

Где считается
─────────────
Один раз на запрос, в `auth.resolve()`, одним запросом к базе (человек, его
отдел и обе стопки прав — `load`), и едет дальше в снимке `Member.rights`.
Маршруты спрашивают только снимок: `rights.can("journal", "edit")`.

Журнал операций и отчёты режутся только по разделу: у операций нет отдела,
и «видит журнал» значит «видит весь журнал» — экран прав так и пишет
(`note`). Договоры режутся по строкам и полям (`contracts/service.py`,
`Access`).

Другие отделы
─────────────
Область «своего отдела» или «где ответственный» можно расширить отделами,
договоры которых человек **только видит** (`scope.departments`, 28.09.2026:
юристу ЮО показать договоры НО, не давая их править). Правка такого договора
— отказ сервера, а не спрятанная кнопка: лист и карточка получают договор с
`readonly` и сами правку не предлагают.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.accounts_model import AccessGrant

LEVELS = ("none", "view", "edit")
_RANK = {"none": 0, "view": 1, "edit": 2}
FULL = ("none", "view", "edit")
VIEW_ONLY = ("none", "view")
#: Роли, которым права не записываются: они видят всё.
ADMIN_ROLES = frozenset({"owner", "admin"})
FIELD_PREFIX = "contracts.field."
ROW_SCOPES = ("all", "department", "own")


@dataclass(frozen=True)
class Resource:
    key: str
    title: str
    group: str
    levels: tuple[str, ...] = FULL
    #: Пометка системы на экране прав: «{ весь журнал }».
    note: str = ""


#: Разделы в порядке колонки. Ключи — договор с фронтом (`me.access`) и с
#: объявлением маршрутов; переименование ключа — это миграция прав.
RESOURCES: tuple[Resource, ...] = (
    Resource("contracts", "Договоры", "Договоры"),
    Resource("journal", "Журнал", "Учёт", note="весь журнал"),
    Resource("table", "Таблица", "Учёт", note="весь журнал"),
    Resource("calendar", "Календарь", "Учёт"),
    Resource("invoices", "Счета", "Учёт"),
    Resource("recurrences", "Повторения", "Учёт"),
    Resource("import", "Загрузка", "Учёт"),
    Resource("sheets", "Книги Google", "Учёт"),
    # Сводка в колонке разделов: остатки по счетам, «нам должны», «мы должны».
    # Своим правом, а не заодно с журналом: кассиру открывают журнал, чтобы он
    # вносил операции, а не чтобы знал, сколько у компании денег. Пока сводку
    # показывали каждому с любым денежным разделом, остатки видели все.
    Resource("reports.summary", "Остатки и долги", "Отчёты", VIEW_ONLY, note="колонка слева"),
    Resource("reports.cash", "Деньги", "Отчёты", VIEW_ONLY, note="весь журнал"),
    Resource("reports.profit", "Прибыль", "Отчёты", VIEW_ONLY, note="весь журнал"),
    Resource("reports.debts", "Долги", "Отчёты", VIEW_ONLY, note="весь журнал"),
    Resource("reports.balance", "Баланс", "Отчёты", VIEW_ONLY, note="весь журнал"),
    Resource("reports.indicators", "Показатели", "Отчёты", VIEW_ONLY, note="весь журнал"),
    Resource("reports.statement", "Выписка по счёту", "Отчёты", VIEW_ONLY, note="весь журнал"),
    Resource("reports.projects", "Проекты", "Отчёты", VIEW_ONLY, note="весь журнал"),
    # Правка здесь — поставить план на месяц.
    Resource("reports.plan", "План/Факт", "Отчёты", note="весь журнал"),
    Resource("integrations", "Подключения", "Настройка"),
    Resource("rules", "Автоправила", "Настройка"),
    Resource("dictionaries", "Справочники и счета", "Настройка"),
    Resource("people", "Сотрудники и права", "Кабинет"),
    Resource("audit", "Журнал действий", "Кабинет", VIEW_ONLY),
)
RESOURCE_BY_KEY: dict[str, Resource] = {item.key: item for item in RESOURCES}
#: Разделы с деньгами — им нужны справочники (счета, статьи) для форм.
#: Сводке справочники не нужны: у неё нет ни формы, ни пункта в колонке.
MONEY_RESOURCES: tuple[str, ...] = tuple(
    item.key for item in RESOURCES if item.key not in ("contracts", "people", "audit", "reports.summary")
)

#: Прежние роли → личные права, равные прежним способностям (ревизия 0019
#: и приглашение по почте со старой ролью).
#:
#: * `viewer` читал всё, кроме людей: `read`.
#: * `accountant` вёл учёт (`read` + `write`), но не заводил счета, не
#:   архивировал справочники и не трогал подключения — это была способность
#:   `accounts`. Поэтому справочники и подключения у него «видит», а новую
#:   статью или контрагента из формы он заводит правом журнала, как и раньше.
#: * `people` и `audit` не было ни у кого из них.
_VIEWABLE = [item.key for item in RESOURCES if item.key not in ("people", "audit")]
LEGACY_GRANTS: dict[str, dict[str, str]] = {
    "viewer": {key: "view" for key in _VIEWABLE},
    "accountant": {
        **{key: "view" for key in _VIEWABLE},
        **{
            key: "edit"
            for key in (
                "contracts", "journal", "table", "calendar", "invoices",
                "recurrences", "import", "sheets", "rules", "reports.plan",
            )
        },
    },
}


def max_level(resource: str) -> str:
    if resource.startswith(FIELD_PREFIX):
        return "edit"
    item = RESOURCE_BY_KEY.get(resource)
    return item.levels[-1] if item else "none"


def cap(resource: str, level: str) -> str:
    top = max_level(resource)
    return level if _RANK.get(level, 0) <= _RANK[top] else top


def rank(level: str) -> int:
    return _RANK.get(level, 0)


def resource_title(key: str) -> str:
    item = RESOURCE_BY_KEY.get(key)
    if item is not None:
        return item.title
    if key.startswith(FIELD_PREFIX):
        return f"поле «{key[len(FIELD_PREFIX):]}»"
    return key


_EMPTY: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class Rights:
    """Права человека в компании — снимок на запрос."""

    #: `owner` / `admin` / `employee`; пусто — компания не выбрана.
    role: str = ""
    #: Записанное (личное поверх отдельского), без полей договора.
    levels: Mapping[str, str] = field(default_factory=lambda: _EMPTY)
    contract_rows: str = "all"
    contract_entities: frozenset[uuid.UUID] = frozenset()
    #: Отделы, договоры которых видны вдобавок к области строк — только чтение.
    contract_departments: frozenset[uuid.UUID] = frozenset()
    #: Поля договора: ключ поля → уровень; только записанное.
    fields: Mapping[str, str] = field(default_factory=lambda: _EMPTY)
    employee_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None

    @property
    def is_admin(self) -> bool:
        return self.role in ADMIN_ROLES

    def level(self, resource: str) -> str:
        if self.is_admin:
            return max_level(resource)
        if not self.role:
            return "none"
        if resource.startswith(FIELD_PREFIX):
            return self.field_level(resource[len(FIELD_PREFIX):])
        return cap(resource, self.levels.get(resource, "none"))

    def can(self, resource: str, level: str = "view") -> bool:
        return rank(self.level(resource)) >= rank(level)

    def can_any(self, resources: Iterable[str], level: str = "view") -> bool:
        return any(self.can(resource, level) for resource in resources)

    def field_level(self, key: str) -> str:
        """Уровень поля договора: записанное, но не выше права на договоры."""
        base = self.level("contracts")
        if self.is_admin:
            return base
        own = self.fields.get(key)
        if own is None:
            return base
        return own if rank(own) <= rank(base) else base

    def access_map(self) -> dict[str, str]:
        return {item.key: self.level(item.key) for item in RESOURCES}

    def contracts_scope(self) -> dict[str, Any]:
        if self.is_admin:
            return {"rows": "all", "entities": [], "fields": {}}
        return {
            "rows": self.contract_rows,
            "entities": sorted(str(item) for item in self.contract_entities),
            "departments": sorted(str(item) for item in self.contract_departments),
            "department_id": str(self.department_id) if self.department_id else None,
            "employee_id": str(self.employee_id) if self.employee_id else None,
            # Только отличающиеся от уровня договоров: скрытое и «только видит».
            "fields": {key: self.field_level(key) for key in sorted(self.fields)},
        }

    def abilities(self) -> frozenset[str]:
        """Прежние способности — для старых экранов, которые спрашивают их."""
        if self.role == "owner":
            return frozenset({"read", "write", "accounts", "people", "company"})
        if self.role == "admin":
            return frozenset({"read", "write", "accounts", "people"})
        if not self.role:
            return frozenset()
        out: set[str] = set()
        if any(self.can(item.key) for item in RESOURCES):
            out.add("read")
        if self.can("journal", "edit"):
            out.add("write")
        if self.can("dictionaries", "edit"):
            out.add("accounts")
        if self.can("people", "edit"):
            out.add("people")
        return frozenset(out)


def _uuids(raw: Any) -> set[uuid.UUID]:
    out: set[uuid.UUID] = set()
    for item in raw or []:
        try:
            out.add(uuid.UUID(str(item)))
        except ValueError:
            continue
    return out


def compute(
    role: str,
    rows: Iterable[tuple[str | None, str | None, str | None, Any]],
    *,
    employee_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
) -> Rights:
    """Права из строк `(subject_kind, resource, level, scope)`."""
    if role in ADMIN_ROLES or not role:
        return Rights(role=role, employee_id=employee_id, department_id=department_id)
    personal: dict[str, tuple[str, dict]] = {}
    department: dict[str, tuple[str, dict]] = {}
    for kind, resource, level, scope in rows:
        if not resource or level not in _RANK:
            continue
        target = personal if kind == "employee" else department
        target[resource] = (level, scope if isinstance(scope, dict) else {})
    merged = {**department, **personal}
    levels = {key: value[0] for key, value in merged.items() if not key.startswith(FIELD_PREFIX)}
    fields = {
        key[len(FIELD_PREFIX):]: value[0]
        for key, value in merged.items()
        if key.startswith(FIELD_PREFIX)
    }
    scope = merged.get("contracts", ("none", {}))[1]
    rows_scope = scope.get("rows") if scope.get("rows") in ROW_SCOPES else "all"
    entities = _uuids(scope.get("entities"))
    # «Все договоры» и так видят каждый отдел — лишние отделы там ничего не значат.
    departments = _uuids(scope.get("departments")) if rows_scope != "all" else set()
    return Rights(
        role=role,
        levels=MappingProxyType(levels),
        contract_rows=rows_scope,
        contract_entities=frozenset(entities),
        contract_departments=frozenset(departments),
        fields=MappingProxyType(fields),
        employee_id=employee_id,
        department_id=department_id,
    )


def load(session: Session, workspace_id: uuid.UUID, user_id: uuid.UUID, role: str) -> Rights:
    """Права на запрос: один запрос к базе, и только у сотрудника.

    Человек, его отдел и обе стопки прав — одним `SELECT … LEFT JOIN`: это
    считается на каждом запросе, включая опрос реестра раз в две секунды.
    """
    if role in ADMIN_ROLES or not role:
        return Rights(role=role)
    from app.finance.contracts.models import Employee

    rows = session.execute(
        sa.select(
            Employee.id,
            Employee.department_id,
            AccessGrant.subject_kind,
            AccessGrant.resource,
            AccessGrant.level,
            AccessGrant.scope,
        )
        .select_from(Employee)
        .outerjoin(
            AccessGrant,
            sa.and_(
                AccessGrant.workspace_id == Employee.workspace_id,
                sa.or_(
                    sa.and_(AccessGrant.subject_kind == "employee", AccessGrant.subject_id == Employee.id),
                    sa.and_(
                        AccessGrant.subject_kind == "department",
                        AccessGrant.subject_id == Employee.department_id,
                    ),
                ),
            ),
        )
        .where(
            Employee.workspace_id == workspace_id,
            Employee.user_id == user_id,
            Employee.archived_at.is_(None),
        )
    ).all()
    if not rows:
        return Rights(role=role)
    employee_id, department_id = rows[0][0], rows[0][1]
    return compute(
        role,
        [(kind, resource, level, scope) for _eid, _did, kind, resource, level, scope in rows],
        employee_id=employee_id,
        department_id=department_id,
    )


# ── Записи прав ──────────────────────────────────────────────────────────────


def grants_of(
    session: Session, workspace_id: uuid.UUID, kind: str, subject_id: uuid.UUID
) -> dict[str, dict[str, Any]]:
    rows = session.scalars(
        sa.select(AccessGrant).where(
            AccessGrant.workspace_id == workspace_id,
            AccessGrant.subject_kind == kind,
            AccessGrant.subject_id == subject_id,
        )
    )
    return {
        row.resource: {"level": row.level, **({"scope": row.scope} if row.scope else {})}
        for row in rows
    }


def field_keys(session: Session, workspace_id: uuid.UUID) -> list[tuple[str, str]]:
    """Поля реестра договоров компании: ключ и подпись."""
    from app.finance.contracts.models import EntityField

    return [
        (key, title)
        for key, title in session.execute(
            sa.select(EntityField.key, EntityField.title)
            .where(
                EntityField.workspace_id == workspace_id,
                EntityField.entity == "contract",
                EntityField.archived_at.is_(None),
            )
            .order_by(EntityField.position, EntityField.created_at)
        )
    ]


def catalog(session: Session, workspace_id: uuid.UUID) -> dict[str, Any]:
    """Что показывать на экране прав: разделы и поля договора."""
    return {
        "resources": [
            {
                "key": item.key,
                "title": item.title,
                "group": item.group,
                "levels": list(item.levels),
                **({"note": item.note} if item.note else {}),
            }
            for item in RESOURCES
        ],
        "fields": [
            {"key": f"{FIELD_PREFIX}{key}", "field": key, "title": title, "levels": list(FULL)}
            for key, title in field_keys(session, workspace_id)
        ],
        "row_scopes": list(ROW_SCOPES),
    }


class GrantError(ValueError):
    """Запись права нельзя принять — с текстом для человека."""


def clean_scope(session: Session, workspace_id: uuid.UUID, raw: Any) -> dict[str, Any]:
    """Область договоров: какие строки и какими юрлицами. Чужие юрлица — отказ."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise GrantError("Область договоров записана неверно")
    rows = raw.get("rows", "all")
    if rows not in ROW_SCOPES:
        raise GrantError("Какие договоры: все, своего отдела или где он ответственный")
    entities: list[str] = []
    wanted = raw.get("entities") or []
    if wanted:
        from app.finance.contracts.models import GroupEntity

        try:
            ids = {uuid.UUID(str(item)) for item in wanted}
        except ValueError as exc:
            raise GrantError("Юрлицо указано неверно") from exc
        known = set(
            session.scalars(
                sa.select(GroupEntity.counterparty_id).where(
                    GroupEntity.workspace_id == workspace_id,
                    GroupEntity.counterparty_id.in_(ids),
                )
            )
        )
        if known != ids:
            raise GrantError("Такого нашего юрлица нет")
        entities = sorted(str(item) for item in ids)
    departments: list[str] = []
    wanted_departments = raw.get("departments") or []
    if wanted_departments and rows != "all":
        from app.finance.contracts.models import Department

        try:
            ids = {uuid.UUID(str(item)) for item in wanted_departments}
        except ValueError as exc:
            raise GrantError("Отдел указан неверно") from exc
        known = set(
            session.scalars(
                sa.select(Department.id).where(Department.workspace_id == workspace_id, Department.id.in_(ids))
            )
        )
        if known != ids:
            raise GrantError("Такого отдела нет")
        departments = sorted(str(item) for item in ids)
    out: dict[str, Any] = {"rows": rows}
    if entities:
        out["entities"] = entities
    if departments:
        out["departments"] = departments
    return out


def put_grants(
    session: Session,
    workspace_id: uuid.UUID,
    kind: str,
    subject_id: uuid.UUID,
    changes: Mapping[str, Any],
    *,
    by: uuid.UUID | None,
) -> list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]]:
    """Записать права субъекта. Возвращает `(ресурс, было, стало)` по изменённым.

    `None` у человека — «как у отдела» (личная запись снимается); у отдела
    `None` и `none` значат одно: записи нет — доступа нет.
    """
    fields = {f"{FIELD_PREFIX}{key}" for key, _title in field_keys(session, workspace_id)}
    existing = {
        row.resource: row
        for row in session.scalars(
            sa.select(AccessGrant).where(
                AccessGrant.workspace_id == workspace_id,
                AccessGrant.subject_kind == kind,
                AccessGrant.subject_id == subject_id,
            )
        )
    }
    done: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = []
    now = datetime.now(timezone.utc)
    for resource, raw in changes.items():
        if resource not in RESOURCE_BY_KEY and resource not in fields:
            raise GrantError(f"Раздела «{resource}» нет")
        if raw is None:
            level, scope_raw = None, None
        elif isinstance(raw, str):
            level, scope_raw = raw, None
        elif isinstance(raw, dict):
            level, scope_raw = raw.get("level"), raw.get("scope")
        else:
            raise GrantError(f"{resource_title(resource)}: право записано неверно")
        if level is not None and level not in LEVELS:
            raise GrantError(f"{resource_title(resource)}: уровень — нет, видит или правит")
        if level is not None and rank(level) > rank(max_level(resource)):
            raise GrantError(f"{resource_title(resource)}: здесь можно только смотреть")
        if kind == "department" and level == "none" and not resource.startswith(FIELD_PREFIX):
            level = None  # у отдела «нет» — это отсутствие записи
        scope = clean_scope(session, workspace_id, scope_raw) if resource == "contracts" else {}
        row = existing.get(resource)
        before = (
            {"level": row.level, **({"scope": row.scope} if row.scope else {})} if row is not None else None
        )
        if level is None:
            if row is not None:
                session.delete(row)
                done.append((resource, before, None))
            continue
        if row is not None and scope_raw is None and resource == "contracts":
            # Прислали только уровень — область остаётся прежней.
            scope = dict(row.scope or {})
        after = {"level": level, **({"scope": scope} if scope else {})}
        if before == after:
            continue
        if row is None:
            session.add(
                AccessGrant(
                    workspace_id=workspace_id,
                    subject_kind=kind,
                    subject_id=subject_id,
                    resource=resource,
                    level=level,
                    scope=scope,
                    updated_by=by,
                    updated_at=now,
                )
            )
        else:
            row.level = level
            row.scope = scope
            row.updated_by = by
            row.updated_at = now
        done.append((resource, before, after))
    session.flush()
    return done


def grant_legacy(
    session: Session, workspace_id: uuid.UUID, employee_id: uuid.UUID, role: str, *, by: uuid.UUID | None
) -> None:
    """Права прежней роли `accountant`/`viewer` — лично человеку."""
    grants = LEGACY_GRANTS.get(role)
    if grants:
        put_grants(session, workspace_id, "employee", employee_id, grants, by=by)


def drop_subject(session: Session, workspace_id: uuid.UUID, kind: str, subject_id: uuid.UUID) -> None:
    session.execute(
        sa.delete(AccessGrant).where(
            AccessGrant.workspace_id == workspace_id,
            AccessGrant.subject_kind == kind,
            AccessGrant.subject_id == subject_id,
        )
    )


LEVEL_TITLES = {"none": "Нет", "view": "Видит", "edit": "Правит", None: "как у отдела"}


def describe_change(resource: str, before: dict | None, after: dict | None, kind: str = "department") -> str:
    """«Журнал — Видит → Нет» для журнала действий.

    Отсутствие записи у отдела — «Нет», у человека — «как у отдела».
    """
    missing = "Нет" if kind == "department" else LEVEL_TITLES[None]
    was = LEVEL_TITLES.get(before.get("level"), "Нет") if before else missing
    now = LEVEL_TITLES.get(after.get("level"), "Нет") if after else missing
    title = resource_title(resource)
    if before and after and before.get("level") == after.get("level"):
        return f"{title}: область изменена"
    return f"{title} — {was} → {now}"


__all__ = [
    "ADMIN_ROLES",
    "FIELD_PREFIX",
    "LEGACY_GRANTS",
    "LEVELS",
    "MONEY_RESOURCES",
    "RESOURCES",
    "RESOURCE_BY_KEY",
    "ROW_SCOPES",
    "GrantError",
    "Resource",
    "Rights",
    "catalog",
    "clean_scope",
    "compute",
    "describe_change",
    "drop_subject",
    "field_keys",
    "grant_legacy",
    "grants_of",
    "load",
    "max_level",
    "put_grants",
    "rank",
    "resource_title",
]
