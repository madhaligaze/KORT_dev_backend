"""Участники и оплаты - вызовом «спросить» (этап 3, шаг 3: чтение всего, что пишется).

До шага API модели записывало участников и оплаты командами, а прочитать их
списком могло только маршрутами экранов («Участники», экран оплат): оболочка
на одних вызовах завела бы оплату и не смогла бы её показать.

* `participants` - стороны договоров: по любому написанию и БИН (`q`), все,
  наши юрлица или без БИН (`show`) - тот же список, что экран «Участники».
* `payments` - оплаты из журнала фактов за период (`since`, `until`): день,
  сумма, договор (у деления - его часть), плательщик, источник (выписка, 1С,
  KORT). `state`: разложенные по договорам, без договора или все. Без права
  настройки - оплаты своих договоров; оплаты без договора - владельцу и
  администратору, как экран оплат.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from app.finance.contracts.service import today
from app.finance.model import VERSION
from app.finance.model.context import Ctx
from app.finance.service import FinanceError

SHOWS = ("all", "own", "no_bin")
STATES = ("all", "placed", "without")
#: Откуда оплата: журнал денег KORT (выписка или руками) и решения KORT - «KORT», 1С - «1С».
SOURCE_TITLES = {"journal": "KORT", "1c": "1С", "kort": "KORT", "book": "таблица"}
LIMIT = 500


def _day(raw: Any, default: date) -> date:
    if raw in (None, ""):
        return default
    try:
        return raw if isinstance(raw, date) else date.fromisoformat(str(raw))
    except ValueError as exc:
        raise FinanceError("День - ГГГГ-ММ-ДД") from exc


def _limit(raw: Any, default: int) -> int:
    try:
        return max(1, min(LIMIT, int(raw or default)))
    except (TypeError, ValueError) as exc:
        raise FinanceError(f"Сколько строк - число от 1 до {LIMIT}") from exc


def participants(ctx: Ctx, raw: dict[str, Any]) -> dict[str, Any]:
    from app.finance.core import participants as core_participants

    if not ctx.access.view:
        raise PermissionError("Договоры вам не открыты")
    params = dict(raw.get("params") or {})
    show = str(params.get("show") or "all")
    if show not in SHOWS:
        raise FinanceError("Список - all, own или no_bin")
    q = str(params.get("q") or "")[:120]
    found = core_participants.listing(ctx.session, ctx.workspace, ctx.access, q=q, show=show, limit=_limit(params.get("limit"), 60))
    return {
        "version": VERSION,
        "question": {"source": "participants", "params": {key: value for key, value in (("q", q), ("show", show)) if value}},
        "as_of": today().isoformat(),
        "source": "participants",
        "columns": [
            {"key": "name", "title": "Участник", "type": "text"},
            {"key": "bin", "title": "БИН/ИИН", "type": "text"},
            {"key": "own", "title": "Наше юрлицо", "type": "text"},
            {"key": "contracts", "title": "Договоров", "type": "number"},
        ],
        "rows": found["items"],
        "totals": {"participants": found["total"]},
    }


def payments(ctx: Ctx, raw: dict[str, Any]) -> dict[str, Any]:
    from app.finance.contracts.models import Contract
    from app.finance.core import journal
    from app.finance.core.models import Fact
    from app.finance.core.shadow import _visible
    from app.finance.models import Counterparty

    if not ctx.access.view:
        raise PermissionError("Договоры вам не открыты")
    params = dict(raw.get("params") or {})
    now = _day(raw.get("as_of"), today())
    until = _day(params.get("until"), now)
    since = _day(params.get("since"), until.replace(day=1) - timedelta(days=0))
    if since > until:
        raise FinanceError("Период: «с» позже «по»")
    state = str(params.get("state") or "all")
    if state not in STATES:
        raise FinanceError("Оплаты - all, placed или without")
    if state == "without" and not ctx.access.setup:
        raise PermissionError("Оплаты без договора видят владелец и администратор")
    contract_filter = str(params.get("contract") or "")
    party_filter = str(params.get("party") or "")
    journal.ensure(ctx.session, ctx.workspace.id)
    query = sa.select(Fact).where(
        Fact.workspace_id == ctx.workspace.id, Fact.ended_seq.is_(None), Fact.void.is_(False), Fact.kind == "payment",
        Fact.day >= since, Fact.day <= until,
    )
    if contract_filter:
        query = query.where(Fact.contract_id == uuid.UUID(contract_filter))
    if party_filter:
        query = query.where(Fact.party_id == uuid.UUID(party_filter))
    if state == "placed":
        query = query.where(Fact.contract_id.is_not(None))
    elif state == "without":
        query = query.where(Fact.contract_id.is_(None))
    facts = list(ctx.session.scalars(query.order_by(Fact.day.desc(), Fact.key)))
    visible: set[uuid.UUID] | None = None
    if not ctx.access.setup:
        visible = {item.id for item in _visible(ctx.session, ctx.workspace, ctx.access)}
        facts = [fact for fact in facts if fact.contract_id is not None and fact.contract_id in visible]
    total = sum((fact.amount or Decimal("0") for fact in facts), Decimal("0"))
    limit = _limit(params.get("limit"), 200)
    shown = facts[:limit]
    contract_ids = {fact.contract_id for fact in shown if fact.contract_id}
    numbers = dict(ctx.session.execute(sa.select(Contract.id, Contract.number).where(Contract.id.in_(contract_ids))).all()) if contract_ids else {}
    party_ids = {fact.party_id for fact in shown if fact.party_id}
    names = dict(ctx.session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.id.in_(party_ids))).all()) if party_ids else {}
    rows = []
    for fact in shown:
        data = fact.data or {}
        rows.append({
            "key": fact.key,
            "day": fact.day.isoformat() if fact.day else None,
            "amount": format(fact.amount.quantize(Decimal("0.01")), "f") if fact.amount is not None else None,
            "contract": {"id": str(fact.contract_id), "number": numbers.get(fact.contract_id) or ""} if fact.contract_id else None,
            "party": names.get(fact.party_id, "") if fact.party_id else "",
            "direction": data.get("direction") or ("out" if (fact.amount or 0) < 0 else "in"),
            "source": fact.source,
            "source_title": SOURCE_TITLES.get(fact.source, fact.source),
            "months": data.get("months") or [],
            "part": ":" in fact.key.partition(":")[2],
            "comment": str(data.get("comment") or "")[:240],
        })
    return {
        "version": VERSION,
        "question": {"source": "payments", "params": {"since": since.isoformat(), "until": until.isoformat(), "state": state,
                                                       **({"contract": contract_filter} if contract_filter else {}),
                                                       **({"party": party_filter} if party_filter else {})}},
        "as_of": now.isoformat(),
        "source": "payments",
        "columns": [
            {"key": "day", "title": "День", "type": "date"},
            {"key": "party", "title": "Плательщик", "type": "text"},
            {"key": "contract", "title": "Договор", "type": "text"},
            {"key": "amount", "title": "Сумма", "type": "money"},
            {"key": "source_title", "title": "Откуда", "type": "text"},
        ],
        "rows": rows,
        "totals": {"payments": len(facts), "shown": len(shown), "amount": format(total.quantize(Decimal("0.01")), "f")},
    }


SOURCES = {"participants": participants, "payments": payments}

__all__ = ["SOURCES", "participants", "payments"]
