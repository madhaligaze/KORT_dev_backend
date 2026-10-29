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
(`CACHE_TTL`). Квота Google - 60 чтений в минуту на
аккаунт, поэтому чтение одно на компанию за раз и не чаще срока кэша.

С 29.10.2026 чтение и разбор листа - общие со сверкой ядра и «Счетами»
(`core/book.sheet`, `core/book.parse`): здесь из тех же строк листа (`Line`)
собираются строки «Оплачено» (`index_of`). Таблица бывает и файлом,
положенным в KORT (`connect_file`), - читается так же.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.core.columns import column, with_company_names
from app.finance.layout import Column
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


def source_of(session: Session, workspace_id: uuid.UUID) -> SummarySource | None:
    return session.get(SummarySource, workspace_id)


#: Ключ «Оплачено (сводка)» в строках читателей общего разбора (`core/book.derive`).
_DERIVED = "summary"
#: Без чего «Оплачено (сводка)» не читается: номер, клиент и поступления.
REQUIRED = ("customer", "number", "paid")


def index_of(book: Any) -> Index:
    """«Оплачено (сводка)» из общего разбора листа (`core/book.parse`) - прежними правилами строки.

    Строка - с номером и клиентом; поступление - числом, прочерк и пусто - ноль;
    наше юрлицо, месяц и отдел - как написаны в таблице.
    """
    from app.finance.core import book as core_book

    missing = core_book.require(book, REQUIRED)
    if missing:
        raise SummaryLayoutError(missing)
    by_number: dict[str, list[Row]] = {}
    digest = hashlib.sha1()
    count = 0
    for line in book.lines:
        number = line.get("number").strip()
        customer = line.get("customer").strip()
        key = number_key(number)
        if not key or not customer:
            continue
        item = Row(
            customer=customer,
            customer_key=party_key(customer),
            number=number,
            own=line.get("own").strip(),
            paid=read_money(line.get("paid")),
            month=line.get("month").strip(),
            department=line.get("department").strip(),
        )
        by_number.setdefault(key, []).append(item)
        digest.update(f"{key}|{item.customer_key}|{item.own}|{item.paid}|{item.month}\n".encode())
        count += 1
    return Index(
        rev=digest.hexdigest()[:16],
        read_at=book.read_at,
        title=book.title,
        worksheet=book.worksheet,
        by_number=by_number,
        rows=count,
        drift=book.drift,
    )


def build_index(
    values: Sequence[Sequence[Any]], *, title: str, worksheet: str, columns: Sequence[Column] = COLUMNS
) -> Index:
    """Разобрать значения листа сводки. Шапка - первая строка."""
    from app.finance.core import book as core_book

    try:
        book = core_book.parse(values, title=title, worksheet=worksheet, columns=core_book.columns_with(columns))
    except core_book.LayoutBroken as exc:
        raise SummaryLayoutError(str(exc)) from exc
    except core_book.BookError as exc:
        raise SummaryError(str(exc)) from exc
    return index_of(book)


# ── Чтение: общее со сверкой и «Счетами» ────────────────────────────────────


@dataclass
class _Slot:
    """«Оплачено (сводка)» по общему чтению таблицы (`core/book.sheet`): строки и отказ."""

    index: Index | None = None
    error: str = ""
    at: float = 0.0


def _view(shared: Any) -> _Slot:
    from app.finance.core import book as core_book

    error = shared.error
    try:
        index = core_book.derive(shared, _DERIVED, index_of)
    except SummaryError as exc:
        index, error = None, str(exc)
    return _Slot(index=index, error=error, at=shared.at)


def get(session: Session, workspace_id: uuid.UUID, *, wait: float = FIRST_READ_WAIT, force: bool = False) -> _Slot | None:
    """Сводка компании: прочитанная, при необходимости - перечитанная.

    Чтение и разбор таблицы - общие со сверкой ядра и «Счетами» (`core/book.sheet`):
    первое чтение ждём до `wait` секунд, дальше устаревшее отдаётся сразу, а
    свежее читается в фоне. `None` - сводка не подключена.
    """
    from app.finance.core import book as core_book

    shared = core_book.sheet(session, workspace_id, wait=wait, force=force)
    return _view(shared) if shared is not None else None


def peek(workspace_id: uuid.UUID) -> Index | None:
    """Уже прочитанная сводка - без похода в Google (сборка реестра не ждёт сеть)."""
    from app.finance.core import book as core_book

    shared = core_book.peek(workspace_id)
    return _view(shared).index if shared is not None else None


def forget(workspace_id: uuid.UUID) -> None:
    from app.finance.core import book as core_book

    core_book.forget(workspace_id)


#: Чаще этого «Обновить» книгу не перечитывает: квота Google общая на аккаунт (`core/book.FORCE_GAP`).
FORCE_GAP = 30.0


def contracts_out(session: Session, workspace: Any, access: Any, *, force: bool = False) -> dict[str, Any]:
    """«Оплачено/Остаток (сводка)» видимых договоров и откуда цифры.

    `state` у договора: `found` / `missing` / `other_client` / `ambiguous`
    (`Match`). Остаток - сумма договора минус оплачено, как в книге
    «Разовые»; у договора без суммы его нет.
    """
    from app.finance.contracts.models import Contract
    from app.finance.contracts.service import Registry, people_of, visible_to

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
    book = None
    unread = ""
    try:
        book = core_book._read(spreadsheet_id, tab, extra)
    except core_book.LayoutBroken as exc:
        unread = str(exc)
    except core_book.BookError as exc:
        raise SummaryError(str(exc)) from exc
    index = _connected(book)
    # Отказ «Оплачено» (нет колонки поступлений) - его, а не чтения: «Счетам» и сверке лист читается.
    problem = unread or (core_book.require(book, REQUIRED) if book is not None and index is None else "")
    source = session.get(SummarySource, workspace_id)
    if source is None:
        source = SummarySource(workspace_id=workspace_id, spreadsheet_id=spreadsheet_id, worksheet=tab)
        session.add(source)
    source.kind, source.spreadsheet_id, source.worksheet = "google", spreadsheet_id, tab
    source.file_name, source.digest, source.values = "", "", None
    if book is not None:
        source.title = book.title
    source.updated_by, source.updated_at = user_id, datetime.now(timezone.utc)
    session.flush()
    key, extra = core_book.source_key(session, workspace_id, source)
    core_book.prime(workspace_id, key, extra, book, unread)
    return source, index, problem


def _connected(book: Any) -> Index | None:
    if book is None:
        return None
    try:
        return index_of(book)
    except SummaryLayoutError:
        return None


def connect_file(
    session: Session, workspace_id: uuid.UUID, *, values: list[list[str]], file_name: str, worksheet: str, user_id: uuid.UUID | None,
) -> tuple[SummarySource, str]:
    """Сводная таблица файлом: строки - в базу, читатели берут их оттуда (`core/book.sheet`).

    Компания, которая ведёт сводную в Excel, получает то же, что с листом
    Google: «Оплачено (сводка)», «Счета», сверку и «Переход». Новая версия -
    новой загрузкой: читается то, что положили последним. Возвращает источник и
    отказ словами, если сверка ядра такую таблицу не читает (нет «Нашего юрлица»).
    """
    from app.finance.core import book as core_book

    if len(values) < 2:
        raise SummaryError(f"В «{file_name}» нет строк договоров - класть нечего")
    digest = hashlib.sha1(repr(values).encode("utf-8")).hexdigest()[:16]
    source = session.get(SummarySource, workspace_id)
    if source is None:
        source = SummarySource(workspace_id=workspace_id, spreadsheet_id="", worksheet=worksheet)
        session.add(source)
    source.kind, source.spreadsheet_id, source.worksheet = "file", "", worksheet
    source.file_name, source.title, source.digest, source.values = file_name, file_name, digest, values
    source.updated_by, source.updated_at = user_id, datetime.now(timezone.utc)
    session.flush()
    core_book.forget(workspace_id)
    slot = core_book.sheet(session, workspace_id, wait=0)
    book = slot.book if slot is not None else None
    problem = (slot.error if slot is not None else "") or (core_book.problem_of(book) if book is not None else "")
    return source, problem


def disconnect(session: Session, workspace_id: uuid.UUID) -> None:
    session.execute(sa.delete(SummarySource).where(SummarySource.workspace_id == workspace_id))
    forget(workspace_id)


_STATE = ("kind", "spreadsheet_id", "worksheet", "title", "file_name", "digest", "values")


def state_of(session: Session, workspace_id: uuid.UUID) -> dict[str, Any] | None:
    """Что подключено сводной сейчас - целиком, чтобы «Вернуть» положило это обратно (`restore`)."""
    source = source_of(session, workspace_id)
    if source is None:
        return None
    return {key: getattr(source, key) for key in _STATE}


def restore(session: Session, workspace_id: uuid.UUID, before: dict[str, Any] | None, user_id: uuid.UUID | None) -> None:
    """Сводная - как была до подключения файла (`state_of`); не было никакой - отключить."""
    if not before:
        disconnect(session, workspace_id)
        return
    source = session.get(SummarySource, workspace_id)
    if source is None:
        source = SummarySource(workspace_id=workspace_id, spreadsheet_id="", worksheet="")
        session.add(source)
    source.kind = before.get("kind") or "google"
    source.spreadsheet_id, source.worksheet = before.get("spreadsheet_id") or "", before.get("worksheet") or ""
    source.title, source.file_name, source.digest = before.get("title") or "", before.get("file_name") or "", before.get("digest") or ""
    source.values = before.get("values")
    source.updated_by, source.updated_at = user_id, datetime.now(timezone.utc)
    session.flush()
    forget(workspace_id)


def status(session: Session, workspace_id: uuid.UUID) -> dict[str, Any] | None:
    """Подключённая сводка и что о ней известно - без чтения книги."""
    from app.finance.core import book as core_book

    source = source_of(session, workspace_id)
    if source is None:
        return None
    shared = core_book.peek(workspace_id)
    key, _extra = core_book.source_key(session, workspace_id, source)
    if shared is not None and shared.source != core_book.ANY and shared.source[:3] != key[:3]:
        shared = None
    return source_out(source, _view(shared) if shared is not None else None)


def source_out(source: SummarySource | None, slot: _Slot | None) -> dict[str, Any] | None:
    if source is None:
        return None
    index = slot.index if slot is not None else None
    file = getattr(source, "kind", "google") == "file"
    return {
        "kind": "file" if file else "google",
        "spreadsheet_id": source.spreadsheet_id,
        "worksheet": source.worksheet,
        "file_name": source.file_name if file else "",
        "title": (index.title if index else "") or source.title,
        "url": "" if file else f"https://docs.google.com/spreadsheets/d/{source.spreadsheet_id}/edit",
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
    "connect_file",
    "contracts_out",
    "disconnect",
    "get",
    "index_of",
    "peek",
    "read_money",
    "restore",
    "source_of",
    "source_out",
    "spreadsheet_id_of",
    "state_of",
    "status",
]
