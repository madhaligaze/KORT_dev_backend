"""Книга-сводка компании целиком: строка - договор за месяц со всей цепочкой.

Откуда
──────
Та же книга, что дают «Оплачено (сводка)» и «Счета → Не выставлены»
(`SummarySource`): у BBC - «Осн.Общая сводка BBC 2026», лист «Сводка все ЮР
лица». В строке - всё, что книги юрлиц знают о договоре за месяц: условия
(юрлицо, вид услуги, сумма, период), счёт, оплаты из журнала, АВР с датой
подписания клиентом, ЭСФ, сальдо по актам и по начислениям, статус.

Зачем ядру
──────────
Пока учёт живёт в книгах, ядро считает то же самое из своих событий и
сверяет с этой строкой - «тень» (`core/shadow.py`). Отсюда же берутся
написания клиентов («Заказчик», «По Выписке Банка», «По 1C») и коды наших
юрлиц («Наша Фирма»). Книгу ядро не правит: пишет в книги юрлиц только
карточка счёта (`contracts/bill_book.py`).

Как читается
────────────
* Колонки - по названиям (`app.finance.layout`); `hint` - где стояли 24.10.2026.
* Пустые и служебные ячейки книги («#N/A», «-», «FALSE») - отсутствие
  значения, а не ноль.
* Месяц строки - из «Техн. 1» («ИЮЛЬ 2026»; у разового на исполнении -
  «на исп. (начало ОКТЯБРЬ 2026)»), иначе из «Периода с». «Старые (до
  Май/Июня 2026)» - строки до начала помесячных листов: у них месяца нет.
* Строка → договор - тем же правилом, что «Оплачено (сводка)»: номер, потом
  клиент; два подходящих клиента - спорно, не угадываем (`Matcher`).
"""
from __future__ import annotations

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

#: Колонки сводки (24.10.2026, A=0). Денежные и датные - только по точному
#: названию: «Сумма (наша)», «Сумма (часть 1)» и «Сумма Договора» начинаются
#: одинаково, и «похожее» прочитало бы соседнюю.
COLUMNS: tuple[Column, ...] = (
    Column("month_no", "Мес", ("мес",), hint=2, required=False, strict=True),
    Column("month", "Месяц строки (Техн. 1)", ("техн. 1",), hint=3, required=False, strict=True),
    Column("own", "Наша Фирма", ("наша фирма",), hint=5),
    Column("kind", "Вид Услуги", ("вид услуги",), hint=6, required=False, strict=True),
    Column("staff", "Наш Сотрудник", ("наш сотрудник",), hint=7, required=False, strict=True),
    Column("customer", "Заказчик", ("заказчик (название фирмы)", "заказчик"), hint=8),
    Column("bank_name", "По Выписке Банка", ("по выписке банка",), hint=9, required=False, strict=True),
    Column("onec_name", "По 1C", ("по 1c", "по 1с"), hint=10, required=False, strict=True),
    Column("bill_day", "Число Выставления Счета", ("число выставления счета",), hint=11, required=False, strict=True),
    Column("amount", "Сумма Договора", ("сумма договора",), hint=12, required=False, strict=True),
    Column("department", "Отдел", ("отдел",), hint=13, required=False, strict=True),
    Column("subject", "Предмет договора", ("предмет договра", "предмет договора"), hint=14, required=False, strict=True),
    Column("signed_at", "Дата заключения Договора", ("дата заключения договора",), hint=15, required=False, strict=True),
    Column("number", "№ Договора", ("№ договора", "номер договора"), hint=16),
    Column("amendments", "Доп Соглашения", ("доп соглашения",), hint=17, required=False, strict=True),
    Column("period_from", "Период с (нач.)", ("период с (нач.)",), hint=18, required=False, strict=True),
    Column("period_to", "Период по (зав.)", ("период по (зав.)",), hint=19, required=False, strict=True),
    Column("note", "Доп. Коммент.", ("доп. коммент.",), hint=20, required=False, strict=True),
    Column("opening_acts", "Сальдо Начало (АВР)", ("сальдо начало (авр)",), hint=22, required=False, strict=True),
    Column("bill_state", "Счет", ("счет",), hint=24, required=False, strict=True),
    Column("bill_period", "Счет за Период (Коммент)", ("счет за период (коммент)",), hint=25, required=False, strict=True),
    Column("bill_no", "№ Счета", ("№ счета",), hint=26, required=False, strict=True),
    Column("bill_date", "Дата выстав. Счета", ("дата выстав. счета",), hint=27, required=False, strict=True),
    Column("paid_state", "Факт Оплата", ("факт оплата",), hint=28, required=False, strict=True),
    Column("paid", "Сумма Факт Поступ.", ("сумма факт поступ.",), hint=29, required=False, strict=True),
    Column("pay1_date", "Дата (часть 1)", ("дата (часть 1)",), hint=30, required=False, strict=True),
    Column("pay1_sum", "Сумма (часть 1)", ("сумма (часть 1)",), hint=31, required=False, strict=True),
    Column("pay2_date", "Дата (часть 2)", ("дата (часть 2)",), hint=32, required=False, strict=True),
    Column("pay2_sum", "Сумма (часть 2)", ("сумма (часть 2)",), hint=33, required=False, strict=True),
    Column("pay3_date", "Дата (часть 3)", ("дата (часть 3)",), hint=34, required=False, strict=True),
    Column("pay3_sum", "Сумма (часть 3)", ("сумма (часть 3)",), hint=35, required=False, strict=True),
    Column("act_state", "АВР (наша)", ("авр (наша)",), hint=36, required=False, strict=True),
    Column("act_sum", "Сумма (наша)", ("сумма (наша)",), hint=37, required=False, strict=True),
    Column("act_no", "№ АВР (наша)", ("№ авр (наша)",), hint=38, required=False, strict=True),
    Column("act_date", "Дата (наша)", ("дата (наша)",), hint=39, required=False, strict=True),
    Column("accepted_state", "АВР (клиент принял)", ("авр (клиент принял)",), hint=40, required=False, strict=True),
    Column("accepted_date", "Дата АВР (клиент принял)", ("дата авр (клиент принял)",), hint=41, required=False, strict=True),
    Column("esf_state", "ЭСФ (отпр.)", ("эсф (отпр.)",), hint=42, required=False, strict=True),
    Column("esf_date", "Дата ЭСФ (отпр.)", ("дата эсф (отпр.)",), hint=43, required=False, strict=True),
    Column("comment", "Коммент", ("коммент",), hint=44, required=False, strict=True),
    Column("closing_acts", "Сальдо Конец (АВР)", ("сальдо конец (авр)",), hint=46, required=False, strict=True),
    Column(
        "status", "Статус", ("статус (продл./приостн./разв./на исп.)", "статус"), hint=48, required=False, strict=True
    ),
    Column("opening_accruals", "Сальдо Начало (без АВР)", ("сальдо начало (без авр)",), hint=53, required=False, strict=True),
    Column("closing_accruals", "Сальдо Конец (без АВР)", ("сальдо конец (без авр)",), hint=54, required=False, strict=True),
    Column("refund_sum", "Сумма Возврата", ("сумма возврата",), hint=55, required=False, strict=True),
    Column("refund_date", "Дата Возврата", ("дата возврата",), hint=56, required=False, strict=True),
)

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
        # «Дата АВР (клиент принял)» в части листов - число без формата даты.
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


def build_book(values: Sequence[Sequence[Any]], *, title: str, worksheet: str, spreadsheet_id: str = "") -> Book:
    """Разобрать значения листа. Шапка - первая строка; строка листа 2 - первая данных."""
    if not values:
        raise BookError(f"Лист «{worksheet}» пуст")
    try:
        layout = resolve_layout(worksheet, COLUMNS, [str(cell) for cell in values[0]])
    except LayoutError as exc:
        raise BookError(str(exc)) from exc
    rows: list[BookRow] = []
    digest = hashlib.sha1()
    for offset, raw in enumerate(values[1:]):
        row = [str(cell) for cell in raw]
        customer = text_of(layout.cell(row, "customer"))
        number = text_of(layout.cell(row, "number"))
        if not customer and not number:
            continue
        cell = lambda key: layout.cell(row, key)  # noqa: E731 - короче по месту
        period_from = date_of(cell("period_from"))
        label = text_of(cell("month"))
        payments = tuple(
            Payment(date_of(cell(f"pay{part}_date")), amount)
            for part in (1, 2, 3)
            if (amount := money_of(cell(f"pay{part}_sum"))) not in (None, Decimal("0"))
        )
        item = BookRow(
            line=offset + 2,
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
        rows.append(item)
        digest.update(f"{item.key}|{item.amount}|{item.bill_no}|{item.paid}|{item.act_no}|{item.closing_acts}\n".encode())
    if not rows:
        raise BookError(f"В листе «{worksheet}» нет ни одной строки договора - таблица, похоже, ещё собирается")
    return Book(
        read_at=datetime.now(timezone.utc),
        title=title,
        worksheet=worksheet,
        spreadsheet_id=spreadsheet_id,
        rows=rows,
        drift=layout.describe_drift(),
        rev=digest.hexdigest()[:16],
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
        #: Код юрлица в книге («BBCL») → юрлицо KORT, если код записан у юрлица.
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


# ── Кэш ─────────────────────────────────────────────────────────────────────


@dataclass
class _Slot:
    source: tuple[str, str]
    book: Book | None = None
    error: str = ""
    at: float = 0.0
    reading: threading.Event | None = None


_slots: dict[uuid.UUID, _Slot] = {}
_guard = threading.Lock()


def _read(spreadsheet_id: str, worksheet: str) -> Book:
    if not google.is_configured():
        raise BookError("Не заданы креды Google - таблицу прочитать нечем")
    try:
        meta = google.spreadsheet_meta(spreadsheet_id)
        values = google.fetch_tab_values(spreadsheet_id, worksheet, fresh=True)
    except google.GoogleError as exc:
        raise BookError(str(exc)) from exc
    return build_book(values, title=meta.get("title", ""), worksheet=worksheet, spreadsheet_id=spreadsheet_id)


def _refresh(slot: _Slot, done: threading.Event) -> None:
    try:
        book = _read(*slot.source)
        with _guard:
            slot.book, slot.error = book, ""
    except BookError as exc:
        with _guard:
            slot.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - чтение книги не должно ронять ядро
        log.exception("сводка ядра: таблица не прочиталась")
        with _guard:
            slot.error = f"Таблица не прочиталась: {exc}"
    finally:
        with _guard:
            slot.at = time.monotonic()
            slot.reading = None
        done.set()


def current(
    session: Session, workspace_id: uuid.UUID, *, wait: float = FIRST_READ_WAIT, force: bool = False
) -> tuple[Book | None, str]:
    """Книга компании и отказ чтения. `(None, "")` - книга-сводка не подключена."""
    source = source_of(session, workspace_id)
    if source is None:
        return None, ""
    key = (source.spreadsheet_id, source.worksheet)
    with _guard:
        slot = _slots.get(workspace_id)
        if slot is not None and slot.source == ANY:
            return slot.book, slot.error
        if slot is None or slot.source != key:
            slot = _slots[workspace_id] = _Slot(source=key)
        age = time.monotonic() - slot.at
        stale = force or age >= (ERROR_RETRY if slot.book is None and slot.error else CACHE_TTL) or (
            slot.book is None and not slot.error
        )
        if slot.reading is not None:
            done = slot.reading
        elif stale:
            done = slot.reading = threading.Event()
            threading.Thread(target=_refresh, args=(slot, done), daemon=True).start()
        else:
            done = threading.Event()
            done.set()
    if (slot.book is None or force) and wait > 0:
        done.wait(wait)
    with _guard:
        return slot.book, slot.error


#: Книга, положенная руками (`put`), - отдаётся при любом подключённом источнике.
ANY = ("*", "*")


def put(workspace_id: uuid.UUID, book: Book) -> None:
    """Положить прочитанную книгу (тесты, сквозные прогоны без Google)."""
    with _guard:
        _slots[workspace_id] = _Slot(source=ANY, book=book, at=time.monotonic())


def forget(workspace_id: uuid.UUID | None = None) -> None:
    with _guard:
        if workspace_id is None:
            _slots.clear()
        else:
            _slots.pop(workspace_id, None)


def rows_of(book: Book, matcher: Matcher) -> Iterable[tuple[BookRow, RowMatch]]:
    for row in book.rows:
        yield row, matcher.match(row)


__all__ = [
    "Book",
    "BookError",
    "BookRow",
    "COLUMNS",
    "Matcher",
    "Payment",
    "RowMatch",
    "build_book",
    "current",
    "date_of",
    "flag_of",
    "forget",
    "money_of",
    "month_of",
    "put",
    "rows_of",
    "text_of",
]
