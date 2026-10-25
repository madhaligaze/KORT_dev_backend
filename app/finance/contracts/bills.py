"""Счета книги-сводки: какие пора выставить, какие выставлены - и что записал KORT.

Откуда
──────
Та же книга-сводка, что даёт «Оплачено (сводка)» (`summary.py`, запись
`SummarySource`): у BBC - «Осн.Общая сводка BBC 2026», лист «Сводка все ЮР
лица», строка - договор за месяц. Лист собирает формула (`IMPORTRANGE`) из книг
девяти юрлиц, а колонку «Счет» каждая книга считает сама: заполнен «№ Счета» -
`TRUE`, нет - состояние словами: «Просрочка более 5 дней», «3 дня до
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

#: Колонки сводки. `hint` - где стояли 22.10.2026 (A=0): подсказка для
#: одинаковых заголовков и строки «книгу поправили», а не адрес.
COLUMNS: tuple[Column, ...] = (
    Column("bill", "Счет", ("счет",), hint=24),
    Column("customer", "Заказчик", ("заказчик (название фирмы)", "заказчик", "клиент"), hint=8),
    Column("number", "№ Договора", ("№ договора", "номер договора"), hint=16),
    Column("own", "Наша Фирма", ("наша фирма",), hint=5),
    Column("month", "Месяц строки (Техн. 1)", ("техн. 1",), hint=3, required=False),
    Column("month_no", "Мес", ("мес",), hint=2, required=False),
    Column("kind", "Вид Услуги", ("вид услуги",), hint=6, required=False),
    Column("staff", "Наш Сотрудник", ("наш сотрудник",), hint=7, required=False),
    Column("amount", "Сумма Договора", ("сумма договора",), hint=12, required=False),
    Column("department", "Отдел", ("отдел",), hint=13, required=False),
    Column("period_from", "Период с (нач.)", ("период с (нач.)", "период с"), hint=18, required=False),
    Column("period_to", "Период по (зав.)", ("период по (зав.)", "период по"), hint=19, required=False),
    Column("note", "Доп. Коммент.", ("доп. коммент.", "доп. комментарий"), hint=20, required=False),
    # Для карточки и шаблона финансистов: как клиент назван в 1С (по нему
    # выставляют счёт) и когда заключён договор.
    Column("onec_name", "По 1C", ("по 1c", "по 1с"), hint=10, required=False, strict=True),
    Column("signed_at", "Дата заключения Договора", ("дата заключения договора",), hint=15, required=False, strict=True),
    # Поля счёта - только по точному названию: «Счет за Период» иначе нашлась бы
    # «похожей» на «Счет», и лист без неё не читался бы вовсе.
    Column("bill_period", "Счет за Период (Коммент)", ("счет за период (коммент)",), hint=25, required=False, strict=True),
    Column("bill_no", "№ Счета", ("№ счета",), hint=26, required=False, strict=True),
    Column("bill_date", "Дата выстав. Счета", ("дата выстав. счета",), hint=27, required=False, strict=True),
    Column("paid", "Факт Оплата", ("факт оплата",), hint=28, required=False),
    Column(
        "status",
        "Статус договора",
        ("статус (продл./приостн./разв./на исп.)", "статус"),
        hint=48,
        required=False,
    ),
)


class BillsError(Exception):
    """Книгу нельзя прочитать - с текстом для человека."""


def state_of(value: Any) -> str | None:
    """Группа строки по значению «Счет»; `None` - счёт выставлен."""
    text = norm(value)
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
        }


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


def build_board(
    values: Sequence[Sequence[Any]], *, title: str, worksheet: str, spreadsheet_id: str = ""
) -> Board:
    """Разобрать значения листа. Шапка - первая строка, строка листа 2 - первая данных."""
    if not values:
        raise BillsError(f"Лист «{worksheet}» пуст")
    try:
        layout = resolve_layout(worksheet, COLUMNS, [str(cell) for cell in values[0]])
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
    source: tuple[str, str]
    board: Board | None = None
    error: str = ""
    at: float = 0.0
    reading: threading.Event | None = None


_slots: dict[uuid.UUID, _Slot] = {}
_guard = threading.Lock()


def _read(spreadsheet_id: str, worksheet: str) -> Board:
    if not google.is_configured():
        raise BillsError("Не заданы креды Google - таблицу прочитать нечем")
    try:
        meta = google.spreadsheet_meta(spreadsheet_id)
        values = google.fetch_tab_values(spreadsheet_id, worksheet, fresh=True)
    except google.GoogleError as exc:
        raise BillsError(str(exc)) from exc
    return build_board(values, title=meta.get("title", ""), worksheet=worksheet, spreadsheet_id=spreadsheet_id)


def _refresh(slot: _Slot, done: threading.Event) -> None:
    try:
        board = _read(*slot.source)
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


def _start(workspace_id: uuid.UUID, source: tuple[str, str], *, force: bool = False) -> tuple[_Slot, threading.Event]:
    """Слот компании и событие текущего чтения (начатого сейчас или раньше)."""
    with _guard:
        slot = _slots.get(workspace_id)
        if slot is None or slot.source != source:
            slot = _slots[workspace_id] = _Slot(source=source)
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
    slot, done = _start(workspace_id, (source.spreadsheet_id, source.worksheet), force=force)
    if (slot.board is None or force) and wait > 0:
        done.wait(wait)
    return slot


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
    return (view(workspace_id, switched(session, workspace_id, board)) if board is not None else None), error


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
    slot, _ = _start(workspace_id, (source.spreadsheet_id, source.worksheet))
    with _guard:
        board = slot.board
    if board is None:
        return None
    counts = view(workspace_id, switched(session, workspace_id, board)).counts()
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
    "counts_out",
    "current",
    "forget",
    "get",
    "mark",
    "read_date",
    "recent",
    "state_of",
    "switched",
    "view",
    "window_start",
]
