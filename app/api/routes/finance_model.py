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

from app.api.routes.finance import _guard, _workspace, require_access
from app.finance.auth import Member
from app.finance.contracts import service as contracts_service
from app.finance.db import finance_session, get_finance_session_factory
from app.finance.model import HEADER, MAJOR, VERSION
from app.finance.model import apply as apply_module
from app.finance.model import ask as ask_module
from app.finance.model import describe as describe_module
from app.finance.model import explain as explain_module
from app.finance.model import schema as schema_module
from app.finance.model import stream as stream_module
from app.finance.model.context import of_member
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

#: Вызовы открыты тому, кому открыт хоть один источник видов.
RESOURCES = ("contracts", "reports.debts", "invoices")
reader = require_access(RESOURCES)


def _fail(exc: Exception) -> JSONResponse:
    headers = {HEADER: VERSION}
    command = getattr(exc, "index", None)
    cause = getattr(exc, "cause", exc)
    extra = {"command": command} if command is not None else {}
    if isinstance(exc, apply_module.Changed):
        return JSONResponse(status_code=409, content={"code": "changed", "detail": str(exc), "proposal": exc.proposal}, headers=headers)
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
    """Вызов в транзакции: записалось - коммит, отказ - откат и ответ словами."""
    _guard()
    try:
        with finance_session() as session:
            ctx = of_member(session, _workspace(session, member), member)
            return action(ctx)
    except HTTPException:
        raise
    except (FinanceError, PermissionError, contracts_service.NotFound) as exc:
        return _fail(exc)


def _dry(member: Member, action):
    """Прогон без следа: транзакция откатывается всегда, что бы ни сделал вызов."""
    _guard()
    session = get_finance_session_factory()()
    try:
        ctx = of_member(session, _workspace(session, member), member)
        return action(ctx)
    except HTTPException:
        raise
    except (FinanceError, PermissionError, contracts_service.NotFound) as exc:
        return _fail(exc)
    finally:
        session.rollback()
        session.close()


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


# ── Предложить, применить, отменить ──────────────────────────────────────────


def _commands(body: Any) -> list[dict[str, Any]]:
    return [item.model_dump() for item in body.commands]


@router.post("/propose")
def model_propose(body: schema_module.ProposeIn, member: Member = Depends(reader)) -> Any:
    """Предложить: команды прогоном, который откатывается, - что заведётся и изменится, долг до и после."""
    return _dry(member, lambda ctx: apply_module.propose(ctx, _commands(body)))


@router.post("/apply")
def model_apply(body: schema_module.ApplyIn, member: Member = Depends(reader)) -> Any:
    """Применить: пачка одной транзакцией и одной точкой восстановления; право - у каждой команды своё. Пишет журнал."""
    return _call(member, lambda ctx: apply_module.apply(ctx, _commands(body), expect=body.expect))


@router.post("/undo")
def model_undo(body: schema_module.UndoIn, member: Member = Depends(reader)) -> Any:
    """Отменить: вернуть точку восстановления (свою - любой, чужую - администратор). Пишет журнал."""
    try:
        point_id = uuid.UUID(body.point)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Точка восстановления записана неверно") from exc
    return _call(member, lambda ctx: apply_module.undo(ctx, point_id))


@router.get("/points")
def model_points(limit: int = Query(default=60, ge=1, le=200), member: Member = Depends(reader)) -> Any:
    """Точки восстановления: свои, у администратора - все."""
    return _call(member, lambda ctx: apply_module.points(ctx, limit=limit))


# ── Подписаться ──────────────────────────────────────────────────────────────


def _changes_once(member: Member, cursor: str, include: frozenset[str], final: bool, contracts_since: int | None = None) -> Any:
    """Одна проверка: сдвинулось - ответ; нет и время ждать ещё есть - `None`."""
    _guard()
    try:
        with finance_session() as session:
            ctx = of_member(session, _workspace(session, member), member)
            if not final and cursor and not stream_module.moved(ctx, cursor):
                return None
            return stream_module.changes(ctx, cursor, include=include, contracts_since=contracts_since)
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
