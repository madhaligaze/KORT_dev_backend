"""«Не выставлены» - счета, которые по книге-сводке пора выставить, а галочки «Счет» нет.

Откуда
──────
Та же книга-сводка, что даёт «Оплачено (сводка)» (`summary.py`, запись
`SummarySource`): у BBC - «Осн.Общая сводка BBC 2026», лист «Сводка все ЮР
лица», строка - договор за месяц. Лист собирает формула (`IMPORTRANGE`) из книг
девяти юрлиц, а колонку «Счет» каждая книга считает сама: галочка - `TRUE`, без
галочки - состояние словами: «Просрочка более 5 дней», «3 дня до выставления»,
«Еще рано» (разобрано 22.10.2026).

Что здесь по-другому и почему
─────────────────────────────
* **Состояние - словами книги, а не своим счётом.** Свой срок («просрочено
  4 дня» от «Периода с») однажды разошёлся бы с книгой на день, и в KORT, в
  канале и в книге стояло бы три ответа на один вопрос. KORT только раскладывает
  слова книги по группам (`state_of`); незнакомое значение не угадывается, а
  уходит как есть в группу «Без срока».
* **Только чтение.** Счёт выставляют в 1С и отмечают в книге своего юрлица:
  сводку собирает формула, правка в ней пропала бы. По этим строкам KORT не
  заводит ни счетов, ни ожиданий - иначе долг клиента появился бы дважды: в
  книге и в «Долгах».
* **Колонки - по названиям** (`app.finance.layout`): рядом с «Счет» стоят «№
  Счета», «Счет за Период» и «Счет2», и прибитая буква однажды читала бы
  соседнюю.

Чтение - одно на компанию раз в `CACHE_TTL`, как у сводки оплат; его делят
раздел «Счета», число у пункта колонки и бот (`bills_bot.py`).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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

#: Группы невыставленных счетов - в порядке срочности.
OVERDUE = "overdue"  # «Просрочка 1 день», «Просрочка более 5 дней»
SOON = "soon"  # «3 дня до выставления»
EARLY = "early"  # «Еще рано»
UNDATED = "undated"  # пусто, FALSE без слов, незнакомое значение
GROUPS = (OVERDUE, SOON, EARLY, UNDATED)

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
    Column("note", "Доп. Коммент.", ("доп. коммент.", "доп. комментарий"), hint=20, required=False),
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
    if "до выставления" in text:
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


def _int(value: Any) -> int | None:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class Bill:
    """Строка книги без галочки «Счет»: договор за месяц."""

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
    paid: str
    status: str
    note: str

    @property
    def key(self) -> str:
        return bill_key(self.own, self.customer, self.number, self.month)

    def out(self) -> dict[str, Any]:
        return {
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
            "paid": self.paid,
            "status": self.status,
            "note": self.note,
        }


def _order(bill: Bill) -> tuple[Any, ...]:
    """Старое - первым: просрочка за июнь важнее просрочки за октябрь."""
    return (GROUPS.index(bill.group), bill.period_from or date.max, bill.own, bill.customer, bill.line)


@dataclass
class Board:
    """Прочитанная книга: невыставленные счета и откуда они."""

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

    def of(self, group: str) -> list[Bill]:
        return [bill for bill in self.bills if bill.group == group]

    def counts(self) -> dict[str, int]:
        out = {group: 0 for group in GROUPS}
        for bill in self.bills:
            out[bill.group] += 1
        return out

    def total(self, group: str) -> Decimal:
        return sum((bill.amount or Decimal("0") for bill in self.of(group)), Decimal("0"))


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
    issued: set[str] = set()
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
        if group is None:
            issued.add(
                bill_key(layout.cell(row, "own").strip(), customer, number, layout.cell(row, "month").strip())
            )
            continue
        amount_text = layout.cell(row, "amount").strip()
        bills.append(
            Bill(
                line=offset + 2,
                group=group,
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
                paid=_paid(layout.cell(row, "paid")),
                status=layout.cell(row, "status").strip(),
                note=layout.cell(row, "note").strip(),
            )
        )
    if not count:
        # Пустой лист под шапкой - это сводка, которую формула ещё собирает, а не
        # «всё выставлено»: прежнее чтение остаётся, бот не правит пост в пустоту.
        raise BillsError(f"В листе «{worksheet}» нет ни одной строки договора - книга, похоже, ещё собирается")
    bills.sort(key=_order)
    return Board(
        read_at=datetime.now(timezone.utc),
        title=title,
        worksheet=worksheet,
        spreadsheet_id=spreadsheet_id,
        bills=bills,
        rows=count,
        drift=layout.describe_drift(),
        issued=issued,
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
        raise BillsError("Не заданы креды Google - книгу прочитать нечем")
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
        log.exception("счета: книга не прочиталась")
        with _guard:
            slot.error = f"Книга не прочиталась: {exc}"
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


def forget(workspace_id: uuid.UUID) -> None:
    with _guard:
        _slots.pop(workspace_id, None)


# ── Наружу ───────────────────────────────────────────────────────────────────


def counts_out(board: Board) -> dict[str, Any]:
    counts = board.counts()
    return {
        **counts,
        "overdue_total": str(board.total(OVERDUE)),
        "soon_total": str(board.total(SOON)),
    }


def board_out(session: Session, workspace_id: uuid.UUID, *, force: bool = False) -> dict[str, Any]:
    """Раздел «Счета → Не выставлены»: строки, числа по группам и откуда они."""
    slot = get(session, workspace_id, force=force)
    if slot is None:
        return {"source": None, "items": [], "counts": None}
    with _guard:
        board, error = slot.board, slot.error
    source = source_of(session, workspace_id)
    out: dict[str, Any] = {
        "source": {
            "title": (board.title if board else "") or (source.title if source else ""),
            "worksheet": source.worksheet if source else "",
            "url": f"https://docs.google.com/spreadsheets/d/{source.spreadsheet_id}/edit" if source else "",
            "read_at": board.read_at.isoformat() if board else None,
            "drift": board.drift if board else "",
            "error": error,
        },
        "items": [bill.out() for bill in board.bills] if board else [],
        "counts": counts_out(board) if board else None,
    }
    return out


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
    counts = board.counts()
    return {OVERDUE: counts[OVERDUE], SOON: counts[SOON]}


__all__ = [
    "Bill",
    "BillsError",
    "Board",
    "COLUMNS",
    "EARLY",
    "GROUPS",
    "OVERDUE",
    "SOON",
    "UNDATED",
    "badge",
    "bill_key",
    "board_out",
    "build_board",
    "counts_out",
    "forget",
    "get",
    "read_date",
    "state_of",
]
