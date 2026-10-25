"""Первые шаги компании (фаза 9, R18 и R20): от сводной таблицы до «Долгов» за один заход.

Компания, которая ведёт учёт в сводной таблице (строка - договор за месяц, как
«Сводка все ЮР лица» у BBC), подключает её - и KORT заводит по ней модель
шагами. Каждый шаг до записи показывает числа, пишет журнал, а пачка
договоров и сальдо возвращается точкой восстановления:

1. **Наши юрлица** - коды колонки «Наша Фирма», которых нет у юрлиц KORT,
   заводятся юрлицами (имя - код; полное имя и БИН дописываются потом).
2. **Договоры** - строки таблицы без договора в KORT, однозначные (`missing`:
   такого номера нет ни у одного договора): номер, клиент, наше юрлицо,
   сумма последнего месяца, как начислять и день периода - как «Завести» в
   «Сверке» (`changes.contract_from_rows`). Спорные - номер у другого
   клиента, несколько кандидатов - не заводятся: их решает человек в
   «Сверке → Начисления → Нет в реестре».
3. **Начало учёта и сальдо** - оплаты ведутся с первого числа последнего
   наступившего месяца таблицы, где есть «Сальдо Начало (без АВР)», и
   входящее сальдо договоров на этот день переносится из таблицы
   (`core/opening.py`). С этого дня долг считает KORT: сальдо + начисления -
   оплаты. Начало, записанное раньше, шаг не меняет.
4. **Долги** - «Нам должны» сразу: то же число, что в разделе и на «Сегодня».

Слов BBC модуль не знает: коды юрлиц - из таблицы, колонки - словарём
колонок компании (`core/book.py`).
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts import restore
from app.finance.contracts import setup as contracts_setup
from app.finance.contracts.models import Contract
from app.finance.contracts.service import Access, Actor
from app.finance.core import opening as opening_module
from app.finance.core import settings as core_settings
from app.finance.core.accruals import month_key, month_start
from app.finance.layout import norm
from app.finance.models import Workspace

#: Сколько групп строк показать в плане: остальное - числом.
PREVIEW = 60
STEPS = ("entities", "contracts", "since")


def _require(access: Access) -> None:
    if not access.setup:
        raise PermissionError("Первые шаги проходит владелец или администратор")


def _money(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _entities(book: Any, entity_by_code: dict[str, Any]) -> list[dict[str, Any]]:
    """Коды «Наша Фирма» таблицы: сколько строк и есть ли такое юрлицо в KORT."""
    seen: dict[str, dict[str, Any]] = {}
    for row in book.rows:
        code = " ".join((row.own or "").split())
        if not code:
            continue
        item = seen.setdefault(norm(code), {"code": code, "rows": 0, "id": None})
        item["rows"] += 1
    for key, item in seen.items():
        entity = entity_by_code.get(key)
        item["id"] = str(entity) if entity is not None else None
    return sorted(seen.values(), key=lambda item: (-item["rows"], item["code"]))


def proposed_since(book: Any, today: date) -> date:
    """Первое число последнего наступившего месяца таблицы с «Сальдо Начало»; нет такого - текущего."""
    current = month_key(today)
    months = sorted({row.month for row in book.rows if row.month and row.month <= current and row.opening_accruals is not None})
    return month_start(months[-1] if months else current)


def plan(session: Session, workspace: Workspace, access: Access, book: Any, *, today: date) -> dict[str, Any]:
    """Что KORT заведёт по таблице - по шагам, с числами до записи."""
    from app.finance.core.debts import Debts
    from app.finance.core.shadow import Reconciler

    _require(access)
    reconciler = Reconciler(session, workspace, access, book)
    entities = _entities(book, reconciler.matcher.entity_by_code)
    groups = reconciler.unknown_groups()
    states = Counter(group["state"] for group in groups)
    missing = [group for group in groups if group["state"] == "missing"]
    matched_rows = sum(len(item.rows) for months in reconciler.grouped.values() for item in months.values())
    settings = core_settings.payments(session, workspace)
    since = settings.since or proposed_since(book, today)
    openings = opening_module.plan(session, workspace, access, book, since=since)
    contracts_total = session.scalar(
        sa.select(sa.func.count()).select_from(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
    ) or 0
    debts = Debts(session, workspace, access, day=today) if settings.since is not None else None
    totals = debts.totals() if debts is not None else None
    return {
        "book": {"title": book.title, "worksheet": book.worksheet, "rows": len(book.rows), "matched": matched_rows},
        "entities": {"items": entities, "missing": sum(1 for item in entities if item["id"] is None)},
        "contracts": {
            "total": contracts_total,
            "missing": len(missing),
            "missing_rows": sum(len(group["lines"]) for group in missing),
            "disputed": sum(count for state, count in states.items() if state != "missing"),
            "groups": [
                {key: group[key] for key in ("key", "own", "customer", "number", "months", "amount")} for group in missing[:PREVIEW]
            ],
        },
        "since": {
            "current": settings.since.isoformat() if settings.since else None,
            "proposed": proposed_since(book, today).isoformat(),
        },
        "openings": {
            "since": since.isoformat(),
            "new": openings["counts"].get("new", 0) + openings["counts"].get("changed", 0),
            "same": openings["counts"].get("same", 0) + openings["counts"].get("manual", 0),
            "debt": openings["debt"],
        },
        "debts": (
            {
                "owed": totals["owed"],
                "contracts": sum(1 for item in debts.items.values() if item.owed > 0),
                "advance": totals["advance"],
            }
            if debts is not None and totals is not None
            else None
        ),
    }


def add_entities(session: Session, workspace: Workspace, access: Access, actor: Actor, book: Any) -> dict[str, Any]:
    """Шаг 1: завести нашими юрлицами коды «Наша Фирма», которых в KORT нет."""
    from app.finance.core.book import Matcher

    _require(access)
    made: list[str] = []
    for item in _entities(book, Matcher(session, workspace.id).entity_by_code):
        if item["id"] is not None:
            continue
        contracts_setup.add_entity(session, workspace, name=item["code"], code=item["code"])
        made.append(item["code"])
    if made:
        session.flush()
        history.write(
            session, workspace, kind="core.start", entity="group_entities", entity_id=None,
            title=f"по сводной таблице заведены наши юрлица: {', '.join(made)}",
            after={"codes": made}, actor=actor.email,
        )
    return {"done": len(made), "codes": made}


def add_contracts(session: Session, workspace: Workspace, access: Access, actor: Actor, book: Any) -> dict[str, Any]:
    """Шаг 2: завести договоры по однозначным строкам без договора - пачкой, с точкой восстановления."""
    from app.finance.core import changes
    from app.finance.core.shadow import Reconciler

    _require(access)
    if not access.edit:
        raise PermissionError("Заводить договоры можно с правом правки реестра")
    reconciler = Reconciler(session, workspace, access, book)
    rows_by_key: dict[str, list[Any]] = {}
    for row, match in reconciler.unmatched:
        if match.state == "missing":
            rows_by_key.setdefault(reconciler.unknown_key(row), []).append(row)
    ids: list[str] = []
    for key in sorted(rows_by_key):
        rows = sorted(rows_by_key[key], key=lambda row: row.month)
        contract = changes.contract_from_rows(session, workspace, access, actor, rows, entity_by_code=reconciler.matcher.entity_by_code)
        ids.append(str(contract.id))
    if not ids:
        return {"done": 0, "point": None}
    title = f"по сводной таблице заведено договоров: {len(ids)}"
    point = restore._point(session, workspace, actor, "contracts_create", title, {"ids": ids})
    history.write(
        session, workspace, kind="core.start", entity="contracts", entity_id=None,
        title=title, after={"count": len(ids)}, actor=actor.email,
    )
    return {"done": len(ids), "point": restore._point_ref(point)}


def start_since(
    session: Session, workspace: Workspace, access: Access, actor: Actor, book: Any, *, today: date, since: date | None = None
) -> dict[str, Any]:
    """Шаг 3: начать учёт оплат (если не начат) и перенести входящее сальдо из таблицы на этот день."""
    _require(access)
    settings = core_settings.payments(session, workspace)
    day = settings.since
    if day is None:
        day = since or proposed_since(book, today)
        core_settings.set_payments(session, workspace, access, actor, since=day)
    moved = opening_module.apply(session, workspace, access, actor, book, since=day)
    return {"since": day.isoformat(), **moved}


__all__ = ["PREVIEW", "STEPS", "add_contracts", "add_entities", "plan", "proposed_since", "start_since"]
