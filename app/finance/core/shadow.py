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
from app.finance.contracts.models import Contract, GroupEntity
from app.finance.contracts.service import Access, Registry, people_of, visible_to
from app.finance.core import book as book_module
from app.finance.core.accruals import WHY, Accrual, Ledger, Schedule, month_key, month_start
from app.finance.layout import norm
from app.finance.models import Counterparty, Workspace
from app.finance.onec.match import number_loose

#: Чем строка расходится: сумма, начало периода, наше юрлицо, нет у KORT, нет в книге.
LINE_CODES = ("amount", "period", "executor", "no_kort", "no_book")
#: Виды предложений. Простые (поле договора) применяются и пачкой.
PROPOSAL_KINDS = (
    "billing", "period_day", "billing_from", "amount_from", "amount_before", "amount_fix",
    "executor_from", "executor_before", "adjust", "pause", "stop", "end",
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


@dataclass
class Line:
    contract_id: uuid.UUID
    month: str
    codes: list[str]
    kort: Accrual | None
    book: BookMonth | None

    def out(self, *, money: bool) -> dict[str, Any]:
        return {
            "contract_id": str(self.contract_id),
            "month": self.month,
            "codes": self.codes,
            "kort": self.kort.out() if self.kort else None,
            "book": self.book.out(money=money) if self.book else None,
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
        }


@dataclass
class ContractShadow:
    contract: Contract
    schedule: Schedule
    lines: list[Line]
    proposals: list[Proposal]

    @property
    def clean(self) -> bool:
        return all(not line.codes for line in self.lines) and not self.proposals


@dataclass
class Shadow:
    window: list[str]
    contracts: dict[uuid.UUID, ContractShadow] = field(default_factory=dict)
    #: Строки книги без договора в KORT - сгруппированные по договору книги.
    unknown: list[dict[str, Any]] = field(default_factory=list)

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
            for code in line.codes:
                codes[code] += 1
        return {
            "contracts": len(self.contracts),
            "clean": sum(1 for item in self.contracts.values() if item.clean),
            "months": total,
            "same": codes["same"],
            **{code: codes[code] for code in LINE_CODES},
            "unknown": len(self.unknown),
            "proposals": sum(1 for _ in self.proposals()),
        }


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
        self.names = dict(
            session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace.id)).all()
        )
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
        return ContractShadow(contract, schedule, lines, self._propose(contract, schedule, kort_months, book_months))

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
        add = lambda kind, value, text, months=(), options=(): out.append(  # noqa: E731
            Proposal(contract.id, kind, value, text, list(months), list(options))
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
            self._propose_total(schedule, book, add)
            return out
        if schedule.why == "ended_unknown" and not kort:
            last = months[-1]
            if last < self.window[-1]:
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
                add("billing_from", {"billing_from": first.isoformat()}, f"начислять с {first:%d.%m.%Y} - так первый месяц в таблице", months[:1])
        self._propose_executor(contract, kort, book, add)
        self._propose_amount(contract, kort, book, add)
        self._propose_gaps(contract, kort, book, months, add)
        return out

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
                add("billing_from", {"billing_from": start.isoformat()}, f"начислить в {month_in(month)} ({start:%d.%m.%Y}) - так в таблице", [month])
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
        """Наше юрлицо - кодом («BBCL»), остальные - именем."""
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
    rows = []
    for month in reversed(ordered):
        accrual, row = kort.get(month), book_months.get(month)
        if accrual is None and row is None:
            continue
        rows.append(
            {
                "month": month,
                "kort": accrual.out() if accrual else None,
                "book": row.out(money=money) if row else None,
                "codes": _compare(accrual, row) if month in book_window else [],
                "paused": month in schedule.paused,
            }
        )
    proposals: list[dict[str, Any]] = []
    if reconciler and access.edit:
        item = reconciler.contract_shadow(contract)
        proposals = [proposal.out() for proposal in item.proposals] if item else []
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
    "LINE_CODES",
    "Line",
    "PROPOSAL_KINDS",
    "Proposal",
    "Reconciler",
    "Shadow",
    "WHY",
    "contract_months",
    "month_title",
    "reconcile",
]
