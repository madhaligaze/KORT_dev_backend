"""«Найти или сделать» (фаза 8): договоры по номеру и клиенту - для окна Ctrl+K.

Участников ищет `core/participants.py` (по любому написанию и началу БИН), разделы
и действия - сам экран. Здесь - договоры, открытые человеку: номер так, как его
пишут люди («ОБО-62», «№ОБО/62», «обо62» - один номер), и клиент по имени без
кавычек и формы. Сначала точный номер, потом номер с начала, потом внутри
номера, потом клиент: «62» не должен прятать «ОБО/62» за десятком «ЮО/620».
"""
from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts.fields import number_key, party_key
from app.finance.contracts.models import GroupEntity, ListValue
from app.finance.contracts.service import Access
from app.finance.models import Counterparty, Workspace
from app.finance.onec.match import number_loose

LIMIT = 8


def contracts(session: Session, workspace: Workspace, access: Access, query: str, *, limit: int = LIMIT) -> list[dict[str, Any]]:
    from app.finance.core.shadow import _visible

    text = (query or "").strip()
    if len(text) < 2:
        return []
    key, loose, party = number_key(text), number_loose(text), party_key(text)
    names = dict(session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace.id)).all())
    codes = {
        entity_id: code
        for entity_id, code in session.execute(
            sa.select(GroupEntity.counterparty_id, GroupEntity.code).where(GroupEntity.workspace_id == workspace.id)
        )
        if code
    }
    statuses = dict(
        session.execute(sa.select(ListValue.id, ListValue.value).where(ListValue.workspace_id == workspace.id, ListValue.field_key == "status")).all()
    )
    ranked: list[tuple[int, str, Any]] = []
    for contract in _visible(session, workspace, access):
        own_key, own_loose = number_key(contract.number), number_loose(contract.number)
        customer = names.get(contract.customer_id, "") if contract.customer_id else ""
        if key and own_key == key or loose and own_loose == loose:
            rank = 0
        elif key and own_key.startswith(key) or loose and own_loose.startswith(loose):
            rank = 1
        elif key and key in own_key or loose and len(loose) >= 2 and loose in own_loose:
            rank = 2
        elif party and party in party_key(customer):
            rank = 3
        else:
            continue
        ranked.append((rank, own_key, contract))
    ranked.sort(key=lambda item: (item[0], len(item[1]), item[1]))
    out = []
    for rank, _key, contract in ranked[:limit]:
        out.append(
            {
                "id": str(contract.id),
                "number": contract.number,
                "customer": names.get(contract.customer_id, "") if contract.customer_id else "",
                "executor": (codes.get(contract.executor_id) or names.get(contract.executor_id, "")) if contract.executor_id else "",
                "status": statuses.get(contract.status_id, "") if contract.status_id else "",
                "by": "number" if rank < 3 else "client",
            }
        )
    return out


__all__ = ["LIMIT", "contracts"]
