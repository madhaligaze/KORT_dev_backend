"""API модели (`/finance/model/*`): семь вызовов ядра для любого фронта.

Вызовы - `app/finance/model/` (описание там); здесь только HTTP: вход,
версия, коды отказов. Маршруты объявляют раздел видов («Договоры», «Долги»,
«Счета» - хоть один): что именно видно и можно, решает сам вызов по правам
вошедшего - тем же кодом, что экраны.

Коды отказов - словами в `detail` и машинным `code`:
* 409 `model_version` - клиент прислал `X-Kort-Model` другой старшей версии;
* 422 `mode_required` - команде нужен ответ «опечатка или с даты» (`fields`);
* 409 `conflict` - поле поменял другой после `known_seq` (`conflicts`);
* 409 `changed` - договоры поменялись после предложения (`proposal` - новое);
* 403 / 404 / 400 - нет права, нет такого, записано неверно; у пачки -
  `command` (номер команды).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.routes.finance import _guard, _workspace, require_access, require_role
from app.finance import auth, history
from app.finance.auth import Member
from app.finance.contracts import service as contracts_service
from app.finance.db import finance_session, get_finance_session_factory, retrying
from app.finance.model import HEADER, MAJOR, VERSION
from app.finance.model import apply as apply_module
from app.finance.model import ask as ask_module
from app.finance.model import describe as describe_module
from app.finance.model import explain as explain_module
from app.finance.model import schema as schema_module
from app.finance.model import stream as stream_module
from app.finance.model.context import of_member
from app.finance.model.entry import Question
from app.finance.service import FinanceError


def _version(response: Response, kort_model: str | None = Header(default=None, alias=HEADER)) -> None:
    """Заголовок версии в каждом ответе; клиент другой старшей версии - отказ, а не чужие поля."""
    response.headers[HEADER] = VERSION
    if kort_model:
        try:
            major = int(str(kort_model).split(".")[0])
        except ValueError:
            major = -1
        if major != MAJOR:
            raise HTTPException(
                status_code=409,
                detail={"code": "model_version", "detail": f"Сервер говорит на версии {VERSION}, клиент - на {kort_model}: обновите клиент"},
                headers={HEADER: VERSION},
            )


router = APIRouter(prefix="/finance/model", tags=["finance-model"], dependencies=[Depends(_version)])

#: Вызовы открыты тому, кому открыт хоть один источник видов или раздел, у
#: которого есть команды (журнал денег, загрузка, счета); что именно можно -
#: решает сам вызов и право каждой команды.
RESOURCES = ("contracts", "reports.debts", "invoices", "journal", "import", "dictionaries")
reader = require_access(RESOURCES)


def _fail(exc: Exception) -> JSONResponse:
    headers = {HEADER: VERSION}
    command = getattr(exc, "index", None)
    cause = getattr(exc, "cause", exc)
    extra = {"command": command} if command is not None else {}
    if isinstance(exc, apply_module.Changed):
        return JSONResponse(status_code=409, content={"code": "changed", "detail": str(exc), "proposal": exc.proposal}, headers=headers)
    if isinstance(cause, Question):
        return JSONResponse(status_code=422, content={"code": "question", "question": cause.question, "detail": str(exc), **extra}, headers=headers)
    if isinstance(cause, contracts_service.ModeRequired):
        return JSONResponse(status_code=422, content={"code": "mode_required", "fields": cause.fields, "detail": str(exc), **extra}, headers=headers)
    if isinstance(cause, contracts_service.FieldConflict):
        return JSONResponse(status_code=409, content={"code": "conflict", "conflicts": cause.conflicts, "detail": str(exc), **extra}, headers=headers)
    if isinstance(cause, contracts_service.NotFound):
        return JSONResponse(status_code=404, content={"code": "not_found", "detail": str(exc), **extra}, headers=headers)
    if isinstance(cause, PermissionError):
        return JSONResponse(status_code=403, content={"code": "forbidden", "detail": str(exc), **extra}, headers=headers)
    if isinstance(exc, FinanceError) or isinstance(cause, FinanceError):
        return JSONResponse(status_code=400, content={"code": "refused", "detail": str(exc), **extra}, headers=headers)
    raise exc


def _call(member: Member, action):
    """Вызов в транзакции: записалось - коммит, отказ - откат и ответ словами.
    Жертва взаимоблокировки повторяется (`db.retrying`)."""
    _guard()

    def once():
        with finance_session() as session:
            ctx = of_member(session, _workspace(session, member), member)
            return action(ctx)

    try:
        return retrying(once)
    except HTTPException:
        raise
    except (FinanceError, PermissionError, contracts_service.NotFound) as exc:
        return _fail(exc)


def _dry(member: Member, action):
    """Прогон без следа: транзакция откатывается всегда, что бы ни сделал вызов."""
    _guard()

    def once():
        session = get_finance_session_factory()()
        try:
            ctx = of_member(session, _workspace(session, member), member)
            return action(ctx)
        finally:
            session.rollback()
            session.close()

    try:
        return retrying(once)
    except HTTPException:
        raise
    except (FinanceError, PermissionError, contracts_service.NotFound) as exc:
        return _fail(exc)


# ── Схема и описать ──────────────────────────────────────────────────────────


@router.get("")
def model_catalog(member: Member = Depends(reader)) -> dict[str, Any]:
    """Версия, вызовы и команды со схемами JSON."""
    _guard()
    return schema_module.catalog()


@router.get("/describe")
def model_describe(member: Member = Depends(reader)) -> Any:
    """Описать: схема компании - поля, меры, источники, виды, юрлица, отделы, команды, права спрашивающего."""
    return _call(member, describe_module.describe)


# ── Спросить и объяснить ─────────────────────────────────────────────────────


@router.post("/ask")
def model_ask(body: schema_module.AskIn, force: bool = Query(default=False), member: Member = Depends(reader)) -> Any:
    """Спросить: сохранённый вид или вопрос без сохранения, на день. Ничего не пишет."""
    return _call(member, lambda ctx: ask_module.ask(ctx, body.model_dump(exclude_none=True), force=force))


@router.get("/ask")
def model_ask_view(
    view: str = Query(default="", max_length=64),
    as_of: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    member: Member = Depends(reader),
) -> Any:
    """Спросить сохранённый вид - адресом: такой ответ десктоп сохраняет и показывает без связи."""
    return _call(member, lambda ctx: ask_module.ask(ctx, {"view": view, **({"as_of": as_of} if as_of else {})}))


@router.get("/explain")
def model_explain(ref: str = Query(min_length=1, max_length=4096), member: Member = Depends(reader)) -> Any:
    """Объяснить: число по ссылке из ответа - слагаемые до фактов журнала."""
    return _call(member, lambda ctx: explain_module.explain(ctx, ref))


@router.post("/compare")
def model_compare(body: schema_module.CompareIn, member: Member = Depends(reader)) -> Any:
    """Сравнить: таблица «договор за месяц» (файл base64 или сводная Google компании) против KORT по звеньям. Ничего не пишет."""
    from app.finance.intake import compare as compare_module

    def action(ctx):
        from app.finance.contracts.service import today

        entity = uuid.UUID(body.entity) if body.entity else None
        if body.data:
            from app.finance.model.entry import _file

            name = body.name or "таблица.xlsx"
            book = compare_module.table_book(
                ctx.session, ctx.workspace, _file(name, body.data), name, today=today(), entity=entity, answers=body.answers,
            )
        else:
            book, error = compare_module.company_book(ctx.session, ctx.workspace.id)
            if book is None:
                raise compare_module.CompareError(error or "Сводная таблица компании не подключена - пришлите таблицу файлом")
        links = tuple(body.links) if body.links else compare_module.LINKS
        return {"version": VERSION, **compare_module.compare(ctx.session, ctx.workspace, ctx.access, book, today=today(),
                                                             links=links, entity=entity)}

    return _call(member, action)


# ── Предложить, применить, отменить ──────────────────────────────────────────


def _commands(body: Any) -> list[dict[str, Any]]:
    return [item.model_dump() for item in body.commands]


def _writes(member: Member) -> None:
    """Ключ только для чтения не предлагает, не применяет и не отменяет."""
    if member.key == "read":
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "detail": "Ключ только для чтения: предложить, применить и отменить ему закрыты"},
            headers={HEADER: VERSION},
        )


@router.post("/propose")
def model_propose(body: schema_module.ProposeIn, member: Member = Depends(reader)) -> Any:
    """Предложить: команды прогоном, который откатывается, - что заведётся и изменится, долг до и после."""
    _writes(member)
    return _dry(member, lambda ctx: apply_module.propose(ctx, _commands(body)))


@router.post("/apply")
def model_apply(body: schema_module.ApplyIn, member: Member = Depends(reader)) -> Any:
    """Применить: пачка одной транзакцией и одной точкой восстановления; право - у каждой команды своё. Пишет журнал."""
    _writes(member)
    return _call(member, lambda ctx: apply_module.apply(ctx, _commands(body), expect=body.expect))


@router.post("/undo")
def model_undo(body: schema_module.UndoIn, member: Member = Depends(reader)) -> Any:
    """Отменить: вернуть точку восстановления (свою - любой, чужую - администратор). Пишет журнал."""
    _writes(member)
    try:
        point_id = uuid.UUID(body.point)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Точка восстановления записана неверно") from exc
    return _call(member, lambda ctx: apply_module.undo(ctx, point_id))


@router.get("/points")
def model_points(limit: int = Query(default=60, ge=1, le=200), member: Member = Depends(reader)) -> Any:
    """Точки восстановления: свои, у администратора - все."""
    return _call(member, lambda ctx: apply_module.points(ctx, limit=limit))


# ── Ключи для чужого сервиса ─────────────────────────────────────────────────

#: Ключи выдают и отзывают владелец и администратор - из сеанса, не ключом.
keeper = require_role("owner", "admin")


class KeyIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    #: `read` - описать, спросить, объяснить, подписаться; `write` - ещё предложить, применить, отменить.
    level: str = Field(default="read", pattern="^(read|write)$")


def _session_only(member: Member) -> None:
    if member.key:
        raise HTTPException(status_code=403, detail="Ключами распоряжаются из сеанса учётки, а не ключом")


@router.get("/keys")
def model_keys(member: Member = Depends(keeper)) -> dict[str, Any]:
    """Ключи API модели компании: кем выданы, когда спрашивали в последний раз, отозваны ли. Токенов здесь нет."""
    _session_only(member)
    _guard()
    with finance_session() as session:
        return {"keys": auth.keys_of(session, member.workspace_id)}


@router.post("/keys", status_code=201)
def model_key_create(body: KeyIn, member: Member = Depends(keeper)) -> dict[str, Any]:
    """Выдать ключ: токен - только в этом ответе, в базе - его хеш. Ключ действует правами выдавшего. Пишет журнал."""
    _session_only(member)
    _guard()
    with finance_session() as session:
        try:
            key, token = auth.create_key(session, member, title=body.title, level=body.level)
        except auth.AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        workspace = _workspace(session, member)
        history.write(
            session, workspace, kind="api_key.create", entity="api_key", entity_id=key.id,
            title=f"ключ API «{key.title}» выдан: {'чтение и запись' if key.level == 'write' else 'только чтение'}",
            after={"title": key.title, "level": key.level, "prefix": key.prefix}, category="admin",
        )
        return {"id": str(key.id), "title": key.title, "level": key.level, "prefix": key.prefix, "token": token}


@router.delete("/keys/{key_id}")
def model_key_revoke(key_id: uuid.UUID, member: Member = Depends(keeper)) -> dict[str, Any]:
    """Отозвать ключ: следующий его запрос - 401. Пишет журнал."""
    _session_only(member)
    _guard()
    with finance_session() as session:
        try:
            key = auth.revoke_key(session, member.workspace_id, key_id)
        except auth.AuthError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        workspace = _workspace(session, member)
        history.write(
            session, workspace, kind="api_key.revoke", entity="api_key", entity_id=key.id,
            title=f"ключ API «{key.title}» отозван", before={"title": key.title, "level": key.level}, category="admin",
        )
        return {"id": str(key.id), "revoked": True}


# ── Подписаться ──────────────────────────────────────────────────────────────


def _changes_once(member: Member, cursor: str, include: frozenset[str], final: bool, contracts_since: int | None = None) -> Any:
    """Одна проверка: сдвинулось - ответ; нет и время ждать ещё есть - `None`."""
    _guard()

    def once():
        with finance_session() as session:
            ctx = of_member(session, _workspace(session, member), member)
            if not final and cursor and not stream_module.moved(ctx, cursor):
                return None
            # Подписчик проводит журнал (`stream.changes`) - и может стать жертвой взаимоблокировки.
            return stream_module.changes(ctx, cursor, include=include, contracts_since=contracts_since)

    try:
        return retrying(once)
    except HTTPException:
        raise
    except (FinanceError, PermissionError, contracts_service.NotFound) as exc:
        return _fail(exc)


@router.get("/changes")
async def model_changes(
    request: Request,
    cursor: str = Query(default="", max_length=2048),
    wait: float = Query(default=0, ge=0, le=stream_module.WAIT_LIMIT),
    include: str = Query(default="", max_length=64),
    contracts_since: int | None = Query(default=None, ge=0),
    member: Member = Depends(reader),
) -> Any:
    """Подписаться: что изменилось после курсора. `wait` - ждать изменения до N секунд, не держа поток;
    `contracts_since` - начать с номера реестра, который у клиента уже есть."""
    wanted = frozenset(item for item in include.split(",") if item)
    if not cursor and contracts_since is not None:
        # Начало с номера реестра отвечает сразу: дальше клиент ждёт уже по курсору.
        return await run_in_threadpool(_changes_once, member, cursor, wanted, True, contracts_since)
    deadline = time.monotonic() + wait
    while True:
        final = time.monotonic() >= deadline
        found = await run_in_threadpool(_changes_once, member, cursor, wanted, final)
        if found is not None:
            return found
        if await request.is_disconnected():
            return Response(status_code=204, headers={HEADER: VERSION})
        await asyncio.sleep(1.0)
