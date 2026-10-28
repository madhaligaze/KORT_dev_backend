"""Сверка начислений KORT с книгой-сводкой - тень перед переключением.

Книга-сводка держит по строке на договор и месяц: сумму, «Период с/по», наше
юрлицо, счёт, оплату, АВР. Пока правда в книге, KORT считает начисления сам
(`core/accruals.py`) и сравнивает по каждому договору и месяцу: совпало -
звено готово к переключению; расходится - строкой с причиной; чего в
условиях договора не хватает, чтобы совпало, - предложением: вид
начисления, день периода, начало начислений, сумма или исполнитель с даты,
пауза, окончание, поправка месяца. Решает человек: сверка ничего не пишет
сама, а записанное - обычные изменения договора с историей.

Как решаются спорные случаи - без догадок:
* Первый период договора, начатого внутри окна книги, может быть неполным:
  день периода берётся по остальным месяцам.
* Сумма разошлась в одном последнем месяце - это может быть и новая цена, и
  разовая скидка: предложение спрашивает «с этого месяца» или «только этот
  месяц». Разошлась несколько месяцев подряд до конца окна - новая цена с
  первого из них.
* Книга кончилась раньше KORT - договор закончился или стоит: спрашивается,
  что из двух.
"""
from __future__ import annotations

import calendar
import hashlib
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts.fields import number_key
from app.finance.contracts.models import Contract, GroupEntity, ListValue
from app.finance.contracts.service import Access, Registry, people_of, visible_to
from app.finance.core import book as book_module
from app.finance.core.accruals import WHY, Accrual, Ledger, Schedule, month_key, month_start
from app.finance.core.models import Document
from app.finance.layout import norm
from app.finance.models import Counterparty, Workspace
from app.finance.onec.match import number_loose

#: Чем строка расходится: сумма, начало периода, наше юрлицо, нет у KORT, нет в книге.
LINE_CODES = ("amount", "period", "executor", "no_kort", "no_book")
#: Виды предложений. Простые (поле договора) применяются и пачкой.
PROPOSAL_KINDS = (
    "billing", "period_day", "billing_from", "amount_from", "amount_before", "amount_fix",
    "executor_from", "executor_before", "adjust", "pause", "stop", "end", "status",
)
BULK_KINDS = ("billing", "period_day", "billing_from")

_ZERO = Decimal("0")


def _money(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _day(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _amount_text(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f}".replace(",", " ").replace(".00", "")


MONTH_NAMES = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)
#: «с сентября», «в сентябре» - тексты предложений читаются фразой.
MONTHS_FROM = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
MONTHS_IN = (
    "январе", "феврале", "марте", "апреле", "мае", "июне",
    "июле", "августе", "сентябре", "октябре", "ноябре", "декабре",
)


def month_title(key: str) -> str:
    return f"{MONTH_NAMES[int(key[5:7]) - 1]} {key[:4]}"


def month_from(key: str) -> str:
    return f"{MONTHS_FROM[int(key[5:7]) - 1]} {key[:4]}"


def month_in(key: str) -> str:
    return f"{MONTHS_IN[int(key[5:7]) - 1]} {key[:4]}"


@dataclass
class BookMonth:
    """Строки книги одного договора за месяц (обычно одна)."""

    rows: list[book_module.BookRow]
    entity_id: uuid.UUID | None = None

    @property
    def amount(self) -> Decimal | None:
        values = [row.amount for row in self.rows if row.amount is not None]
        return sum(values, _ZERO) if values else None

    @property
    def period_from(self) -> date | None:
        return next((row.period_from for row in self.rows if row.period_from), None)

    @property
    def period_to(self) -> date | None:
        return next((row.period_to for row in self.rows if row.period_to), None)

    @property
    def code(self) -> str:
        return self.rows[0].own if self.rows else ""

    def out(self, *, money: bool) -> dict[str, Any]:
        row = self.rows[0]
        out: dict[str, Any] = {
            "rows": len(self.rows),
            "line": row.line,
            "amount": _money(self.amount),
            "period_from": _day(self.period_from),
            "period_to": _day(self.period_to),
            "code": self.code,
            "entity_id": str(self.entity_id) if self.entity_id else None,
            "bill_no": row.bill_no,
            "bill_date": _day(row.bill_date),
            "act_no": row.act_no,
            "act_date": _day(row.act_date),
            "accepted": row.accepted_state,
            "esf_date": _day(row.esf_date),
            "status": row.status,
        }
        if money:
            paid = [item.paid for item in self.rows if item.paid is not None]
            out["paid"] = _money(sum(paid, _ZERO)) if paid else None
            out["act_sum"] = _money(row.act_sum)
            out["closing"] = _money(row.closing_accruals)
        return out


class EntityTally:
    """Сверка звена по нашим юрлицам - для «Перехода» (`core/switch.py`).

    Месяц договора - тому юрлицу, что было исполнителем в этом месяце: после
    перевода клиента старые месяцы готовят к переходу прежнее ТОО, а не новое.
    Договор у юрлица совпал, если совпали все его месяцы у этого юрлица.
    """

    def __init__(self) -> None:
        self.months: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self.contracts: dict[str, set[uuid.UUID]] = defaultdict(set)
        self.dirty: dict[str, set[uuid.UUID]] = defaultdict(set)
        #: Юрлицо → месяц → [сверено, совпало]: переход - после закрытых месяцев без расхождений.
        self.by_month: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))

    def add(self, entity_id: uuid.UUID | None, contract_id: uuid.UUID, same: bool, month: str = "") -> None:
        key = str(entity_id or "")
        tally = self.months[key]
        tally[0] += 1
        tally[1] += int(same)
        self.contracts[key].add(contract_id)
        if not same:
            self.dirty[key].add(contract_id)
        if month:
            slot = self.by_month[key][month]
            slot[0] += 1
            slot[1] += int(same)

    def out(self) -> dict[str, dict[str, Any]]:
        return {
            key: {
                "months": months,
                "same": same,
                "contracts": len(self.contracts[key]),
                "contracts_same": len(self.contracts[key] - self.dirty[key]),
                "by_month": {month: list(slot) for month, slot in sorted(self.by_month[key].items())},
            }
            for key, (months, same) in self.months.items()
        }


def month_owner(contract: Contract, kort: Any = None, book: BookMonth | None = None) -> uuid.UUID | None:
    """Наше юрлицо месяца договора: исполнитель начисления, иначе код юрлица в строке таблицы, иначе договора."""
    executor = getattr(kort, "executor_id", None) or getattr(kort, "executor", None)
    if executor:
        return executor
    if book is not None and book.entity_id:
        return book.entity_id
    return contract.executor_id


def _snap_money(value: Decimal | None) -> str:
    return format((value or _ZERO).quantize(Decimal("0.01")), "f")


def _kort_amount(accrual: Accrual | None) -> Decimal | None:
    if accrual is None:
        return None
    return _ZERO if accrual.skipped else accrual.amount


def kort_side(accrual: Accrual | None) -> dict[str, Any]:
    """То, чем месяц KORT сравнивается с таблицей (`_compare`), - снимок решения «верно у KORT».

    Условия договора поменялись, и KORT считает месяц иначе - решение о
    прежнем расчёте больше не действует.
    """
    if accrual is None:
        return {"none": True}
    return {
        "amount": _snap_money(_kort_amount(accrual)),
        "period_from": _day(accrual.period_from),
        "executor_id": str(accrual.executor_id) if accrual.executor_id else None,
    }


def book_side(book: BookMonth | None) -> dict[str, Any]:
    """То же со стороны таблицы: правку таблицы после решения оно не прикрывает."""
    if book is None:
        return {"none": True}
    return {
        "amount": _snap_money(book.amount),
        "period_from": _day(book.period_from),
        "entity_id": str(book.entity_id) if book.entity_id else None,
    }


def _month_name(key: str) -> str:
    return month_title(key).split()[0]


@dataclass(frozen=True)
class Slip:
    """Таблица не сходится сама с собой: соседние месяцы договора не стыкуются по периодам.

    `gap` - дни между «по» прошлого месяца и «с» этого не покрыты ни одним
    месяцем, `overlap` - покрыты дважды. Условия договора дают периоды встык,
    поэтому это ошибка таблицы, а не договора.
    """

    kind: str
    first: date
    last: date
    before: str
    month: str
    before_to: date
    month_from: date

    @property
    def days(self) -> str:
        if self.first == self.last:
            return f"{self.first:%d.%m}"
        return f"{self.first:%d.%m} - {self.last:%d.%m}"

    @property
    def text(self) -> str:
        ends = f"{_month_name(self.before)} по {self.before_to:%d.%m}, {_month_name(self.month)} с {self.month_from:%d.%m}"
        if self.kind == "gap":
            return f"таблица пропустила {self.days}: {ends}"
        return f"таблица считает {self.days} дважды: {ends}"

    @property
    def shift_text(self) -> str:
        if self.kind == "gap":
            return f"таблица идёт со сдвигом с пропуска {self.days}"
        return f"таблица идёт со сдвигом с {self.days}, посчитанного дважды"


def _span(month: BookMonth) -> tuple[date | None, date | None]:
    """Период месяца таблицы целиком: месяц бывает разбит на несколько строк."""
    froms = [row.period_from for row in month.rows if row.period_from]
    tos = [row.period_to for row in month.rows if row.period_to]
    return (min(froms) if froms else None, max(tos) if tos else None)


def table_slips(book: dict[str, BookMonth]) -> dict[str, Slip]:
    """Месяцы договора, где «Период с» таблицы не продолжает «Период по» прошлого месяца."""
    out: dict[str, Slip] = {}
    months = sorted(book)
    for before, month in zip(months, months[1:]):
        if _shift_month(before, 1) != month:
            continue
        before_to = _span(book[before])[1]
        month_from = _span(book[month])[0]
        if before_to is None or month_from is None:
            continue
        expected = before_to + timedelta(days=1)
        if month_from > expected:
            out[month] = Slip("gap", expected, month_from - timedelta(days=1), before, month, before_to, month_from)
        elif month_from < expected:
            out[month] = Slip("overlap", month_from, before_to, before, month, before_to, month_from)
    return out


def decision_out(doc: Document | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    at = doc.updated_at or doc.created_at
    return {"id": str(doc.id), "at": at.isoformat() if at else None, "batch": (doc.meta or {}).get("batch") or None}


@dataclass
class Line:
    contract_id: uuid.UUID
    month: str
    codes: list[str]
    kort: Accrual | None
    book: BookMonth | None
    #: Решение человека «верно у KORT» (`changes.verify_month`): действует, пока обе стороны те же.
    verified: Document | None = None
    #: Решение было, но KORT или таблица с тех пор изменились - месяц снова расходится.
    stale: Document | None = None
    #: Что изменилось после решения: `kort`, `book`, `both`.
    stale_side: str = ""
    #: Почему месяц расходится, если это видно из данных: ошибка периода таблицы, день периода KORT.
    hint: str = ""
    #: Ошибка периода таблицы: `gap`, `overlap`, `shift` (сдвиг после неё); пусто - нет.
    slip: str = ""

    @property
    def same(self) -> bool:
        """Сошлось: совпало с таблицей или человек решил, что верно у KORT."""
        return not self.codes or self.verified is not None

    def out(self, *, money: bool) -> dict[str, Any]:
        return {
            "contract_id": str(self.contract_id),
            "month": self.month,
            "codes": self.codes,
            "kort": self.kort.out() if self.kort else None,
            "book": self.book.out(money=money) if self.book else None,
            "hint": self.hint,
            "slip": self.slip,
            "verified": decision_out(self.verified),
            "stale": {**decision_out(self.stale), "side": self.stale_side} if self.stale is not None else None,
        }


@dataclass
class Proposal:
    contract_id: uuid.UUID
    kind: str
    value: dict[str, Any]
    text: str
    months: list[str] = field(default_factory=list)
    #: Что человек выбирает, если предложение - вопрос («с этого месяца» или
    #: «только этот месяц»; «закончился» или «стоит»).
    options: list[str] = field(default_factory=list)
    #: Подписи ответов, которых нет в интерфейсе заранее: названия статусов компании.
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        raw = f"{self.contract_id}|{self.kind}|{sorted(self.value.items())}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def out(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "contract_id": str(self.contract_id),
            "kind": self.kind,
            "value": self.value,
            "text": self.text,
            "months": self.months,
            "options": self.options,
            "labels": self.labels,
        }


@dataclass
class ContractShadow:
    contract: Contract
    schedule: Schedule
    lines: list[Line]
    proposals: list[Proposal]

    @property
    def clean(self) -> bool:
        return all(line.same for line in self.lines) and not self.proposals


@dataclass
class Shadow:
    window: list[str]
    contracts: dict[uuid.UUID, ContractShadow] = field(default_factory=dict)
    #: Строки книги без договора в KORT - сгруппированные по договору книги.
    unknown: list[dict[str, Any]] = field(default_factory=list)

    def of_entity(self, entity_id: uuid.UUID, code: str) -> Shadow:
        """Сверка одного нашего юрлица - как её считает «Переход»: месяцы, где оно исполнитель, и строки таблицы с его кодом."""
        out = Shadow(window=self.window)
        for contract_id, item in self.contracts.items():
            lines = [line for line in item.lines if month_owner(item.contract, line.kort, line.book) == entity_id]
            if lines:
                out.contracts[contract_id] = ContractShadow(item.contract, item.schedule, lines, item.proposals)
        out.unknown = [group for group in self.unknown if norm(group["own"]) == norm(code)]
        return out

    def lines(self) -> Iterable[Line]:
        for item in self.contracts.values():
            yield from item.lines

    def proposals(self) -> Iterable[Proposal]:
        for item in self.contracts.values():
            yield from item.proposals

    def counts(self) -> dict[str, int]:
        codes = Counter()
        total = 0
        for line in self.lines():
            total += 1
            if not line.codes:
                codes["same"] += 1
                continue
            if line.verified is not None:
                codes["verified"] += 1
                continue
            for code in line.codes:
                codes[code] += 1
        return {
            "contracts": len(self.contracts),
            "clean": sum(1 for item in self.contracts.values() if item.clean),
            "months": total,
            "same": codes["same"],
            "verified": codes["verified"],
            **{code: codes[code] for code in LINE_CODES},
            "unknown": len(self.unknown),
            "proposals": sum(1 for _ in self.proposals()),
        }


def _settled(proposal: Proposal, lines: list[Line]) -> bool:
    """Расходящиеся месяцы предложения решены «верно у KORT» - подгонять условия под таблицу не нужно.

    Совпавшие месяцы предложения (день периода охватывает все месяцы таблицы)
    решать нечего: держит только расходящийся месяц без решения.
    """
    by_month = {line.month: line for line in lines}
    covered = [by_month[month] for month in proposal.months if month in by_month and by_month[month].codes]
    return bool(covered) and all(line.verified is not None for line in covered)


def _visible(session: Session, workspace: Workspace, access: Access) -> list[Contract]:
    contracts = list(
        session.scalars(sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None)))
    )
    registry = Registry(session, workspace)
    people = people_of(session, [item.id for item in contracts])
    return [item for item in contracts if visible_to(item, registry, access, people.get(item.id, []))]


def _compare(kort: Accrual | None, book: BookMonth | None) -> list[str]:
    if kort is None:
        return ["no_kort"]
    if book is None:
        return ["no_book"]
    codes: list[str] = []
    ours = _ZERO if kort.skipped else (kort.amount or _ZERO)
    if ours != (book.amount or _ZERO):
        codes.append("amount")
    if book.period_from and kort.period_from != book.period_from:
        codes.append("period")
    if book.entity_id and kort.executor_id and book.entity_id != kort.executor_id:
        codes.append("executor")
    return codes


def _month_end(day: date) -> bool:
    return day.day == calendar.monthrange(day.year, day.month)[1]


def _months_word(months: list[str]) -> str:
    names = [month_title(item).split()[0] for item in months]
    if len(names) <= 3:
        return ", ".join(names)
    return f"{names[0]} - {names[-1]}"


class Reconciler:
    """Одна сверка: книга, договоры, начисления - на запрос.

    `only` - сверка одного договора (лента месяцев в карточке): из книги
    сопоставляются только строки с его номером, а не все полторы тысячи на
    каждое открытие карточки.
    """

    def __init__(
        self, session: Session, workspace: Workspace, access: Access, book: book_module.Book, *, only: Contract | None = None
    ):
        self.session = session
        self.workspace = workspace
        self.access = access
        self.book = book
        self.window = sorted({row.month for row in book.rows if row.month})
        self.matcher = book_module.Matcher(session, workspace.id)
        self.contracts = [only] if only is not None else _visible(session, workspace, access)
        self.ledger = Ledger(session, workspace.id, self.contracts)
        ids = {item.id for item in self.contracts}
        self.verified: dict[tuple[uuid.UUID, str], Document] = {
            (doc.contract_id, month_key(doc.period)): doc
            for doc in session.scalars(
                sa.select(Document).where(
                    Document.workspace_id == workspace.id, Document.kind == "verified", Document.deleted_at.is_(None)
                )
            )
            if doc.contract_id in ids and doc.period is not None
        }
        self.names = dict(
            session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace.id)).all()
        )
        self.status_names: dict[uuid.UUID, str] = {}
        #: Фаза → статус компании с этой фазой, первый по порядку списка.
        self.statuses: dict[str, ListValue] = {}
        for row in session.scalars(
            sa.select(ListValue)
            .where(ListValue.workspace_id == workspace.id, ListValue.field_key == "status")
            .order_by(ListValue.position, ListValue.created_at)
        ):
            self.status_names[row.id] = row.value
            phase = (row.meaning or {}).get("phase")
            if phase and row.archived_at is None:
                self.statuses.setdefault(phase, row)
        self.codes = {
            entity_id: code
            for entity_id, code in session.execute(
                sa.select(GroupEntity.counterparty_id, GroupEntity.code).where(GroupEntity.workspace_id == workspace.id)
            )
            if code
        }
        self.grouped: dict[uuid.UUID, dict[str, BookMonth]] = defaultdict(dict)
        self.unmatched: list[tuple[book_module.BookRow, book_module.RowMatch]] = []
        keys = {number_key(only.number), number_loose(only.number)} - {""} if only is not None else set()
        for row in book.rows:
            if not row.month:
                continue
            if only is not None and not ({number_key(row.number), number_loose(row.number)} & keys):
                continue
            match = self.matcher.match(row)
            if only is not None and match.contract_id != only.id:
                continue
            if match.state == "found" and match.contract_id is not None:
                months = self.grouped[match.contract_id]
                entry = months.get(row.month)
                if entry is None:
                    entry = months[row.month] = BookMonth(rows=[], entity_id=self.matcher.entity_by_code.get(norm(row.own)))
                entry.rows.append(row)
            else:
                self.unmatched.append((row, match))

    @property
    def since(self) -> date:
        return month_start(self.window[0])

    @property
    def until(self) -> date:
        return month_start(self.window[-1])

    def contract_shadow(self, contract: Contract) -> ContractShadow | None:
        schedule = self.ledger.schedule(contract, since=self.since, until=self.until)
        book_months = self.grouped.get(contract.id, {})
        kort_months = schedule.by_month()
        if not kort_months and not book_months:
            return None
        lines = [
            Line(contract.id, month, _compare(kort_months.get(month), book_months.get(month)), kort_months.get(month), book_months.get(month))
            for month in self.window
            if month in kort_months or month in book_months
        ]
        self._explain(contract, schedule, lines, book_months)
        proposals = [
            proposal
            for proposal in self._propose(contract, schedule, kort_months, book_months)
            if not _settled(proposal, lines)
        ]
        return ContractShadow(contract, schedule, lines, proposals)

    def _explain(self, contract: Contract, schedule: Schedule, lines: list[Line], book: dict[str, BookMonth]) -> None:
        """Почему месяцы расходятся, если это видно из данных, и решения «верно у KORT».

        Ошибка периода таблицы - пропущенный или дважды посчитанный день - и
        месяцы после неё, которые таблица продолжает со сдвигом, пока
        расходится одно начало периода. Иначе у расхождения периода - откуда
        день периода у KORT. Решение действует, пока KORT и таблица считают
        месяц так же, как в момент решения.
        """
        slips = table_slips(book)
        run: Slip | None = None
        previous = ""
        for line in lines:
            slip = slips.get(line.month)
            if slip is not None:
                run = slip if "period" in line.codes else None
            elif run is not None and not (
                line.book is not None and line.codes == ["period"] and previous and _shift_month(previous, 1) == line.month
            ):
                run = None
            previous = line.month
            if not line.codes:
                continue
            if slip is not None:
                line.hint, line.slip = slip.text, slip.kind
            elif run is not None:
                line.hint, line.slip = run.shift_text, "shift"
            elif "no_kort" in line.codes:
                line.hint = self._why_not(contract, schedule, line)
            elif "no_book" in line.codes:
                line.hint = self._why_missing(schedule, line, book)
            elif "period" in line.codes and line.kort is not None and schedule.period_day:
                if contract.period_day:
                    line.hint = f"у KORT день периода {schedule.period_day} - так в условиях договора"
                elif schedule.start is not None:
                    line.hint = f"у KORT день периода {schedule.period_day} - по началу начислений {schedule.start:%d.%m.%Y}"
            doc = self.verified.get((contract.id, line.month))
            if doc is None:
                continue
            meta = doc.meta or {}
            kort_same = meta.get("kort") == kort_side(line.kort)
            book_same = meta.get("book") == book_side(line.book)
            if kort_same and book_same:
                line.verified = doc
            else:
                line.stale = doc
                line.stale_side = "both" if not (kort_same or book_same) else "kort" if not kort_same else "book"

    def _why_not(self, contract: Contract, schedule: Schedule, line: Line) -> str:
        """Почему у KORT нет начисления за месяц, который есть в таблице, - из условий договора."""
        if line.month in schedule.paused:
            return "у KORT в этом месяце договор приостановлен"
        # Причина всего договора - первой; «закончился без даты» - только после его начала.
        if schedule.why in ("terms", "unset", "no_date", "never"):
            return f"у KORT не начисляется: {WHY[schedule.why]}"
        start = schedule.start
        if start is not None and schedule.kind == "total":
            return f"у KORT разовый договор начислен в {month_in(month_key(start))}"
        if start is not None and line.month < month_key(start):
            return f"у KORT начисления с {start:%d.%m.%Y}"
        if schedule.why:
            return f"у KORT не начисляется: {WHY[schedule.why]}"
        day = line.book.period_from if line.book is not None and line.book.period_from else month_start(line.month)
        end = self.ledger.timeline(contract).on(day).end_date
        if end is not None and day > end:
            return f"у KORT договор закончился {end:%d.%m.%Y}"
        return ""

    @staticmethod
    def _why_missing(schedule: Schedule, line: Line, book: dict[str, BookMonth]) -> str:
        """Почему KORT начисляет месяц, которого в таблице нет, - из того, что в таблице есть.

        Конец договора в таблице и пропуск посреди - у своих предложений
        («закончился или стоит», «пауза»); здесь - таблица, которая договор
        ещё не ведёт или ведёт с более позднего месяца.
        """
        since = f"; у KORT начисления с {schedule.start:%d.%m.%Y}" if schedule.start else ""
        if not book:
            return "в таблице у договора нет ни одной строки" + since
        first = min(book)
        if line.month < first:
            return f"в таблице договор с {month_from(first)}" + since
        return ""

    def difference(self, line: Line) -> str:
        """Чем месяц расходится - словами, для решения и журнала."""
        kort, book = line.kort, line.book
        parts: list[str] = []
        for code in line.codes:
            if code == "no_kort":
                parts.append(f"у KORT начисления нет, в таблице {_amount_text(book.amount if book else None)}")
            elif code == "no_book":
                parts.append(f"в таблице строки нет, у KORT {_amount_text(_kort_amount(kort))}")
            elif code == "amount":
                parts.append(f"сумма у KORT {_amount_text(_kort_amount(kort))}, в таблице {_amount_text(book.amount)}")
            elif code == "period":
                parts.append(f"период у KORT с {kort.period_from:%d.%m}, в таблице с {book.period_from:%d.%m}")
            elif code == "executor":
                parts.append(f"юрлицо у KORT {self.party_label(kort.executor_id)}, в таблице {book.code}")
        return "; ".join(parts)

    def run(self) -> Shadow:
        shadow = Shadow(window=self.window)
        if not self.window:
            return shadow
        for contract in self.contracts:
            item = self.contract_shadow(contract)
            if item is not None:
                shadow.contracts[contract.id] = item
        shadow.unknown = self._unknown()
        return shadow

    # ── Предложения ──────────────────────────────────────────────────────────

    def _propose(
        self, contract: Contract, schedule: Schedule, kort: dict[str, Accrual], book: dict[str, BookMonth]
    ) -> list[Proposal]:
        if not book:
            return []
        out: list[Proposal] = []
        add = lambda kind, value, text, months=(), options=(), labels=None: out.append(  # noqa: E731
            Proposal(contract.id, kind, value, text, list(months), list(options), dict(labels or {}))
        )
        months = sorted(book)
        if contract.billing not in ("month", "total"):
            spans = [(item.period_to - item.period_from).days for item in book.values() if item.period_from and item.period_to]
            monthly = len(months) >= 2 or bool(spans and all(27 <= span <= 31 for span in spans))
            add(
                "billing",
                {"billing": "month" if monthly else "total"},
                f"начислять {'в месяц' if monthly else 'всю сумму разово'} - в таблице {len(months)} "
                + ("месяц" if len(months) == 1 else "месяца" if len(months) < 5 else "месяцев"),
                months,
            )
            return out
        start = schedule.start
        if start is None:
            first = book[months[0]].period_from or month_start(months[0])
            add("billing_from", {"billing_from": first.isoformat()}, f"начислять с {first:%d.%m.%Y} - как первый месяц в таблице", months[:1])
            return out
        if contract.billing == "total":
            if not self._propose_status(contract, schedule, kort, book, add):
                self._propose_total(schedule, book, add)
            return out
        if schedule.why == "never" and not kort:
            self._propose_status(contract, schedule, kort, book, add)
            return out
        if schedule.why == "ended_unknown" and not kort:
            last = months[-1]
            if last >= self.window[-1]:
                # Таблица начисляет до конца окна - договор не закончился, ошибся статус или таблица.
                self._propose_status(contract, schedule, kort, book, add)
            else:
                end = book[last].period_to or (month_start(last) + timedelta(days=27))
                add(
                    "end",
                    {"end_date": end.isoformat()},
                    f"договор закончился, а дата окончания не указана: в таблице последний - {month_title(last)}, окончание {end:%d.%m.%Y}",
                    [last],
                )
            return out
        started_inside = month_key(start) >= self.window[0]
        self._propose_period_day(contract, schedule, book, months, started_inside, add)
        if started_inside:
            first = book[months[0]].period_from
            if first and first != start and not (contract.billing_from == first):
                # Начало позже - уходят и месяцы KORT до первого месяца таблицы: они - месяцы предложения.
                earlier = [month for month in sorted(kort) if month < months[0]]
                add(
                    "billing_from",
                    {"billing_from": first.isoformat()},
                    f"начислять с {first:%d.%m.%Y} - так первый месяц в таблице",
                    earlier + months[:1],
                )
        self._propose_executor(contract, kort, book, add)
        self._propose_amount(contract, kort, book, add)
        self._propose_gaps(contract, kort, book, months, add)
        return out

    def _propose_status(self, contract, schedule, kort, book, add) -> bool:
        """Статус не даёт KORT начислять, а таблица начисляет: договор действует - или ошиблась таблица.

        Только когда из-за статуса KORT не начисляет совсем («не состоялся»,
        «закончился» без даты окончания) и статус не менялся соглашениями с
        датой: тогда исправить статус - сказать, что он записан неверно, без
        выдуманной истории «до такого-то дня был расторгнут». Разовому - два
        ответа («На исполнении», «Исполнен»): какой из них, знает человек.
        """
        if kort or schedule.why not in ("never", "ended_unknown"):
            return False
        if self.ledger.timeline(contract).changes("status"):
            return False
        months = [month for month in sorted(book) if (book[month].amount or _ZERO) > 0]
        phases = ("in_progress", "fulfilled") if contract.billing == "total" else ("active",)
        choices = {phase: self.statuses[phase] for phase in phases if phase in self.statuses}
        if not months or not choices:
            return False
        current = self.status_names.get(contract.status_id) if contract.status_id else None
        add(
            "status",
            {"statuses": {phase: str(row.id) for phase, row in choices.items()}},
            f"статус «{current or 'не задан'}» - KORT не начисляет, а таблица начисляет: {_months_word(months)}",
            months,
            tuple(choices),
            {phase: f"Статус «{row.value}»" for phase, row in choices.items()},
        )
        return True

    def _propose_period_day(self, contract, schedule, book, months, started_inside, add) -> None:
        froms = [book[month].period_from for month in months if book[month].period_from]
        candidates = froms[1:] if started_inside and len(froms) > 1 else froms
        if not candidates:
            return
        days = Counter(item.day for item in candidates)
        day, _count = days.most_common(1)[0]
        # День в конце месяца книга обрезает (31 → 30, 29 → 28): такие не спорят.
        consistent = all(item.day == day or (_month_end(item) and item.day < day) for item in candidates)
        if consistent and day != schedule.period_day:
            add(
                "period_day",
                {"period_day": day},
                f"период с {day}-го числа - так в таблице"
                + (f" (сейчас с {schedule.period_day}-го)" if schedule.period_day else ""),
                months,
            )

    def _next(self, month: str) -> str | None:
        index = self.window.index(month) + 1
        return self.window[index] if index < len(self.window) else None

    @staticmethod
    def _start(month: str, kort: dict[str, Accrual], book: dict[str, BookMonth]) -> date:
        """Начало периода месяца - как в книге, иначе как у KORT.

        Дата изменения «с даты» берётся из книги: день периода KORT ещё может
        быть не тем (его поправит своё предложение), и сумма «с 27.09»
        после поправки дня на 1-е попала бы в октябрь, а не в сентябрь.
        """
        if month in book and book[month].period_from:
            return book[month].period_from
        if month in kort:
            return kort[month].period_from
        return month_start(month)

    def _runs(self, months: list[str], value) -> list[tuple[Any, list[str]]]:
        """Соседние месяцы с одним значением в книге - отрезками."""
        runs: list[tuple[Any, list[str]]] = []
        for month in months:
            current = value(month)
            if runs and runs[-1][0] == current and self._next(runs[-1][1][-1]) == month:
                runs[-1][1].append(month)
            else:
                runs.append((current, [month]))
        return runs

    def _propose_executor(self, contract, kort, book, add) -> None:
        """Исполнитель по отрезкам книги: перевод вперёд или история до перевода.

        Последний отрезок книги не тот, что в договоре, - перевод с его начала
        («исполнитель с даты»). Ранний отрезок не тот, а следующий уже совпал
        с договором, - перевод был, но договор не знает, кем исполнялся до
        него: история «до даты был другой», текущий исполнитель не меняется.
        """
        both = [month for month in self.window if month in kort and month in book and book[month].entity_id]
        runs = self._runs(both, lambda month: book[month].entity_id)
        for index, (target, months) in enumerate(runs):
            if all(kort[month].executor_id == target for month in months):
                continue
            code = book[months[0]].code or self.names.get(target, "")
            if index == len(runs) - 1:
                since = self._start(months[0], kort, book)
                add(
                    "executor_from",
                    {"executor": str(target), "from": since.isoformat()},
                    f"исполнитель с {since:%d.%m.%Y}: {code} - так в таблице с {month_from(months[0])}",
                    months,
                )
                continue
            after, following = runs[index + 1]
            if self._next(months[-1]) == following[0] and all(kort[month].executor_id == after for month in following):
                change = self._start(following[0], kort, book)
                add(
                    "executor_before",
                    {"executor": str(target), "until": change.isoformat(), "after": str(after)},
                    f"до {change - timedelta(days=1):%d.%m.%Y} исполнитель {code} - так в таблице за {_months_word(months)}",
                    months,
                )

    def _propose_amount(self, contract, kort, book, add) -> None:
        """Сумма по отрезкам книги.

        * Последний отрезок с другой суммой - новая цена с его начала; один
          последний месяц - вопрос «с этого месяца или только этот».
        * Ранний отрезок с другой суммой, а следующий уже совпал - цена
          сменилась, и договор не знает прежней: история «до даты было».
        * Один месяц между равными соседями - разовое: поправка месяца.
        * У договора без суммы (сумма меняется каждый месяц) - поправки месяцев.
        """
        both = [month for month in self.window if month in kort and month in book and kort[month].adjustment is None]
        runs = self._runs(both, lambda month: book[month].amount or _ZERO)

        def adjust(months: list[str]) -> None:
            for month in months:
                value = book[month].amount
                if value in (None, _ZERO):
                    add("adjust", {"month": month, "skip": True}, f"{month_title(month)}: не начислять - в таблице за месяц пусто", [month])
                else:
                    add(
                        "adjust",
                        {"month": month, "amount": _money(value)},
                        f"{month_title(month)}: {_amount_text(value)} вместо {_amount_text(kort[month].amount)} - так в таблице",
                        [month],
                    )

        for index, (target, months) in enumerate(runs):
            if all((kort[month].amount or _ZERO) == target for month in months):
                continue
            if target == _ZERO or any(kort[month].base is None for month in months):
                adjust(months)
                continue
            if index == len(runs) - 1:
                since = self._start(months[0], kort, book)
                base = kort[months[0]].base
                add(
                    "amount_from",
                    {"amount": _money(target), "from": since.isoformat()},
                    f"сумма {_amount_text(target)} с {since:%d.%m.%Y} - так в таблице с {month_from(months[0])}"
                    + (f" (сейчас {_amount_text(base)})" if base is not None else ""),
                    months,
                    () if len(months) > 1 else ("from", "month"),
                )
                continue
            after, following = runs[index + 1]
            before = runs[index - 1][0] if index else None
            if len(months) == 1 and before is not None and before == after:
                adjust(months)
            elif self._next(months[-1]) == following[0] and all((kort[month].amount or _ZERO) == after for month in following):
                change = self._start(following[0], kort, book)
                add(
                    "amount_before",
                    {"amount": _money(target), "until": change.isoformat(), "after": _money(after)},
                    f"до {change - timedelta(days=1):%d.%m.%Y} сумма была {_amount_text(target)} - так в таблице за {_months_word(months)}",
                    months,
                )
            else:
                adjust(months)

    def _propose_gaps(self, contract, kort, book, months, add) -> None:
        inside = [month for month in self.window if months[0] < month < months[-1] and month not in book and month in kort]
        runs: list[list[str]] = []
        for month in inside:
            if runs and self.window.index(month) == self.window.index(runs[-1][-1]) + 1:
                runs[-1].append(month)
            else:
                runs.append([month])
        for run in runs:
            after = self.window[self.window.index(run[-1]) + 1]
            since = kort[run[0]].period_from
            resume = (kort[after].period_from if after in kort else month_start(after))
            add(
                "pause",
                {"from": since.isoformat(), "to": resume.isoformat()},
                f"в таблице нет: {_months_word(run)} - приостановлен с {since:%d.%m.%Y} по {resume - timedelta(days=1):%d.%m.%Y}?",
                run,
            )
        tail = [month for month in self.window if month > months[-1] and month in kort]
        if tail:
            since = kort[tail[0]].period_from
            last_to = book[months[-1]].period_to or (since - timedelta(days=1))
            add(
                "stop",
                {"from": since.isoformat(), "end_date": last_to.isoformat()},
                f"в таблице нет: {_months_word(tail)} - договор закончился {last_to:%d.%m.%Y} или стоит с {since:%d.%m.%Y}?",
                tail,
                ("end", "pause"),
            )

    def _propose_total(self, schedule, book, add) -> None:
        months = sorted(book)
        accrual = schedule.accruals[0] if schedule.accruals else None
        if accrual is None and schedule.why:
            # Не начисляется по причине (не состоялся, нет даты): дата из книги её не снимет.
            return
        if len(months) == 1:
            month = months[0]
            row = book[month]
            if accrual is None or accrual.month != month:
                start = row.period_from or month_start(month)
                add(
                    "billing_from",
                    {"billing_from": start.isoformat()},
                    f"начислить в {month_in(month)} ({start:%d.%m.%Y}) - так в таблице",
                    sorted({month, accrual.month} if accrual is not None else {month}),
                )
            elif row.amount is not None and accrual.adjustment is None and accrual.amount != row.amount:
                add(
                    "amount_fix",
                    {"amount": _money(row.amount)},
                    f"сумма договора {_amount_text(row.amount)} - так в таблице (сейчас {_amount_text(accrual.amount)})",
                    [month],
                )

    # ── Строки без договора ──────────────────────────────────────────────────

    @staticmethod
    def unknown_key(row: book_module.BookRow) -> str:
        """Договор книги без пары в KORT: юрлицо, клиент, номер - без оформления."""
        raw = "|".join(norm(part) for part in (row.own, row.customer, row.number))
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def unknown_rows(self, key: str) -> list[book_module.BookRow]:
        return sorted((row for row, _match in self.unmatched if self.unknown_key(row) == key), key=lambda row: row.month)

    def unknown_groups(self) -> list[dict[str, Any]]:
        """Строки таблицы без договора в KORT - группами по юрлицу, клиенту и номеру, как «Нет в реестре»."""
        return self._unknown()

    def _unknown(self) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        contracts = {item.id: item for item in self.contracts}
        for row, match in self.unmatched:
            key = self.unknown_key(row)
            group = groups.setdefault(
                key,
                {
                    "key": key,
                    "own": row.own,
                    "customer": row.customer,
                    "number": row.number,
                    "state": match.state,
                    "months": [],
                    "amount": None,
                    "lines": [],
                    "candidates": [
                        {
                            "id": str(cid),
                            "number": contracts[cid].number,
                            "customer": self.names.get(contracts[cid].customer_id, ""),
                        }
                        for cid in match.candidates
                        if cid in contracts
                    ],
                },
            )
            group["months"].append(row.month)
            group["lines"].append(row.line)
            if row.amount is not None:
                group["amount"] = _money(row.amount)
        out = list(groups.values())
        for group in out:
            group["months"] = sorted(set(group["months"]))
        out.sort(key=lambda item: (item["state"], item["own"], item["customer"]))
        return out

    # ── Выдача ───────────────────────────────────────────────────────────────

    def party_label(self, party_id: uuid.UUID | None) -> str:
        """Наше юрлицо - кодом («ТС»), остальные - именем."""
        if party_id is None:
            return ""
        return self.codes.get(party_id) or self.names.get(party_id, "")

    def contract_out(self, item: ContractShadow) -> dict[str, Any]:
        contract = item.contract
        return {
            "id": str(contract.id),
            "number": contract.number,
            "customer": self.names.get(contract.customer_id, ""),
            "executor": self.party_label(contract.executor_id),
            "billing": contract.billing,
            "period_day": item.schedule.period_day,
            "start": _day(item.schedule.start),
            "why": WHY.get(item.schedule.why, ""),
            "paused": item.schedule.paused,
            "clean": item.clean,
        }

    def out(self, shadow: Shadow, *, money: bool) -> dict[str, Any]:
        return {
            "window": shadow.window,
            "counts": shadow.counts(),
            "contracts": {str(cid): self.contract_out(item) for cid, item in shadow.contracts.items()},
            # Совпавшие месяцы не отдаются строками - только числом.
            "lines": [line.out(money=money) for line in shadow.lines() if line.codes],
            "proposals": [proposal.out() for proposal in shadow.proposals()],
            "unknown": shadow.unknown,
        }


def reconcile(session: Session, workspace: Workspace, access: Access, book: book_module.Book) -> Shadow:
    return Reconciler(session, workspace, access, book).run()


#: Сколько месяцев назад показывает лента договора без книги - и один вперёд.
RIBBON_BACK = 5


def _shift_month(key: str, by: int) -> str:
    index = int(key[:4]) * 12 + int(key[5:7]) - 1 + by
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _vat_reader(session: Session, workspace: Workspace, contract: Contract):
    """НДС месяца договора для счёта и АВР: у исполнителя-плательщика на день «Периода с», ставка года, режим договора.

    Не плательщик или «без НДС» - `None`: строки НДС у месяца нет.
    """
    from app.finance.core import tax
    from app.finance.core import vat as vat_module

    periods: dict[uuid.UUID, list] = {}
    known = tax.years(session, workspace.id)
    mode = contract.vat_mode or "included"

    def read(accrual: Accrual | None) -> dict[str, Any] | None:
        if accrual is None or accrual.skipped or not accrual.amount or accrual.executor_id is None:
            return None
        period = tax.regime_on(session, accrual.executor_id, accrual.period_from, periods)
        if period is None or not period.vat_payer:
            return None
        rate = tax.year_of(session, workspace.id, accrual.period_from.year, known).vat_rate
        net, vat, gross = vat_module.vat_of(accrual.amount, payer=True, mode=mode, rate=rate)
        if not vat:
            return None
        return {"mode": mode, "rate": format(rate.normalize(), "f"), "net": _money(net), "vat": _money(vat), "gross": _money(gross)}

    return read


def contract_months(
    session: Session,
    workspace: Workspace,
    access: Access,
    contract: Contract,
    book: book_module.Book | None,
    *,
    money: bool,
    on: date,
) -> dict[str, Any]:
    """Лента месяцев договора: начисление KORT и строка книги за каждый месяц.

    Окно - последние месяцы и следующий; с книгой - ещё и её месяцы. Чем
    месяц расходится с книгой, видно только в месяцах книги: вне её окна
    «нет в книге» - не расхождение, а просто другое время.
    """
    current = month_key(on)
    months = {_shift_month(current, by) for by in range(-RIBBON_BACK, 2)}
    reconciler = Reconciler(session, workspace, access, book, only=contract) if book is not None else None
    if reconciler is not None and not reconciler.window:
        reconciler = None
    book_window = set(reconciler.window) if reconciler else set()
    months |= book_window
    ordered = sorted(months)
    ledger = Ledger(session, workspace.id, [contract])
    schedule = ledger.schedule(contract, since=month_start(ordered[0]), until=month_start(ordered[-1]))
    kort = schedule.by_month()
    book_months = reconciler.grouped.get(contract.id, {}) if reconciler else {}
    item = reconciler.contract_shadow(contract) if reconciler else None
    lines = {line.month: line for line in item.lines} if item else {}
    vat_of = _vat_reader(session, workspace, contract)
    rows = []
    for month in reversed(ordered):
        accrual, row = kort.get(month), book_months.get(month)
        if accrual is None and row is None:
            continue
        line = lines.get(month) if month in book_window else None
        rows.append(
            {
                "month": month,
                "kort": accrual.out() if accrual else None,
                "book": row.out(money=money) if row else None,
                "codes": _compare(accrual, row) if month in book_window else [],
                "paused": month in schedule.paused,
                "hint": line.hint if line else "",
                "verified": decision_out(line.verified) if line else None,
                "vat": vat_of(accrual),
            }
        )
    proposals = [proposal.out() for proposal in item.proposals] if item and access.edit else []
    return {
        "schedule": {
            "kind": schedule.kind,
            "start": _day(schedule.start),
            "period_day": schedule.period_day,
            "why": WHY.get(schedule.why, ""),
            "paused": schedule.paused,
        },
        "months": rows,
        "book": reconciler is not None,
        "window": sorted(book_window),
        "proposals": proposals,
    }


__all__ = [
    "BULK_KINDS",
    "BookMonth",
    "ContractShadow",
    "EntityTally",
    "LINE_CODES",
    "Line",
    "PROPOSAL_KINDS",
    "Proposal",
    "Reconciler",
    "Shadow",
    "Slip",
    "WHY",
    "book_side",
    "contract_months",
    "decision_out",
    "kort_side",
    "month_owner",
    "month_title",
    "reconcile",
    "table_slips",
]
