"""HTTP-маршруты доступа «Финансов»: люди, права, журнал действий, уведомления.

Всё это — личный кабинет, а не колонка разделов: отделы и сотрудники, права
галочками, журнал действий компании и «Ждут решения». Вход и компания — те
же, что у раздела (`require_access`, `_workspace` из `routes/finance.py`).

Что фронт получает на отказ
───────────────────────────
* 403 — действие не открыто (права «Сотрудники и права», чужая роль:
  администратор не меняет владельца и других администраторов);
* 404 — нет такого или он чужой; ответ одинаковый, иначе перебор
  идентификаторов выдавал бы, кто есть в другой компании;
* 400 — значение нельзя принять, с текстом;
* 422 — фильтр журнала записан неверно.

Обработчики — обычный `def`: внутри синхронная SQLAlchemy.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.api.routes.finance import (
    UNDO_RESOURCES,
    _guard,
    _workspace,
    current_member,
    require_access,
    undo_entry,
)
from app.finance import access as access_module
from app.finance import audit, history, notifications, people
from app.finance.auth import Member
from app.finance.db import finance_session

router = APIRouter(prefix="/finance", tags=["finance-people"])


def _people_fail(exc: people.PeopleError) -> HTTPException:
    if isinstance(exc, people.NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, people.Forbidden):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


# ── Отделы и сотрудники ─────────────────────────────────────────────────────


class DepartmentIn(BaseModel):
    code: str | None = None
    title: str | None = None
    archived: bool | None = None


class EmployeeIn(BaseModel):
    full_name: str | None = None
    job_title: str | None = None
    department_id: UUID | None = None
    phone: str | None = None
    #: Галочка «Доступ в систему»: учётка по номеру, ждёт пароль 72 часа.
    access: bool | None = None
    #: `employee` | `admin` (администратора назначает владелец).
    role: str | None = None


class AccountIn(BaseModel):
    phone: str | None = None
    role: str = "employee"


@router.get("/people")
def people_overview(
    include_archived: bool = Query(False), member: Member = Depends(require_access("people"))
) -> dict[str, Any]:
    """Кабинет → «Сотрудники»: отделы со счётчиками и все сотрудники одним ответом."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {
            "departments": people.list_departments(session, workspace.id, include_archived=include_archived),
            "employees": people.list_employees(session, workspace.id, include_archived=include_archived),
        }


@router.get("/people/departments")
def list_departments(
    include_archived: bool = Query(False), member: Member = Depends(require_access("people"))
) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {"items": people.list_departments(session, workspace.id, include_archived=include_archived)}


@router.post("/people/departments", status_code=201)
def create_department(body: DepartmentIn, member: Member = Depends(require_access("people", "edit"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            item = people.create_department(session, workspace, member, body.model_dump(exclude_unset=True))
        except people.PeopleError as exc:
            raise _people_fail(exc) from exc
        return people.department_out(item)


@router.patch("/people/departments/{department_id}")
def update_department(
    department_id: UUID, body: DepartmentIn, member: Member = Depends(require_access("people", "edit"))
) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            item = people.update_department(
                session, workspace, member, department_id, body.model_dump(exclude_unset=True)
            )
        except people.PeopleError as exc:
            raise _people_fail(exc) from exc
        return people.department_out(item)


@router.get("/people/employees")
def list_employees(
    include_archived: bool = Query(False), member: Member = Depends(require_access("people"))
) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {"items": people.list_employees(session, workspace.id, include_archived=include_archived)}


@router.post("/people/employees", status_code=201)
def create_employee(body: EmployeeIn, member: Member = Depends(require_access("people", "edit"))) -> dict[str, Any]:
    """«+ Сотрудник». С `access: true` нужен `phone`: учётка ждёт пароль 72 часа."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            employee = people.create_employee(session, workspace, member, body.model_dump(exclude_unset=True))
        except people.PeopleError as exc:
            raise _people_fail(exc) from exc
        return people.employee_payload(session, workspace.id, employee)


@router.get("/people/employees/{employee_id}")
def get_employee(employee_id: UUID, member: Member = Depends(require_access("people"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            employee = people.get_employee(session, workspace.id, employee_id)
        except people.PeopleError as exc:
            raise _people_fail(exc) from exc
        return people.employee_payload(session, workspace.id, employee)


@router.patch("/people/employees/{employee_id}")
def update_employee(
    employee_id: UUID, body: EmployeeIn, member: Member = Depends(require_access("people", "edit"))
) -> dict[str, Any]:
    """ФИО, должность, отдел, телефон, роль (только владелец), `access` да/нет."""
    _guard()
    data = body.model_dump(exclude_unset=True)
    if "department_id" in data and data["department_id"] is not None:
        data["department_id"] = str(data["department_id"])
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            employee = people.update_employee(session, workspace, member, employee_id, data)
        except people.PeopleError as exc:
            raise _people_fail(exc) from exc
        return people.employee_payload(session, workspace.id, employee)


@router.delete("/people/employees/{employee_id}")
def archive_employee(employee_id: UUID, member: Member = Depends(require_access("people", "edit"))) -> dict[str, Any]:
    """Убрать в архив вместе с доступом. Удалить нельзя: человек стоит в договорах."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            employee = people.archive_employee(session, workspace, member, employee_id)
        except people.PeopleError as exc:
            raise _people_fail(exc) from exc
        return people.employee_payload(session, workspace.id, employee)


@router.post("/people/employees/{employee_id}/account", status_code=201)
def create_account(
    employee_id: UUID, body: AccountIn, member: Member = Depends(require_access("people", "edit"))
) -> dict[str, Any]:
    """Открыть вход сотруднику из справочника: телефон — учётка ждёт пароль."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            employee = people.get_employee(session, workspace.id, employee_id)
            people.create_account(session, workspace, member, employee, phone=body.phone, role=body.role)
        except people.PeopleError as exc:
            raise _people_fail(exc) from exc
        return people.employee_payload(session, workspace.id, employee)


def _employee_action(member: Member, employee_id: UUID, action) -> dict[str, Any]:
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            result = action(session, workspace)
            employee = people.get_employee(session, workspace.id, employee_id)
        except people.PeopleError as exc:
            raise _people_fail(exc) from exc
        payload = people.employee_payload(session, workspace.id, employee)
        if isinstance(result, int):
            payload["sessions_closed"] = result
        return payload


@router.post("/people/employees/{employee_id}/reset")
def reset_password(employee_id: UUID, member: Member = Depends(require_access("people", "edit"))) -> dict[str, Any]:
    """Сброс: старый пароль не действует, сеансы закрыты, снова ждёт пароль 72 часа.

    Сотрудников сбрасывает администратор, администраторов — владелец, владельца —
    только команда на сервере.
    """
    _guard()
    return _employee_action(
        member, employee_id, lambda s, w: people.reset_password(s, w, member, employee_id)
    )


@router.post("/people/employees/{employee_id}/block")
def block_employee(employee_id: UUID, member: Member = Depends(require_access("people", "edit"))) -> dict[str, Any]:
    _guard()
    return _employee_action(
        member, employee_id, lambda s, w: people.set_blocked(s, w, member, employee_id, blocked=True)
    )


@router.post("/people/employees/{employee_id}/unblock")
def unblock_employee(employee_id: UUID, member: Member = Depends(require_access("people", "edit"))) -> dict[str, Any]:
    _guard()
    return _employee_action(
        member, employee_id, lambda s, w: people.set_blocked(s, w, member, employee_id, blocked=False)
    )


@router.post("/people/employees/{employee_id}/end-sessions")
def end_sessions(employee_id: UUID, member: Member = Depends(require_access("people", "edit"))) -> dict[str, Any]:
    _guard()
    return _employee_action(
        member, employee_id, lambda s, w: people.end_employee_sessions(s, w, member, employee_id)
    )


@router.get("/people/employees/{employee_id}/sessions")
def employee_sessions(employee_id: UUID, member: Member = Depends(require_access("people"))) -> dict[str, Any]:
    """Сеансы сотрудника: устройство, IP, последнее действие."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return {"items": people.employee_sessions(session, workspace, member, employee_id)}
        except people.PeopleError as exc:
            raise _people_fail(exc) from exc


# ── Права ────────────────────────────────────────────────────────────────────


class GrantsIn(BaseModel):
    #: Чужой ключ — отказ, а не «200 и ничего не записано»: тело
    #: `{"contracts": …}` без `changes` раньше проходило молча.
    model_config = ConfigDict(extra="forbid")

    #: `{ресурс: "none"|"view"|"edit" | {level, scope} | null}`. `null` у
    #: человека — «как у отдела».
    changes: dict[str, Any] | None = None
    #: Прежнее имя того же поля.
    grants: dict[str, Any] | None = None


@router.get("/access/catalog")
def access_catalog(member: Member = Depends(require_access("people"))) -> dict[str, Any]:
    """Разделы с уровнями и поля договора — строки экрана прав."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return access_module.catalog(session, workspace.id)


def _subject(session, workspace_id: UUID, kind: str, subject_id: UUID):
    """Отдел или сотрудник этой компании; чужой — 404, как несуществующий."""
    from app.finance.contracts.models import Department, Employee

    if kind == "department":
        item = session.get(Department, subject_id)
    elif kind == "employee":
        item = session.get(Employee, subject_id)
    else:
        raise HTTPException(status_code=404, detail="Права бывают у отдела или сотрудника")
    if item is None or item.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Не найдено")
    return item


def _effective(rights: access_module.Rights, fields: list[tuple[str, str]]) -> dict[str, str]:
    out = rights.access_map()
    for key, _title in fields:
        out[f"{access_module.FIELD_PREFIX}{key}"] = rights.field_level(key)
    return out


def _access_payload(session, workspace_id: UUID, kind: str, item) -> dict[str, Any]:
    from app.finance.accounts_model import FinanceMembership
    from app.finance.contracts.models import Department

    fields = access_module.field_keys(session, workspace_id)
    if kind == "department":
        grants = access_module.grants_of(session, workspace_id, "department", item.id)
        rights = access_module.compute(
            "employee", [("department", key, value["level"], value.get("scope")) for key, value in grants.items()]
        )
        return {
            "subject": {"kind": "department", "id": str(item.id), "code": item.code, "title": item.title or item.code},
            "grants": grants,
            "effective": _effective(rights, fields),
            "contracts_scope": rights.contracts_scope(),
        }
    role = None
    if item.user_id is not None:
        membership = session.scalar(
            sa.select(FinanceMembership).where(
                FinanceMembership.workspace_id == workspace_id, FinanceMembership.user_id == item.user_id
            )
        )
        role = membership.role if membership is not None else None
    department = session.get(Department, item.department_id) if item.department_id else None
    personal = access_module.grants_of(session, workspace_id, "employee", item.id)
    department_grants = (
        access_module.grants_of(session, workspace_id, "department", department.id) if department else {}
    )
    rows = [("department", key, value["level"], value.get("scope")) for key, value in department_grants.items()]
    rows += [("employee", key, value["level"], value.get("scope")) for key, value in personal.items()]
    rights = access_module.compute(
        role if role in access_module.ADMIN_ROLES else "employee",
        rows,
        employee_id=item.id,
        department_id=item.department_id,
    )
    return {
        "subject": {
            "kind": "employee",
            "id": str(item.id),
            "title": item.full_name,
            "role": role,
            # Владелец и администратор видят всё: экран пишет одну строку.
            "admin": role in access_module.ADMIN_ROLES,
            "department": (
                {"id": str(department.id), "code": department.code, "title": department.title or department.code}
                if department
                else None
            ),
        },
        "grants": personal,
        "department_grants": department_grants,
        "effective": _effective(rights, fields),
        "contracts_scope": rights.contracts_scope(),
    }


@router.get("/access/{kind}/{subject_id}")
def get_access(kind: str, subject_id: UUID, member: Member = Depends(require_access("people"))) -> dict[str, Any]:
    """Права отдела (`grants`) или человека (`grants` — личные, `department_grants`, `effective` — итог)."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        item = _subject(session, workspace.id, kind, subject_id)
        return _access_payload(session, workspace.id, kind, item)


@router.put("/access/{kind}/{subject_id}")
def put_access(
    kind: str, subject_id: UUID, body: GrantsIn, member: Member = Depends(require_access("people", "edit"))
) -> dict[str, Any]:
    """Записать права. Меняется только присланное; ответ — как у GET."""
    _guard()
    changes = body.changes if body.changes is not None else (body.grants or {})
    with finance_session() as session:
        workspace = _workspace(session, member)
        item = _subject(session, workspace.id, kind, subject_id)
        if kind == "employee":
            from app.finance.accounts_model import FinanceMembership

            role = None
            if item.user_id is not None:
                role = session.scalar(
                    sa.select(FinanceMembership.role).where(
                        FinanceMembership.workspace_id == workspace.id, FinanceMembership.user_id == item.user_id
                    )
                )
            if role in access_module.ADMIN_ROLES:
                raise HTTPException(status_code=400, detail="Администратор видит и правит всё — права ему не записываются")
            if not member.rights.is_admin and item.user_id == member.user_id:
                raise HTTPException(status_code=403, detail="Свои права меняет администратор")
        elif not member.rights.is_admin and item.id == member.rights.department_id:
            raise HTTPException(status_code=403, detail="Права своего отдела меняет администратор")
        try:
            done = access_module.put_grants(session, workspace.id, kind, item.id, changes, by=member.user_id)
        except access_module.GrantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if done:
            label = (
                f"отдела {item.code}" if kind == "department" else people.short_name(item.full_name) or item.full_name
            )
            parts = [access_module.describe_change(resource, before, after, kind) for resource, before, after in done]
            shown = "; ".join(parts[:3]) + (f" и ещё {len(parts) - 3}" if len(parts) > 3 else "")
            history.write(
                session, workspace, kind="access.change", entity=kind, entity_id=item.id,
                title=f"права {label}: {shown}" if kind == "department" else f"права · {label}: {shown}",
                before={resource: before for resource, before, _after in done},
                after={resource: after for resource, _before, after in done},
            )
            if any(resource.startswith("contracts") for resource, _b, _a in done):
                from app.finance.contracts.fields import bump

                # Открытые реестры перечитают схему: поля и строки по правам.
                bump(session, workspace.id, "schema")
        return _access_payload(session, workspace.id, kind, item)


# ── Журнал действий ─────────────────────────────────────────────────────────


class ViewIn(BaseModel):
    section: str
    contract_id: UUID | None = None


def _date(value: str | None, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field}: ждём дату вида 2026-09-24") from exc


@router.get("/audit")
def read_audit(
    member: Member = Depends(current_member),
    q: str | None = None,
    user_id: UUID | None = None,
    employee_id: UUID | None = None,
    department_id: UUID | None = None,
    category: str | None = None,
    kind: str | None = None,
    entity_id: UUID | None = None,
    since: str | None = None,
    until: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """Журнал действий: `{items, next_cursor}`; курсор — на следующую страницу.

    С правом «Журнал действий» — вся компания и любые фильтры. Без него —
    только свои действия («Моё · Действия»): фильтр по человеку ставится сам.
    """
    _guard()
    if member.workspace_id is None:
        raise HTTPException(status_code=409, detail="Выберите компанию")
    if not member.rights.can("audit"):
        user_id, employee_id, department_id = member.user_id, None, None
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            page = audit.listing(
                session,
                workspace.id,
                member.rights,
                person=user_id,
                employee=employee_id,
                department=department_id,
                categories=category,
                kinds=kind,
                entity_id=entity_id,
                q=q,
                date_from=_date(since, "since"),
                date_to=_date(until, "until"),
                cursor=cursor,
                limit=limit,
            )
        except audit.AuditError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Оба имени курсора: `next_cursor` — основное, `next` — короткое.
        return {"items": page["items"], "next_cursor": page["next_cursor"], "next": page["next_cursor"]}


@router.post("/audit/view")
def audit_view(body: ViewIn, member: Member = Depends(current_member)) -> dict[str, bool]:
    """Сигнал «открыт раздел» / «открыт договор». Не чаще раза в минуту на раздел в сеансе."""
    _guard()
    if member.workspace_id is None:
        raise HTTPException(status_code=409, detail="Выберите компанию")
    try:
        section = audit.section_for_view(body.section)
    except audit.AuditError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    needed = "journal" if section == "overview" else section
    if not member.rights.can(needed):
        raise HTTPException(status_code=403, detail=f"Раздел «{audit.SECTION_TITLES[section]}» вам не открыт")
    with finance_session() as session:
        workspace = _workspace(session, member)
        number = ""
        if body.contract_id is not None:
            from app.finance.contracts import service as contracts_service

            access = contracts_service.access_of(member)
            try:
                contract = contracts_service.get_contract(session, workspace, body.contract_id)
                registry = contracts_service.Registry(session, workspace)
                people_now = contracts_service.people_of(session, [contract.id]).get(contract.id, [])
                if not contracts_service.visible_to(contract, registry, access, people_now):
                    raise contracts_service.NotFound("Договор не найден")
            except contracts_service.NotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            number = contract.number or ""
        recorded = audit.record_view(
            session, workspace, session_id=member.session_id, section=section,
            contract_id=body.contract_id, contract_number=number,
        )
        return {"recorded": recorded}


@router.post("/audit/{entry_id}/undo")
def audit_undo(entry_id: UUID, member: Member = Depends(require_access(UNDO_RESOURCES, "edit"))) -> dict[str, Any]:
    """«Откатить» из журнала действий — то же, что `POST /finance/history/{id}/undo`."""
    _guard()
    return undo_entry(member, entry_id)


# ── Уведомления ─────────────────────────────────────────────────────────────


@router.get("/notifications")
def list_notifications(
    include_resolved: bool = Query(False), member: Member = Depends(require_access("people"))
) -> dict[str, Any]:
    """«Ждут решения» и «Недавно»: `{items, pending}`; опрос раз в 30 с."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return notifications.listing(
            session, workspace.id, viewer_id=member.user_id, include_resolved=include_resolved
        )


@router.post("/notifications/{notification_id}/resolve")
def resolve_notification(
    notification_id: UUID, member: Member = Depends(require_access("people", "edit"))
) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            item = notifications.resolve(session, workspace.id, notification_id, by=member.user_id)
        except notifications.NotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        history.write(
            session, workspace, kind="notification.resolve", entity="notification", entity_id=item.id,
            title=f"запрос закрыт: {item.kind}", after={"kind": item.kind},
        )
        return {"ok": True, "id": str(item.id), "pending": notifications.pending_count(session, workspace.id)}


__all__ = ["router"]
