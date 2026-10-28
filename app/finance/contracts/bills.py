"""Счета книги-сводки: какие пора выставить, какие выставлены - и что записал KORT.

Откуда
──────
Та же сводная таблица, что даёт «Оплачено (сводка)» (`summary.py`, запись
`SummarySource`), строка - договор за месяц. Лист собирает формула
(`IMPORTRANGE`) из таблиц юрлиц, а отметку счёта каждая таблица считает сама:
заполнен номер счёта - `TRUE`, нет - состояние словами: «Просрочка более 5 дней», «3 дня до
выставления», «Еще рано» (разобрано 22.10.2026).

Что здесь по-другому и почему
─────────────────────────────
* **Состояние - словами книги, а не своим счётом.** Свой срок («просрочено
  4 дня» от «Периода с») однажды разошёлся бы с книгой на день, и в KORT, в
  канале и в книге стояло бы три ответа на один вопрос. KORT только раскладывает
  слова книги по группам (`state_of`); незнакомое значение не угадывается, а
  уходит как есть в группу «Без срока».
* **Сводку KORT не правит.** Её собирает формула, правка в ней пропала бы.
  Счёт отмечают в книге юрлица - руками или из карточки KORT
  (`bill_book.py`). По этим строкам KORT не заводит ни счетов, ни ожиданий -
  иначе долг клиента появился бы дважды: в книге и в «Долгах».
* **Записанное из KORT видно сразу** (`mark`, `view`). Сводка догоняет книгу
  юрлица с задержкой: формуле `IMPORTRANGE` нужны секунды-минуты, да и само
  чтение держится `CACHE_TTL`. Без отметки выставленный счёт ещё минуту висел
  бы в списке и в канале, и его выставили бы второй раз.
* **Колонки - по названиям** (`app.finance.layout`): рядом с «Счет» стоят «№
  Счета», «Счет за Период» и «Счет2», и прибитая буква однажды читала бы
  соседнюю.
* **Юрлицо, переведённое на KORT** («Сверка → Переход», `core/switch.py`), -
  исключение из первого пункта: его счета считает KORT по начислениям
  договоров (`switched`), словами той же формулы, а месяц без строки в
  таблице приходит счётом без строки (`line = 0`), который записывается в
  KORT. Строка без договора в KORT остаётся словами таблицы.

Чтение - одно на компанию раз в `CACHE_TTL`, как у сводки оплат; его делят
раздел «Счета», число у пункта колонки и бот (`bills_bot.py`).
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.finance import google
from app.finance.contracts.summary import read_money, source_of
from app.finance.core.columns import column, with_company_names
from app.finance.layout import Column, LayoutError, norm, resolve_layout

log = logging.getLogger(__name__)

#: Сколько держится прочитанная книга. Две минуты - как у сводки оплат.
CACHE_TTL = 120.0
#: Через сколько повторять чтение, которое кончилось отказом.
ERROR_RETRY = 60.0
#: Сколько ждать Google на первом чтении, прежде чем ответить «читаем».
FIRST_READ_WAIT = 20.0
#: Чаще этого «Обновить» книгу не перечитывает: квота Google общая на аккаунт.
FORCE_GAP = 30.0
#: Сколько держится записанное из KORT, пока сводка его не покажет. Дольше -
#: значит, в книге его поправили руками, и права книга.
MARK_TTL = 600.0
#: Время книг - по Казахстану (UTC+5): «сегодня» и окно «Выставлены».
TZ = timezone(timedelta(hours=5))

#: Группы невыставленных счетов - в порядке срочности.
OVERDUE = "overdue"  # «Просрочка 1 день», «Просрочка более 5 дней»
SOON = "soon"  # «3 дня до выставления», «Сегодня день выставления»
EARLY = "early"  # «Еще рано»
UNDATED = "undated"  # пусто, FALSE без слов, незнакомое значение
GROUPS = (OVERDUE, SOON, EARLY, UNDATED)
#: Выставленный счёт: «Счет» = TRUE.
DONE = "issued"

#: Галочка «Счет» в форматированном значении: Google пишет её по локали книги.
ISSUED = frozenset({"true", "истина"})
PAID = frozenset({"true", "истина"})

#: Колонки сводки - ключи ядра (`core/columns.py`); шапка компании - её
#: словарь колонок. «bill» - колонка «Счет» (в ядре `bill_state`), «paid» -
#: отметка «оплачено» (в ядре `paid_state`).
COLUMNS: tuple[Column, ...] = (
    column("bill", core="bill_state", required=True, strict=False),
    column("customer", required=True, strict=False),
    column("number", required=True, strict=False),
    column("own", required=True, strict=False),
    column("month", strict=False),
    column("month_no", strict=False),
    column("kind", strict=False),
    column("staff", strict=False),
    column("amount", strict=False),
    column("department", strict=False),
    column("period_from", strict=False),
    column("period_to", strict=False),
    column("note", strict=False),
    # Для карточки и шаблона счетов: как клиент назван в 1С (по нему
    # выставляют счёт) и когда заключён договор.
    column("onec_name"),
    column("signed_at"),
    # Поля счёта - только по точному названию: «Счёт за период» иначе нашлась
    # бы «похожей» на «Счёт», и лист без неё не читался бы вовсе.
    column("bill_period"),
    column("bill_no"),
    column("bill_date"),
    column("paid", core="paid_state", strict=False),
    column("status", strict=False),
)


class BillsError(Exception):
    """Книгу нельзя прочитать - с текстом для человека."""


def state_of(value: Any, words: dict[str, str] | None = None) -> str | None:
    """Группа строки по значению «Счет»; `None` - счёт выставлен.

    `words` - слова компании (`core/settings.table_words`, колонка «Счет»):
    смысл, который им дал человек, главнее обычных слов ниже.
    """
    text = norm(value)
    meaning = (words or {}).get(text)
    if meaning:
        return None if meaning == "issued" else meaning
    if text in ISSUED:
        return None
    if text.startswith("просрочка"):
        return OVERDUE
    # «Сегодня день выставления» уходило в «Без срока»: день счёта, а не неизвестность.
    if "до выставления" in text or text.startswith("сегодня"):
        return SOON
    if text.startswith("еще рано"):
        return EARLY
    return UNDATED


def read_date(value: Any) -> date | None:
    """«25.10.2026» → дата; пусто и непонятное - `None`, а не догадка."""
    text = str(value or "").strip()
    for pattern in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _paid(value: Any) -> str:
    text = norm(value)
    if text in PAID:
        return "paid"
    return "part" if text.startswith("част") else ""


def bill_key(own: str, customer: str, number: str, month: str) -> str:
    """Чей это счёт: юрлицо, клиент, № договора, месяц строки - без оформления.

    Не номер строки: лист сводки сортирует формула, и строка переезжает.
    """
    return "|".join(norm(part) for part in (own, customer, number, month))


def bill_id(key: str) -> str:
    """Короткий постоянный адрес счёта - для ссылки из канала и адреса карточки."""
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _int(value: Any) -> int | None:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class Bill:
    """Строка книги-сводки: договор за месяц. `group` - `DONE`, если «Счет» = TRUE."""

    line: int
    group: str
    text: str
    own: str
    customer: str
    number: str
    month: str
    month_no: int | None
    kind: str
    staff: str
    department: str
    amount: Decimal | None
    period_from: date | None
    period_to: date | None
    paid: str
    status: str
    note: str
    bill_no: str = ""
    bill_date: str = ""
    bill_period: str = ""
    onec_name: str = ""
    signed_at: str = ""
    #: `kort` - счета юрлица ведёт KORT (`core/switch.py`): состояние посчитано по
    #: начислениям договора, а не словами таблицы; `line == 0` - строки в таблице нет.
    source: str = ""
    contract_id: str = ""
    #: Пост бота по слову «Вида Услуги» из словаря компании (`abon`, `rent`, `skip`);
    #: пусто - бот понимает вид сам (`bills_bot.kind_of`).
    post: str = ""

    @property
    def key(self) -> str:
        return bill_key(self.own, self.customer, self.number, self.month)

    @property
    def id(self) -> str:
        return bill_id(self.key)

    def out(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "line": self.line,
            "group": self.group,
            "text": self.text,
            "own": self.own,
            "customer": self.customer,
            "number": self.number,
            "month": self.month,
            "kind": self.kind,
            "staff": self.staff,
            "department": self.department,
            "amount": str(self.amount) if self.amount is not None else None,
            "period_from": self.period_from.isoformat() if self.period_from else None,
            "period_to": self.period_to.isoformat() if self.period_to else None,
            "paid": self.paid,
            "status": self.status,
            "note": self.note,
            "bill_no": self.bill_no,
            "bill_date": self.bill_date,
            "bill_period": self.bill_period,
            "onec_name": self.onec_name,
            "signed_at": self.signed_at,
            "source": self.source,
            "contract_id": self.contract_id or None,
            "key": self.key,
            "month_no": self.month_no,
            "post": self.post,
        }

    @classmethod
    def from_out(cls, item: dict[str, Any]) -> "Bill":
        """Счёт из ответа - обратно в строку: бот берёт счета вызовом «спросить» (`model/ask.py`)."""
        amount = item.get("amount")
        return cls(
            line=int(item.get("line") or 0),
            group=str(item.get("group") or ""),
            text=str(item.get("text") or ""),
            own=str(item.get("own") or ""),
            customer=str(item.get("customer") or ""),
            number=str(item.get("number") or ""),
            month=str(item.get("month") or ""),
            month_no=item.get("month_no"),
            kind=str(item.get("kind") or ""),
            staff=str(item.get("staff") or ""),
            department=str(item.get("department") or ""),
            amount=Decimal(str(amount)) if amount not in (None, "") else None,
            period_from=date.fromisoformat(item["period_from"]) if item.get("period_from") else None,
            period_to=date.fromisoformat(item["period_to"]) if item.get("period_to") else None,
            paid=str(item.get("paid") or ""),
            status=str(item.get("status") or ""),
            note=str(item.get("note") or ""),
            bill_no=str(item.get("bill_no") or ""),
            bill_date=str(item.get("bill_date") or ""),
            bill_period=str(item.get("bill_period") or ""),
            onec_name=str(item.get("onec_name") or ""),
            signed_at=str(item.get("signed_at") or ""),
            source=str(item.get("source") or ""),
            contract_id=str(item.get("contract_id") or ""),
            post=str(item.get("post") or ""),
        )


def _order(bill: Bill) -> tuple[Any, ...]:
    """Старое - первым: просрочка за июнь важнее просрочки за октябрь."""
    return (GROUPS.index(bill.group), bill.period_from or date.max, bill.own, bill.customer, bill.line)


@dataclass
class Board:
    """Прочитанная книга: невыставленные и выставленные счета и откуда они."""

    read_at: datetime
    title: str
    worksheet: str
    spreadsheet_id: str
    bills: list[Bill] = field(default_factory=list)
    rows: int = 0
    drift: str = ""
    #: Ключи строк с галочкой «Счет» (`bill_key`): бот убирает их из своих
    #: сообщений. Пропавшая строка - не «выставлен»: её могла на минуту
    #: потерять формула сводки.
    issued: set[str] = field(default_factory=set)
    #: Выставленные строки целиком - для «Выставлены» и карточки счёта.
    done: list[Bill] = field(default_factory=list)
    #: Юрлица, чьи счета ведёт KORT (коды), и отказ расчёта, если был:
    #: тогда их строки - словами таблицы, как до перехода.
    kort: tuple[str, ...] = ()
    kort_error: str = ""

    def of(self, group: str) -> list[Bill]:
        return [bill for bill in self.bills if bill.group == group]

    def counts(self) -> dict[str, int]:
        out = {group: 0 for group in GROUPS}
        for bill in self.bills:
            out[bill.group] += 1
        return out

    def total(self, group: str) -> Decimal:
        return sum((bill.amount or Decimal("0") for bill in self.of(group)), Decimal("0"))

    def find(self, wanted: str) -> Bill | None:
        """Счёт по `bill_id` - среди невыставленных и выставленных."""
        return next((bill for bill in [*self.bills, *self.done] if bill.id == wanted), None)

    @classmethod
    def from_answer(cls, answer: dict[str, Any]) -> "Board | None":
        """Книга из ответа «спросить» вида «Не выставлены»; `None` - книга не подключена или не прочиталась."""
        source = answer.get("board") or None
        if source is None or not source.get("read_at"):
            return None
        return cls(
            read_at=datetime.fromisoformat(source["read_at"]),
            title=str(source.get("title") or ""),
            worksheet=str(source.get("worksheet") or ""),
            spreadsheet_id="",
            bills=[Bill.from_out(item) for item in answer.get("rows") or []],
            rows=len(answer.get("rows") or []),
            drift=str(source.get("drift") or ""),
            issued=set(answer.get("issued") or []),
            done=[Bill.from_out(item) for item in answer.get("done") or []],
            kort=tuple(source.get("kort") or ()),
            kort_error=str(source.get("kort_error") or ""),
        )


#: Ключ колонки здесь → ключ ядра в словаре колонок компании, где они разные.
_CORE_KEYS = {"bill": "bill_state", "paid": "paid_state"}


def columns_for(extra: dict[str, list[str]] | None) -> tuple[Column, ...]:
    """Колонки «Счетов» со своими названиями компании - тот же словарь, что у ядра («Колонки для ядра»)."""
    return with_company_names(COLUMNS, extra, _CORE_KEYS) if extra else COLUMNS


def build_board(
    values: Sequence[Sequence[Any]], *, title: str, worksheet: str, spreadsheet_id: str = "",
    columns: Sequence[Column] = COLUMNS,
) -> Board:
    """Разобрать значения листа. Шапка - первая строка, строка листа 2 - первая данных."""
    if not values:
        raise BillsError(f"Лист «{worksheet}» пуст")
    try:
        layout = resolve_layout(worksheet, columns, [str(cell) for cell in values[0]])
    except LayoutError as exc:
        raise BillsError(str(exc)) from exc
    bills: list[Bill] = []
    done: list[Bill] = []
    count = 0
    for offset, raw in enumerate(values[1:]):
        row = [str(cell) for cell in raw]
        customer = layout.cell(row, "customer").strip()
        number = layout.cell(row, "number").strip()
        if not customer and not number:
            continue
        count += 1
        text = layout.cell(row, "bill").strip()
        group = state_of(text)
        amount_text = layout.cell(row, "amount").strip()
        bill = Bill(
            line=offset + 2,
            group=DONE if group is None else group,
            text=text,
            own=layout.cell(row, "own").strip(),
            customer=customer,
            number=number,
            month=layout.cell(row, "month").strip(),
            month_no=_int(layout.cell(row, "month_no")),
            kind=layout.cell(row, "kind").strip(),
            staff=layout.cell(row, "staff").strip(),
            department=layout.cell(row, "department").strip(),
            amount=read_money(amount_text) if amount_text else None,
            period_from=read_date(layout.cell(row, "period_from")),
            period_to=read_date(layout.cell(row, "period_to")),
            paid=_paid(layout.cell(row, "paid")),
            status=layout.cell(row, "status").strip(),
            note=layout.cell(row, "note").strip(),
            bill_no=layout.cell(row, "bill_no").strip(),
            bill_date=layout.cell(row, "bill_date").strip(),
            bill_period=layout.cell(row, "bill_period").strip(),
            onec_name=layout.cell(row, "onec_name").strip(),
            signed_at=layout.cell(row, "signed_at").strip(),
        )
        (done if group is None else bills).append(bill)
    if not count:
        # Пустой лист под шапкой - это сводка, которую формула ещё собирает, а не
        # «всё выставлено»: прежнее чтение остаётся, бот не правит пост в пустоту.
        raise BillsError(f"В листе «{worksheet}» нет ни одной строки договора - таблица, похоже, ещё собирается")
    bills.sort(key=_order)
    return Board(
        read_at=datetime.now(timezone.utc),
        title=title,
        worksheet=worksheet,
        spreadsheet_id=spreadsheet_id,
        bills=bills,
        rows=count,
        drift=layout.describe_drift(),
        issued={bill.key for bill in done},
        done=done,
    )


# ── Записанное из KORT ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Mark:
    """Что KORT только что записал в книгу юрлица - до того, как это покажет сводка."""

    issued: bool
    bill_no: str
    bill_date: str
    bill_period: str
    note: str
    written_at: datetime
    at: float


_marks: dict[uuid.UUID, dict[str, Mark]] = {}


def mark(workspace_id: uuid.UUID, key: str, values: dict[str, str]) -> None:
    """Запомнить записанное в книгу юрлица: `values` - как его покажет книга."""
    stamp = Mark(
        issued=bool(values.get("bill_no", "").strip()),
        bill_no=values.get("bill_no", ""),
        bill_date=values.get("bill_date", ""),
        bill_period=values.get("bill_period", ""),
        note=values.get("note", ""),
        written_at=datetime.now(timezone.utc),
        at=time.monotonic(),
    )
    with _guard:
        _marks.setdefault(workspace_id, {})[key] = stamp
        slot = _slots.get(workspace_id)
        if slot is not None:
            slot.at = 0.0  # следующее обращение перечитает сводку


def _group_by_date(period_from: date | None, today: date) -> str:
    """Группа снятой отметки до перечитывания сводки - тем же правилом, что формула книги."""
    if period_from is None:
        return UNDATED
    if (period_from - today).days > 3:
        return EARLY
    return SOON if period_from >= today else OVERDUE


def view(workspace_id: uuid.UUID, board: Board) -> Board:
    """Книга с записанным из KORT: пока сводка его не показала, верим записи.

    Отметка уходит, когда сводка, прочитанная после записи, показывает то же
    самое, или через `MARK_TTL`: тогда книгу, видимо, поправили руками, и права
    книга, а не KORT.
    """
    now = time.monotonic()
    with _guard:
        marks = _marks.get(workspace_id)
        if not marks:
            return board
        for key, stamp in list(marks.items()):
            shown = (key in board.issued) == stamp.issued
            if now - stamp.at > MARK_TTL or (shown and board.read_at >= stamp.written_at):
                del marks[key]
        marks = dict(marks)
    if not marks:
        return board
    today = datetime.now(TZ).date()
    bills: list[Bill] = []
    done: list[Bill] = []
    for bill in [*board.bills, *board.done]:
        stamp = marks.get(bill.key)
        if stamp is None:
            (done if bill.group == DONE else bills).append(bill)
            continue
        fields = {
            "bill_no": stamp.bill_no,
            "bill_date": stamp.bill_date,
            "bill_period": stamp.bill_period,
            "note": stamp.note,
        }
        if stamp.issued:
            done.append(dataclasses.replace(bill, group=DONE, text="TRUE", **fields))
        else:
            group = bill.group if bill.group != DONE else _group_by_date(bill.period_from, today)
            text = bill.text if bill.group != DONE else "отметка снята в KORT"
            bills.append(dataclasses.replace(bill, group=group, text=text, **fields))
    bills.sort(key=_order)
    return dataclasses.replace(
        board, bills=bills, done=done, issued={bill.key for bill in done}
    )


# ── Кэш ─────────────────────────────────────────────────────────────────────


@dataclass
class _Slot:
    #: Таблица, лист и подпись словаря колонок компании: новое название колонки - перечитать.
    source: tuple[str, ...]
    board: Board | None = None
    error: str = ""
    at: float = 0.0
    reading: threading.Event | None = None
    extra: dict[str, list[str]] = field(default_factory=dict)


_slots: dict[uuid.UUID, _Slot] = {}
_guard = threading.Lock()


def _read(spreadsheet_id: str, worksheet: str, extra: dict[str, list[str]] | None = None) -> Board:
    if not google.is_configured():
        raise BillsError("Не заданы креды Google - таблицу прочитать нечем")
    try:
        meta = google.spreadsheet_meta(spreadsheet_id)
        values = google.fetch_tab_values(spreadsheet_id, worksheet, fresh=True)
    except google.GoogleError as exc:
        raise BillsError(str(exc)) from exc
    return build_board(
        values, title=meta.get("title", ""), worksheet=worksheet, spreadsheet_id=spreadsheet_id, columns=columns_for(extra)
    )


def _source_key(session: Session, workspace_id: uuid.UUID, source: Any) -> tuple[tuple[str, ...], dict[str, list[str]]]:
    """Ключ чтения: таблица, лист и подпись словаря колонок компании - и сам словарь."""
    from app.finance.core import book as core_book
    from app.finance.core import settings as core_settings

    extra = core_settings.book_columns(session, workspace_id)
    return (source.spreadsheet_id, source.worksheet, core_book.signature(extra)), extra


def _refresh(slot: _Slot, done: threading.Event) -> None:
    try:
        # Без своих названий колонок - прежний вызов в два аргумента (его подменяют тесты).
        board = _read(*slot.source[:2], extra=slot.extra) if slot.extra else _read(*slot.source[:2])
        with _guard:
            slot.board, slot.error = board, ""
    except BillsError as exc:
        with _guard:
            slot.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - чтение книги не должно ронять раздел
        log.exception("счета: таблица не прочиталась")
        with _guard:
            slot.error = f"Таблица не прочиталась: {exc}"
    finally:
        with _guard:
            slot.at = time.monotonic()
            slot.reading = None
        done.set()


def _start(
    workspace_id: uuid.UUID, source: tuple[str, ...], *, force: bool = False, extra: dict[str, list[str]] | None = None
) -> tuple[_Slot, threading.Event]:
    """Слот компании и событие текущего чтения (начатого сейчас или раньше)."""
    with _guard:
        slot = _slots.get(workspace_id)
        if slot is None or slot.source != source:
            slot = _slots[workspace_id] = _Slot(source=source, extra=dict(extra or {}))
        age = time.monotonic() - slot.at
        # Отказ Google не повторяется на каждый запрос: раз в минуту, иначе
        # ошибка съела бы квоту всего аккаунта.
        stale = age >= (ERROR_RETRY if slot.board is None and slot.error else CACHE_TTL) or (
            slot.board is None and not slot.error
        )
        if force and age >= FORCE_GAP:
            stale = True
        if slot.reading is not None:
            return slot, slot.reading
        if not stale:
            done = threading.Event()
            done.set()
            return slot, done
        done = slot.reading = threading.Event()
    threading.Thread(target=_refresh, args=(slot, done), daemon=True).start()
    return slot, done


def get(session: Session, workspace_id: uuid.UUID, *, wait: float = FIRST_READ_WAIT, force: bool = False) -> _Slot | None:
    """Книга компании: прочитанная, при необходимости - перечитанная.

    Первое чтение ждём до `wait` секунд; «Обновить» (`force`) - тоже, чтобы
    человек увидел свежее, а не то, что было до нажатия. `None` - книга-сводка
    не подключена.
    """
    source = source_of(session, workspace_id)
    if source is None:
        return None
    key, extra = _source_key(session, workspace_id, source)
    slot, done = _start(workspace_id, key, force=force, extra=extra)
    if (slot.board is None or force) and wait > 0:
        done.wait(wait)
    return slot


def worded(board: Board, words: dict[str, dict[str, str]]) -> Board:
    """Сводка со словами компании (`core/settings.table_words`): смысл слов «Счет» и пост бота по «Виду Услуги».

    Применяется к прочитанному, без нового чтения таблицы: слово, которому
    дали смысл, действует сразу.
    """
    bill_words, kind_words = words.get("bill") or {}, words.get("kind") or {}
    if not bill_words and not kind_words:
        return board
    bills: list[Bill] = []
    done: list[Bill] = []
    for bill in [*board.bills, *board.done]:
        state = state_of(bill.text, bill_words) if bill_words else (None if bill.group == DONE else bill.group)
        group = DONE if state is None else state
        post = kind_words.get(norm(bill.kind), "")
        if group != bill.group or post != bill.post:
            bill = dataclasses.replace(bill, group=group, post=post)
        (done if bill.group == DONE else bills).append(bill)
    bills.sort(key=_order)
    return dataclasses.replace(board, bills=bills, done=done, issued={bill.key for bill in done})


def prepared(session: Session, workspace_id: uuid.UUID, board: Board) -> Board:
    """Прочитанная сводка такой, какой её видят список, число у пункта и бот: слова компании, затем юрлица на KORT."""
    from app.finance.core import settings as core_settings

    return switched(session, workspace_id, worded(board, core_settings.table_words(session, workspace_id)))


def switched(session: Session, workspace_id: uuid.UUID, board: Board) -> Board:
    """Сводка, где счета юрлиц, переведённых на KORT, посчитаны KORT (`core/switch.py`).

    Сбой расчёта не роняет раздел и бот: строки таких юрлиц остаются словами
    таблицы, а причина - в `kort_error`, и раздел её показывает.
    """
    try:
        from app.finance.core import switch

        return switch.bills_board(session, workspace_id, board)
    except Exception as exc:  # noqa: BLE001 - список счетов важнее расчёта KORT
        log.exception("счета: KORT не посчитал счета юрлиц")
        return dataclasses.replace(board, kort_error=f"Счета юрлиц, переведённых на KORT, сейчас из таблицы: {exc}")


def current(session: Session, workspace_id: uuid.UUID, **kwargs: Any) -> tuple[Board | None, str]:
    """Книга с записанным из KORT и отказ чтения, если был. `(None, "")` - книга не подключена."""
    slot = get(session, workspace_id, **kwargs)
    if slot is None:
        return None, ""
    with _guard:
        board, error = slot.board, slot.error
    return (view(workspace_id, prepared(session, workspace_id, board)) if board is not None else None), error


def forget(workspace_id: uuid.UUID) -> None:
    with _guard:
        _slots.pop(workspace_id, None)
        _marks.pop(workspace_id, None)


# ── Наружу ───────────────────────────────────────────────────────────────────


def counts_out(board: Board) -> dict[str, Any]:
    counts = board.counts()
    return {
        **counts,
        "overdue_total": str(board.total(OVERDUE)),
        "soon_total": str(board.total(SOON)),
    }


def window_start(today: date) -> date:
    """Первое число прошлого месяца: с него «Выставлены» и окно бота."""
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return date(year, month, 1)


def recent(board: Board, today: date | None = None) -> list[Bill]:
    """Выставленные за прошлый и текущий месяц (по «Периоду с») - свежие первыми.

    Весь год выставленного - тысяча строк, и правят обычно вчерашнее: окно то
    же, что у бота.
    """
    start = window_start(today or datetime.now(TZ).date())
    items = [bill for bill in board.done if bill.period_from is not None and bill.period_from >= start]
    return sorted(items, key=lambda bill: (bill.period_from or date.min, bill.line), reverse=True)


def board_out(session: Session, workspace_id: uuid.UUID, *, force: bool = False) -> dict[str, Any]:
    """Раздел «Счета»: невыставленные с числами по группам, недавно выставленные и откуда они."""
    board, error = current(session, workspace_id, force=force)
    source = source_of(session, workspace_id)
    if source is None:
        return {"source": None, "items": [], "issued": [], "counts": None}
    return {
        "source": {
            "title": (board.title if board else "") or source.title,
            "worksheet": source.worksheet,
            "url": f"https://docs.google.com/spreadsheets/d/{source.spreadsheet_id}/edit",
            "read_at": board.read_at.isoformat() if board else None,
            "drift": board.drift if board else "",
            "error": error,
            "kort": list(board.kort) if board else [],
            "kort_error": board.kort_error if board else "",
        },
        "items": [bill.out() for bill in board.bills] if board else [],
        "issued": [bill.out() for bill in recent(board)] if board else [],
        "counts": counts_out(board) if board else None,
    }


def badge(session: Session, workspace_id: uuid.UUID) -> dict[str, int] | None:
    """Число у пункта «Счета» - без похода в Google: устаревшая книга дочитается в фоне."""
    source = source_of(session, workspace_id)
    if source is None:
        return None
    key, extra = _source_key(session, workspace_id, source)
    slot, _ = _start(workspace_id, key, extra=extra)
    with _guard:
        board = slot.board
    if board is None:
        return None
    counts = view(workspace_id, prepared(session, workspace_id, board)).counts()
    return {OVERDUE: counts[OVERDUE], SOON: counts[SOON]}


__all__ = [
    "Bill",
    "BillsError",
    "Board",
    "COLUMNS",
    "DONE",
    "EARLY",
    "GROUPS",
    "OVERDUE",
    "SOON",
    "UNDATED",
    "badge",
    "bill_id",
    "bill_key",
    "board_out",
    "build_board",
    "columns_for",
    "counts_out",
    "current",
    "forget",
    "get",
    "mark",
    "read_date",
    "recent",
    "prepared",
    "state_of",
    "switched",
    "worded",
    "view",
    "window_start",
]
