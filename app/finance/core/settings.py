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

Словарь колонок сводной таблицы (`book_columns`, фаза 9, R14)
──────────────────────────────────────────────────────────────
Ядро читает сводную таблицу по названиям колонок (`core/book.py`, `COLUMNS`),
и эти названия - шапка таблицы BBC. Другая компания назовёт «Сумма Договора»
«Сумма по договору» - и лист не прочитается. Словарь - названия колонок
компании, добавленные к названиям ядра: ключ колонки → написания. Только
названия, не номера (правило проекта «колонки - по названиям»).

Разметка таблиц (`intake`, этап 2, шаг «Excel любой формы»)
────────────────────────────────────────────────────────────
Таблицу отдела, которую KORT не узнал сам, человек размечает один раз: что
значит строка, что значит каждая колонка, порядок частей даты, чьи это
договоры. `words` - словарь компании по формам (ключ колонки → шапки): в
любом следующем файле с такой шапкой колонка узнаётся без вопроса.
`layouts` - разметка листа целиком по подписи его шапки (`intake/layout.
signature` - набор названий, не порядок: «Июль» с переставленными
колонками - тот же лист).
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


def book_columns(session: Session, workspace_id: uuid.UUID) -> dict[str, list[str]]:
    """Словарь колонок компании: ключ колонки сводной таблицы → её написания у компании."""
    row = session.get(CoreSettings, workspace_id)
    raw = ((row.data if row is not None else None) or {}).get("book_columns") or {}
    return {str(key): [str(name) for name in names if str(name).strip()] for key, names in raw.items() if isinstance(names, list)}


def set_book_column(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *, key: str, name: str, on: bool, known: dict[str, str]
) -> dict[str, list[str]]:
    """Добавить (`on`) или убрать написание колонки сводной таблицы. `known` - ключи ядра → подписи."""
    if not access.setup:
        raise PermissionError("Словарь колонок меняет владелец или администратор")
    if key not in known:
        raise FinanceError("Такой колонки у ядра нет")
    clean = " ".join(str(name or "").split())
    if not clean or len(clean) > 200:
        raise FinanceError("Название колонки - как в шапке таблицы, до 200 знаков")
    row = _row(session, workspace)
    if row is None:
        row = CoreSettings(workspace_id=workspace.id, data={})
        session.add(row)
    data = dict(row.data or {})
    columns = {column: list(names) for column, names in (data.get("book_columns") or {}).items()}
    before = list(columns.get(key, []))
    items = [item for item in before if norm(item) != norm(clean)]
    if on:
        items.append(clean)
    if items:
        columns[key] = items
    else:
        columns.pop(key, None)
    data["book_columns"] = columns
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
        title=f"колонка «{known[key]}» сводной таблицы {'читается и как' if on else 'больше не читается как'} «{clean}»",
        before={"book_columns": {key: before}},
        after={"book_columns": {key: items}},
        actor=actor.email,
    )
    return book_columns(session, workspace.id)


def payments_out(value: Payments, *, since_used: date | None) -> dict[str, Any]:
    return {
        "since": value.since.isoformat() if value.since else None,
        "since_used": since_used.isoformat() if since_used else None,
        "sources": {str(key): item for key, item in value.sources.items()},
        "ignore": {kind: list(value.ignore.get(kind, ())) for kind in IGNORE_KINDS},
    }


# ── Слова компании в колонках таблицы (R12, R13) ────────────────────────────
#
# Значения колонки «Счет» пишет формула таблицы своими словами («Просрочка
# более 5 дней», «Еще рано», TRUE у BBC), а «Вид Услуги» - человек («Абон.П.»,
# «Аренда»). Слова BBC KORT знает сам (`contracts/bills.state_of`,
# `bills_bot.kind_of`); слово другой компании получает смысл здесь - и
# список «Не выставлены», бот и шаблон понимают его так же.

#: Смыслы слова колонки «Счет»: выставлен, просрочен, скоро, ещё рано, без срока.
BILL_MEANINGS = ("issued", "overdue", "soon", "early", "undated")
#: Смыслы слова «Вида Услуги» для постов бота: абонентская плата, аренда, не присылать.
KIND_MEANINGS = ("abon", "rent", "skip")
WORD_PARTS = {"bill": BILL_MEANINGS, "kind": KIND_MEANINGS}
WORD_TITLES = {
    "issued": "выставлен",
    "overdue": "просрочен",
    "soon": "скоро",
    "early": "ещё рано",
    "undated": "без срока",
    "abon": "абонентская плата",
    "rent": "аренда",
    "skip": "не присылать",
}
PART_TITLES = {"bill": "«Счет»", "kind": "«Вид Услуги»"}


def table_words(session: Session, workspace_id: uuid.UUID) -> dict[str, dict[str, str]]:
    """Слова компании: колонка → слово без оформления (`norm`) → смысл."""
    row = session.get(CoreSettings, workspace_id)
    raw = ((row.data if row is not None else None) or {}).get("table_words") or {}
    return {
        part: {norm(word): meaning for word, meaning in (raw.get(part) or {}).items() if meaning in meanings}
        for part, meanings in WORD_PARTS.items()
    }


def set_table_word(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *, part: str, word: str, meaning: str
) -> dict[str, dict[str, str]]:
    """Дать слову колонки смысл (`meaning` пусто - снять, тогда снова как KORT понимает сам)."""
    if not access.setup:
        raise PermissionError("Слова таблицы задаёт владелец или администратор")
    if part not in WORD_PARTS:
        raise FinanceError("Такой колонки в словаре нет")
    if meaning and meaning not in WORD_PARTS[part]:
        raise FinanceError("Такого смысла у этой колонки нет")
    key = norm(word)
    if not key or len(key) > 200:
        raise FinanceError("Слово - как в таблице, до 200 знаков")
    row = _row(session, workspace)
    if row is None:
        row = CoreSettings(workspace_id=workspace.id, data={})
        session.add(row)
    data = dict(row.data or {})
    words = {name: dict(items) for name, items in (data.get("table_words") or {}).items()}
    items = words.setdefault(part, {})
    before = items.get(key, "")
    if meaning:
        items[key] = meaning
    else:
        items.pop(key, None)
    data["table_words"] = words
    row.data = data
    row.updated_by = actor.user_id
    row.updated_at = datetime.now(timezone.utc)
    session.flush()
    shown = " ".join(str(word).split())
    history.write(
        session,
        workspace,
        kind="core.settings",
        entity="core_settings",
        entity_id=None,
        title=(
            f"слово «{shown}» колонки {PART_TITLES[part]} - {WORD_TITLES[meaning]}"
            if meaning
            else f"слово «{shown}» колонки {PART_TITLES[part]} - снова как KORT понимает сам"
        ),
        before={"table_words": {part: {key: before}}},
        after={"table_words": {part: {key: meaning}}},
        actor=actor.email,
    )
    return table_words(session, workspace.id)


__all__ = [
    "BILL_MEANINGS",
    "IGNORE_KINDS",
    "KIND_MEANINGS",
    "PAYMENT_SOURCES",
    "Payments",
    "SOURCE_TITLES",
    "WORD_TITLES",
    "book_columns",
    "payments",
    "payments_out",
    "set_book_column",
    "set_ignore",
    "set_payments",
    "set_table_word",
    "table_words",
]


# ── Разметка таблиц (этап 2, «Excel любой формы») ───────────────────────────

#: Сколько разметок держит компания: таблиц отделов - десятки, не тысячи.
LAYOUTS_MAX = 500


def intake_words(session: Session, workspace_id: uuid.UUID) -> dict[str, dict[str, list[str]]]:
    """Словарь компании для приёмника: форма → ключ колонки → шапки, назначенные человеком."""
    row = session.get(CoreSettings, workspace_id)
    raw = (((row.data if row is not None else None) or {}).get("intake") or {}).get("words") or {}
    return {form: {key: [str(name) for name in names] for key, names in (items or {}).items()} for form, items in raw.items()}


def intake_layouts(session: Session, workspace_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """Разметки листов по подписи шапки."""
    row = session.get(CoreSettings, workspace_id)
    return dict((((row.data if row is not None else None) or {}).get("intake") or {}).get("layouts") or {})


def remember_layout(
    session: Session, workspace: Workspace, access: Access, actor: Actor, signature: str, layout: dict[str, Any]
) -> None:
    """Запомнить разметку листа и шапки её колонок - в словарь компании (повтор ничего не пишет)."""
    if not access.edit:
        raise PermissionError("Размечать таблицы можно с правом правки реестра")
    row = _row(session, workspace)
    if row is None:
        row = CoreSettings(workspace_id=workspace.id, data={})
        session.add(row)
    data = dict(row.data or {})
    section = dict(data.get("intake") or {})
    layouts = dict(section.get("layouts") or {})
    words = {form: {key: list(names) for key, names in items.items()} for form, items in (section.get("words") or {}).items()}
    form = str(layout.get("form") or "")
    added: list[str] = []
    for header, key in (layout.get("columns") or {}).items():
        name = norm(header)
        if not key or not name or len(name) > 200:
            continue
        names = words.setdefault(form, {}).setdefault(str(key), [])
        if name not in names:
            names.append(name)
            added.append(name)
    if layouts.get(signature) == layout and not added:
        return
    layouts[signature] = layout
    if len(layouts) > LAYOUTS_MAX:
        raise FinanceError("Разметок таблиц слишком много - уберите старые")
    section["layouts"], section["words"] = layouts, words
    data["intake"] = section
    row.data = data
    row.updated_by = actor.user_id
    row.updated_at = datetime.now(timezone.utc)
    session.flush()
    history.write(
        session, workspace, kind="core.settings", entity="core_settings", entity_id=None,
        title=f"разметка листа «{layout.get('sheet') or '-'}»" + (f": колонки {', '.join(f'«{name}»' for name in added[:6])}" if added else ""),
        after={"intake": {"layout": signature, "words": added}}, actor=actor.email,
    )
