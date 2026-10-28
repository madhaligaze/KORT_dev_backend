"""«Оплачено (сводка)» - факт оплаты договора из книги-сводки компании.

Как это было устроено в таблицах копии прода (разобрано 27.09.2026)
──────────────────────────────────────────────────────────────────
Главная таблица - сводная: туда `IMPORTRANGE` сводит таблицы восьми юрлиц,
строка - договор за месяц. Таблица юротдела «Разовые» держит скрытый лист
«Сводка ЮО» (та же сводная, отдел ЮО) и колонку «Оплачено» формулой:

    VLOOKUP(Заказчик & № договора & код фирмы; 'Сводка ЮО'!A:AD; поступило за месяц)

где код фирмы - код нашего юрлица из справочника исполнителей, а колонка A
сводной - «Заказчик & №» и код юрлица. Остаток = сумма договора − оплачено.

Что здесь по-другому и почему
─────────────────────────────
* **Колонки - по названиям** (`app.finance.layout`), а не по буквам: в сводку
  вставляют колонки, и прибитая «AD» однажды тихо читала бы соседнюю.
* **Ключ - номер договора, потом клиент.** Склейка строк ломается на любом
  разночтении: в реестре «ТОО Бухгалтерская Фирма Ajour», в сводке
  «Бухгалтерская фирма «Ajour»», и `VLOOKUP` отвечал бы нулём - а ноль в
  «Оплачено» выглядит как «не платили». Поэтому: строки с тем же номером
  (`number_key`), среди них - клиент того же ключа (`party_key`) или одно
  имя внутри другого (не короче пяти знаков), или подтверждённое написание.
* **Не угадывать.** Номер есть, а клиент другой - не наш договор («номер
  совпал, клиент другой»). Подходят два разных клиента - спорно, в
  «Оплачено» не идёт ничего. Код фирмы, если он записан у нашего юрлица,
  сужает выбор, но не заменяет клиента.
* **Строк несколько - сумма.** Книга брала первую найденную строку; у
  разового договора она одна, у растянутого по месяцам - оплаты в каждой.

Суммы не хранятся: книгу читает сервер и держит ответ в памяти
(`CACHE_TTL`), как лист дашборда. Квота Google - 60 чтений в минуту на
аккаунт, поэтому чтение одно на компанию за раз и не чаще срока кэша.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.core.columns import column, with_company_names
from app.finance.layout import Column, LayoutError, resolve_layout
from app.finance import google
from app.finance.contracts.fields import number_key, party_key
from app.finance.contracts.models import SummarySource

log = logging.getLogger(__name__)

#: Сколько держится прочитанная сводка. Две минуты (до 29.09.2026 - пять):
#: подпись «откуда оплата» и время чтения над «Разовыми» убраны, осталась
#: одна кнопка «Обновить», и сводка должна свежеть сама - лист спрашивает её
#: раз в минуту, а книгу сервер перечитывает фоном по истечении срока. Одно
#: чтение в две минуты на компанию квоте Google (60 в минуту) не мешает.
CACHE_TTL = 120.0
#: Через сколько повторять чтение, которое кончилось отказом.
ERROR_RETRY = 60.0
#: Сколько ждать Google на первом чтении, прежде чем ответить «читаем».
FIRST_READ_WAIT = 20.0
#: Короче - не ищем одно имя внутри другого: «ТОО А» нашлось бы везде.
MIN_CONTAINED = 5

#: Колонки сводки - ключи ядра (`core/columns.py`); шапка компании - её
#: словарь колонок.
COLUMNS: tuple[Column, ...] = (
    column("customer", required=True, strict=False),
    column("number", required=True, strict=False),
    column("paid", required=True, strict=False),
    column("own", strict=False),
    column("month", strict=False),
    column("department", strict=False),
    column("amount", strict=False),
)


class SummaryError(Exception):
    """Сводку нельзя прочитать - с текстом для человека."""


class SummaryLayoutError(SummaryError):
    """Таблица читается, но нужные колонки названы по-своему: их называют в
    «Колонках для ядра» (словарь колонок компании), и таблица читается дальше."""


def columns_for(extra: dict[str, list[str]] | None) -> tuple[Column, ...]:
    """Колонки сводки со своими названиями компании - тот же словарь, что у ядра и «Счетов»."""
    return with_company_names(COLUMNS, extra) if extra else COLUMNS


@dataclass(frozen=True)
class Row:
    customer: str
    customer_key: str
    number: str
    own: str
    paid: Decimal
    month: str
    department: str


@dataclass(frozen=True)
class Match:
    """Что сводка знает о договоре.

    `state`: `found` - строки нашлись; `missing` - номера в сводке нет;
    `other_client` - номер есть, клиент другой; `ambiguous` - подходят разные
    клиенты, и выбрать нельзя.
    """

    state: str
    paid: Decimal | None = None
    months: tuple[str, ...] = ()
    rows: int = 0
    customer: str = ""
    candidates: tuple[str, ...] = ()
    #: `kort` - оплаты юрлица ведёт KORT («Сверка → Переход»): оплачено - из разнесённых
    #: оплат, а не из колонки таблицы.
    source: str = ""


@dataclass
class Index:
    rev: str
    read_at: datetime
    title: str
    worksheet: str
    by_number: dict[str, list[Row]] = field(default_factory=dict)
    rows: int = 0
    drift: str = ""

    def _groups(
        self, number: str | None, customer_names: Iterable[str], own_code: str
    ) -> tuple[dict[str, list[Row]], list[list[Row]]]:
        """Строки номера по клиентам и те группы, чей клиент - этот договор."""
        key = number_key(number)
        rows = self.by_number.get(key, []) if key else []
        code = (own_code or "").strip().lower()
        if code and any(row.own.strip().lower() == code for row in rows):
            rows = [row for row in rows if row.own.strip().lower() == code]
        groups: dict[str, list[Row]] = {}
        for row in rows:
            groups.setdefault(row.customer_key, []).append(row)
        names = [name for name in customer_names if name]
        good = [group for group_key, group in groups.items() if any(_same_client(group_key, name) for name in names)]
        return groups, good

    def rows_of(self, number: str | None, customer_names: Iterable[str], own_code: str = "") -> list[Row]:
        """Строки, из которых сложилось «Оплачено» договора (`match`), - для «объяснить»."""
        _groups, good = self._groups(number, customer_names, own_code)
        return list(good[0]) if len(good) == 1 else []

    def match(
        self,
        number: str | None,
        customer_names: Iterable[str],
        own_code: str = "",
    ) -> Match:
        groups, good = self._groups(number, customer_names, own_code)
        if not groups:
            return Match("missing")
        if len(good) == 1:
            chosen = good[0]
            months = tuple(dict.fromkeys(row.month for row in chosen if row.month))
            return Match(
                "found",
                paid=sum((row.paid for row in chosen), Decimal("0")),
                months=months,
                rows=len(chosen),
                customer=chosen[0].customer,
            )
        if len(good) > 1:
            return Match("ambiguous", candidates=tuple(group[0].customer for group in good))
        return Match("other_client", candidates=tuple(group[0].customer for group in groups.values()))


def _same_client(summary_key: str, name: str) -> bool:
    mine = party_key(name)
    if not mine or not summary_key:
        return False
    if mine == summary_key:
        return True
    short, long = sorted((mine, summary_key), key=len)
    return len(short) >= MIN_CONTAINED and short in long


def read_money(text: Any) -> Decimal:
    """«464 000», «1 234,50», «-» → число; пусто и прочерк - ноль (как у книги)."""
    raw = str(text or "").replace(" ", "").replace(" ", "").replace(" ", "").strip()
    # Прочерк в книге пишут и дефисом, и длинным тире (\u2014): это чужие
    # данные, поэтому тире здесь остаётся - экранированным (CLAUDE.md).
    if not raw or raw in ("-", "\u2014"):
        return Decimal("0")
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()")
    if "," in raw and "." not in raw:
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return Decimal("0")
    return -value if negative else value


def build_index(
    values: Sequence[Sequence[Any]], *, title: str, worksheet: str, columns: Sequence[Column] = COLUMNS
) -> Index:
    """Разобрать значения листа сводки. Шапка - первая строка."""
    if not values:
        raise SummaryError(f"Лист «{worksheet}» пуст")
    try:
        layout = resolve_layout(worksheet, columns, [str(cell) for cell in values[0]])
    except LayoutError as exc:
        raise SummaryLayoutError(str(exc)) from exc
    by_number: dict[str, list[Row]] = {}
    digest = hashlib.sha1()
    count = 0
    for raw in values[1:]:
        row = [str(cell) for cell in raw]
        number = layout.cell(row, "number").strip()
        customer = layout.cell(row, "customer").strip()
        key = number_key(number)
        if not key or not customer:
            continue
        item = Row(
            customer=customer,
            customer_key=party_key(customer),
            number=number,
            own=layout.cell(row, "own").strip(),
            paid=read_money(layout.cell(row, "paid")),
            month=layout.cell(row, "month").strip(),
            department=layout.cell(row, "department").strip(),
        )
        by_number.setdefault(key, []).append(item)
        digest.update(f"{key}|{item.customer_key}|{item.own}|{item.paid}|{item.month}\n".encode())
        count += 1
    return Index(
        rev=digest.hexdigest()[:16],
        read_at=datetime.now(timezone.utc),
        title=title,
        worksheet=worksheet,
        by_number=by_number,
        rows=count,
        drift=layout.describe_drift(),
    )


# ── Кэш ─────────────────────────────────────────────────────────────────────


@dataclass
class _Slot:
    #: Таблица, лист и подпись словаря колонок компании: новое название колонки - перечитать.
    source: tuple[str, ...]
    index: Index | None = None
    error: str = ""
    at: float = 0.0
    reading: threading.Event | None = None
    extra: dict[str, list[str]] = field(default_factory=dict)


_slots: dict[uuid.UUID, _Slot] = {}
_guard = threading.Lock()


def source_of(session: Session, workspace_id: uuid.UUID) -> SummarySource | None:
    return session.get(SummarySource, workspace_id)


def _read(spreadsheet_id: str, worksheet: str, extra: dict[str, list[str]] | None = None) -> Index:
    if not google.is_configured():
        raise SummaryError("Не заданы креды Google - сводку прочитать нечем")
    try:
        meta = google.spreadsheet_meta(spreadsheet_id)
        values = google.fetch_tab_values(spreadsheet_id, worksheet, fresh=True)
    except google.GoogleError as exc:
        raise SummaryError(str(exc)) from exc
    return build_index(values, title=meta.get("title", ""), worksheet=worksheet, columns=columns_for(extra))


def _read_with(spreadsheet_id: str, worksheet: str, extra: dict[str, list[str]]) -> Index:
    # Без своих названий колонок - прежний вызов в два аргумента (его подменяют тесты).
    return _read(spreadsheet_id, worksheet, extra) if extra else _read(spreadsheet_id, worksheet)


def _key(session: Session, workspace_id: uuid.UUID, source: SummarySource) -> tuple[tuple[str, ...], dict[str, list[str]]]:
    """Ключ чтения: таблица, лист и подпись словаря колонок компании - и сам словарь."""
    from app.finance.core import book as core_book
    from app.finance.core import settings as core_settings

    extra = core_settings.book_columns(session, workspace_id)
    return (source.spreadsheet_id, source.worksheet, core_book.signature(extra)), extra


def _refresh(workspace_id: uuid.UUID, slot: _Slot, done: threading.Event) -> None:
    try:
        index = _read_with(slot.source[0], slot.source[1], slot.extra)
        with _guard:
            slot.index, slot.error = index, ""
    except SummaryError as exc:
        with _guard:
            slot.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - чтение книги не должно ронять реестр
        log.exception("сводка не прочиталась")
        with _guard:
            slot.error = f"Сводка не прочиталась: {exc}"
    finally:
        with _guard:
            slot.at = time.monotonic()
            slot.reading = None
        done.set()


def _start(
    workspace_id: uuid.UUID, source: tuple[str, ...], extra: dict[str, list[str]] | None = None
) -> tuple[_Slot, threading.Event]:
    """Слот компании и событие текущего чтения (начатого сейчас или раньше)."""
    with _guard:
        slot = _slots.get(workspace_id)
        if slot is None or slot.source != source:
            slot = _slots[workspace_id] = _Slot(source=source, extra=dict(extra or {}))
        age = time.monotonic() - slot.at
        # Отказ Google (нет доступа, квота) не повторяется на каждый запрос:
        # раз в минуту, иначе ошибка съела бы квоту всего аккаунта.
        stale = age >= (ERROR_RETRY if slot.index is None and slot.error else CACHE_TTL) or (
            slot.index is None and not slot.error
        )
        if slot.reading is not None:
            return slot, slot.reading
        if not stale:
            done = threading.Event()
            done.set()
            return slot, done
        done = slot.reading = threading.Event()
    threading.Thread(target=_refresh, args=(workspace_id, slot, done), daemon=True).start()
    return slot, done


def get(session: Session, workspace_id: uuid.UUID, *, wait: float = FIRST_READ_WAIT, force: bool = False) -> _Slot | None:
    """Сводка компании: прочитанная, при необходимости - перечитанная.

    Первое чтение ждём до `wait` секунд; дальше устаревшая сводка отдаётся
    сразу, а свежая читается в фоне. `None` - сводка не подключена.
    """
    source = source_of(session, workspace_id)
    if source is None:
        return None
    key, extra = _key(session, workspace_id, source)
    if force:
        with _guard:
            slot = _slots.get(workspace_id)
            if slot is not None and slot.reading is None:
                slot.at = 0.0
    slot, done = _start(workspace_id, key, extra)
    if slot.index is None and wait > 0:
        done.wait(wait)
    return slot


def peek(workspace_id: uuid.UUID) -> Index | None:
    """Уже прочитанная сводка - без похода в Google (сборка реестра не ждёт сеть)."""
    with _guard:
        slot = _slots.get(workspace_id)
        return slot.index if slot is not None else None


def forget(workspace_id: uuid.UUID) -> None:
    with _guard:
        _slots.pop(workspace_id, None)


#: Чаще этого «Обновить» книгу не перечитывает: квота Google общая на аккаунт.
FORCE_GAP = 30.0


def contracts_out(session: Session, workspace: Any, access: Any, *, force: bool = False) -> dict[str, Any]:
    """«Оплачено/Остаток (сводка)» видимых договоров и откуда цифры.

    `state` у договора: `found` / `missing` / `other_client` / `ambiguous`
    (`Match`). Остаток - сумма договора минус оплачено, как в книге
    «Разовые»; у договора без суммы его нет.
    """
    from app.finance.contracts.models import Contract
    from app.finance.contracts.service import Registry, people_of, visible_to

    if force:
        with _guard:
            slot = _slots.get(workspace.id)
            if slot is not None and time.monotonic() - slot.at < FORCE_GAP:
                force = False
    source = source_of(session, workspace.id)
    # Без сводной - оплаты KORT (`Registry.summary_of`): у компании без таблицы
    # «Оплачено» и «Остаток» видов иначе были бы пусты всегда.
    slot = get(session, workspace.id, force=force) if source is not None else None
    out: dict[str, Any] = {"source": source_out(source, slot), "contracts": {}}
    if source is not None and (slot is None or slot.index is None):
        return out
    if {"summary_paid", "summary_remaining"} <= set(access.hidden):
        return out
    registry = Registry(session, workspace)
    if slot is not None:
        registry._summary = slot.index  # то, что прочитано сейчас, а не в прошлый раз
    contracts = list(
        session.scalars(
            sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
        )
    )
    people = people_of(session, [item.id for item in contracts])
    registry.parties_for({pid for item in contracts for pid in (item.executor_id, item.customer_id)})
    for item in contracts:
        if not visible_to(item, registry, access, people.get(item.id, [])):
            continue
        match = registry.summary_of(item)
        entry: dict[str, Any] = {"state": match.state}
        if match.source:
            entry["source"] = match.source
        if match.state == "found":
            entry.update(
                paid=str(match.paid),
                remaining=str(item.amount - match.paid) if item.amount is not None else None,
                months=list(match.months),
                rows=match.rows,
                customer=match.customer,
            )
        elif match.candidates:
            entry["candidates"] = list(match.candidates)[:3]
        out["contracts"][str(item.id)] = entry
    return out


# ── Подключение ──────────────────────────────────────────────────────────────


def spreadsheet_id_of(link: str) -> str:
    """Id книги из ссылки Google или сам id."""
    text = (link or "").strip()
    marker = "/spreadsheets/d/"
    if marker in text:
        text = text.split(marker, 1)[1]
    return text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()


def connect(
    session: Session, workspace_id: uuid.UUID, *, link: str, worksheet: str, user_id: uuid.UUID | None
) -> tuple[SummarySource, Index | None, str]:
    """Подключить книгу-сводку: прочитать её сразу и сохранить, если читается.

    Таблица, где нужные колонки названы по-своему, подключается тоже - с
    отказом словами (третье значение): колонки называют в «Колонках для
    ядра», и она читается. Нет доступа, нет листа - не подключается: там
    сопоставлять нечего.
    """
    from app.finance.core import book as core_book
    from app.finance.core import settings as core_settings

    spreadsheet_id = spreadsheet_id_of(link)
    tab = (worksheet or "").strip()
    if not spreadsheet_id or not tab:
        raise SummaryError("Нужны ссылка на таблицу и название листа")
    extra = core_settings.book_columns(session, workspace_id)
    index: Index | None = None
    problem = ""
    try:
        index = _read_with(spreadsheet_id, tab, extra)
    except SummaryLayoutError as exc:
        problem = str(exc)
    source = session.get(SummarySource, workspace_id)
    if source is None:
        source = SummarySource(workspace_id=workspace_id, spreadsheet_id=spreadsheet_id, worksheet=tab)
        session.add(source)
    source.spreadsheet_id, source.worksheet = spreadsheet_id, tab
    if index is not None:
        source.title = index.title
    source.updated_by, source.updated_at = user_id, datetime.now(timezone.utc)
    session.flush()
    with _guard:
        _slots[workspace_id] = _Slot(
            source=(spreadsheet_id, tab, core_book.signature(extra)), index=index, error=problem, at=time.monotonic(),
            extra=dict(extra),
        )
    return source, index, problem


def disconnect(session: Session, workspace_id: uuid.UUID) -> None:
    session.execute(sa.delete(SummarySource).where(SummarySource.workspace_id == workspace_id))
    forget(workspace_id)


def status(session: Session, workspace_id: uuid.UUID) -> dict[str, Any] | None:
    """Подключённая сводка и что о ней известно - без чтения книги."""
    source = source_of(session, workspace_id)
    if source is None:
        return None
    with _guard:
        slot = _slots.get(workspace_id)
    if slot is not None and slot.source[:2] != (source.spreadsheet_id, source.worksheet):
        slot = None
    return source_out(source, slot)


def source_out(source: SummarySource | None, slot: _Slot | None) -> dict[str, Any] | None:
    if source is None:
        return None
    index = slot.index if slot is not None else None
    return {
        "spreadsheet_id": source.spreadsheet_id,
        "worksheet": source.worksheet,
        "title": (index.title if index else "") or source.title,
        "url": f"https://docs.google.com/spreadsheets/d/{source.spreadsheet_id}/edit",
        "read_at": index.read_at.isoformat() if index else None,
        "rows": index.rows if index else 0,
        "rev": index.rev if index else "",
        "drift": index.drift if index else "",
        "error": slot.error if slot is not None else "",
    }


__all__ = [
    "CACHE_TTL",
    "COLUMNS",
    "Index",
    "Match",
    "SummaryError",
    "SummaryLayoutError",
    "build_index",
    "columns_for",
    "connect",
    "contracts_out",
    "disconnect",
    "get",
    "peek",
    "read_money",
    "source_of",
    "source_out",
    "spreadsheet_id_of",
    "status",
]
