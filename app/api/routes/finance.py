"""HTTP-маршруты раздела «Финансы».

Своя авторизация, а не общая с дашбордом
────────────────────────────────────────
Раньше здесь стояли `require_block_user("finance")` и `require_admin` из
`app.bbc.deps`, то есть в раздел пускали учётки BBC Dashboard. Это было неверно
по существу: в «Финансах» каждая компания регистрируется сама и ведёт свой
учёт, к BBC отношения не имея, — а BBC однажды может сам переехать сюда, и
тогда зависимость смотрела бы от общего к частному.

Теперь у раздела свои учётки (`app.finance.auth`), и `app.bbc` этот файл не
импортирует вовсе. Компания берётся **из сессии**, а не «по умолчанию»: один
человек ведёт несколько компаний, и переключатель в шапке меняет контекст
сессии.

Обработчики — обычный `def`, а не `async def`: внутри синхронные SQLAlchemy и
openpyxl, разбор файла на двадцать тысяч строк считается секундами. В
`async def` это встало бы колом в цикле событий и подвесило заодно дашборд;
обычный `def` FastAPI уводит в пул потоков.
"""
from __future__ import annotations

import calendar as calendar_module
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote
from uuid import UUID

import sqlalchemy as sa
from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field

from app.finance import (
    access as access_module,
    auth,
    autotag,
    grid as grid_module,
    history,
    integrations as integrations_module,
    invoices as invoices_module,
    live as live_module,
    notifications,
    recurring,
    reports,
    rules,
    service,
    sheets,
)
from app.finance.auth import AuthError, Member
from app.finance.config import finance_settings
from app.finance.db import finance_session
from app.finance.importing import ImportError_, analyze
from app.finance.models import (
    Account,
    Category,
    Counterparty,
    ImportBatch,
    ImportRow,
    Operation,
    Plan,
    Project,
    Rule,
    Tag,
)
from app.finance import export as export_module
from app.finance.service import FinanceError, VersionConflict

log = logging.getLogger(__name__)

router = APIRouter(prefix="/finance", tags=["finance"])

def _guard() -> None:
    if not finance_settings.enabled:
        raise HTTPException(status_code=404, detail="KORT выключен")


#: Окружения, где `Secure` на cookie означал бы «войти нельзя»: локальный
#: запуск и тесты ходят по http. Тот же список и та же причина, что у дашборда.
_INSECURE_ENVIRONMENTS = frozenset({"development", "dev", "local", "test", "testing"})


def _cookie_secure(request: Request) -> bool:
    """Ставить ли `Secure` на cookie сессии.

    Решает окружение, а схема запроса и `X-Forwarded-Proto` могут только
    добавить флаг, но не снять: браузер приходит на Next по HTTPS, Next
    проксирует на API по HTTP, и бэкенд видит `http://api:8000` — то есть схема
    здесь описывает внутреннюю сеть, а не то, как ходит человек. Ошибиться в
    сторону `Secure` безопасно, в обратную — нет.
    """
    if request.url.scheme == "https":
        return True
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if forwarded.lower() == "https":
        return True
    from app.core.config import settings

    return (settings.environment or "").strip().lower() not in _INSECURE_ENVIRONMENTS


def _set_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=int(auth.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def client_ip(request: Request) -> str:
    """Адрес человека. Справочно: его ставит прокси Next заголовком `x-client-ip`.

    Прямой запрос к API может прислать любой заголовок, поэтому адрес — это
    подпись в журнале и ключ ограничителя перебора, но не доказательство.
    """
    value = (request.headers.get("x-client-ip") or "").split(",")[0].strip()
    if not value and request.client is not None:
        value = request.client.host or ""
    return value[:64]


def _agent(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:400]


def signed_in(request: Request) -> Member:
    """Вошедший — любой, включая временный пароль. Нет сессии — 401 с текстом.

    Сама по себе не открывает ничего: маршруты берут одну из зависимостей
    ниже, и временный пароль пускают только к смене пароля.
    """
    _guard()
    with finance_session() as session:
        member = auth.resolve(
            session,
            request.cookies.get(auth.COOKIE_NAME),
            ip=client_ip(request),
            user_agent=_agent(request),
        )
    if member is None:
        raise HTTPException(status_code=401, detail="Войдите в KORT")
    return member


TEMPORARY_PASSWORD = "Сначала смените временный пароль"


def _enter(member: Member) -> None:
    """Автор, сеанс и адрес — в контекст запроса: их возьмёт каждая запись журнала."""
    history.set_context(
        history.AuditContext(
            user_id=member.user_id,
            session_id=member.session_id,
            ip=member.ip,
            user_agent=member.user_agent,
            actor=member.login,
        )
    )


def _declare(dependency, kind: str, value: Any):
    """Пометить зависимость: тест обхода маршрутов читает эту пометку."""
    dependency.__finance_access__ = (kind, value)
    return dependency


def _denied(resources: tuple[str, ...], level: str, member: Member) -> str:
    title = access_module.resource_title(resources[0])
    if level == "edit" and member.rights.can_any(resources, "view"):
        return f"В разделе «{title}» вам можно только смотреть"
    return f"Раздел «{title}» вам не открыт"


def require_access(resource: str | tuple[str, ...], level: str = "view"):
    """Зависимость «раздел открыт на этом уровне» — объявление права маршрута.

    Каждый маршрут `/finance/*` объявляет свой раздел: чтение — `view`,
    изменение — `edit` (проверяет `tests/test_finance_access.py`, обходя все
    маршруты приложения). Несколько разделов — «любой из них»: справочники
    нужны формам и журнала, и счетов.

    Проверка асинхронная намеренно: только так автор, сеанс и адрес,
    записанные в контекст, доезжают до обработчика в пуле потоков (см.
    `history.py`).
    """
    resources = (resource,) if isinstance(resource, str) else tuple(resource)
    for key in resources:
        if key not in access_module.RESOURCE_BY_KEY:
            raise ValueError(f"неизвестный раздел прав: {key}")
    if level not in ("view", "edit"):
        raise ValueError(f"неизвестный уровень: {level}")

    async def _dependency(member: Member = Depends(signed_in)) -> Member:
        if member.must_change_password:
            raise HTTPException(status_code=403, detail=TEMPORARY_PASSWORD)
        if member.workspace_id is None:
            raise HTTPException(status_code=409, detail="Выберите компанию")
        if not member.rights.can_any(resources, level):
            raise HTTPException(status_code=403, detail=_denied(resources, level, member))
        _enter(member)
        return member

    return _declare(_dependency, "resource", (resources, level))


def require_role(*roles: str):
    """Только владелец (или владелец и администратор) — то, чего нет в правах разделов."""

    async def _dependency(member: Member = Depends(signed_in)) -> Member:
        if member.must_change_password:
            raise HTTPException(status_code=403, detail=TEMPORARY_PASSWORD)
        if member.workspace_id is None:
            raise HTTPException(status_code=409, detail="Выберите компанию")
        if member.role not in roles:
            raise HTTPException(
                status_code=403,
                detail="Это может только владелец компании" if roles == ("owner",)
                else "Это может владелец или администратор",
            )
        _enter(member)
        return member

    return _declare(_dependency, "role", roles)


async def _self(member: Member = Depends(signed_in)) -> Member:
    if member.must_change_password:
        raise HTTPException(status_code=403, detail=TEMPORARY_PASSWORD)
    _enter(member)
    return member


async def _password_change(member: Member = Depends(signed_in)) -> Member:
    _enter(member)
    return member


#: Своё: профиль, сеансы, свои действия. Права раздела не нужны, но временный
#: пароль не пускает и сюда. Список таких маршрутов закрыт в тесте обхода.
current_member = _declare(_self, "self", "own")
#: Единственная дверь, открытая временному паролю, — сменить его.
password_member = _declare(_password_change, "self", "password")


def _workspace(session, member: Member):
    """Компания текущей сессии.

    Отсутствие выбранной компании — не ошибка сервера: так бывает у человека,
    которого исключили из компании, пока он был в разделе. Отвечаем 409 и
    текстом, по которому фронт покажет выбор компании.
    """
    if member.workspace_id is None:
        raise HTTPException(status_code=409, detail="Выберите компанию")
    try:
        return service.get_workspace(session, member.workspace_id)
    except FinanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _actor(member: Any) -> str:
    """Подпись под операцией — логин (почта или телефон), а не имя: имя меняют."""
    return getattr(member, "login", "") or getattr(member, "email", "") or ""


def _fail(exc: FinanceError) -> HTTPException:
    if isinstance(exc, VersionConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _parse_date(value: str | None, *, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field}: ждём дату вида 2026-09-17") from exc


def _money(value: Any, *, field: str) -> Decimal:
    """Сумма из тела запроса. Проверка та же, что у записи в базу.

    Раньше здесь ловился только нечитаемый текст, а величина — нет: сорок
    девяток доезжали до Postgres и возвращались пятисотой. Проверка живёт в
    `service.check_money`, чтобы ответ был одинаковым, откуда бы сумма ни
    пришла — из формы, из ячейки таблицы или из файла.
    """
    try:
        return service.check_money(value, field=field)
    except FinanceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (InvalidOperation, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"{field}: это не сумма") from exc


# ── Учётки и компании ────────────────────────────────────────────────────────
#
# Раздел живёт сам по себе: регистрация открыта, компания создаётся вместе с
# первым пользователем, он её владелец. Ни одной проверки прав дашборда здесь
# нет и не должно появиться.


class RegisterIn(BaseModel):
    email: str
    password: str
    company: str
    full_name: str = ""


class LoginIn(BaseModel):
    email: str
    password: str


def _me_payload(session, member: Member) -> dict[str, Any]:
    """Кто вошёл и что ему открыто — `me` и ответ входа.

    `access` — все разделы с уровнем (`none` / `view` / `edit`), `role`,
    `contracts_scope` и `pending_requests` (число открытых просьб для рамы
    того, кто правит людей). `abilities` — прежние способности для экранов,
    ещё не переведённых на `access`.
    """
    from app.finance.contracts.models import Department, Employee

    rights = member.rights
    pending = 0
    employee_out = None
    if member.workspace_id is not None:
        if rights.can("people", "edit"):
            pending = notifications.pending_count(session, member.workspace_id)
        row = session.execute(
            sa.select(Employee, Department)
            .outerjoin(Department, Department.id == Employee.department_id)
            .where(Employee.workspace_id == member.workspace_id, Employee.user_id == member.user_id)
        ).first()
        if row is not None:
            employee, department = row
            employee_out = {
                "id": str(employee.id),
                "full_name": employee.full_name,
                "job_title": employee.job_title or "",
                "department": (
                    {"id": str(department.id), "code": department.code, "title": department.title or department.code}
                    if department is not None
                    else None
                ),
            }
    return {
        "authenticated": True,
        "user": {
            "id": str(member.user_id),
            "email": member.email,
            "phone": member.phone,
            "full_name": member.full_name,
            "must_change_password": member.must_change_password,
        },
        "company": (
            {"id": str(member.workspace_id), "title": member.workspace_title, "role": member.role}
            if member.workspace_id
            else None
        ),
        "companies": auth.companies_of(session, member.user_id),
        "abilities": sorted(rights.abilities()),
        "role": member.role or None,
        "access": rights.access_map(),
        "contracts_scope": rights.contracts_scope(),
        "pending_requests": pending,
        "employee": employee_out,
    }


def _auth_fail(exc: AuthError, status: int) -> HTTPException:
    if isinstance(exc, auth.TooManyAttempts):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, auth.NeedsPassword):
        # 409, а не 401: экран по нему переходит к «Придумайте пароль».
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=status, detail=str(exc))


@router.post("/auth/register", status_code=201)
def auth_register(body: RegisterIn, request: Request, response: Response) -> dict[str, Any]:
    """Регистрация компании: почта, пароль, название, имя — и человек внутри.

    Имя обязательно: без него владелец везде подписан почтой — в раме, в
    списке сотрудников, автором каждой записи журнала.
    """
    _guard()
    if len(body.full_name.strip()) < 2:
        raise HTTPException(status_code=400, detail="Укажите своё имя")
    with finance_session() as session:
        try:
            member, token = auth.register(
                session,
                email=body.email,
                password=body.password,
                company=body.company,
                full_name=body.full_name,
                user_agent=_agent(request),
                ip=client_ip(request),
            )
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = _me_payload(session, member)
    _set_cookie(request, response, token)
    return payload


@router.post("/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    _guard()
    # Неудача пишет событие и счётчик — это должно сохраниться и при отказе,
    # поэтому ошибка поднимается после выхода из транзакции.
    failure: AuthError | None = None
    with finance_session() as session:
        try:
            member, token = auth.login(
                session,
                email=body.email,
                password=body.password,
                user_agent=_agent(request),
                ip=client_ip(request),
            )
            payload = _me_payload(session, member)
        except AuthError as exc:
            failure = exc
    if failure is not None:
        # 401, а не 400: фронт по коду решает, показывать форму входа снова
        # или сообщение о недостатке прав.
        raise _auth_fail(failure, 401) from failure
    _set_cookie(request, response, token)
    return payload


# ── Вход сотрудника по номеру ───────────────────────────────────────────────


class PhoneIn(BaseModel):
    phone: str


class PhonePasswordIn(BaseModel):
    phone: str
    password: str


@router.post("/auth/phone/start")
def auth_phone_start(body: PhoneIn, request: Request) -> dict[str, str]:
    """Первый шаг: `{"step": "password" | "set_password"}`.

    Незнакомый номер получает `password`, как и знакомый.
    """
    _guard()
    with finance_session() as session:
        try:
            step = auth.phone_start(session, phone=body.phone, ip=client_ip(request))
        except AuthError as exc:
            raise _auth_fail(exc, 400) from exc
    return {"step": step}


@router.post("/auth/phone/login")
def auth_phone_login(body: PhonePasswordIn, request: Request, response: Response) -> dict[str, Any]:
    """Номер и пароль → сеанс. Ответ — как у `me`."""
    _guard()
    failure: AuthError | None = None
    with finance_session() as session:
        try:
            member, token = auth.phone_login(
                session, phone=body.phone, password=body.password,
                user_agent=_agent(request), ip=client_ip(request),
            )
            payload = _me_payload(session, member)
        except AuthError as exc:
            failure = exc
    if failure is not None:
        raise _auth_fail(failure, 401) from failure
    _set_cookie(request, response, token)
    return payload


@router.post("/auth/phone/set-password")
def auth_phone_set_password(body: PhonePasswordIn, request: Request, response: Response) -> dict[str, Any]:
    """Задать пароль в окне ожидания и сразу войти. Ответ — как у `me`."""
    _guard()
    failure: AuthError | None = None
    with finance_session() as session:
        try:
            member, token = auth.phone_set_password(
                session, phone=body.phone, password=body.password,
                user_agent=_agent(request), ip=client_ip(request),
            )
            payload = _me_payload(session, member)
        except AuthError as exc:
            failure = exc
    if failure is not None:
        raise _auth_fail(failure, 400) from failure
    _set_cookie(request, response, token)
    return payload


@router.post("/auth/phone/forgot")
def auth_phone_forgot(body: PhoneIn, request: Request) -> dict[str, bool]:
    """«Забыл пароль»: просьба администратору. Ответ одинаковый, есть номер или нет."""
    _guard()
    with finance_session() as session:
        try:
            auth.phone_forgot(session, phone=body.phone, user_agent=_agent(request), ip=client_ip(request))
        except AuthError as exc:
            # Неверный формат номера — не тайна: его видит и сама форма.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/auth/logout")
def auth_logout(request: Request, response: Response) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        auth.logout(
            session, request.cookies.get(auth.COOKIE_NAME), ip=client_ip(request), user_agent=_agent(request)
        )
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    """Кто вошёл. Отдаёт `authenticated: false` вместо 401.

    Этот маршрут спрашивают при открытии страницы, и 401 в консоли браузера на
    каждом заходе гостя — шум, из которого потом не видно настоящих ошибок.
    """
    _guard()
    with finance_session() as session:
        member = auth.resolve(
            session, request.cookies.get(auth.COOKIE_NAME), ip=client_ip(request), user_agent=_agent(request)
        )
        if member is None:
            return {"authenticated": False}
        return _me_payload(session, member)


class SwitchIn(BaseModel):
    company_id: UUID


@router.post("/auth/switch")
def auth_switch(body: SwitchIn, request: Request, member: Member = Depends(current_member)) -> dict[str, Any]:
    """Сменить компанию в текущей сессии."""
    _guard()
    token = request.cookies.get(auth.COOKIE_NAME)
    with finance_session() as session:
        try:
            switched = auth.switch_company(session, token or "", body.company_id)
        except AuthError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        history.write(
            session, None, workspace_id=body.company_id, kind="auth.switch",  # type: ignore[arg-type]
            entity="workspace", entity_id=body.company_id,
            title=f"вход в компанию «{switched.workspace_title}»",
        )
        return _me_payload(session, switched)


class CompanyIn(BaseModel):
    title: str


@router.post("/auth/companies", status_code=201)
def auth_add_company(
    body: CompanyIn, member: Member = Depends(current_member)
) -> dict[str, Any]:
    """Ещё одна компания тому же человеку — он её владелец."""
    _guard()
    with finance_session() as session:
        try:
            created = auth.add_company(session, member, title=body.title)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return created


@router.patch("/auth/company")
def auth_rename_company(
    body: CompanyIn, member: Member = Depends(require_role("owner"))
) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        before = workspace.title
        try:
            service.rename_workspace(session, workspace, title=body.title)
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session, workspace, kind="company.rename", entity="workspace", entity_id=workspace.id,
            title=f"компания переименована: «{before}» → «{workspace.title}»",
            before={"title": before}, after={"title": workspace.title},
        )
        return {"id": str(workspace.id), "title": workspace.title}


class InviteIn(BaseModel):
    email: str
    password: str
    #: `admin` | `employee`; прежние `accountant`/`viewer` принимаются.
    role: str = "accountant"
    full_name: str = ""


@router.get("/auth/members")
def auth_members(member: Member = Depends(require_access("people", "view"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {"items": auth.members_of(session, workspace.id)}


@router.post("/auth/members", status_code=201)
def auth_invite(
    body: InviteIn, member: Member = Depends(require_access("people", "edit"))
) -> dict[str, Any]:
    """Добавить человека в компанию с временным паролем."""
    _guard()
    with finance_session() as session:
        try:
            return auth.invite(
                session,
                member,
                email=body.email,
                role=body.role,
                full_name=body.full_name,
                password=body.password,
            )
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


class RoleIn(BaseModel):
    role: str


@router.patch("/auth/members/{user_id}")
def auth_change_role(
    user_id: UUID, body: RoleIn, member: Member = Depends(require_access("people", "edit"))
) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        try:
            auth.change_role(session, member, user_id=user_id, role=body.role)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}


@router.delete("/auth/members/{user_id}")
def auth_remove_member(
    user_id: UUID, member: Member = Depends(require_access("people", "edit"))
) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        try:
            auth.remove_member(session, member, user_id=user_id)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/auth/password")
def auth_password(
    body: PasswordIn, member: Member = Depends(password_member)
) -> dict[str, Any]:
    """Смена своего пароля. Доступна и тем, у кого пароль временный.

    Остальные сеансы закрываются: `sessions_closed` — сколько.
    """
    _guard()
    with finance_session() as session:
        try:
            closed = auth.set_password(session, member, old=body.old_password, new=body.new_password)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "sessions_closed": closed}


@router.get("/auth/sessions")
def auth_sessions(member: Member = Depends(current_member)) -> dict[str, Any]:
    """Свои открытые сессии — чтобы увидеть чужой вход и отозвать его.

    У каждой: `user_agent`, `ip`, `created_at`, `last_seen_at`, `current`.
    """
    _guard()
    with finance_session() as session:
        return {"items": auth.sessions_of(session, member.user_id, current=member.session_id)}


@router.delete("/auth/sessions/{session_id}")
def auth_revoke_session(
    session_id: UUID, member: Member = Depends(current_member)
) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        try:
            auth.revoke_session(session, member, session_id)
        except AuthError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}


@router.post("/auth/sessions/end-others")
def auth_end_other_sessions(member: Member = Depends(current_member)) -> dict[str, int]:
    """«Завершить все, кроме этого»."""
    _guard()
    with finance_session() as session:
        closed = auth.end_sessions(session, member.user_id, keep=member.session_id)
        if member.workspace_id is not None:
            history.write(
                session, None, workspace_id=member.workspace_id,  # type: ignore[arg-type]
                kind="auth.sessions_end", entity="user", entity_id=member.user_id,
                title=f"остальные сеансы завершены: {closed}", after={"sessions_closed": closed},
            )
        return {"closed": closed}


class ProfileIn(BaseModel):
    full_name: str | None = None
    phone: str | None = None


@router.patch("/auth/profile")
def auth_profile(body: ProfileIn, member: Member = Depends(require_role("owner", "admin"))) -> dict[str, Any]:
    """Свои ФИО и телефон — владельцу и администратору; сотруднику их задаёт администратор."""
    _guard()
    with finance_session() as session:
        try:
            user = auth.set_profile(session, member, full_name=body.full_name, phone=body.phone)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": str(user.id), "full_name": user.full_name, "phone": user.phone or ""}


# ── Обзор ────────────────────────────────────────────────────────────────────


@router.get("/overview")
def overview(member: Member = Depends(require_access("reports.summary"))) -> dict[str, Any]:
    """Сводка в колонке разделов: счета, остатки и ближайшие ожидания.

    Один запрос, а не четыре: раздел открывают десятки раз в день, и каждый
    лишний круг до сервера человек ощущает как «подвисло».

    Своё право, а не журнал: раньше сводку получал любой с журналом, и
    сотрудник, которому открыли журнал и таблицу, видел остатки всех счетов.
    """
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        balances = reports.account_balances(session, workspace.id)
        debt = reports.debts(session, workspace.id)
        total = sum(Decimal(item["balance"]) for item in balances)
        total_with_plan = sum(Decimal(item["balance_with_plan"]) for item in balances)
        operations, count = service.list_operations(
            session, workspace.id, service.OperationFilter(statuses=("plan",)), limit=5
        )
        return {
            "workspace": {
                "id": str(workspace.id),
                "title": workspace.title,
                "currency": workspace.base_currency,
            },
            "accounts": balances,
            "total": str(total),
            "total_with_plan": str(total_with_plan),
            "receivable": debt["receivable"]["total"],
            "payable": debt["payable"]["total"],
            "overdue_receivable": debt["receivable"]["overdue"],
            "planned_count": count,
            "planned_preview": [
                {
                    "id": str(operation.id),
                    "kind": operation.kind,
                    "paid_at": operation.paid_at.isoformat(),
                    "amount": str(operation.amount),
                    "comment": operation.comment,
                }
                for operation in operations
            ],
        }


# ── Справочники ──────────────────────────────────────────────────────────────


@router.get("/dictionaries")
def dictionaries(member: Member = Depends(require_access(access_module.MONEY_RESOURCES))) -> dict[str, Any]:
    """Все справочники разом — ими наполняются выпадающие списки форм.

    Начальный остаток счёта — только тем, кому остатки открыты: вместе с
    журналом он даёт остаток на сегодня, то есть ту же сводку, которую
    закрывает право «Остатки и долги». Формам журнала нужно только имя счёта.
    """
    _guard()
    sees_balances = member.rights.can_any(("dictionaries", "reports.summary"))
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {
            "accounts": [
                {
                    "id": str(item.id),
                    "name": item.name,
                    "kind": item.kind,
                    "currency": item.currency,
                    "starting_balance": str(item.starting_balance) if sees_balances else None,
                    "excluded_from_reports": item.excluded_from_reports,
                    "number": item.number or "",
                }
                for item in service.list_accounts(session, workspace.id)
            ],
            "categories": [
                {
                    "id": str(item.id),
                    "name": item.name,
                    "side": item.side,
                    "system_key": item.system_key,
                    "nature": item.nature,
                }
                for item in service.list_categories(session, workspace.id)
            ],
            "counterparties": [
                {"id": str(item.id), "name": item.name, "role": item.role}
                for item in service.list_counterparties(session, workspace.id)
            ],
            "projects": [
                {"id": str(item.id), "name": item.name, "closed": item.closed}
                for item in service.list_projects(session, workspace.id)
            ],
            "tags": [{"id": str(item.id), "name": item.name} for item in service.list_tags(session, workspace.id)],
        }


class AccountIn(BaseModel):
    name: str
    kind: str = "bank"
    currency: str | None = None
    starting_balance: str = "0"
    excluded_from_reports: bool = False
    #: Номер счёта в банке (IBAN). По нему выписка находит свой счёт.
    number: str = ""


@router.post("/accounts")
def create_account(body: AccountIn, member: Member = Depends(require_access("dictionaries", "edit"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            account = service.create_account(
                session,
                workspace,
                name=body.name,
                kind=body.kind,
                currency=body.currency,
                starting_balance=_money(body.starting_balance, field="starting_balance"),
                excluded_from_reports=body.excluded_from_reports,
                number=body.number,
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session, workspace, kind="account.create", entity="account", entity_id=account.id,
            title=f"счёт заведён: «{account.name}»",
            after={"name": account.name, "kind": account.kind, "currency": account.currency,
                   "starting_balance": str(account.starting_balance), "number": account.number or ""},
        )
        return {"id": str(account.id), "name": account.name, "number": account.number}


class EntryIn(BaseModel):
    name: str
    #: Для категорий — сторона учёта, для контрагентов — роль.
    side: str | None = None
    role: str | None = None


class StartingBalanceIn(BaseModel):
    starting_balance: str


@router.patch("/accounts/{account_id}")
def patch_account_balance(
    account_id: UUID,
    body: StartingBalanceIn,
    member: Member = Depends(require_access("dictionaries", "edit")),
) -> dict[str, Any]:
    """Начальный остаток счёта. Меняет остаток во всех отчётах задним числом.

    Поэтому право то же, что у состава счетов, и запись в истории с отменой:
    опечатку в начальном остатке обязаны уметь вернуть одной кнопкой.
    """
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            account, before = service.set_starting_balance(
                session, workspace, account_id, _money(body.starting_balance, field="Начальный остаток")
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session,
            workspace,
            kind="account.balance",
            entity="account",
            entity_id=account.id,
            title=f"начальный остаток «{account.name}»: {before} → {account.starting_balance}",
            before={"starting_balance": str(before)},
            after={"starting_balance": str(account.starting_balance)},
            actor=_actor(member),
        )
        return {
            "id": str(account.id),
            "name": account.name,
            "starting_balance": str(account.starting_balance),
        }


class AccountNumberIn(BaseModel):
    number: str = ""


@router.put("/accounts/{account_id}/number")
def put_account_number(
    account_id: UUID,
    body: AccountNumberIn,
    member: Member = Depends(require_access("dictionaries", "edit")),
) -> dict[str, Any]:
    """Номер счёта в банке. Пустая строка снимает номер.

    Право то же, что у состава счетов: номер решает, на какой счёт ляжет
    следующая выписка, — это не подпись, а адрес денег.
    """
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            account, before = service.set_account_number(session, workspace, account_id, body.number)
        except FinanceError as exc:
            raise _fail(exc) from exc
        if before != account.number:
            history.write(
                session,
                workspace,
                kind="account.number",
                entity="account",
                entity_id=account.id,
                title=f"номер счёта «{account.name}»: {before or '—'} → {account.number or '—'}",
                before={"number": before},
                after={"number": account.number},
                actor=_actor(member),
            )
        return {"id": str(account.id), "name": account.name, "number": account.number}


@router.post("/dictionaries/{kind}")
def create_entry(kind: str, body: EntryIn, member: Member = Depends(require_access(("dictionaries", "journal", "table"), "edit"))) -> dict[str, Any]:
    """Создать статью, контрагента, проект или тег.

    Счёт сюда не попадает намеренно: он требует прав администратора и заводится
    своим маршрутом выше.
    """
    _guard()
    if kind not in ("categories", "counterparties", "projects", "tags"):
        raise HTTPException(status_code=404, detail="Неизвестный справочник")
    with finance_session() as session:
        workspace = _workspace(session, member)
        if kind == "categories":
            if body.side not in ("income", "expense"):
                raise HTTPException(status_code=422, detail="У категории должна быть сторона: доход или расход")
            item = service.ensure_category(session, workspace.id, body.side, body.name)
        elif kind == "counterparties":
            item = service.ensure_counterparty(session, workspace.id, body.name, role=body.role or "client")
        elif kind == "projects":
            item = service.ensure_project(session, workspace.id, body.name)
        else:
            item = service.ensure_tag(session, workspace.id, body.name)
        if item is None:
            raise HTTPException(status_code=422, detail="Пустое название")
        history.write(
            session, workspace, kind="dictionary.create", entity=kind, entity_id=item.id,
            title=f"{_DICTIONARY_TITLES[kind]} «{item.name}»", after={"name": item.name},
        )
        return {"id": str(item.id), "name": item.name}


_DICTIONARY_TITLES = {
    "accounts": "счёт",
    "categories": "статья",
    "counterparties": "контрагент",
    "projects": "проект",
    "tags": "тег",
}


@router.delete("/dictionaries/{kind}/{item_id}")
def archive_entry(kind: str, item_id: UUID, member: Member = Depends(require_access("dictionaries", "edit"))) -> dict[str, bool]:
    """Удалить запись справочника — в корзину (`finance/trash.py`), не из базы."""
    _guard()
    models = {
        "accounts": Account,
        "categories": Category,
        "counterparties": Counterparty,
        "projects": Project,
        "tags": Tag,
    }
    if kind not in models:
        raise HTTPException(status_code=404, detail="Неизвестный справочник")
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            service.archive(session, models[kind], workspace.id, item_id)
        except FinanceError as exc:
            raise _fail(exc) from exc
        item = session.get(models[kind], item_id)
        history.write(
            session, workspace, kind="dictionary.archive", entity=kind, entity_id=item_id,
            title=f"{_DICTIONARY_TITLES[kind]} «{getattr(item, 'name', '')}» удалён в корзину",
        )
        return {"ok": True}


class NatureIn(BaseModel):
    nature: str = Field(pattern="^(revenue|cogs|operating|financial|depreciation|tax|other)$")


@router.patch("/dictionaries/categories/{item_id}/nature")
def set_category_nature(
    item_id: UUID, body: NatureIn, member: Member = Depends(require_access("dictionaries", "edit"))
) -> dict[str, Any]:
    """Природа статьи: себестоимость, операционный расход, проценты, амортизация…

    От неё зависят показатели: без неё «Закуп товара» и «Аренда» — просто два
    расхода, и валовую прибыль с EBITDA посчитать нечем.
    """
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        category = session.get(Category, item_id)
        if category is None or category.workspace_id != workspace.id:
            raise HTTPException(status_code=404, detail="Статья не найдена")
        before = category.nature
        category.nature = body.nature
        session.flush()
        history.write(
            session, workspace, kind="category.nature", entity="categories", entity_id=category.id,
            title=f"природа статьи «{category.name}»: {before or '—'} → {category.nature}",
            before={"nature": before}, after={"nature": category.nature},
        )
        return {"id": str(category.id), "nature": category.nature}


# ── Журнал ───────────────────────────────────────────────────────────────────


def _operation_out(
    operation: Operation,
    *,
    names: dict[str, dict[UUID, str]],
    splits: dict[UUID, list[Any]],
    tags: dict[UUID, list[UUID]],
) -> dict[str, Any]:
    return {
        "id": str(operation.id),
        "version": operation.version,
        "kind": operation.kind,
        "status": operation.status,
        "paid_at": operation.paid_at.isoformat(),
        "accrued_at": operation.accrued_at.isoformat() if operation.accrued_at else None,
        "period_start": operation.period_start.isoformat() if operation.period_start else None,
        "period_end": operation.period_end.isoformat() if operation.period_end else None,
        "amount": str(operation.amount),
        "currency": operation.currency,
        "amount_base": str(operation.amount_base),
        "account_from": names["accounts"].get(operation.account_from_id, ""),
        "account_to": names["accounts"].get(operation.account_to_id, ""),
        "account_from_id": str(operation.account_from_id) if operation.account_from_id else None,
        "account_to_id": str(operation.account_to_id) if operation.account_to_id else None,
        "category": names["categories"].get(operation.category_id, ""),
        "category_id": str(operation.category_id) if operation.category_id else None,
        "counterparty": names["counterparties"].get(operation.counterparty_id, ""),
        "counterparty_id": str(operation.counterparty_id) if operation.counterparty_id else None,
        "projects": [
            {
                "id": str(split.project_id),
                "name": names["projects"].get(split.project_id, ""),
                "amount": str(split.amount),
            }
            for split in splits.get(operation.id, [])
        ],
        "split_state": operation.split_state,
        "tags": [str(tag_id) for tag_id in tags.get(operation.id, [])],
        "comment": operation.comment,
        "source": operation.source,
        "import_batch_id": str(operation.import_batch_id) if operation.import_batch_id else None,
    }


def _names(session, workspace_id: UUID) -> dict[str, dict[UUID, str]]:
    return {
        "accounts": {item.id: item.name for item in service.list_accounts(session, workspace_id, with_archived=True)},
        "categories": {item.id: item.name for item in service.list_categories(session, workspace_id)},
        "counterparties": {item.id: item.name for item in service.list_counterparties(session, workspace_id)},
        "projects": {item.id: item.name for item in service.list_projects(session, workspace_id)},
    }


#: Что бывает в фильтрах журнала. Держим рядом с разбором фильтра, а не в
#: моделях: это словарь HTTP-слоя, и отказ показывается человеку отсюда.
KINDS = frozenset({"income", "expense", "transfer"})
STATUSES = frozenset({"fact", "plan"})


def _filter(
    date_from: str | None,
    date_to: str | None,
    kinds: str | None,
    statuses: str | None,
    search: str | None,
    by: str,
    amount_from: str | None,
    amount_to: str | None,
    account_id: UUID | None,
    category_id: UUID | None,
    counterparty_id: UUID | None,
    project_id: UUID | None,
) -> service.OperationFilter:
    def split(value: str | None, allowed: frozenset[str], *, field: str) -> tuple[str, ...]:
        """Разобрать список через запятую и отказать на незнакомом значении.

        Молчаливый пропуск был хуже отказа: `kinds=нечто` не фильтровал
        ничего, журнал отдавал все операции, и человек читал полный список как
        «расходов по этому виду столько». Фильтр, который не фильтрует, —
        это неверная цифра, а не пустой экран.
        """
        parts = tuple(part.strip() for part in (value or "").split(",") if part.strip())
        unknown = [part for part in parts if part not in allowed]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"{field}: не знаю значение «{unknown[0]}». Бывают: {', '.join(sorted(allowed))}",
            )
        return parts

    return service.OperationFilter(
        date_from=_parse_date(date_from, field="date_from"),
        date_to=_parse_date(date_to, field="date_to"),
        kinds=split(kinds, KINDS, field="kinds"),
        statuses=split(statuses, STATUSES, field="statuses"),
        search=search or "",
        by=by,
        amount_from=_money(amount_from, field="amount_from") if amount_from else None,
        amount_to=_money(amount_to, field="amount_to") if amount_to else None,
        account_ids=(account_id,) if account_id else (),
        category_ids=(category_id,) if category_id else (),
        counterparty_ids=(counterparty_id,) if counterparty_id else (),
        project_ids=(project_id,) if project_id else (),
    )


@router.get("/operations")
def list_operations(
    member: Member = Depends(require_access("journal")),
    date_from: str | None = None,
    date_to: str | None = None,
    kinds: str | None = None,
    statuses: str | None = None,
    search: str | None = None,
    by: str = Query(default="paid", pattern="^(paid|accrued)$"),
    amount_from: str | None = None,
    amount_to: str | None = None,
    account_id: UUID | None = None,
    category_id: UUID | None = None,
    counterparty_id: UUID | None = None,
    project_id: UUID | None = None,
    limit: int = Query(default=250, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Журнал операций с фильтрами."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        flt = _filter(
            date_from, date_to, kinds, statuses, search, by, amount_from, amount_to,
            account_id, category_id, counterparty_id, project_id,
        )
        operations, total = service.list_operations(session, workspace.id, flt, limit=limit, offset=offset)
        ids = [operation.id for operation in operations]
        names = _names(session, workspace.id)
        splits = service.operation_projects(session, ids)
        tags = service.operation_tags(session, ids)
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            # По всему фильтру, а не по странице — см. `service.operation_sums`.
            "sums": {key: str(value) for key, value in service.operation_sums(session, workspace.id, flt).items()},
            "items": [
                _operation_out(operation, names=names, splits=splits, tags=tags)
                for operation in operations
            ],
        }


@router.get("/export/journal.xlsx")
def export_journal(
    member: Member = Depends(require_access("journal")),
    date_from: str | None = None,
    date_to: str | None = None,
    kinds: str | None = None,
    statuses: str | None = None,
    search: str | None = None,
    by: str = Query(default="paid", pattern="^(paid|accrued)$"),
    amount_from: str | None = None,
    amount_to: str | None = None,
    account_id: UUID | None = None,
    category_id: UUID | None = None,
    counterparty_id: UUID | None = None,
    project_id: UUID | None = None,
) -> Response:
    """Журнал с теми же фильтрами — файлом Excel. См. `app.finance.export`."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        flt = _filter(
            date_from, date_to, kinds, statuses, search, by, amount_from, amount_to,
            account_id, category_id, counterparty_id, project_id,
        )
        try:
            data, count = export_module.journal_xlsx(session, workspace, flt)
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session, workspace, kind="journal.export", entity="operations",
            title=f"выгрузка журнала · {count} операций",
            after={"count": count, "date_from": date_from, "date_to": date_to},
        )
        title = workspace.title
    name = f"Журнал — {title} — {date.today():%d.%m.%Y}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            # Имя по-русски — через `filename*`: заголовок без него браузер
            # показал бы кракозябрами или обрезал на первой кириллической букве.
            "Content-Disposition": f"attachment; filename=\"journal.xlsx\"; filename*=UTF-8''{quote(name)}",
        },
    )


class ProjectSplitIn(BaseModel):
    project_id: UUID
    amount: str


class CategorySplitIn(BaseModel):
    """Часть платежа, отнесённая к статье."""

    category_id: UUID
    amount: str


class OperationIn(BaseModel):
    kind: str = Field(pattern="^(income|expense|transfer)$")
    status: str = Field(default="fact", pattern="^(fact|plan)$")
    paid_at: str
    accrued_at: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    amount: str
    currency: str | None = None
    rate: str | None = None
    account_from_id: UUID | None = None
    account_to_id: UUID | None = None
    category_id: UUID | None = None
    counterparty_id: UUID | None = None
    comment: str = ""
    projects: list[ProjectSplitIn] = Field(default_factory=list)
    categories: list[CategorySplitIn] = Field(default_factory=list)
    tags: list[UUID] = Field(default_factory=list)


@router.post("/operations")
def create_operation(body: OperationIn, member: Member = Depends(require_access(("journal", "calendar"), "edit"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        paid_at = _parse_date(body.paid_at, field="paid_at")
        if paid_at is None:
            raise HTTPException(status_code=422, detail="Без даты платежа операции не бывает")
        data = service.OperationInput(
            kind=body.kind,
            status=body.status,
            paid_at=paid_at,
            accrued_at=_parse_date(body.accrued_at, field="accrued_at"),
            period_start=_parse_date(body.period_start, field="period_start"),
            period_end=_parse_date(body.period_end, field="period_end"),
            amount=_money(body.amount, field="amount"),
            currency=body.currency,
            rate=_money(body.rate, field="rate") if body.rate else None,
            account_from_id=body.account_from_id,
            account_to_id=body.account_to_id,
            category_id=body.category_id,
            counterparty_id=body.counterparty_id,
            comment=body.comment,
            projects=[(item.project_id, _money(item.amount, field="projects")) for item in body.projects],
            categories=[
                (item.category_id, _money(item.amount, field="categories")) for item in body.categories
            ],
            tags=list(body.tags),
            source="app",
        )
        try:
            operation = service.create_operation(session, workspace, data, actor=_actor(member))
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session,
            workspace,
            kind="operation.create",
            entity="operation",
            entity_id=operation.id,
            after=history.snapshot(operation),
            actor=_actor(member),
        )
        names = _names(session, workspace.id)
        splits = service.operation_projects(session, [operation.id])
        return _operation_out(operation, names=names, splits=splits, tags={})


class OperationPatch(BaseModel):
    version: int | None = None
    status: str | None = Field(default=None, pattern="^(fact|plan)$")
    paid_at: str | None = None
    accrued_at: str | None = None
    amount: str | None = None
    account_from_id: UUID | None = None
    account_to_id: UUID | None = None
    category_id: UUID | None = None
    counterparty_id: UUID | None = None
    comment: str | None = None
    projects: list[ProjectSplitIn] | None = None
    categories: list[CategorySplitIn] | None = None
    tags: list[UUID] | None = None


@router.patch("/operations/{operation_id}")
def patch_operation(
    operation_id: UUID, body: OperationPatch, member: Member = Depends(require_access(("journal", "calendar"), "edit"))
) -> dict[str, Any]:
    _guard()
    changes: dict[str, Any] = {}
    if body.status is not None:
        changes["status"] = body.status
    if body.paid_at is not None:
        changes["paid_at"] = _parse_date(body.paid_at, field="paid_at")
    if body.accrued_at is not None:
        changes["accrued_at"] = _parse_date(body.accrued_at, field="accrued_at")
    if body.amount is not None:
        changes["amount"] = _money(body.amount, field="amount")
    for key in ("account_from_id", "account_to_id", "category_id", "counterparty_id"):
        value = getattr(body, key)
        if value is not None:
            changes[key] = value
    if body.comment is not None:
        changes["comment"] = body.comment
    if body.projects is not None:
        changes["projects"] = [(item.project_id, _money(item.amount, field="projects")) for item in body.projects]
    if body.categories is not None:
        changes["categories"] = [
            (item.category_id, _money(item.amount, field="categories")) for item in body.categories
        ]
    if body.tags is not None:
        changes["tags"] = list(body.tags)
    if not changes:
        raise HTTPException(status_code=422, detail="Нечего менять")

    with finance_session() as session:
        workspace = _workspace(session, member)
        existing = session.get(Operation, operation_id)
        before = history.snapshot(existing) if existing is not None else {}
        try:
            operation = service.update_operation(
                session, workspace, operation_id, changes, version=body.version, actor=_actor(member)
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session,
            workspace,
            kind="operation.settle" if changes.get("status") == "fact" else "operation.update",
            entity="operation",
            entity_id=operation.id,
            before=before,
            after=history.snapshot(operation),
            actor=_actor(member),
        )
        names = _names(session, workspace.id)
        splits = service.operation_projects(session, [operation.id])
        tags = service.operation_tags(session, [operation.id])
        return _operation_out(operation, names=names, splits=splits, tags=tags)


@router.delete("/operations/{operation_id}")
def delete_operation(operation_id: UUID, member: Member = Depends(require_access(("journal", "calendar"), "edit"))) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        existing = session.get(Operation, operation_id)
        before = history.snapshot(existing) if existing is not None else {}
        try:
            service.delete_operation(session, workspace, operation_id, actor=_actor(member))
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session,
            workspace,
            kind="operation.delete",
            entity="operation",
            entity_id=operation_id,
            before=before,
            actor=_actor(member),
        )
        return {"ok": True}


# ── Табличный вид ────────────────────────────────────────────────────────────


@router.get("/grid")
def read_grid(
    member: Member = Depends(require_access("table")),
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Журнал как лист: шапка, строки и значения для выпадающих списков.

    `seq` — номер изменения журнала, с которого лист начинает опрос
    (`/grid/changes`); читается до выборки строк, как у реестра договоров.
    """
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        seq = live_module.current_seq(session, workspace.id)
        flt = service.OperationFilter(
            date_from=_parse_date(date_from, field="date_from"),
            date_to=_parse_date(date_to, field="date_to"),
        )
        operations, total = service.list_operations(
            session, workspace.id, flt, limit=min(limit or finance_settings.grid_max_rows, finance_settings.grid_max_rows)
        )
        payload = grid_module.build_grid(session, workspace, operations)
        payload["total"] = total
        payload["seq"] = seq
        return payload


@router.get("/grid/changes")
def grid_changes(since: int = Query(0, ge=0), member: Member = Depends(require_access("table"))) -> dict[str, Any]:
    """Живой режим листа «Таблица»: строки, изменённые после `since`, и снятые.

    Лист опрашивает раз в 2 с, пока вкладка видна; почти всегда ответ пустой —
    два чтения счётчика.
    """
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return live_module.changes(session, workspace, since)


class CellPatch(BaseModel):
    operation_id: UUID
    column: str
    value: Any = None
    version: int | None = None


@router.patch("/grid/cell")
def patch_cell(body: CellPatch, member: Member = Depends(require_access("table", "edit"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        # Правка ячейки — та же правка операции, что из карточки, и пишется в
        # журнал так же: с состоянием до и после и с откатом. Раньше лист
        # менял операцию молча, и «кто поменял сумму» по нему было не узнать.
        existing = session.get(Operation, body.operation_id)
        before = (
            history.snapshot(existing)
            if existing is not None and existing.workspace_id == workspace.id
            else {}
        )
        try:
            operation = grid_module.apply_cell(
                session, workspace, body.operation_id, body.column, body.value,
                version=body.version, actor=_actor(member),
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        after = history.snapshot(operation)
        if after != before:
            history.write(
                session, workspace, kind="operation.update", entity="operation", entity_id=operation.id,
                title=f"правка в таблице: {body.column}", before=before, after=after,
            )
        names = _names(session, workspace.id)
        splits = service.operation_projects(session, [operation.id])
        tags = service.operation_tags(session, [operation.id])
        # `row` — строка листа после правки: лист пишет её обратно в свои
        # ячейки по месту, а не перечитывает журнал (см. `grid.row_of`).
        return {
            **_operation_out(operation, names=names, splits=splits, tags=tags),
            "row": grid_module.row_of(session, workspace, operation),
        }


class GridRowIn(BaseModel):
    cells: dict[str, Any]


@router.post("/grid/row")
def add_grid_row(body: GridRowIn, member: Member = Depends(require_access("table", "edit"))) -> dict[str, Any]:
    """Новая операция из строки, набранной внизу листа."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            operation = grid_module.append_row(session, workspace, body.cells, actor=_actor(member))
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session, workspace, kind="operation.create", entity="operation", entity_id=operation.id,
            title="операция заведена строкой таблицы", after=history.snapshot(operation),
        )
        names = _names(session, workspace.id)
        splits = service.operation_projects(session, [operation.id])
        return {
            **_operation_out(operation, names=names, splits=splits, tags={}),
            "row": grid_module.row_of(session, workspace, operation),
        }


# ── Отчёты ───────────────────────────────────────────────────────────────────


def _period(date_from: str | None, date_to: str | None) -> tuple[date, date]:
    """Период отчёта. По умолчанию — шесть месяцев, включая текущий целиком.

    Полгода, а не месяц: отчёт из одного столбца не отвечает ни на один
    управленческий вопрос — сравнивать не с чем.

    Про «включая текущий целиком»
    ─────────────────────────────
    Сначала здесь стояло `end = первое число текущего месяца`, и это был
    дефект: операции текущего месяца после первого числа в отчёты не попадали
    вовсе. Экран выглядел исправным — столбец сентября на месте, — но стоял в
    нём ноль, а журнал в это же время показывал одиннадцать операций.
    Поймано сквозным прогоном 18 сентября 2026 на разделе «План/Факт», где
    пустота видна сразу.

    Поэтому конец периода — последний день текущего месяца. Будущие даты внутри
    месяца ничего не портят: плановые платежи в отчётах и так показаны
    отдельными столбцами.
    """
    today = date.today()
    end = _parse_date(date_to, field="date_to")
    if end is None:
        last_day = calendar_module.monthrange(today.year, today.month)[1]
        end = date(today.year, today.month, last_day)
    start = _parse_date(date_from, field="date_from")
    if start is None:
        month = end.month - 5
        year = end.year
        while month <= 0:
            month += 12
            year -= 1
        start = date(year, month, 1)
    if start > end:
        raise HTTPException(status_code=422, detail="Начало периода позже его конца")
    return start, end


@router.get("/reports/cash-flow")
def report_cash_flow(
    member: Member = Depends(require_access("reports.cash")),
    date_from: str | None = None,
    date_to: str | None = None,
    group: str = Query(default="category", pattern="^(category|counterparty|project)$"),
) -> dict[str, Any]:
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = _workspace(session, member)
        return reports.cash_flow(session, workspace.id, start, end, group=group)


@router.get("/reports/profit")
def report_profit(
    member: Member = Depends(require_access("reports.profit")),
    date_from: str | None = None,
    date_to: str | None = None,
    group: str = Query(default="category", pattern="^(category|counterparty|project)$"),
) -> dict[str, Any]:
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = _workspace(session, member)
        return reports.profit_and_loss(session, workspace.id, start, end, group=group)


@router.get("/reports/debts")
def report_debts(member: Member = Depends(require_access("reports.debts")), as_of: str | None = None) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return reports.debts(session, workspace.id, as_of=_parse_date(as_of, field="as_of"))


@router.get("/reports/projects")
def report_projects(
    member: Member = Depends(require_access("reports.projects")), date_from: str | None = None, date_to: str | None = None
) -> dict[str, Any]:
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = _workspace(session, member)
        return reports.projects_report(session, workspace.id, start, end)


@router.get("/reports/calendar")
def report_calendar(
    member: Member = Depends(require_access("calendar")),
    year: int = Query(default=0, ge=0, le=2200),
    month: int = Query(default=0, ge=0, le=12),
) -> dict[str, Any]:
    _guard()
    today = date.today()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return reports.calendar(session, workspace.id, year or today.year, month or today.month)


@router.get("/reports/plan-actual")
def report_plan_actual(
    member: Member = Depends(require_access("reports.plan")),
    date_from: str | None = None,
    date_to: str | None = None,
    method: str = Query(default="cash", pattern="^(cash|accrual)$"),
) -> dict[str, Any]:
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = _workspace(session, member)
        return reports.plan_actual(session, workspace.id, start, end, method=method)


class PlanIn(BaseModel):
    month: str
    side: str = Field(pattern="^(income|expense)$")
    method: str = Field(default="cash", pattern="^(cash|accrual)$")
    amount: str
    category_id: UUID | None = None
    project_id: UUID | None = None
    comment: str = ""


@router.post("/plans")
def upsert_plan(body: PlanIn, member: Member = Depends(require_access("reports.plan", "edit"))) -> dict[str, Any]:
    """Поставить или изменить план на месяц по статье."""
    _guard()
    month = _parse_date(body.month, field="month")
    if month is None:
        raise HTTPException(status_code=422, detail="Не указан месяц плана")
    month = date(month.year, month.month, 1)
    with finance_session() as session:
        workspace = _workspace(session, member)
        existing = session.scalar(
            sa.select(Plan).where(
                Plan.workspace_id == workspace.id,
                Plan.month == month,
                Plan.side == body.side,
                Plan.method == body.method,
                Plan.category_id == body.category_id,
                Plan.project_id == body.project_id,
            )
        )
        amount = _money(body.amount, field="amount")
        # План — то же число в отчёте «План / Факт», что и факт, и проверяется
        # так же: отрицательный план вычитался бы из плана по статье, а месяц
        # с опечаткой в годе навсегда остался бы строкой, которую не с чем
        # сравнить.
        if amount < 0:
            raise HTTPException(
                status_code=422,
                detail="План не бывает отрицательным: сторону задаёт «доход» или «расход»",
            )
        try:
            service.check_date(month, field="Месяц плана")
        except FinanceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        before = {"amount": str(existing.amount), "comment": existing.comment} if existing is not None else {}
        if existing is None:
            existing = Plan(
                workspace_id=workspace.id,
                month=month,
                side=body.side,
                method=body.method,
                category_id=body.category_id,
                project_id=body.project_id,
                amount=amount,
                comment=body.comment,
                created_by=_actor(member),
            )
            session.add(existing)
        else:
            existing.amount = amount
            existing.comment = body.comment
        session.flush()
        history.write(
            session, workspace, kind="plan.set", entity="plan", entity_id=existing.id,
            title=f"план на {month:%m.%Y}: {before.get('amount', '—')} → {existing.amount}",
            before=before, after={"amount": str(existing.amount), "comment": existing.comment},
        )
        return {"id": str(existing.id), "month": month.isoformat(), "amount": str(existing.amount)}



# ── Автоправила ──────────────────────────────────────────────────────────────


class RuleIn(BaseModel):
    name: str
    conditions: list[dict[str, Any]]
    actions: dict[str, Any]
    match: str = "all"


@router.get("/rules")
def list_rules(member: Member = Depends(require_access("rules"))) -> dict[str, Any]:
    """Правила компании вместе со счётчиком срабатываний.

    Счётчик важнее, чем кажется: правило, которое не совпало ни разу, выглядит
    работающим, и человек уверен, что разметка идёт.
    """
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {
            "items": [
                {
                    "id": str(rule.id),
                    "name": rule.name,
                    "active": rule.active,
                    "match": rule.match,
                    "conditions": rule.conditions,
                    "actions": rule.actions,
                    "applied_count": rule.applied_count,
                    "position": rule.position,
                }
                for rule in rules.list_rules(session, workspace.id)
            ]
        }


@router.post("/rules", status_code=201)
def create_rule(body: RuleIn, member: Member = Depends(require_access("rules", "edit"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            rule = rules.create_rule(
                session,
                workspace.id,
                name=body.name,
                conditions=body.conditions,
                actions=body.actions,
                match=body.match,
                actor=_actor(member),
            )
        except rules.RuleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        history.write(
            session, workspace, kind="rule.create", entity="rule", entity_id=rule.id,
            title=f"правило «{rule.name}» заведено",
            after={"name": rule.name, "conditions": rule.conditions, "actions": rule.actions, "match": rule.match},
        )
        return {"id": str(rule.id), "name": rule.name}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: UUID, member: Member = Depends(require_access("rules", "edit"))) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        existing = session.get(Rule, rule_id)
        before = (
            {"name": existing.name, "conditions": existing.conditions, "actions": existing.actions}
            if existing is not None and existing.workspace_id == workspace.id
            else {}
        )
        try:
            rules.delete_rule(session, workspace.id, rule_id)
        except rules.RuleError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        history.write(
            session, workspace, kind="rule.delete", entity="rule", entity_id=rule_id,
            title=f"правило «{before.get('name', '')}» удалено", before=before,
        )
        return {"ok": True}


class RuleToggleIn(BaseModel):
    active: bool


@router.patch("/rules/{rule_id}")
def toggle_rule(
    rule_id: UUID, body: RuleToggleIn, member: Member = Depends(require_access("rules", "edit"))
) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            rule = rules.toggle_rule(session, workspace.id, rule_id, body.active)
        except rules.RuleError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        history.write(
            session, workspace, kind="rule.toggle", entity="rule", entity_id=rule.id,
            title=f"правило «{rule.name}» {'включено' if rule.active else 'выключено'}",
            after={"active": rule.active},
        )
        return {"id": str(rule.id), "active": rule.active}


class RuleApplyIn(BaseModel):
    #: По умолчанию трогаем только неразмеченное: переразметка задним числом
    #: меняет отчёты, которые человек уже видел и, возможно, кому-то показал.
    only_uncategorized: bool = True


@router.post("/rules/apply")
def apply_rules(
    body: RuleApplyIn, member: Member = Depends(require_access("rules", "edit"))
) -> dict[str, Any]:
    """Применить правила к уже заведённым операциям."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        done = rules.apply_to_operations(
            session, workspace.id, only_uncategorized=body.only_uncategorized
        )
        history.write(
            session, workspace, kind="rules.apply", entity="operations",
            title=f"правила применены: {done.get('updated', 0)} операций",
            after={"updated": done.get("updated", 0), "only_uncategorized": body.only_uncategorized},
        )
        return done


@router.get("/rules/suggest")
def suggest_rules(member: Member = Depends(require_access("rules"))) -> dict[str, Any]:
    """С чего начать разметку: частые слова в неразмеченных операциях."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {"items": rules.suggest(session, workspace.id)}


# ── Авторазметка ─────────────────────────────────────────────────────────────


@router.get("/autotag")
def autotag_preview(member: Member = Depends(require_access("rules"))) -> dict[str, Any]:
    """Что разметится по тексту операций — группами, ничего не записывая."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return autotag.preview(session, workspace)


class AutotagGroupIn(BaseModel):
    side: str = Field(pattern="^(income|expense)$")
    category: str


class AutotagIn(BaseModel):
    groups: list[AutotagGroupIn]


@router.post("/autotag")
def autotag_apply(body: AutotagIn, member: Member = Depends(require_access("rules", "edit"))) -> dict[str, Any]:
    """Разметить выбранные группы. Одна запись в истории — одна отмена на всё."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            done = autotag.apply(session, workspace, [(item.side, item.category) for item in body.groups])
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session,
            workspace,
            kind="autotag.apply",
            entity="operations",
            title=f"авторазметка: {done['updated']} операций",
            # Пустая разметка — тоже действие человека, но откатывать в ней нечего.
            after={"items": done["items"]} if done["updated"] else {"updated": 0},
            actor=_actor(member),
        )
        return {"updated": done["updated"], "by_category": done["by_category"]}


# ── Импорт ───────────────────────────────────────────────────────────────────


@router.post("/import/preview")
def import_preview(
    member: Member = Depends(require_access("import", "edit")),
    file: UploadFile = File(...),
    date_order: str | None = Query(default=None, pattern="^(dmy|mdy)$"),
    default_account: str | None = None,
) -> dict[str, Any]:
    """Разобрать файл и показать, что получится. Ничего не записывает в учёт.

    Разбор сохраняется партией импорта: человек может уйти разбираться со
    строками и вернуться, не загружая файл заново.
    """
    _guard()
    data = file.file.read()
    if len(data) > finance_settings.import_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Файл больше {finance_settings.import_max_mb} МБ — разделите его на части",
        )
    with finance_session() as session:
        workspace = _workspace(session, member)
        accounts = [account.name for account in service.list_accounts(session, workspace.id)]
        try:
            preview = analyze(
                data,
                file.filename or "файл",
                accounts,
                date_order=date_order,
                default_account=default_account,
                account_numbers=service.account_numbers(session, workspace.id),
            )
        except ImportError_ as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if len(preview.rows) > finance_settings.import_max_rows:
            raise HTTPException(
                status_code=413,
                detail=f"В файле {len(preview.rows)} строк — потолок {finance_settings.import_max_rows}",
            )
        # Разметка правилами — ДО записи. Человек видит «412 строк лягут в
        # „Продукты“ по правилу „Magnum“» и может передумать; у соседей по
        # рынку правила срабатывают уже после того, как операции заведены.
        rule_hits = rules.preview_rows(
            rules.list_rules(session, workspace.id, only_active=True),
            [row.values for row in preview.rows],
        )
        return _preview_response(session, workspace, preview, rule_hits, member)


def _preview_response(
    session: Any,
    workspace: Any,
    preview: Any,
    rule_hits: dict[str, int],
    member: Member,
) -> dict[str, Any]:
    """Ответ предпросмотра — один и тот же для файла и для книги Google.

    Собран отдельной функцией не ради краткости: два ответа с разным набором
    полей значили бы, что экран импорта показывает про книгу меньше, чем про
    файл, — и «сколько строк отложено» стало бы зависеть от того, откуда данные.
    """
    batch = service.save_preview(session, workspace, preview, actor=_actor(member))
    history.write(
        session, workspace, kind="import.preview", entity="import_batch", entity_id=batch.id,
        title=f"разбор «{preview.file_name}»: {len(preview.rows)} строк",
        after={"file_name": preview.file_name, "rows": len(preview.rows)},
    )
    return {
        "batch_id": str(batch.id),
        "file_name": preview.file_name,
        "header_line": preview.header_line,
        "counts": preview.counts,
        "question": preview.question,
        "date_order": preview.date_reading.order,
        "date_evidence": preview.date_reading.evidence,
        "mapping": preview.mapping,
        "unused_columns": preview.unused_columns,
        "accounts_missing": preview.accounts_missing,
        "accounts_suggested": preview.accounts_suggested,
        # Сверка с остатками, которые напечатал банк, — PDF или таблицей.
        "bank": service.reconcile_statement(session, workspace, preview),
        "rules_applied": rule_hits,
        "rows": [
            {
                "line": row.line,
                "state": row.state,
                "problems": row.problems,
                "values": row.values,
                "raw": row.raw,
            }
            for row in preview.rows
        ],
    }


# ── Счета-фактуры ───────────────────────────────────────────────────────────


class InvoiceLineIn(BaseModel):
    title: str = ""
    quantity: str = "1"
    price: str = "0"


class InvoiceIn(BaseModel):
    kind: str = Field(default="out", pattern="^(out|in)$")
    number: str = ""
    issued_at: str
    due_at: str
    vat_rate: str = "0"
    counterparty_id: UUID | None = None
    project_id: UUID | None = None
    category_id: UUID | None = None
    account_id: UUID | None = None
    comment: str = ""
    lines: list[InvoiceLineIn] = Field(default_factory=list)


@router.get("/invoices")
def list_invoices(
    member: Member = Depends(require_access("invoices")),
    kind: str | None = Query(default=None, pattern="^(out|in)$"),
) -> dict[str, Any]:
    """Счета и их сводка: сколько выставлено, сколько не оплачено, сколько просрочено."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {
            "items": invoices_module.list_invoices(session, workspace.id, kind=kind),
            "summary": invoices_module.summary(session, workspace.id),
        }


@router.post("/invoices", status_code=201)
def create_invoice(body: InvoiceIn, member: Member = Depends(require_access("invoices", "edit"))) -> dict[str, Any]:
    """Выставить счёт. Ожидание по нему появляется сразу — это и есть долг."""
    _guard()
    issued_at = _parse_date(body.issued_at, field="issued_at")
    due_at = _parse_date(body.due_at, field="due_at")
    if issued_at is None or due_at is None:
        raise HTTPException(status_code=422, detail="Нужны дата счёта и срок оплаты")
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            invoice = invoices_module.create(
                session,
                workspace,
                kind=body.kind,
                issued_at=issued_at,
                due_at=due_at,
                lines=[line.model_dump() for line in body.lines],
                vat_rate=body.vat_rate or "0",
                number=body.number,
                counterparty_id=body.counterparty_id,
                project_id=body.project_id,
                category_id=body.category_id,
                account_id=body.account_id,
                comment=body.comment,
                actor=_actor(member),
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session,
            workspace,
            kind="invoice.create",
            entity="invoice",
            entity_id=invoice.id,
            title=f"счёт {invoice.number} на {invoice.amount_gross}",
            after={"number": invoice.number, "amount": str(invoice.amount_gross)},
            actor=_actor(member),
        )
        return invoices_module.read(session, workspace.id, invoice.id)


@router.get("/invoices/{invoice_id}")
def read_invoice(invoice_id: UUID, member: Member = Depends(require_access("invoices"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return invoices_module.read(session, workspace.id, invoice_id)
        except FinanceError as exc:
            raise _fail(exc) from exc


@router.post("/invoices/{invoice_id}/void")
def void_invoice(invoice_id: UUID, member: Member = Depends(require_access("invoices", "edit"))) -> dict[str, Any]:
    """Отменить счёт вместе с его ожиданием."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            invoice = invoices_module.void(session, workspace.id, invoice_id)
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session,
            workspace,
            kind="invoice.void",
            entity="invoice",
            entity_id=invoice.id,
            title=f"счёт {invoice.number} отменён",
            actor=_actor(member),
        )
        return {"ok": True}


# ── Повторяющиеся операции ──────────────────────────────────────────────────


class RecurrenceIn(BaseModel):
    title: str
    kind: str = Field(pattern="^(income|expense|transfer)$")
    amount: str
    period: str = Field(default="month", pattern="^(week|month|quarter|year)$")
    day: int = 1
    start_at: str
    until: str | None = None
    account_from_id: UUID | None = None
    account_to_id: UUID | None = None
    category_id: UUID | None = None
    counterparty_id: UUID | None = None
    project_id: UUID | None = None
    comment: str = ""


@router.get("/recurrences")
def list_recurrences(member: Member = Depends(require_access("recurrences"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {"items": recurring.list_recurrences(session, workspace.id), "horizon_days": recurring.HORIZON_DAYS}


@router.post("/recurrences", status_code=201)
def create_recurrence(
    body: RecurrenceIn, member: Member = Depends(require_access("recurrences", "edit"))
) -> dict[str, Any]:
    """Создать повторение и сразу разложить ожидания на горизонт вперёд."""
    _guard()
    start_at = _parse_date(body.start_at, field="start_at")
    if start_at is None:
        raise HTTPException(status_code=422, detail="Нужна дата начала")
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            rule = recurring.create(
                session,
                workspace,
                title=body.title,
                kind=body.kind,
                amount=_money(body.amount, field="amount"),
                period=body.period,
                day=body.day,
                start_at=start_at,
                until=_parse_date(body.until, field="until"),
                account_from_id=body.account_from_id,
                account_to_id=body.account_to_id,
                category_id=body.category_id,
                counterparty_id=body.counterparty_id,
                project_id=body.project_id,
                comment=body.comment,
                actor=_actor(member),
            )
            created = recurring.materialize(session, workspace, actor=_actor(member))
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session,
            workspace,
            kind="recurrence.create",
            entity="recurrence",
            entity_id=rule.id,
            title=f"повторение «{rule.title}», ожиданий создано {created}",
            actor=_actor(member),
        )
        return {"id": str(rule.id), "created": created}


@router.post("/recurrences/materialize")
def materialize_recurrences(member: Member = Depends(require_access("recurrences", "edit"))) -> dict[str, Any]:
    """Продлить горизонт ожиданий. Повторный вызов ничего не удваивает."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        created = recurring.materialize(session, workspace, actor=_actor(member))
        # Экран зовёт продление при каждом открытии; пустой проход ничего не
        # менял, и строка «продлено: 0» в журнале была бы шумом.
        if created:
            history.write(
                session, workspace, kind="recurrence.materialize", entity="recurrence",
                title=f"ожидания повторений продлены: {created}", after={"created": created},
            )
        return {"created": created}


@router.patch("/recurrences/{recurrence_id}")
def toggle_recurrence(
    recurrence_id: UUID,
    active: bool = Query(...),
    member: Member = Depends(require_access("recurrences", "edit")),
) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            rule = recurring.set_active(session, workspace.id, recurrence_id, active)
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session, workspace, kind="recurrence.toggle", entity="recurrence", entity_id=rule.id,
            title=f"повторение «{rule.title}» {'включено' if rule.active else 'выключено'}",
            after={"active": rule.active},
        )
        return {"id": str(rule.id), "active": rule.active}


@router.delete("/recurrences/{recurrence_id}")
def delete_recurrence(
    recurrence_id: UUID,
    with_future: bool = Query(default=True),
    member: Member = Depends(require_access("recurrences", "edit")),
) -> dict[str, Any]:
    """Удалить повторение. Будущие неоплаченные ожидания уходят вместе с ним."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            removed = recurring.remove(session, workspace.id, recurrence_id, with_future=with_future)
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session,
            workspace,
            kind="recurrence.remove",
            entity="recurrence",
            entity_id=recurrence_id,
            title=f"повторение удалено, снято ожиданий {removed}",
            actor=_actor(member),
        )
        return {"removed": removed}


# ── История действий ────────────────────────────────────────────────────────


@router.get("/history")
def read_history(
    member: Member = Depends(require_access(("audit", "journal"))), limit: int = Query(default=100, ge=1, le=500)
) -> dict[str, Any]:
    """Кто и что менял. Отменяемые записи помечены `can_undo`."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {"items": history.listing(session, workspace.id, limit=limit)}


#: Разделы, правка которых откатывается из журнала. Какой нужен именно для
#: этой записи, решает `audit.undo_resources` по её сущности.
UNDO_RESOURCES = ("journal", "table", "calendar", "dictionaries", "rules")


def undo_entry(member: Member, entry_id: UUID) -> dict[str, Any]:
    """Откат записи журнала — с правом правки того раздела, чья это запись.

    Чужая компания и несуществующая запись отвечают одинаково — 404.
    """
    from app.finance import audit
    from app.finance.models import ActionLog

    with finance_session() as session:
        workspace = _workspace(session, member)
        entry = session.get(ActionLog, entry_id)
        if entry is None or entry.workspace_id != workspace.id:
            raise HTTPException(status_code=404, detail="Запись истории не найдена")
        resources = audit.undo_resources(entry)
        if resources and not member.rights.can_any(resources, "edit"):
            raise HTTPException(status_code=403, detail=_denied(resources, "edit", member))
        try:
            return history.undo(session, workspace, entry_id, actor=_actor(member))
        except FinanceError as exc:
            raise _fail(exc) from exc


@router.post("/history/{entry_id}/undo")
def undo_action(entry_id: UUID, member: Member = Depends(require_access(UNDO_RESOURCES, "edit"))) -> dict[str, Any]:
    """Отменить действие — вернуть состояние «до», а не сделать обратное."""
    _guard()
    return undo_entry(member, entry_id)


# ── Интеграции ──────────────────────────────────────────────────────────────


class IntegrationIn(BaseModel):
    slug: str
    kind: str = Field(pattern="^(api|statement|sheets)$")
    title: str = ""
    account_id: UUID | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


@router.get("/integrations")
def list_integrations(member: Member = Depends(require_access("integrations"))) -> dict[str, Any]:
    """Подключения компании и справочник банков с логотипами."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        accounts = {
            str(account.id): account.name
            for account in service.list_accounts(session, workspace.id)
        }
        items = [
            integrations_module.to_dict(
                item, account_name=accounts.get(str(item.account_id), "") if item.account_id else ""
            )
            for item in integrations_module.list_integrations(session, workspace.id)
        ]
        return {"items": items, "catalog": integrations_module.catalog()}


@router.post("/integrations", status_code=201)
def create_integration(
    body: IntegrationIn, member: Member = Depends(require_access("integrations", "edit"))
) -> dict[str, Any]:
    """Подключить источник. Для приёма по адресу токен возвращается один раз."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            integration, token = integrations_module.create(
                session,
                workspace,
                slug=body.slug,
                kind=body.kind,
                title=body.title,
                account_id=body.account_id,
                settings=body.settings,
                actor=_actor(member),
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session,
            workspace,
            kind="integration.create",
            entity="integration",
            entity_id=integration.id,
            title=f"подключение «{integration.title}» ({integration.kind})",
            actor=_actor(member),
        )
        out = integrations_module.to_dict(integration)
        # Токен показывается ровно здесь и больше никогда: в базе он хешем.
        out["token"] = token
        out["inbox_url"] = f"/api/v1/finance/integrations/inbox"
        return out


@router.post("/integrations/{integration_id}/token")
def rotate_integration_token(
    integration_id: UUID, member: Member = Depends(require_access("integrations", "edit"))
) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            token = integrations_module.rotate_token(session, workspace.id, integration_id)
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session, workspace, kind="integration.token", entity="integration", entity_id=integration_id,
            title="токен подключения заменён",
        )
        return {"token": token}


@router.patch("/integrations/{integration_id}")
def set_integration_state(
    integration_id: UUID,
    state: str = Query(pattern="^(active|off)$"),
    member: Member = Depends(require_access("integrations", "edit")),
) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            integration = integrations_module.set_state(session, workspace.id, integration_id, state)
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session, workspace, kind="integration.state", entity="integration", entity_id=integration.id,
            title=f"подключение «{integration.title}» {'включено' if state == 'active' else 'выключено'}",
            after={"state": state},
        )
        return integrations_module.to_dict(integration)


@router.delete("/integrations/{integration_id}")
def delete_integration(
    integration_id: UUID, member: Member = Depends(require_access("integrations", "edit"))
) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            integrations_module.remove(session, workspace.id, integration_id)
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session, workspace, kind="integration.delete", entity="integration", entity_id=integration_id,
            title="подключение удалено",
        )
        return {"ok": True}


class InboxIn(BaseModel):
    """Пачка операций от подключения."""

    operations: list[dict[str, Any]] = Field(default_factory=list)
    #: Разобрать и показать, ничего не записывая.
    dry_run: bool = False


@router.post("/integrations/inbox")
def integration_inbox(
    body: InboxIn,
    request: Request,
    x_finance_token: str = Header(default=""),
) -> dict[str, Any]:
    """Приём операций по адресу — вход для банков и чужих систем.

    Охраняется не учёткой человека, а токеном подключения: присылающая сторона
    — это скрипт, а не человек в браузере. Поэтому здесь нет `current_member`, и
    компания берётся из токена.

    Записываем через тот же разбор, что и файл: строка с минусом — расход,
    неизвестный счёт — отказ строке, а не подстановка наугад.
    """
    _guard()
    if not body.operations:
        raise HTTPException(status_code=422, detail="Пустая пачка: присылать нечего")
    if len(body.operations) > 5000:
        raise HTTPException(status_code=413, detail="За раз принимаем не больше 5000 операций")

    with finance_session() as session:
        try:
            integration = integrations_module.resolve_token(session, x_finance_token)
        except FinanceError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        workspace = service.get_workspace(session, integration.workspace_id)
        rows = integrations_module.rows_of(body.operations)

        accounts = {account.name: account for account in service.list_accounts(session, workspace.id)}
        default_account = (
            session.get(Account, integration.account_id) if integration.account_id else None
        )
        categories = {item.name: item for item in service.list_categories(session, workspace.id)}

        accepted = 0
        rejected: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            account = accounts.get(row["account"] or "") or default_account
            if account is None:
                rejected.append({"line": index, "problem": "счёт не указан и у подключения его нет"})
                continue
            if row["kind"] == "income":
                account_to, account_from = account.id, None
            else:
                account_to, account_from = None, account.id
            data = service.OperationInput(
                kind=row["kind"],
                paid_at=row["paid_at"],
                amount=row["amount"],
                account_from_id=account_from,
                account_to_id=account_to,
                category_id=(
                    categories[row["category"]].id
                    if row["category"] and row["category"] in categories
                    else None
                ),
                comment=row["comment"],
                source="integration",
                external_key=row["external_key"],
                integration_id=integration.id,
            )
            if body.dry_run:
                accepted += 1
                continue
            try:
                service.create_operation(session, workspace, data, actor=f"интеграция «{integration.title}»")
                accepted += 1
            except FinanceError as exc:
                rejected.append({"line": index, "problem": str(exc)})

        if not body.dry_run and accepted:
            integrations_module.mark_received(session, integration, accepted)
            history.write(
                session,
                workspace,
                kind="integration.receive",
                entity="integration",
                entity_id=integration.id,
                title=f"из «{integration.title}» пришло операций: {accepted}",
                actor=f"интеграция «{integration.title}»",
            )
        return {
            "accepted": accepted,
            "rejected": rejected,
            "dry_run": body.dry_run,
            "integration": integration.title,
        }


# ── Баланс, показатели, выписка по счёту ────────────────────────────────────


@router.get("/reports/balance")
def report_balance(
    member: Member = Depends(require_access("reports.balance")), as_of: str | None = None
) -> dict[str, Any]:
    """Чем компания владеет и что должна — на дату."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return reports.balance(session, workspace.id, as_of=_parse_date(as_of, field="as_of"))


@router.get("/reports/indicators")
def report_indicators(
    member: Member = Depends(require_access("reports.indicators")),
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """EBITDA, валовая прибыль, маржа — по природе статей."""
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = _workspace(session, member)
        return reports.indicators(session, workspace.id, start, end)


@router.get("/reports/statement")
def report_account_statement(
    account_id: UUID,
    member: Member = Depends(require_access("reports.statement")),
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Выписка по счёту для сверки с банком."""
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return reports.account_statement(session, workspace.id, account_id, start, end)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


# ── Книги Google ────────────────────────────────────────────────────────────


@router.get("/sheets/books")
def sheets_books(member: Member = Depends(require_access("sheets"))) -> dict[str, Any]:
    """Книги Google, открытые сервисному аккаунту программы.

    Неготовность доступа — не ошибка экрана: `configured: false` показывается
    объяснением, что книгу нужно открыть сервисному аккаунту, а не отказом.
    """
    _guard()
    if not sheets.is_configured():
        return {"configured": False, "items": []}
    try:
        return {"configured": True, "items": sheets.books()}
    except sheets.SheetsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sheets/books/{book_id}")
def sheets_book(book_id: str, member: Member = Depends(require_access("sheets"))) -> dict[str, Any]:
    """Вкладки книги и ссылка на неё саму."""
    _guard()
    try:
        return sheets.tabs(book_id)
    except sheets.SheetsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class SheetImportIn(BaseModel):
    book_id: str
    tab: str
    date_order: str | None = Field(default=None, pattern="^(dmy|mdy)$")
    default_account: str | None = None


@router.post("/sheets/preview")
def sheets_preview(
    body: SheetImportIn,
    member: Member = Depends(require_access("sheets", "edit")),
) -> dict[str, Any]:
    """Разобрать вкладку книги — тем же разбором, что и загруженный файл."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        accounts = [account.name for account in service.list_accounts(session, workspace.id)]
        try:
            preview = sheets.preview_tab(
                body.book_id,
                body.tab,
                accounts,
                date_order=body.date_order,
                default_account=body.default_account,
                account_numbers=service.account_numbers(session, workspace.id),
            )
        except ImportError_ as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except sheets.SheetsError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if len(preview.rows) > finance_settings.import_max_rows:
            raise HTTPException(
                status_code=413,
                detail=f"Во вкладке {len(preview.rows)} строк — потолок {finance_settings.import_max_rows}",
            )
        rule_hits = rules.preview_rows(
            rules.list_rules(session, workspace.id, only_active=True),
            [row.values for row in preview.rows],
        )
        return _preview_response(session, workspace, preview, rule_hits, member)


@router.get("/import/batches")
def list_batches(member: Member = Depends(require_access("import")), limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    """Прошлые загрузки: что за файл, сколько завелось, сколько отложено."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        batches = session.scalars(
            sa.select(ImportBatch)
            .where(ImportBatch.workspace_id == workspace.id)
            .order_by(ImportBatch.created_at.desc())
            .limit(limit)
        )
        return {
            "items": [
                {
                    "id": str(batch.id),
                    "file_name": batch.file_name,
                    "status": batch.status,
                    "created_at": batch.created_at.isoformat() if batch.created_at else None,
                    "applied_at": batch.applied_at.isoformat() if batch.applied_at else None,
                    "rows_total": batch.rows_total,
                    "rows_imported": batch.rows_imported,
                    "rows_failed": batch.rows_failed,
                    "rows_skipped": batch.rows_skipped,
                    "rows_duplicate": batch.rows_duplicate,
                    "decisions": batch.decisions,
                }
                for batch in batches
            ]
        }


@router.get("/import/batches/{batch_id}")
def read_batch(batch_id: UUID, member: Member = Depends(require_access("import"))) -> dict[str, Any]:
    """Строки партии: заведённые, отложенные и почему."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        batch = session.get(ImportBatch, batch_id)
        if batch is None or batch.workspace_id != workspace.id:
            raise HTTPException(status_code=404, detail="Загрузка не найдена")
        rows = session.scalars(
            sa.select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.line)
        )
        return {
            "id": str(batch.id),
            "file_name": batch.file_name,
            "status": batch.status,
            "mapping": batch.mapping,
            "decisions": batch.decisions,
            "counts": {
                "total": batch.rows_total,
                "imported": batch.rows_imported,
                "failed": batch.rows_failed,
                "skipped": batch.rows_skipped,
                "duplicate": batch.rows_duplicate,
            },
            "rows": [
                {
                    "line": row.line,
                    "state": row.state,
                    "problems": row.problems,
                    "values": row.parsed,
                    "raw": row.raw,
                    "operation_id": str(row.operation_id) if row.operation_id else None,
                }
                for row in rows
            ],
        }


class ApplyIn(BaseModel):
    #: Пусто — завести все готовые строки. Список — только эти строки.
    lines: list[int] | None = None
    create_dictionaries: bool = True


@router.post("/import/batches/{batch_id}/apply")
def apply_batch(batch_id: UUID, body: ApplyIn, member: Member = Depends(require_access(("import", "sheets"), "edit"))) -> dict[str, Any]:
    """Завести готовые строки партии.

    Отложенные строки остаются в партии и не мешают: файл из двухсот строк с
    одной испорченной заводит сто девяносто девять.
    """
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            done = service.apply_batch(
                session,
                workspace,
                batch_id,
                actor=_actor(member),
                only_lines=body.lines,
                create_dictionaries=body.create_dictionaries,
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        batch = session.get(ImportBatch, batch_id)
        history.write(
            session, workspace, kind="import.apply", entity="import_batch", entity_id=batch_id,
            title=f"загрузка «{batch.file_name if batch else ''}» · заведено {done.get('imported', 0)}",
            after={key: done.get(key) for key in ("imported", "failed", "skipped", "duplicate", "total")},
        )
        remembered = done.get("remembered")
        if remembered:
            # Номер записан счёту сам, по выписке, — это видно в истории и
            # отменяется оттуда же, как любая правка счёта.
            history.write(
                session,
                workspace,
                kind="account.number",
                entity="account",
                entity_id=UUID(remembered["account_id"]),
                title=f"номер счёта «{remembered['account']}» записан из выписки: {remembered['number']}",
                before={"number": ""},
                after={"number": remembered["number"]},
                actor=_actor(member),
            )
        return done


class RowFixIn(BaseModel):
    patch: dict[str, Any]


@router.patch("/import/batches/{batch_id}/rows/{line}")
def fix_row(batch_id: UUID, line: int, body: RowFixIn, member: Member = Depends(require_access(("import", "sheets"), "edit"))) -> dict[str, Any]:
    """Поправить отложенную строку, не перезагружая файл."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            row = service.fix_import_row(session, workspace, batch_id, line, body.patch)
        except FinanceError as exc:
            raise _fail(exc) from exc
        return {
            "line": row.line,
            "state": row.state,
            "problems": row.problems,
            "values": row.parsed,
        }
