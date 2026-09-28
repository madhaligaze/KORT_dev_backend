"""Личный вид листов «Финансов» (`/finance/looks/{key}`): у каждой учётки свой.

Ширины колонок, перенос, цвета, жирный, скрытые колонки — то, что меняет вид
листа и не меняет ни одного значения. Хранится за учёткой и компанией: вид,
настроенный администратором, сотрудник не видит, и наоборот.

Ключ — лист раздела: `journal`, `registry`, `registry.oneoff`. Содержимое
разбирает и собирает фронт (`src/components/univer/look.ts`); сервер держит
его как есть, ограничивая только размер.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.routes.finance import _guard, _workspace, require_access
from app.finance.auth import Member
from app.finance.db import finance_session
from app.finance.models import SheetLook

router = APIRouter(prefix="/finance/looks", tags=["finance-looks"])

#: Вид — у тех, кому открыт хоть один лист раздела.
sheet_viewer = require_access(("contracts", "journal", "table"), "view")

#: Больше этого вид не бывает: сотни раскрашенных ячеек — десятки килобайт.
MAX_BYTES = 512_000
_KEY = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


class LookIn(BaseModel):
    look: dict[str, Any]


def _key(key: str) -> str:
    if not _KEY.match(key):
        raise HTTPException(status_code=404, detail="Такого листа нет")
    return key


@router.get("/{key}")
def get_look(key: str, member: Member = Depends(sheet_viewer)) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        row = session.get(SheetLook, (member.user_id, workspace.id, _key(key)))
        return {"look": row.look if row is not None else {}}


@router.put("/{key}")
def put_look(key: str, body: LookIn, member: Member = Depends(sheet_viewer)) -> dict[str, Any]:
    """Вид не пишется в журнал действий: это не данные компании, а личная настройка экрана."""
    _guard()
    if len(json.dumps(body.look, ensure_ascii=False)) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Вид листа слишком большой — сбросьте часть оформления")
    with finance_session() as session:
        workspace = _workspace(session, member)
        row = session.get(SheetLook, (member.user_id, workspace.id, _key(key)))
        if row is None:
            row = SheetLook(user_id=member.user_id, workspace_id=workspace.id, key=key)
            session.add(row)
        row.look = body.look
        row.updated_at = datetime.now(timezone.utc)
        return {"ok": True}


@router.delete("/{key}")
def reset_look(key: str, member: Member = Depends(sheet_viewer)) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        row = session.get(SheetLook, (member.user_id, workspace.id, _key(key)))
        if row is not None:
            session.delete(row)
        return {"ok": True}
