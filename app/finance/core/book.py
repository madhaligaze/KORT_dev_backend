"""Сводная таблица компании целиком: строка - договор за месяц со всей цепочкой.

Откуда
──────
Та же таблица, что дают «Оплачено (сводка)» и «Счета → Не выставлены»
(`SummarySource`): лист, куда сходятся таблицы всех юрлиц компании. В строке -
всё, что таблицы юрлиц знают о договоре за месяц: условия (юрлицо, вид
услуги, сумма, период), счёт, оплаты из журнала, АВР с датой подписания
клиентом, ЭСФ, сальдо по актам и по начислениям, статус.

Зачем ядру
──────────
Пока учёт живёт в таблицах, ядро считает то же самое из своих событий и
сверяет с этой строкой - «тень» (`core/shadow.py`). Отсюда же берутся
написания клиентов (заказчик, имя в банке, имя в 1С) и коды наших юрлиц.
Таблицу ядро не правит: пишет в таблицы юрлиц только карточка счёта
(`contracts/bill_book.py`).

Как читается
────────────
* Колонки - по названиям (`app.finance.layout`): ключи ядра с общими словами
  отрасли и словарём компании - её шапкой (`with_company_names`). Без
  словаря таблица чужой формы не читается молча, а показывает, какие колонки
  не нашлись («Колонки для ядра», `layout_of`).
* Пустые и служебные ячейки («#N/A», «-», «FALSE») - отсутствие значения, а
  не ноль.
* Месяц строки - из колонки месяца («ИЮЛЬ 2026»; у разового на исполнении -
  «на исп. (начало ОКТЯБРЬ 2026)»), иначе из «Периода с». «Старые (до
  Май/Июня 2026)» - строки до начала помесячных листов: у них месяца нет.
* Строка → договор - тем же правилом, что «Оплачено (сводка)»: номер, потом
  клиент; два подходящих клиента - спорно, не угадываем (`Matcher`).
* Лист читается и разбирается один раз на всех: сверка ядра, «Оплачено
  (сводка)» и «Счета» берут строки из одного разбора (`sheet`, `parse`), а
  правила строки у каждого свои (`summary.index_of`, `bills.board_of`).
* Сводная бывает и файлом (`summary.connect_file`): его строки приёмник уже
  прочитал словарём компании, в базе они лежат словами ядра (`FILE_COLUMNS`),
  и всё, что умеет лист Google, - сверка, «Переход», «Оплачено», «Счета», -
  умеет и файл.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import google
from app.finance.contracts.fields import number_key, party_key
from app.finance.contracts.models import Contract, CounterpartyName, GroupEntity
from app.finance.contracts.summary import source_of
from app.finance.core.columns import COLUMNS, signature, with_company_names
from app.finance.layout import Column, LayoutError, norm, resolve_layout
from app.finance.models import Counterparty
from app.finance.onec.match import number_loose

log = logging.getLogger(__name__)

#: Сколько держится прочитанная книга - как у сводки оплат и счетов.
CACHE_TTL = 120.0
ERROR_RETRY = 60.0
FIRST_READ_WAIT = 25.0
#: Короче - не ищем одно имя внутри другого: «ТОО А» нашлось бы везде.
MIN_CONTAINED = 5

#: Значения-заглушки книги: ошибка формулы, прочерк, пустая галочка. Прочерк
#: в книгах пишут и длинным тире - разбор обязан его узнавать, поэтому он
#: записан именем символа (правило «длинное тире не писать», CLAUDE.md).
_EMPTY = frozenset({"", "#n/a", "#н/д", "#value!", "#знач!", "#ref!", "#ссылка!", "-", "\N{EM DASH}", "false", "ложь"})
_TRUE = frozenset({"true", "истина"})

MONTHS = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}
_MONTH_IN_TEXT = re.compile(r"(январь|февраль|март|апрель|май|июнь|июль|август|сентябрь|октябрь|ноябрь|декабрь)\s+(\d{4})")


class BookError(Exception):
    """Книгу нельзя прочитать - с текстом для человека."""


class LayoutBroken(BookError):
    """Шапка не разобралась: колонки ядра не нашлись по названиям. `header` - для экрана сопоставления."""

    def __init__(self, message: str, header: list[str]):
        super().__init__(message)
        self.header = header


def columns_for(extra: dict[str, list[str]] | None) -> tuple[Column, ...]:
    """Колонки ядра с написаниями компании (словарь `core/settings.book_columns`)."""
    return with_company_names(COLUMNS, extra) if extra else COLUMNS


#: Колонки сводной, положенной файлом: шапка - подписи колонок ядра. Словарь
#: компании к строкам файла уже применил приёмник, когда читал его
#: (`intake/compare.table_values`), второй раз он не применяется.
FILE_COLUMNS: tuple[Column, ...] = tuple(dataclasses.replace(column, names=(column.title,)) for column in COLUMNS)


def blank(value: Any) -> bool:
    return norm(value) in _EMPTY


def text_of(value: Any) -> str:
    return "" if blank(value) else str(value).strip()


def money_of(value: Any) -> Decimal | None:
    """«100 000», «-50 000», «1 234,50» → число; заглушка книги - `None`, а не ноль."""
    if blank(value):
        return None
    raw = str(value).replace("\xa0", "").replace(" ", "").replace(" ", "").strip()
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()")
    if "," in raw and "." not in raw:
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    return -number if negative else number


def date_of(value: Any) -> date | None:
    """«25.10.2026» → дата; серийный номер Google («46 230») - тоже; прочее - `None`."""
    text = text_of(value)
    if not text:
        return None
    for pattern in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    serial = text.replace("\xa0", "").replace(" ", "")
    if serial.isdigit() and 30000 < int(serial) < 80000:
        # Дата принятия АВР в части листов - число без формата даты.
        return date.fromordinal(date(1899, 12, 30).toordinal() + int(serial))
    return None


def flag_of(value: Any) -> str:
    """Галочка книги: `yes`, `part` («Часть»), пусто - нет."""
    text = norm(value)
    if text in _TRUE:
        return "yes"
    if text.startswith("част"):
        return "part"
    return ""


def month_of(label: str, period_from: date | None) -> str:
    """Месяц строки `ГГГГ-ММ`; у «Старых» и пустых без периода - пусто."""
    found = _MONTH_IN_TEXT.search(norm(label))
    if found:
        return f"{int(found.group(2)):04d}-{MONTHS[found.group(1)]:02d}"
    if norm(label).startswith("старые"):
        return ""
    return f"{period_from:%Y-%m}" if period_from else ""


@dataclass(frozen=True)
class Payment:
    paid_on: date | None
    amount: Decimal


@dataclass(frozen=True)
class BookRow:
    """Строка сводки: договор за месяц, как его видят книги юрлиц."""

    line: int
    own: str
    customer: str
    number: str
    month: str
    month_label: str
    kind: str = ""
    staff: str = ""
    bank_name: str = ""
    onec_name: str = ""
    bill_day: str = ""
    amount: Decimal | None = None
    department: str = ""
    subject: str = ""
    signed_at: date | None = None
    amendments: str = ""
    period_from: date | None = None
    period_to: date | None = None
    note: str = ""
    opening_acts: Decimal | None = None
    bill_state: str = ""
    bill_period: str = ""
    bill_no: str = ""
    bill_date: date | None = None
    paid_state: str = ""
    paid: Decimal | None = None
    payments: tuple[Payment, ...] = ()
    act_state: str = ""
    act_sum: Decimal | None = None
    act_no: str = ""
    act_date: date | None = None
    accepted_state: str = ""
    accepted_date: date | None = None
    esf_state: str = ""
    esf_date: date | None = None
    comment: str = ""
    closing_acts: Decimal | None = None
    status: str = ""
    opening_accruals: Decimal | None = None
    closing_accruals: Decimal | None = None
    refund_sum: Decimal | None = None
    refund_date: date | None = None

    @property
    def key(self) -> str:
        """Чей это месяц: юрлицо, клиент, № договора, месяц строки - без оформления."""
        return "|".join(norm(part) for part in (self.own, self.customer, self.number, self.month_label))

    @property
    def id(self) -> str:
        return hashlib.sha1(self.key.encode()).hexdigest()[:12]

    @property
    def names(self) -> tuple[str, ...]:
        """Как назван клиент: реестр, банк, 1С."""
        return tuple(name for name in (self.customer, self.bank_name, self.onec_name) if name)


@dataclass(frozen=True)
class Line:
    """Строка листа как есть: номер строки и ячейки по ключам колонок, строкой, без разбора.

    Из этих строк свои строки собирают «Оплачено (сводка)» (`summary.index_of`)
    и «Счета» (`bills.board_of`): разбор листа один, правила чтения у каждого свои.
    """

    line: int
    cells: dict[str, str]

    def get(self, key: str) -> str:
        return self.cells.get(key, "")


@dataclass
class Book:
    """Прочитанная сводка и откуда она."""

    read_at: datetime
    title: str
    worksheet: str
    spreadsheet_id: str
    rows: list[BookRow] = field(default_factory=list)
    drift: str = ""
    rev: str = ""
    #: Шапка листа как есть - для словаря колонок компании.
    header: list[str] = field(default_factory=list)
    #: Все строки листа, где есть клиент или номер, - как есть (`Line`).
    lines: list[Line] = field(default_factory=list)
    #: Колонки, которых в листе нет: ключ → подпись. Без чего читатель не читает
    #: лист, решает он сам (`require`): сверке нужно наше юрлицо, «Оплачено» - поступления.
    missing: dict[str, str] = field(default_factory=dict)


#: Без чего лист не читается вовсе: строки без клиента и номера - не договоры.
_ROW_KEYS = ("customer", "number")
#: Без чего не читает сверка ядра: чей договор - наше юрлицо.
BOOK_REQUIRED = ("own", "customer", "number")


def columns_with(given: Sequence[Column], keys: dict[str, str] | None = None) -> tuple[Column, ...]:
    """Колонки ядра, где у ключей читателя - его написания (`keys` - ключ читателя → ключ ядра).

    Разбор листа один на всех читателей: он раскладывает все колонки ядра, а
    читатель, который пришёл со своими написаниями (тесты, прежние вызовы
    `build_index`, `build_board`), приносит их в этот же разбор.
    """
    names = {(keys or {}).get(column.key, column.key): column for column in given}
    return tuple(
        dataclasses.replace(base, title=names[base.key].title, names=names[base.key].names) if base.key in names else base
        for base in COLUMNS
    )


def missing_text(worksheet: str, titles: Sequence[str]) -> str:
    """Отказ «колонки не нашлись» - теми же словами, что `resolve_layout`."""
    return (
        f"Лист «{worksheet}»: не удалось привязать колонки по названиям. "
        "Колонку переименовали или лист заменили другим. "
        + "; ".join(f"«{title}» - не нашёл колонку с таким заголовком" for title in titles)
    )


def require(book: Book, keys: Sequence[str]) -> str:
    """Чего из `keys` в листе нет - отказом словами; всё есть - пусто."""
    lost = [book.missing[key] for key in keys if key in book.missing]
    return missing_text(book.worksheet, lost) if lost else ""


def parse(
    values: Sequence[Sequence[Any]],
    *,
    title: str,
    worksheet: str,
    spreadsheet_id: str = "",
    columns: Sequence[Column] = COLUMNS,
) -> Book:
    """Один разбор листа для всех читателей: шапка по названиям, строки как есть и строки сверки.

    Колонка, которой нет, не роняет разбор - она в `missing`, и каждый читатель
    сам решает, читает ли он без неё (`require`). Роняет только нечитаемое:
    одно название у двух колонок, колонка встречается дважды.
    """
    if not values:
        raise BookError(f"Лист «{worksheet}» пуст")
    header = [str(cell) for cell in values[0]]
    try:
        layout = resolve_layout(worksheet, [dataclasses.replace(column, required=False) for column in columns], header)
    except LayoutError as exc:
        raise LayoutBroken(str(exc), header) from exc
    found = [column.key for column in columns if layout.placements[column.key].found]
    lines: list[Line] = []
    rows: list[BookRow] = []
    digest = hashlib.sha1()
    for offset, raw in enumerate(values[1:]):
        row = [str(cell) for cell in raw]
        cells = {key: layout.cell(row, key) for key in found}
        if not any(cells.get(key, "").strip() for key in _ROW_KEYS):
            continue
        line = Line(offset + 2, cells)
        lines.append(line)
        item = _book_row(line)
        if item is not None:
            rows.append(item)
            digest.update(f"{item.key}|{item.amount}|{item.bill_no}|{item.paid}|{item.act_no}|{item.closing_acts}\n".encode())
    return Book(
        read_at=datetime.now(timezone.utc),
        title=title,
        worksheet=worksheet,
        spreadsheet_id=spreadsheet_id,
        rows=rows,
        drift=layout.describe_drift(),
        rev=digest.hexdigest()[:16],
        header=header,
        lines=lines,
        missing={column.key: column.title for column in columns if column.key not in found},
    )


def problem_of(book: Book) -> str:
    """Почему сверка ядра не читает этот лист; читает - пусто."""
    problem = require(book, BOOK_REQUIRED)
    if problem:
        return problem
    if not book.rows:
        return f"В листе «{book.worksheet}» нет ни одной строки договора - таблица, похоже, ещё собирается"
    return ""


def build_book(
    values: Sequence[Sequence[Any]],
    *,
    title: str,
    worksheet: str,
    spreadsheet_id: str = "",
    columns: Sequence[Column] = COLUMNS,
) -> Book:
    """Разобрать значения листа для сверки ядра. Шапка - первая строка; строка листа 2 - первая данных."""
    book = parse(values, title=title, worksheet=worksheet, spreadsheet_id=spreadsheet_id, columns=columns)
    missing = require(book, BOOK_REQUIRED)
    if missing:
        raise LayoutBroken(missing, book.header)
    problem = problem_of(book)
    if problem:
        raise BookError(problem)
    return book


def _book_row(line: Line) -> BookRow | None:
    """Строка сверки из строки листа; без клиента и номера (после заглушек «#N/A», «-») - не договор."""
    customer = text_of(line.get("customer"))
    number = text_of(line.get("number"))
    if not customer and not number:
        return None
    cell = line.get
    period_from = date_of(cell("period_from"))
    label = text_of(cell("month"))
    payments = tuple(
        Payment(date_of(cell(f"pay{part}_date")), amount)
        for part in (1, 2, 3)
        if (amount := money_of(cell(f"pay{part}_sum"))) not in (None, Decimal("0"))
    )
    return BookRow(
        line=line.line,
        own=text_of(cell("own")),
        customer=customer,
        number=number,
        month=month_of(label, period_from),
        month_label=label,
        kind=text_of(cell("kind")),
        staff=text_of(cell("staff")),
        bank_name=text_of(cell("bank_name")),
        onec_name=text_of(cell("onec_name")),
        bill_day=text_of(cell("bill_day")),
        amount=money_of(cell("amount")),
        department=text_of(cell("department")),
        subject=text_of(cell("subject")),
        signed_at=date_of(cell("signed_at")),
        amendments=text_of(cell("amendments")),
        period_from=period_from,
        period_to=date_of(cell("period_to")),
        note=text_of(cell("note")),
        opening_acts=money_of(cell("opening_acts")),
        bill_state=str(cell("bill_state")).strip(),
        bill_period=text_of(cell("bill_period")),
        bill_no=text_of(cell("bill_no")),
        bill_date=date_of(cell("bill_date")),
        paid_state=flag_of(cell("paid_state")),
        paid=money_of(cell("paid")),
        payments=payments,
        act_state=flag_of(cell("act_state")),
        act_sum=money_of(cell("act_sum")),
        act_no=text_of(cell("act_no")),
        act_date=date_of(cell("act_date")),
        accepted_state=flag_of(cell("accepted_state")),
        accepted_date=date_of(cell("accepted_date")),
        esf_state=flag_of(cell("esf_state")),
        esf_date=date_of(cell("esf_date")),
        comment=text_of(cell("comment")),
        closing_acts=money_of(cell("closing_acts")),
        status=text_of(cell("status")),
        opening_accruals=money_of(cell("opening_accruals")),
        closing_accruals=money_of(cell("closing_accruals")),
        refund_sum=money_of(cell("refund_sum")),
        refund_date=date_of(cell("refund_date")),
    )


# ── Строка → договор ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RowMatch:
    """Чей договор у строки. `state`: `found`, `missing`, `other_client`, `ambiguous`."""

    state: str
    contract_id: uuid.UUID | None = None
    candidates: tuple[uuid.UUID, ...] = ()


def _same_client(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= MIN_CONTAINED and short in long


class Matcher:
    """Договоры компании для сопоставления со строками книги - один раз на запрос."""

    def __init__(self, session: Session, workspace_id: uuid.UUID):
        rows = session.execute(
            sa.select(Contract.id, Contract.number, Contract.customer_id, Contract.executor_id).where(
                Contract.workspace_id == workspace_id, Contract.deleted_at.is_(None)
            )
        ).all()
        self.by_number: dict[str, list[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]]] = {}
        #: Номер без разделителей: «ОБОиНО-62» и «ОБОиНО/62» - один номер,
        #: записанный другой рукой. Только при том же клиенте (`match`).
        self.by_loose: dict[str, list[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]]] = {}
        party_ids: set[uuid.UUID] = set()
        for contract_id, number, customer_id, executor_id in rows:
            key = number_key(number)
            if key:
                self.by_number.setdefault(key, []).append((contract_id, customer_id, executor_id))
                self.by_loose.setdefault(number_loose(number), []).append((contract_id, customer_id, executor_id))
            party_ids.update(pid for pid in (customer_id, executor_id) if pid)
        self.keys: dict[uuid.UUID, set[str]] = {}
        if party_ids:
            for party_id, name in session.execute(
                sa.select(Counterparty.id, Counterparty.name).where(Counterparty.id.in_(party_ids))
            ):
                self.keys.setdefault(party_id, set()).add(party_key(name))
            for party_id, normalized in session.execute(
                sa.select(CounterpartyName.counterparty_id, CounterpartyName.normalized).where(
                    CounterpartyName.workspace_id == workspace_id, CounterpartyName.counterparty_id.in_(party_ids)
                )
            ):
                self.keys.setdefault(party_id, set()).add(normalized)
        #: Код юрлица в таблице («ТМС») → юрлицо KORT, если код записан у юрлица.
        self.entity_by_code: dict[str, uuid.UUID] = {
            norm(code): entity_id
            for entity_id, code in session.execute(
                sa.select(GroupEntity.counterparty_id, GroupEntity.code).where(
                    GroupEntity.workspace_id == workspace_id, GroupEntity.archived_at.is_(None)
                )
            )
            if code and code.strip()
        }

    def _clients(self, candidates, names: list[str]):
        return [
            item for item in candidates
            if item[1] is not None and any(_same_client(key, name) for key in self.keys.get(item[1], ()) for name in names)
        ]

    def match(self, row: BookRow) -> RowMatch:
        names = [party_key(name) for name in row.names]
        candidates = self.by_number.get(number_key(row.number), [])
        loose = False
        if not candidates:
            # Тот же номер другой рукой - только если и клиент тот же.
            candidates = self._clients(self.by_loose.get(number_loose(row.number), []), names)
            loose = True
            if not candidates:
                return RowMatch("missing")
        entity = self.entity_by_code.get(norm(row.own))
        if entity is not None and any(executor == entity for _cid, _cust, executor in candidates):
            candidates = [item for item in candidates if item[2] == entity]
        good = candidates if loose else self._clients(candidates, names)
        if len(good) == 1:
            return RowMatch("found", good[0][0])
        if len(good) > 1:
            return RowMatch("ambiguous", candidates=tuple(item[0] for item in good))
        return RowMatch("other_client", candidates=tuple(item[0] for item in candidates))


# ── Кэш: одно чтение сводной на всех читателей ──────────────────────────────
#
# Сводную таблицу компании читают трое: сверка ядра (здесь), «Оплачено
# (сводка)» (`contracts/summary.py`) и «Счета» (`contracts/bills.py`). До
# 29.10.2026 у каждого было своё чтение листа, свой кэш и свой разбор: лист
# читался трижды, и три разбора могли видеть три состояния таблицы. Теперь
# чтение и разбор - здесь, один раз (`sheet`), а читатели строят свои строки из
# того же `Book` (`derive`). Источник - Google (лист по расписанию) или таблица
# файлом, которую компания положила в KORT (`SummarySource.kind = file`).

#: Чаще этого «Обновить» таблицу не перечитывает: квота Google общая на аккаунт.
FORCE_GAP = 30.0


@dataclass
class _Slot:
    #: Откуда и подпись словаря колонок: другой лист, файл или написание - читать заново.
    source: tuple[str, ...]
    #: Последний разобранный лист (сверке ещё нужны свои колонки - `current`).
    book: Book | None = None
    error: str = ""
    at: float = 0.0
    reading: threading.Event | None = None
    extra: dict[str, list[str]] = field(default_factory=dict)
    #: Шапка листа, который не разобрался, - чтобы её можно было сопоставить.
    header: list[str] = field(default_factory=list)
    #: Строки читателей по этой книге (`derive`): один раз на прочитанное.
    derived: dict[str, tuple[Book, Any]] = field(default_factory=dict)


_slots: dict[uuid.UUID, _Slot] = {}
_guard = threading.Lock()


def _read(spreadsheet_id: str, worksheet: str, extra: dict[str, list[str]] | None = None) -> Book:
    """Лист Google - разобранным (`parse`). Тесты подменяют именно его: сеть им не нужна."""
    if not google.is_configured():
        raise BookError("Не заданы креды Google - таблицу прочитать нечем")
    try:
        meta = google.spreadsheet_meta(spreadsheet_id)
        values = google.fetch_tab_values(spreadsheet_id, worksheet, fresh=True)
    except google.GoogleError as exc:
        raise BookError(str(exc)) from exc
    return parse(values, title=meta.get("title", ""), worksheet=worksheet, spreadsheet_id=spreadsheet_id, columns=columns_for(extra))


def _store(slot: _Slot, read: Any) -> None:
    """Положить в слот прочитанное `read()` - или отказ словами."""
    try:
        book = read()
        with _guard:
            slot.book, slot.error, slot.header = book, "", book.header
    except LayoutBroken as exc:
        with _guard:
            slot.error, slot.header = str(exc), exc.header
    except BookError as exc:
        with _guard:
            slot.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - чтение таблицы не должно ронять ни ядро, ни реестр
        log.exception("сводная таблица не прочиталась")
        with _guard:
            slot.error = f"Таблица не прочиталась: {exc}"
    finally:
        with _guard:
            slot.at = time.monotonic()


def _refresh(slot: _Slot, done: threading.Event) -> None:
    try:
        _store(slot, lambda: _read(slot.source[1], slot.source[2], slot.extra))
    finally:
        with _guard:
            slot.reading = None
        done.set()


def source_key(session: Session, workspace_id: uuid.UUID, source: Any) -> tuple[tuple[str, ...], dict[str, list[str]]]:
    """Ключ чтения: Google - таблица, лист и подпись словаря колонок компании; файл - его отпечаток.

    Файл словарь не перечитывает: его строки уже словами ядра (`FILE_COLUMNS`).
    """
    from app.finance.core import settings as core_settings

    if getattr(source, "kind", "google") == "file":
        return ("file", source.digest or "", source.worksheet or ""), {}
    extra = core_settings.book_columns(session, workspace_id)
    return ("google", source.spreadsheet_id, source.worksheet, signature(extra)), extra


def sheet(session: Session, workspace_id: uuid.UUID, *, wait: float = FIRST_READ_WAIT, force: bool = False) -> _Slot | None:
    """Сводная таблица компании - прочитанная и разобранная один раз на всех читателей.

    Google: первое чтение ждём до `wait` секунд, дальше устаревшее отдаётся
    сразу, а свежее читается в фоне; «Обновить» (`force`) - не чаще
    `FORCE_GAP`. Файл: разбирается из базы, как только его положили. `None` -
    таблица не подключена.
    """
    source = source_of(session, workspace_id)
    if source is None:
        return None
    key, extra = source_key(session, workspace_id, source)
    done: threading.Event | None = None
    with _guard:
        slot = _slots.get(workspace_id)
        if slot is not None and slot.source == ANY:
            return slot
        if slot is None or slot.source != key:
            slot = _slots[workspace_id] = _Slot(source=key, extra=dict(extra))
        parsed = slot.at > 0
        if key[0] != "file":
            age = time.monotonic() - slot.at
            # Отказ Google не повторяется на каждый запрос: раз в минуту, иначе
            # ошибка съела бы квоту всего аккаунта.
            stale = age >= (ERROR_RETRY if slot.book is None and slot.error else CACHE_TTL) or (slot.book is None and not slot.error)
            if force and age >= FORCE_GAP:
                stale = True
            if slot.reading is not None:
                done = slot.reading
            elif stale:
                done = slot.reading = threading.Event()
                threading.Thread(target=_refresh, args=(slot, done), daemon=True).start()
    if key[0] == "file":
        if not parsed:
            _store(slot, lambda: parse(
                source.values or [], title=source.title or source.file_name, worksheet=source.worksheet or source.file_name,
                columns=FILE_COLUMNS,
            ))
        return slot
    if done is not None and (slot.book is None or force) and wait > 0:
        done.wait(wait)
    return slot


def derive(slot: _Slot, name: str, build: Any) -> Any:
    """Строки читателя `name` по книге слота - один раз на прочитанное; книги ещё нет - `None`."""
    with _guard:
        book = slot.book
        hit = slot.derived.get(name)
    if book is None:
        return None
    if hit is not None and hit[0] is book:
        return hit[1]
    value = build(book)
    with _guard:
        slot.derived[name] = (book, value)
    return value


def peek(workspace_id: uuid.UUID) -> _Slot | None:
    """Слот компании без чтения: сборка реестра не ждёт сеть."""
    with _guard:
        return _slots.get(workspace_id)


def expire(workspace_id: uuid.UUID) -> None:
    """Следующее обращение перечитает таблицу Google (KORT только что записал в книгу юрлица)."""
    with _guard:
        slot = _slots.get(workspace_id)
        if slot is not None and slot.source != ANY and slot.source[0] == "google":
            slot.at = 0.0


def prime(workspace_id: uuid.UUID, key: tuple[str, ...], extra: dict[str, list[str]], book: Book | None, error: str = "") -> None:
    """Положить прочитанное при подключении таблицы: первые экраны не ждут второго чтения."""
    with _guard:
        _slots[workspace_id] = _Slot(
            source=key, book=book, error=error, at=time.monotonic(), extra=dict(extra), header=book.header if book else [],
        )


def current(
    session: Session, workspace_id: uuid.UUID, *, wait: float = FIRST_READ_WAIT, force: bool = False
) -> tuple[Book | None, str]:
    """Книга сверки ядра и отказ чтения. `(None, "")` - сводная таблица не подключена."""
    slot = sheet(session, workspace_id, wait=wait, force=force)
    if slot is None:
        return None, ""
    with _guard:
        book, error = slot.book, slot.error
    if book is not None:
        problem = problem_of(book)
        if problem:
            return None, problem
    return book, error


#: Книга, положенная руками (`put`), - отдаётся при любом подключённом источнике.
ANY = ("*", "*")


def put(workspace_id: uuid.UUID, book: Book) -> None:
    """Положить прочитанную книгу (тесты, сквозные прогоны без Google)."""
    with _guard:
        _slots[workspace_id] = _Slot(source=ANY, book=book, at=time.monotonic(), header=book.header)


def forget(workspace_id: uuid.UUID | None = None) -> None:
    with _guard:
        if workspace_id is None:
            _slots.clear()
        else:
            _slots.pop(workspace_id, None)


def rows_of(book: Book, matcher: Matcher) -> Iterable[tuple[BookRow, RowMatch]]:
    for row in book.rows:
        yield row, matcher.match(row)


def layout_of(session: Session, workspace_id: uuid.UUID, *, wait: float = FIRST_READ_WAIT) -> dict[str, Any]:
    """Шапка сводной таблицы и как в ней нашлась каждая колонка ядра - для словаря колонок компании.

    Шапка есть и у листа, который не разобрался: экран сопоставления нужен
    именно тогда.
    """
    from app.finance.core import settings as core_settings
    from app.finance.layout import inspect_layout

    if source_of(session, workspace_id) is None:
        return {"connected": False, "header": [], "columns": [], "error": ""}
    book, error = current(session, workspace_id, wait=wait)
    with _guard:
        slot = _slots.get(workspace_id)
        header = list(book.header if book is not None and book.header else (slot.header if slot is not None else []))
    extra = core_settings.book_columns(session, workspace_id)
    base = {column.key: column for column in COLUMNS}
    # Подпись - смысл ядра («Наше юрлицо»), а не название компании: экран
    # сопоставляет колонку ядра с колонкой таблицы, названия компании - справа.
    columns = [
        {**item, "title": base[item["key"]].title, "names": list(base[item["key"]].names), "extra": extra.get(item["key"], [])}
        for item in inspect_layout(columns_for(extra), header)
    ] if header else []
    return {"connected": True, "header": [cell.strip() for cell in header], "columns": columns, "error": error}


__all__ = [
    "Book",
    "BookError",
    "BookRow",
    "COLUMNS",
    "FILE_COLUMNS",
    "LayoutBroken",
    "Line",
    "Matcher",
    "Payment",
    "RowMatch",
    "build_book",
    "columns_for",
    "columns_with",
    "current",
    "derive",
    "layout_of",
    "date_of",
    "flag_of",
    "forget",
    "money_of",
    "month_of",
    "parse",
    "peek",
    "problem_of",
    "put",
    "require",
    "rows_of",
    "sheet",
    "source_key",
    "signature",
    "text_of",
    "with_company_names",
]
