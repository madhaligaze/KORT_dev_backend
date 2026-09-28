"""Люди компании: отделы, сотрудники, учётки, сброс и блокировка.

Сотрудник и учётка — разные вещи
────────────────────────────────
Сотрудник (`employees`) — человек компании: ФИО, отдел, должность. Он бывает
ответственным в договорах, даже если в систему не входит. Учётка
(`users` + `memberships`) — только у тех, кому дан вход. Поэтому кабинет
работает с сотрудником, а доступ — его необязательная часть: «+ Сотрудник»
без галочки заводит человека в справочник, с галочкой — ещё и учётку,
которая ждёт пароль 72 часа.

У каждого, кто состоит в компании, запись сотрудника есть всегда
(`ensure_employee`): права сотрудника пишутся на эту запись, а владелец и
администраторы стоят в списке «Сотрудники» рядом со всеми.

Кто кого меняет
───────────────
* владелец — всех, кроме себя как владельца; пароль владельца сбрасывается
  только командой на сервере (`python -m app.finance.cli reset-password`);
* администратор и сотрудник с правом «Сотрудники и права: правит» —
  сотрудников; администраторов — нет;
* администратора назначает и снимает только владелец.

Иначе администратор сбросил бы пароль владельцу, сам задал бы новый в окне
ожидания и стал бы владельцем.

**Учётку, состоящую и в другой компании, отсюда не сбросить и не
перенести на другой номер:** администратор одной компании не должен получать
вход в чужую через сброс пароля.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.layout import norm
from app.finance import auth, history, notifications
from app.finance.accounts_model import FinanceMembership, FinanceSession, FinanceUser
from app.finance.contracts.models import Department, Employee
from app.finance.models import POSITION_STEP, Workspace


#: Пояс компании — для «ждёт пароль до 26.09, 18:40» в журнале. Тот же, что
#: у реестра договоров (`contracts/service.COMPANY_TZ`).
_COMPANY_TZ = timezone(timedelta(hours=5))


class PeopleError(Exception):
    """Отказ с текстом для человека — 400."""


class Forbidden(PeopleError):
    """Действие не открыто — 403."""


class NotFound(PeopleError):
    """Нет такого — или он чужой. Ответ одинаковый — 404."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def short_name(full_name: str) -> str:
    """«Ермеков Нурболат» → «Ермеков Н.» — как в строках журнала и договорах."""
    parts = (full_name or "").split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1][0]}."
    return parts[0] if parts else ""


# ── Запись сотрудника у каждой учётки ────────────────────────────────────────


def _next_position(session: Session, workspace_id: uuid.UUID) -> int:
    top = session.scalar(
        sa.select(sa.func.max(Employee.position)).where(Employee.workspace_id == workspace_id)
    )
    return int(top or 0) + POSITION_STEP


def _free_name(
    session: Session,
    workspace_id: uuid.UUID,
    name: str,
    login: str,
    *,
    exclude: uuid.UUID | None = None,
) -> tuple[str, str]:
    """Имя без столкновения с другим сотрудником той же компании.

    Имя уникально — по нему договоры находят ответственного. Совпало при
    заведении учётки — к имени дописывается логин, а не угадывается, что это
    тот же человек: «не угадывать» — правило раздела.
    """
    candidates = [name, f"{name} · {login}" if login else "", f"{name} · {uuid.uuid4().hex[:4]}"]
    for candidate in candidates:
        if not candidate:
            continue
        key = norm(candidate)
        query = sa.select(Employee.id).where(
            Employee.workspace_id == workspace_id, Employee.normalized_name == key
        )
        if exclude is not None:
            query = query.where(Employee.id != exclude)
        if session.scalar(query) is None:
            return candidate, key
    raise PeopleError("Не удалось подобрать имя сотрудника")  # pragma: no cover


def ensure_employee(session: Session, workspace_id: uuid.UUID, user: FinanceUser) -> Employee:
    """Запись сотрудника для учётки — найти или завести."""
    existing = session.scalar(
        sa.select(Employee).where(Employee.workspace_id == workspace_id, Employee.user_id == user.id)
    )
    if existing is not None:
        return existing
    login = user.email or user.phone or ""
    name, key = _free_name(session, workspace_id, (user.full_name or "").strip() or login, login)
    employee = Employee(
        workspace_id=workspace_id,
        full_name=name,
        normalized_name=key,
        user_id=user.id,
        position=_next_position(session, workspace_id),
    )
    session.add(employee)
    session.flush()
    return employee


def ensure_member_employees(session: Session, workspace_id: uuid.UUID) -> None:
    """Досоздать записи сотрудников тем, кто в компании, но без записи.

    Один запрос, и почти всегда пустой: записи заводят регистрация,
    приглашение и ревизия 0019. Остаётся на случай членства, заведённого в
    обход (команда сервера, старые данные).
    """
    has_record = (
        sa.select(Employee.id)
        .where(Employee.workspace_id == workspace_id, Employee.user_id == FinanceUser.id)
        .exists()
    )
    missing = list(
        session.scalars(
            sa.select(FinanceUser)
            .join(FinanceMembership, FinanceMembership.user_id == FinanceUser.id)
            .where(FinanceMembership.workspace_id == workspace_id, ~has_record)
        )
    )
    for user in missing:
        ensure_employee(session, workspace_id, user)


def rename_employee(session: Session, employee: Employee, full_name: str) -> None:
    clean = (full_name or "").strip()
    if len(clean) < 2:
        raise PeopleError("Укажите ФИО")
    key = norm(clean)
    clash = session.scalar(
        sa.select(Employee.id).where(
            Employee.workspace_id == employee.workspace_id,
            Employee.normalized_name == key,
            Employee.id != employee.id,
        )
    )
    if clash is not None:
        raise PeopleError("Сотрудник с таким именем уже есть — уточните ФИО")
    employee.full_name = clean
    employee.normalized_name = key
    session.flush()


# ── Кто кого меняет ──────────────────────────────────────────────────────────


def _membership(session: Session, workspace_id: uuid.UUID, user_id: uuid.UUID | None) -> FinanceMembership | None:
    if user_id is None:
        return None
    return session.scalar(
        sa.select(FinanceMembership).where(
            FinanceMembership.workspace_id == workspace_id, FinanceMembership.user_id == user_id
        )
    )


def _check_people(member: auth.Member, level: str = "edit") -> None:
    if not member.rights.can("people", level):
        raise Forbidden(
            "Людей меняет владелец, администратор или тот, кому открыты «Сотрудники и права»"
            if level == "edit"
            else "Сотрудники вам не открыты"
        )


def _check_manage(member: auth.Member, target: FinanceMembership | None) -> None:
    """Можно ли этому человеку менять учётку `target`."""
    _check_people(member)
    if target is None:
        return
    if target.role == "owner" and target.user_id != member.user_id:
        raise Forbidden("Учётку владельца меняет только он сам")
    if target.role == "admin" and member.role != "owner" and target.user_id != member.user_id:
        raise Forbidden("Администратора меняет только владелец")


def _other_companies(session: Session, user_id: uuid.UUID, workspace_id: uuid.UUID) -> int:
    return int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(FinanceMembership)
            .where(FinanceMembership.user_id == user_id, FinanceMembership.workspace_id != workspace_id)
        )
        or 0
    )


def _event(
    session: Session,
    workspace: Workspace,
    kind: str,
    title: str,
    *,
    employee: Employee | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    entity: str = "employee",
    entity_id: uuid.UUID | None = None,
) -> None:
    history.write(
        session,
        workspace,
        kind=kind,
        entity=entity,
        entity_id=entity_id or (employee.id if employee else None),
        title=title,
        before=before,
        after=after,
    )


# ── Отделы ───────────────────────────────────────────────────────────────────


def _schema_changed(session: Session, workspace_id: uuid.UUID) -> None:
    """Отделы и права видны в схеме реестра — открытые вкладки перечитают её."""
    from app.finance.contracts.fields import bump

    bump(session, workspace_id, "schema")


def department_out(item: Department, count: int = 0) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "code": item.code,
        "title": item.title or item.code,
        "position": item.position,
        "archived": item.archived_at is not None,
        "employees": count,
    }


def list_departments(session: Session, workspace_id: uuid.UUID, *, include_archived: bool = False) -> list[dict[str, Any]]:
    counts = dict(
        session.execute(
            sa.select(Employee.department_id, sa.func.count())
            .where(Employee.workspace_id == workspace_id, Employee.archived_at.is_(None))
            .group_by(Employee.department_id)
        ).all()
    )
    query = sa.select(Department).where(Department.workspace_id == workspace_id)
    if not include_archived:
        query = query.where(Department.archived_at.is_(None))
    return [
        department_out(item, int(counts.get(item.id, 0)))
        for item in session.scalars(query.order_by(Department.position, Department.code))
    ]


def _clean_code(code: Any) -> tuple[str, str]:
    clean = str(code or "").strip()
    if not clean:
        raise PeopleError("У отдела должен быть код: ЮО, ОБО, ФО")
    if len(clean) > 12:
        raise PeopleError("Код отдела — коротко, до 12 знаков")
    return clean, norm(clean)


def get_department(session: Session, workspace_id: uuid.UUID, department_id: uuid.UUID) -> Department:
    item = session.get(Department, department_id)
    if item is None or item.workspace_id != workspace_id:
        raise NotFound("Такого отдела нет")
    return item


def create_department(session: Session, workspace: Workspace, member: auth.Member, data: dict[str, Any]) -> Department:
    _check_people(member)
    code, key = _clean_code(data.get("code"))
    if session.scalar(
        sa.select(Department.id).where(Department.workspace_id == workspace.id, Department.normalized_name == key)
    ):
        raise PeopleError("Отдел с таким кодом уже есть")
    top = session.scalar(
        sa.select(sa.func.max(Department.position)).where(Department.workspace_id == workspace.id)
    )
    item = Department(
        workspace_id=workspace.id,
        code=code,
        normalized_name=key,
        title=str(data.get("title") or "").strip() or code,
        position=int(top or 0) + POSITION_STEP,
    )
    session.add(item)
    session.flush()
    _schema_changed(session, workspace.id)
    _event(session, workspace, "people.department_create", f"новый отдел: {item.code} · {item.title}",
           entity="department", entity_id=item.id, after={"code": item.code, "title": item.title})
    return item


def update_department(
    session: Session, workspace: Workspace, member: auth.Member, department_id: uuid.UUID, data: dict[str, Any]
) -> Department:
    _check_people(member)
    item = get_department(session, workspace.id, department_id)
    before = {"code": item.code, "title": item.title, "archived": item.archived_at is not None}
    if "code" in data and data["code"] is not None:
        code, key = _clean_code(data["code"])
        if session.scalar(
            sa.select(Department.id).where(
                Department.workspace_id == workspace.id,
                Department.normalized_name == key,
                Department.id != item.id,
            )
        ):
            raise PeopleError("Отдел с таким кодом уже есть")
        item.code, item.normalized_name = code, key
    if "title" in data and data["title"] is not None:
        item.title = str(data["title"]).strip() or item.code
    if "archived" in data and data["archived"] is not None:
        if data["archived"] and item.archived_at is None:
            # Люди архивного отдела не попадали бы ни в одну вкладку: ни в
            # отдел (его нет в списке), ни в «Без отдела» (отдел у них есть).
            staff = int(
                session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Employee)
                    .where(Employee.department_id == item.id, Employee.archived_at.is_(None))
                )
                or 0
            )
            if staff:
                raise PeopleError(f"В отделе сотрудников: {staff} — сначала переведите их в другой отдел")
        item.archived_at = _now() if data["archived"] else None
    session.flush()
    after = {"code": item.code, "title": item.title, "archived": item.archived_at is not None}
    if before != after:
        _schema_changed(session, workspace.id)
        _event(session, workspace, "people.department_update", f"отдел {before['code']}: изменён",
               entity="department", entity_id=item.id, before=before, after=after)
    return item


# ── Сотрудники ───────────────────────────────────────────────────────────────


def account_status(user: FinanceUser | None, membership: FinanceMembership | None) -> str:
    """Статус учётки словом: `no_access`, `blocked`, `pending`, `pending_expired`, `active`."""
    if user is None or membership is None:
        return "no_access"
    if membership.blocked_at is not None or user.status == "blocked":
        return "blocked"
    if user.status == "pending":
        until = _aware(user.pending_until)
        return "pending" if until is not None and until > _now() else "pending_expired"
    return "active"


def _employee_out(
    employee: Employee,
    user: FinanceUser | None,
    membership: FinanceMembership | None,
    *,
    last_seen: datetime | None = None,
    requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    status = account_status(user, membership)
    return {
        "id": str(employee.id),
        "full_name": employee.full_name,
        "short_name": short_name(employee.full_name),
        "job_title": employee.job_title or "",
        "department_id": str(employee.department_id) if employee.department_id else None,
        "position": employee.position,
        "archived": employee.archived_at is not None,
        "phone": (user.phone or "") if user is not None else "",
        "status": status,
        "account": (
            {
                "user_id": str(user.id),
                "email": user.email or "",
                "phone": user.phone or "",
                "role": membership.role,
                "status": status,
                "pending_until": _iso(user.pending_until) if user.status == "pending" else None,
                "last_seen_at": _iso(last_seen),
                "last_login_at": _iso(user.last_login_at),
                "blocked_at": _iso(membership.blocked_at),
            }
            if user is not None and membership is not None
            else None
        ),
        "requests": requests or [],
    }


def list_employees(
    session: Session, workspace_id: uuid.UUID, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    """Все сотрудники компании — пятью запросами на любое их число."""
    ensure_member_employees(session, workspace_id)
    query = sa.select(Employee).where(Employee.workspace_id == workspace_id)
    if not include_archived:
        query = query.where(Employee.archived_at.is_(None))
    employees = list(session.scalars(query.order_by(Employee.position, Employee.full_name)))
    user_ids = [item.user_id for item in employees if item.user_id]
    users: dict[uuid.UUID, FinanceUser] = {}
    memberships: dict[uuid.UUID, FinanceMembership] = {}
    seen: dict[uuid.UUID, datetime] = {}
    if user_ids:
        users = {
            item.id: item
            for item in session.scalars(sa.select(FinanceUser).where(FinanceUser.id.in_(user_ids)))
        }
        memberships = {
            item.user_id: item
            for item in session.scalars(
                sa.select(FinanceMembership).where(
                    FinanceMembership.workspace_id == workspace_id,
                    FinanceMembership.user_id.in_(user_ids),
                )
            )
        }
        seen = dict(
            session.execute(
                sa.select(FinanceSession.user_id, sa.func.max(FinanceSession.last_seen_at))
                .where(FinanceSession.user_id.in_(user_ids))
                .group_by(FinanceSession.user_id)
            ).all()
        )
    requests = notifications.open_about(session, workspace_id, user_ids)
    return [
        _employee_out(
            item,
            users.get(item.user_id) if item.user_id else None,
            memberships.get(item.user_id) if item.user_id else None,
            last_seen=seen.get(item.user_id) if item.user_id else None,
            requests=requests.get(item.user_id, []) if item.user_id else [],
        )
        for item in employees
    ]


def get_employee(session: Session, workspace_id: uuid.UUID, employee_id: uuid.UUID) -> Employee:
    item = session.get(Employee, employee_id)
    if item is None or item.workspace_id != workspace_id:
        raise NotFound("Сотрудник не найден")
    return item


def employee_payload(session: Session, workspace_id: uuid.UUID, employee: Employee) -> dict[str, Any]:
    user = session.get(FinanceUser, employee.user_id) if employee.user_id else None
    membership = _membership(session, workspace_id, employee.user_id)
    seen = None
    if user is not None:
        seen = session.scalar(
            sa.select(sa.func.max(FinanceSession.last_seen_at)).where(FinanceSession.user_id == user.id)
        )
    requests = notifications.open_about(session, workspace_id, [user.id]) if user is not None else {}
    return _employee_out(
        employee, user, membership, last_seen=seen,
        requests=requests.get(user.id, []) if user is not None else [],
    )


def _clean_department(session: Session, workspace_id: uuid.UUID, raw: Any) -> uuid.UUID | None:
    if raw in (None, ""):
        return None
    try:
        department_id = uuid.UUID(str(raw))
    except ValueError as exc:
        raise PeopleError("Отдел указан неверно") from exc
    department = get_department(session, workspace_id, department_id)
    if department.archived_at is not None:
        raise PeopleError("Этот отдел в корзине — сначала восстановите его")
    return department.id


def create_employee(session: Session, workspace: Workspace, member: auth.Member, data: dict[str, Any]) -> Employee:
    """«+ Сотрудник»: человек в справочник и, с номером, учётка по номеру.

    Номер — это логин и ничего больше: у записи сотрудника своего телефона нет.
    Поэтому номер без явного `access` открывает вход, а номер при `access:
    false` — отказ. Раньше такой номер молча выбрасывался: человека заводили с
    телефоном, а войти он не мог — «Асхат» 26.09.

    Тёзка из архива возвращается, а не упирается в «такое имя уже есть»:
    имя в компании уникально, и без этого убранного по ошибке человека нельзя
    было ни вернуть, ни завести заново.
    """
    _check_people(member)
    full_name = str(data.get("full_name") or "").strip()
    if len(full_name) < 2:
        raise PeopleError("Укажите ФИО")
    phone = str(data.get("phone") or "").strip()
    access = data.get("access")
    if access is None:
        access = bool(phone)
    if phone and not access:
        raise PeopleError("Номер сохраняется только вместе с доступом в систему")
    key = norm(full_name)
    existing = session.scalar(
        sa.select(Employee).where(Employee.workspace_id == workspace.id, Employee.normalized_name == key)
    )
    if existing is not None and existing.archived_at is None:
        raise PeopleError("Сотрудник с таким именем уже есть — уточните ФИО")
    if existing is not None:
        employee = existing
        employee.archived_at = None
        employee.full_name = full_name
        if data.get("job_title") is not None:
            employee.job_title = str(data.get("job_title") or "").strip()
        employee.department_id = _clean_department(session, workspace.id, data.get("department_id"))
        employee.position = _next_position(session, workspace.id)
        session.flush()
        _schema_changed(session, workspace.id)
        _event(session, workspace, "people.restore", f"сотрудник возвращён из архива: {short_name(employee.full_name)}",
               employee=employee, after={"full_name": full_name, "job_title": employee.job_title})
    else:
        employee = Employee(
            workspace_id=workspace.id,
            full_name=full_name,
            normalized_name=key,
            job_title=str(data.get("job_title") or "").strip(),
            department_id=_clean_department(session, workspace.id, data.get("department_id")),
            position=_next_position(session, workspace.id),
        )
        session.add(employee)
        session.flush()
        _event(session, workspace, "people.create", f"новый сотрудник: {short_name(employee.full_name)}",
               employee=employee, after={"full_name": full_name, "job_title": employee.job_title})
    if access:
        create_account(session, workspace, member, employee, phone=phone or None, role=data.get("role") or "employee")
    return employee


def _free_phone(session: Session, workspace_id: uuid.UUID, clean: str, user: FinanceUser | None) -> None:
    """Номер свободен для `user` — или отказ, который говорит правду.

    Учётка, у которой не осталось ни одного членства, — след закрытого доступа
    или архива. Войти по ней нельзя, а номер она держала навсегда, и новый
    человек с этим номером получал «занят в другой компании», хотя другой
    компании не было. Такой номер отпускается.
    """
    holder = session.scalar(sa.select(FinanceUser).where(FinanceUser.phone == clean))
    if holder is None or (user is not None and holder.id == user.id):
        return
    workspaces = set(
        session.scalars(sa.select(FinanceMembership.workspace_id).where(FinanceMembership.user_id == holder.id))
    )
    if workspace_id in workspaces:
        raise PeopleError("Этот номер уже у другого сотрудника компании")
    if workspaces:
        raise PeopleError("Этот номер занят в другой компании")
    holder.phone = None
    if not holder.email_normalized:
        # Логина не осталось — учётка закрыта (ревизия 0021). Вернуть ей вход
        # можно «Открыть вход» с новым номером: `create_account` снова
        # переведёт её в ожидание пароля.
        holder.status = "blocked"
        holder.password_hash = None
        holder.pending_until = None
    auth.end_sessions(session, holder.id)
    session.flush()


def create_account(
    session: Session,
    workspace: Workspace,
    member: auth.Member,
    employee: Employee,
    *,
    phone: Any,
    role: str = "employee",
) -> FinanceUser:
    """Открыть вход: учётка по номеру ждёт пароль 72 часа."""
    _check_people(member)
    if role not in ("employee", "admin"):
        raise PeopleError("Роль — сотрудник или администратор")
    if role == "admin" and member.role != "owner":
        raise Forbidden("Администратора назначает владелец компании")
    if employee.archived_at is not None:
        raise PeopleError("Сотрудник в корзине — сначала восстановите его в личном кабинете")
    existing = _membership(session, workspace.id, employee.user_id)
    if existing is not None:
        raise PeopleError("У сотрудника уже есть доступ")
    try:
        clean = auth.normalize_phone(str(phone or "")) if str(phone or "").strip() else None
    except auth.AuthError as exc:
        raise PeopleError(str(exc)) from exc

    user = session.get(FinanceUser, employee.user_id) if employee.user_id else None
    if clean is None and (user is None or not user.phone):
        raise PeopleError("Для входа нужен телефон")
    if clean is not None:
        _free_phone(session, workspace.id, clean, user)
    now = _now()
    if user is None:
        user = FinanceUser(
            email=None,
            email_normalized=None,
            phone=clean,
            password_hash=None,
            full_name=employee.full_name,
            status="pending",
            pending_until=now + auth.PENDING_WINDOW,
        )
        session.add(user)
        session.flush()
        employee.user_id = user.id
    else:
        # Доступ открывают снова тому, у кого он был: учётка та же. Пароль —
        # новый, в окне ожидания, как при первом открытии: раньше прежний
        # пароль молча начинал действовать снова, а карточка писала «ждёт
        # пароль до…». Учётку, которая состоит и в другой компании, не трогаем:
        # там её пароль живой.
        others = _other_companies(session, user.id, workspace.id)
        if clean is not None and clean != user.phone:
            if others:
                raise Forbidden("Учётка состоит и в другой компании — номер меняет сам человек")
            user.phone = clean
        if not others or user.status == "pending" or not user.password_hash:
            user.password_hash = None
            user.status = "pending"
            user.pending_until = now + auth.PENDING_WINDOW
            user.must_change_password = False
            auth.end_sessions(session, user.id)
    session.add(
        FinanceMembership(workspace_id=workspace.id, user_id=user.id, role=role, invited_by=member.user_id)
    )
    session.flush()
    until = _aware(user.pending_until)
    _event(
        session, workspace, "people.account",
        f"доступ открыт: {short_name(employee.full_name)}"
        + (f" · ждёт пароль до {until.astimezone(_COMPANY_TZ):%d.%m, %H:%M}" if user.status == "pending" and until else ""),
        employee=employee, after={"phone": user.phone, "role": role},
    )
    return user


def update_employee(
    session: Session, workspace: Workspace, member: auth.Member, employee_id: uuid.UUID, data: dict[str, Any]
) -> Employee:
    employee = get_employee(session, workspace.id, employee_id)
    target = _membership(session, workspace.id, employee.user_id)
    _check_manage(member, target)
    user = session.get(FinanceUser, employee.user_id) if employee.user_id else None
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    if data.get("full_name") is not None and data["full_name"].strip() != employee.full_name:
        before["full_name"] = employee.full_name
        rename_employee(session, employee, data["full_name"])
        after["full_name"] = employee.full_name
        if user is not None and _other_companies(session, user.id, workspace.id) == 0:
            user.full_name = employee.full_name
    if "job_title" in data and data["job_title"] is not None:
        value = str(data["job_title"]).strip()
        if value != (employee.job_title or ""):
            before["job_title"], after["job_title"] = employee.job_title, value
            employee.job_title = value
    if "department_id" in data:
        value = _clean_department(session, workspace.id, data.get("department_id"))
        if value != employee.department_id:
            before["department_id"] = str(employee.department_id) if employee.department_id else None
            after["department_id"] = str(value) if value else None
            employee.department_id = value
    if data.get("phone") is not None and user is not None:
        try:
            clean = auth.normalize_phone(data["phone"])
        except auth.AuthError as exc:
            raise PeopleError(str(exc)) from exc
        if clean != user.phone:
            if _other_companies(session, user.id, workspace.id):
                raise Forbidden("Учётка состоит и в другой компании — номер меняет сам человек")
            _free_phone(session, workspace.id, clean, user)
            before["phone"], after["phone"] = user.phone, clean
            user.phone = clean
    if data.get("role") is not None and target is not None and data["role"] != target.role:
        role = data["role"]
        if role not in ("employee", "admin"):
            raise PeopleError("Роль — сотрудник или администратор")
        if member.role != "owner":
            raise Forbidden("Администратора назначает и снимает только владелец")
        if target.role == "owner" or target.user_id == member.user_id:
            raise Forbidden("Свою роль и роль владельца так не поменять")
        before["role"], after["role"] = target.role, role
        target.role = role
    session.flush()

    if data.get("access") is True and target is None:
        create_account(session, workspace, member, employee, phone=data.get("phone"), role=data.get("role") or "employee")
        after["access"] = True
    elif data.get("access") is False and target is not None:
        _remove_access(session, workspace, member, employee, target)
        after["access"] = False

    if before or (after and "access" not in after):
        _schema_changed(session, workspace.id)
        _event(session, workspace, "people.update", f"сотрудник изменён: {short_name(employee.full_name)}",
               employee=employee, before=before, after=after)
    return employee


def _remove_access(
    session: Session, workspace: Workspace, member: auth.Member, employee: Employee, target: FinanceMembership
) -> None:
    if target.role == "owner":
        raise Forbidden("Владельца компании убрать нельзя")
    if target.user_id == member.user_id:
        raise Forbidden("Свой доступ снять нельзя")
    user_id = target.user_id
    session.delete(target)
    auth.end_sessions(session, user_id, workspace_id=workspace.id)
    notifications.resolve_about(session, workspace.id, user_id, by=member.user_id)
    session.flush()
    _event(session, workspace, "people.access_removed", f"доступ снят: {short_name(employee.full_name)}",
           employee=employee)


def archive_employee(session: Session, workspace: Workspace, member: auth.Member, employee_id: uuid.UUID) -> Employee:
    """Удалить сотрудника — в корзину, с доступом. Из базы не стирается: он в договорах."""
    employee = get_employee(session, workspace.id, employee_id)
    target = _membership(session, workspace.id, employee.user_id)
    _check_manage(member, target)
    if target is not None:
        _remove_access(session, workspace, member, employee, target)
    employee.archived_at = _now()
    session.flush()
    _schema_changed(session, workspace.id)
    _event(session, workspace, "people.archive", f"сотрудник удалён в корзину: {short_name(employee.full_name)}",
           employee=employee)
    return employee


def _account_of(session: Session, workspace_id: uuid.UUID, employee: Employee) -> tuple[FinanceUser, FinanceMembership]:
    membership = _membership(session, workspace_id, employee.user_id)
    user = session.get(FinanceUser, employee.user_id) if employee.user_id else None
    if membership is None or user is None:
        raise PeopleError("У сотрудника нет доступа в систему")
    return user, membership


def reset_password(session: Session, workspace: Workspace, member: auth.Member, employee_id: uuid.UUID) -> Employee:
    """Сброс: старый пароль не действует, сеансы закрыты, учётка снова ждёт пароль."""
    employee = get_employee(session, workspace.id, employee_id)
    user, target = _account_of(session, workspace.id, employee)
    if target.role == "owner":
        raise Forbidden("Пароль владельца сбрасывается только командой на сервере")
    if user.id == member.user_id:
        raise PeopleError("Свой пароль меняется в профиле")
    _check_manage(member, target)
    if _other_companies(session, user.id, workspace.id):
        raise Forbidden("Учётка состоит и в другой компании — сбросить её пароль отсюда нельзя")
    if not user.phone:
        raise PeopleError("Новый пароль задаётся по номеру — сначала впишите сотруднику телефон")
    user.password_hash = None
    user.status = "pending"
    user.pending_until = _now() + auth.PENDING_WINDOW
    user.must_change_password = False
    closed = auth.end_sessions(session, user.id)
    resolved = notifications.resolve_about(session, workspace.id, user.id, by=member.user_id)
    auth._forget_failures(f"phone:{user.phone}")
    session.flush()
    until = _aware(user.pending_until)
    _event(
        session, workspace, "people.reset",
        f"сброс пароля: {short_name(employee.full_name)} · ждёт новый до {until.astimezone(_COMPANY_TZ):%d.%m, %H:%M}",
        employee=employee, after={"sessions_closed": closed, "requests_resolved": resolved,
                                  "pending_until": _iso(user.pending_until)},
    )
    return employee


def set_blocked(
    session: Session, workspace: Workspace, member: auth.Member, employee_id: uuid.UUID, *, blocked: bool
) -> Employee:
    employee = get_employee(session, workspace.id, employee_id)
    _user, target = _account_of(session, workspace.id, employee)
    if target.user_id == member.user_id:
        raise PeopleError("Себе вход не закрыть")
    if target.role == "owner":
        raise Forbidden("Вход владельца не закрыть")
    _check_manage(member, target)
    if blocked == (target.blocked_at is not None):
        return employee
    target.blocked_at = _now() if blocked else None
    closed = auth.end_sessions(session, target.user_id, workspace_id=workspace.id) if blocked else 0
    session.flush()
    _event(
        session, workspace, "people.block" if blocked else "people.unblock",
        f"вход заблокирован: {short_name(employee.full_name)}" if blocked
        else f"вход открыт снова: {short_name(employee.full_name)}",
        employee=employee, after={"sessions_closed": closed} if blocked else None,
    )
    return employee


def end_employee_sessions(session: Session, workspace: Workspace, member: auth.Member, employee_id: uuid.UUID) -> int:
    employee = get_employee(session, workspace.id, employee_id)
    _user, target = _account_of(session, workspace.id, employee)
    _check_manage(member, target)
    keep = member.session_id if target.user_id == member.user_id else None
    closed = auth.end_sessions(session, target.user_id, workspace_id=workspace.id, keep=keep)
    session.flush()
    _event(session, workspace, "people.sessions_end",
           f"сеансы завершены: {short_name(employee.full_name)} · {closed}",
           employee=employee, after={"sessions_closed": closed})
    return closed


def employee_sessions(session: Session, workspace: Workspace, member: auth.Member, employee_id: uuid.UUID) -> list[dict[str, Any]]:
    """Сеансы сотрудника в этой компании.

    Администратор видит сеансы сотрудников, владелец — всех; чужой и
    несуществующий сотрудник отвечают одинаково (урок кабинета BBC: иначе
    перебор выдавал бы, чьи это идентификаторы).
    """
    employee = get_employee(session, workspace.id, employee_id)
    target = _membership(session, workspace.id, employee.user_id)
    if target is None:
        raise NotFound("Сотрудник не найден")
    if target.user_id != member.user_id:
        _check_people(member, "view")
        if target.role == "owner" or (target.role == "admin" and member.role != "owner"):
            raise NotFound("Сотрудник не найден")
    return auth.sessions_of(session, target.user_id, current=member.session_id, workspace_id=workspace.id)


__all__ = [
    "Forbidden",
    "NotFound",
    "PeopleError",
    "account_status",
    "archive_employee",
    "create_account",
    "create_department",
    "create_employee",
    "department_out",
    "employee_payload",
    "employee_sessions",
    "end_employee_sessions",
    "ensure_employee",
    "ensure_member_employees",
    "get_department",
    "get_employee",
    "list_departments",
    "list_employees",
    "rename_employee",
    "reset_password",
    "set_blocked",
    "short_name",
    "update_department",
    "update_employee",
]
