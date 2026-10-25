"""Настройки ядра компании: решения человека, которых нет в данных.

Оплаты (`payments`)
───────────────────
* `since` - с какого дня KORT ведёт оплаты. Начисления раньше этого дня
  оплат в KORT не ждут: долг на этот день - входящее сальдо договора
  (`core/opening.py`). Не задано - первый день месяца самой ранней оплаты
  источника: раньше оплат в KORT нет, и всё, что начислено до неё, выглядело
  бы неоплаченным и забирало бы новые платежи себе.
* `sources` - откуда оплаты нашего юрлица: `journal` (журнал движения денег -
  выписки, журнал ГК) или `1c` (поступления в базе 1С юрлица). Не задано -
  журнал. Источник один на юрлицо: оплата из выписки и та же оплата из 1С -
  одна оплата, и сложить обе значило бы удвоить.
* `ignore` - что не оплаты по договорам: статьи («Перемещения») и плательщики
  («Вознаграждение» - проценты по депозиту) целиком. Иначе в «Оплатах без
  договора» на копии прода лежали 2 828 поступлений, из них 1 442 - проценты,
  и настоящие оплаты клиентов в них терялись (25.10.2026).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts.models import GroupEntity
from app.finance.contracts.service import Access, Actor
from app.finance.core.models import CoreSettings
from app.finance.layout import norm
from app.finance.models import Workspace
from app.finance.service import FinanceError

PAYMENT_SOURCES = ("journal", "1c")
SOURCE_TITLES = {"journal": "журнал", "1c": "1С"}
#: Что можно целиком отметить «не оплаты по договорам».
IGNORE_KINDS = ("categories", "payers")


@dataclass(frozen=True)
class Payments:
    #: Начало учёта оплат, заданное человеком; `None` - по первой оплате.
    since: date | None = None
    #: Наше юрлицо → источник оплат; нет в словаре - журнал.
    sources: dict[uuid.UUID, str] = field(default_factory=dict)
    #: Статьи и плательщики - «не оплаты по договорам», как написал человек.
    ignore: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def source_of(self, entity_id: uuid.UUID | None) -> str:
        return self.sources.get(entity_id, "journal") if entity_id else "journal"

    def ignored(self, kind: str) -> set[str]:
        """Ключи без оформления - сравнивать с `norm` имени."""
        return {norm(item) for item in self.ignore.get(kind, ())}


def _row(session: Session, workspace: Workspace) -> CoreSettings | None:
    return session.get(CoreSettings, workspace.id)


def payments(session: Session, workspace: Workspace) -> Payments:
    data = ((_row(session, workspace) or CoreSettings(data={})).data or {}).get("payments") or {}
    since = None
    if data.get("since"):
        try:
            since = date.fromisoformat(str(data["since"]))
        except ValueError:
            since = None
    sources: dict[uuid.UUID, str] = {}
    for key, value in (data.get("sources") or {}).items():
        try:
            if value in PAYMENT_SOURCES:
                sources[uuid.UUID(str(key))] = value
        except ValueError:
            continue
    ignore = {
        kind: tuple(str(item) for item in ((data.get("ignore") or {}).get(kind) or []) if str(item).strip())
        for kind in IGNORE_KINDS
    }
    return Payments(since=since, sources=sources, ignore=ignore)


def _require_setup(access: Access) -> None:
    if not access.setup:
        raise PermissionError("Настройки учёта оплат меняет владелец или администратор")


def set_payments(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    *,
    since: date | None | str = "keep",
    entity_id: uuid.UUID | None = None,
    source: str | None = None,
) -> Payments:
    """Записать начало учёта оплат (`since=None` - снять) и/или источник оплат юрлица."""
    _require_setup(access)
    row = _row(session, workspace)
    if row is None:
        row = CoreSettings(workspace_id=workspace.id, data={})
        session.add(row)
    data = dict(row.data or {})
    section = dict(data.get("payments") or {})
    before = dict(section)
    words: list[str] = []
    if since != "keep":
        if since is not None and not isinstance(since, date):
            raise FinanceError("Начало учёта оплат - это дата")
        section["since"] = since.isoformat() if isinstance(since, date) else None
        words.append(f"оплаты ведутся с {since:%d.%m.%Y}" if isinstance(since, date) else "начало учёта оплат - по первой оплате")
    if entity_id is not None:
        if source not in PAYMENT_SOURCES:
            raise FinanceError("Оплаты юрлица - из журнала или из 1С")
        entity = session.get(GroupEntity, entity_id)
        if entity is None or entity.workspace_id != workspace.id:
            raise FinanceError("Такого нашего юрлица нет")
        sources = dict(section.get("sources") or {})
        if source == "journal":
            sources.pop(str(entity_id), None)
        else:
            sources[str(entity_id)] = source
        section["sources"] = sources
        words.append(f"оплаты {entity.code or 'юрлица'} - из {'журнала' if source == 'journal' else '1С'}")
    data["payments"] = section
    row.data = data
    row.updated_by = actor.user_id
    row.updated_at = datetime.now(timezone.utc)
    session.flush()
    history.write(
        session,
        workspace,
        kind="core.settings",
        entity="core_settings",
        entity_id=None,
        title="; ".join(words) or "настройки оплат",
        before={"payments": before},
        after={"payments": section},
        actor=actor.email,
    )
    return payments(session, workspace)


def set_ignore(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *, kind: str, name: str, on: bool
) -> Payments:
    """Статья или плательщик целиком - «не оплаты по договорам» (`on`) или снова оплаты."""
    _require_setup(access)
    if kind not in IGNORE_KINDS:
        raise FinanceError("Не оплаты по договорам - статья или плательщик")
    clean = " ".join(str(name or "").split())
    if not clean:
        raise FinanceError("Пустое имя")
    row = _row(session, workspace)
    if row is None:
        row = CoreSettings(workspace_id=workspace.id, data={})
        session.add(row)
    data = dict(row.data or {})
    section = dict(data.get("payments") or {})
    ignore = {key: list(value) for key, value in (section.get("ignore") or {}).items()}
    items = [item for item in ignore.get(kind, []) if norm(item) != norm(clean)]
    if on:
        items.append(clean)
    before = list(ignore.get(kind, []))
    ignore[kind] = items
    section["ignore"] = ignore
    data["payments"] = section
    row.data = data
    row.updated_by = actor.user_id
    row.updated_at = datetime.now(timezone.utc)
    session.flush()
    what = "статья" if kind == "categories" else "плательщик"
    history.write(
        session,
        workspace,
        kind="core.settings",
        entity="core_settings",
        entity_id=None,
        title=f"{what} «{clean}» - {'не оплаты по договорам' if on else 'снова оплаты по договорам'}",
        before={kind: before},
        after={kind: items},
        actor=actor.email,
    )
    return payments(session, workspace)


def payments_out(value: Payments, *, since_used: date | None) -> dict[str, Any]:
    return {
        "since": value.since.isoformat() if value.since else None,
        "since_used": since_used.isoformat() if since_used else None,
        "sources": {str(key): item for key, item in value.sources.items()},
        "ignore": {kind: list(value.ignore.get(kind, ())) for kind in IGNORE_KINDS},
    }


__all__ = [
    "IGNORE_KINDS",
    "PAYMENT_SOURCES",
    "Payments",
    "SOURCE_TITLES",
    "payments",
    "payments_out",
    "set_ignore",
    "set_payments",
]
