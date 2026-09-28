"""HTTP-маршруты корзины «Финансов» (`/finance/trash…`).

Корзина — в личном кабинете, у владельца и администратора: восстановить
удалённое или удалить насовсем. Каждое действие — событие журнала: «кто
стёр договор насовсем» должно быть видно так же, как правка.

* 400 — нельзя (запись используется, уже не в корзине), с текстом;
* 403 — корзина открыта владельцу и администратору.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.api.routes.finance import _guard, _workspace, require_role
from app.finance import history, trash
from app.finance.auth import Member
from app.finance.db import finance_session

router = APIRouter(prefix="/finance/trash", tags=["finance-trash"])

trash_keeper = require_role("owner", "admin")


@router.get("")
def list_trash(member: Member = Depends(trash_keeper)) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        return trash.listing(session, _workspace(session, member))


@router.post("/{kind}/{item_id}/restore")
def restore_item(kind: str, item_id: UUID, member: Member = Depends(trash_keeper)) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            result = trash.restore(session, workspace, kind, item_id)
        except trash.TrashError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        history.write(
            session, workspace, kind=f"trash.restore.{kind}", entity=kind, entity_id=item_id,
            title=f"восстановлено из корзины: {result['title']}", after={"restored": True},
        )
        return result


@router.delete("/{kind}/{item_id}")
def purge_item(kind: str, item_id: UUID, member: Member = Depends(trash_keeper)) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            result = trash.purge(session, workspace, kind, item_id)
        except trash.TrashError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        history.write(
            session, workspace, kind=f"trash.purge.{kind}", entity=kind, entity_id=item_id,
            title=f"удалено насовсем: {result['title']}", before={"in_trash": True},
        )
        return result
