"""Журнал финансистов: строка выписки - строка журнала.

Откуда он такой
───────────────
Финансисты BBC ведут «Журнал ГК» - лист Google, куда раскладывают выписки
банков всех юрлиц группы. Строка - одна строка выписки одного счёта: дата,
счёт, приход или расход, контрагент, подкатегория и категория, «ФОТ/Детали»,
номер договора, назначение платежа и вопрос к строке. Из журнала собирается
всё остальное: остатки на счетах, свод по статьям за месяц, а реестр каждого
юрлица формулой суммирует оплаты договора по «№Дог.» и «ОПиУ периоду» -
так деньги из выписки доходят до договора, который заключили юристы.

Здесь журнал - вид на операции учёта (`models.Operation`), а не второй журнал
рядом: на операциях уже стоят отчёты, «Оплачено» реестра, история с откатом
и корзина. Строка журнала BBC ложится в операцию так:

* «ДДС» - дата платежа (`paid_at`): месяц движения денег;
* «ОПиУ период» - месяц прибыли: период операции (`period_start`/`period_end`,
  и `accrued_at` - отчёт «Прибыль» считает по нему); год («2027 год») -
  период в год; «Старые (до Май/Июня 2026)» - подпись `period_label`: оплата
  не за месяцы учёта;
* «№Дог.» - `contract_ref`, по нему оплата ложится в договор реестра
  (`contracts/payments.py`);
* «Подкатегория» - статья, «Категория» - её группа (родитель статьи);
* перевод между своими счетами («Перемещения») - одна операция-перевод.
  В журнале BBC он виден двумя строками - в выписке каждого счёта; табличный
  вид так его и показывает (`lines_of`), а загрузка сводит две строки в один
  перевод (`_pair_transfers`). Пришла выписка только одного счёта - перевод
  ложится половинкой и дописывается второй выпиской (`_complete_half`);
* «Дробление 1 суммы» - одна сумма выписки на несколько строк (по месяцам,
  договорам, статьям): части - отдельные операции с общей группой
  (`split_group`) и суммой выписки (`split_total`).

Правило проекта «не угадывать» здесь держится так: номер договора из
назначения платежа подставляется, только если в реестре ровно один такой
договор этого юрлица; та же сумма на тот же счёт в тот же день уже в журнале -
строка выписки помечается «возможный повтор» и не заводится без решения
человека.
"""
from __future__ import annotations

import calendar
import hashlib
import io
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import importing, service
from app.finance.contracts.fields import party_key
from app.finance.contracts.models import Contract, Department, GroupEntity, ListValue
from app.finance.layout import norm
from app.finance.models import (
    POSITION_STEP,
    Account,
    Category,
    Counterparty,
    ImportBatch,
    ImportRow,
    Operation,
    OperationProject,
    Project,
    Workspace,
)
from app.finance.service import FinanceError, check_date, check_money

ZERO = Decimal("0")

#: Дата в ячейке и в окне правки - «день.месяц.год»: её печатает человек.
CELL_READING = importing.DateReading("dmy", "ручной ввод в журнале")

MONTH_TITLES = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)

#: Слова группы статей «перемещение между своими счетами». Группа задаёт вид
#: строки: в журнале BBC перевод - это строка «Перемещения» с суммой в
#: «Приходе» или «Расходе» выписки своего счёта.
TRANSFER_GROUPS = ("перемещения", "перемещение", "переводы между счетами", "внутренние переводы")

#: Без статьи, с вопросом, половинка перевода, поступление без договора,
#: дробление не сходится - то, что журнал показывает отдельным отбором.
ATTENTION = ("uncategorized", "question", "half", "no_contract", "split")


# ── Колонки журнала ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JournalColumn:
    key: str
    title: str
    kind: str  # date | money | text | enum | readonly
    width: int
    #: Откуда список значений у колонок-справочников.
    source: str = ""
    #: Колонка есть в шаблоне для заполнения (производные - только в выгрузке).
    template: bool = True
    editable: bool = True


#: Порядок - как в журнале BBC: финансист узнаёт свой лист с первого взгляда.
COLUMNS: tuple[JournalColumn, ...] = (
    JournalColumn("pnl", "ОПиУ период", "enum", 150, source="periods"),
    JournalColumn("paid_at", "Дата", "date", 104),
    JournalColumn("account", "Счёт", "enum", 230, source="accounts"),
    JournalColumn("income", "Приход", "money", 124),
    JournalColumn("expense", "Расход", "money", 124),
    JournalColumn("counterparty", "Контрагент", "enum", 210, source="counterparties"),
    JournalColumn("split", "Дробление 1 суммы", "readonly", 130, editable=False),
    JournalColumn("contract_ref", "№Дог.", "enum", 112, source="contracts"),
    JournalColumn("category", "Подкатегория", "enum", 180, source="categories"),
    JournalColumn("details", "ФОТ/Детали", "text", 160),
    JournalColumn("project", "Проект", "enum", 120, source="projects"),
    JournalColumn("group", "Категория", "readonly", 120, editable=False),
    JournalColumn("comment", "Комментарии", "text", 340),
    JournalColumn("note", "Доп. комменты / Вопросы", "text", 200),
    JournalColumn("month", "ДДС месяц", "readonly", 104, template=False, editable=False),
    JournalColumn("firm", "Фирма", "readonly", 120, template=False, editable=False),
    JournalColumn("code", "Код договора", "readonly", 140, template=False, editable=False),
)
COLUMN_BY_KEY = {column.key: column for column in COLUMNS}


# ── Месяц ОПиУ ───────────────────────────────────────────────────────────────


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _month_title(key: str) -> str:
    year, month = key.split("-")
    return f"{MONTH_TITLES[int(month) - 1]} {year}"


def pnl_of(operation: Operation) -> str:
    """«ОПиУ период» операции одной строкой: «2026-06», «2027», подпись или пусто.

    Пусто - месяц прибыли совпадает с месяцем платежа, как у большинства
    расходов: в журнале BBC у таких строк «ОПиУ период» = «ДДС месяц».
    """
    if operation.period_label:
        return operation.period_label
    start, end = operation.period_start, operation.period_end
    if start is not None:
        end = end or start
        if (start.month, start.day) == (1, 1) and end == date(start.year, 12, 31):
            return f"{start.year:04d}"
        if (start.year, start.month) == (end.year, end.month):
            return month_key(start)
        return f"{month_key(start)}..{month_key(end)}"
    accrued = operation.accrued_at
    if accrued is not None and (accrued.year, accrued.month) != (operation.paid_at.year, operation.paid_at.month):
        return month_key(accrued)
    return ""


def pnl_title(value: str, paid_at: date | None = None) -> str:
    """Подпись месяца ОПиУ для человека. Пусто - месяц платежа."""
    if not value:
        return _month_title(month_key(paid_at)) if paid_at else ""
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return _month_title(value)
    if re.fullmatch(r"\d{4}", value):
        return f"{value} год"
    if re.fullmatch(r"\d{4}-\d{2}\.\.\d{4}-\d{2}", value):
        first, last = value.split("..")
        return f"{_month_title(first)} - {_month_title(last)}"
    return value


def pnl_changes(raw: Any) -> dict[str, Any]:
    """Поля операции для нового «ОПиУ периода» - из того, что напечатал человек.

    «Июнь 2026», «06.2026», «2026-06», «2027 год», «Старые (до …)» или пусто
    (месяц платежа). Месяц и год ложатся периодом и датой начисления, подпись
    - подписью; прежнее значение снимается целиком, чтобы не осталось месяца
    от старой правки под новой подписью.
    """
    text = "" if raw is None else str(raw).strip()
    if re.fullmatch(r"\d{4}-\d{2}\.\.\d{4}-\d{2}", text):
        first, last = text.split("..")
        start = date(int(first[:4]), int(first[5:]), 1)
        end_year, end_month = int(last[:4]), int(last[5:])
        end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
        return {"period_start": start, "period_end": end, "accrued_at": start, "period_label": ""}
    parsed = importing.parse_period(text, CELL_READING) if text else None
    if parsed is None:
        return {"period_start": None, "period_end": None, "accrued_at": None, "period_label": ""}
    start, end, label = parsed
    if start is None:
        return {"period_start": None, "period_end": None, "accrued_at": None, "period_label": label[:200]}
    return {"period_start": start, "period_end": end, "accrued_at": start, "period_label": ""}


def period_options(months: Iterable[str]) -> list[dict[str, str]]:
    """Месяцы ОПиУ для выбора: от первого месяца журнала до конца следующего года, и годы."""
    keys = sorted(set(months))
    today = date.today()
    keys.append(month_key(today))
    first = min(keys)
    year, month = int(first[:4]), int(first[5:])
    last_year = today.year + 1
    out: list[dict[str, str]] = []
    while (year, month) <= (last_year, 12):
        key = f"{year:04d}-{month:02d}"
        out.append({"value": key, "title": _month_title(key)})
        month += 1
        if month > 12:
            year, month = year + 1, 1
    for value in (str(today.year), str(today.year + 1)):
        out.append({"value": value, "title": f"{value} год"})
    return out


# ── Номер договора ───────────────────────────────────────────────────────────

#: Латиница, которую печатают вместо кириллицы в номерах договоров («NºHO/40»
#: вместо «№НО/40»): в ключе номера обе стороны сводятся к одной букве.
_LOOKALIKE = str.maketrans({
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
})
_REF_PREFIX = re.compile(r"^\s*(?:№|n[°º]|no\.|#)\s*", re.IGNORECASE)
_REF_JUNK = re.compile(r"[\s№#«»\"']+")
#: Номер договора внутри назначения платежа: «по договору №ОБО/14 от …»,
#: «ДОГОВОРУ N ОКР/15», «дог. № ЮО-59». Буквенный префикс обязателен:
#: голое «№02» - номер чего угодно, от счёта до трудового договора.
_REF_IN_TEXT = re.compile(
    r"(?:№|\bn[°º]?\.?|\bno\.?)\s*([a-zа-яё]{1,6}(?:\s*[-/]\s*|\s+)?\d{1,5}(?:\s*[-/]\s*\d{1,4})?)",
    re.IGNORECASE,
)


def ref_key(value: Any) -> str:
    """Ключ номера договора: без «№», пробелов, регистра и латиницы-двойника.

    Разделители («/» и «-») остаются: у BBC «№ЮО-59» и «№ЮО/59» - договоры
    разных юрлиц, и сводить их нельзя.
    """
    text = _REF_PREFIX.sub("", str(value or ""))
    text = _REF_JUNK.sub("", text).lower().replace("ё", "е")
    return text.translate(_LOOKALIKE)


def refs_in_text(text: str) -> list[str]:
    """Номера договоров, упомянутые в назначении платежа, - ключами."""
    found: list[str] = []
    for match in _REF_IN_TEXT.finditer(text or ""):
        key = ref_key(match.group(1))
        if key and key not in found:
            found.append(key)
    return found


@dataclass
class ContractCard:
    id: uuid.UUID
    number: str
    customer: str
    executor: str
    ours: uuid.UUID | None
    department: str
    amount: Decimal | None
    billing: str
    status: str

    def out(self, paid: Decimal | None = None) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "number": self.number,
            "customer": self.customer,
            "executor": self.executor,
            "department": self.department,
            "amount": None if self.amount is None else _money(self.amount),
            "billing": self.billing,
            "status": self.status,
            "paid": None if paid is None else _money(paid),
        }


class Contracts:
    """Договоры реестра глазами журнала: номер → договор и сколько оплачено.

    «Оплачено» - то же разнесение, что видит реестр (`payments.allocate`):
    юрист и финансист смотрят на одну цифру, а не на две похожие.
    """

    def __init__(self, session: Session, workspace: Workspace, *, with_paid: bool = True):
        self.session = session
        self.workspace = workspace
        self.with_paid = with_paid
        rows = list(
            session.scalars(
                sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
            )
        )
        self.contracts = rows
        party_ids = ({item.customer_id for item in rows} | {item.executor_id for item in rows}) - {None}
        names = (
            dict(session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.id.in_(party_ids))).all())
            if party_ids
            else {}
        )
        own = set(
            session.scalars(sa.select(GroupEntity.counterparty_id).where(GroupEntity.workspace_id == workspace.id))
        )
        departments = {
            item.id: (item.code or item.title or "")
            for item in session.scalars(sa.select(Department).where(Department.workspace_id == workspace.id))
        }
        statuses = dict(
            session.execute(
                sa.select(ListValue.id, ListValue.value).where(
                    ListValue.workspace_id == workspace.id, ListValue.field_key == "status"
                )
            ).all()
        )
        self.cards: dict[uuid.UUID, ContractCard] = {}
        self.by_ref: dict[str, list[ContractCard]] = defaultdict(list)
        for item in rows:
            ours = item.executor_id if item.executor_id in own else (item.customer_id if item.customer_id in own else None)
            card = ContractCard(
                id=item.id,
                number=item.number or "",
                customer=names.get(item.customer_id, "") if item.customer_id else "",
                executor=names.get(item.executor_id, "") if item.executor_id else "",
                ours=ours,
                department=", ".join(departments[dep] for dep in item.department_ids if departments.get(dep)),
                amount=item.amount,
                billing=item.billing or "",
                status=statuses.get(item.status_id, "") if item.status_id else "",
            )
            self.cards[item.id] = card
            if card.number:
                self.by_ref[ref_key(card.number)].append(card)
        self._paid: dict[uuid.UUID, Decimal] | None = None
        self._of_operation: dict[uuid.UUID, tuple[uuid.UUID, str]] | None = None

    def find(self, ref: str, entity: uuid.UUID | None) -> ContractCard | None:
        """Договор по номеру - только если он один (у юрлица счёта, если оно известно)."""
        key = ref_key(ref)
        if not key:
            return None
        found = self.by_ref.get(key, [])
        if entity is not None:
            found = [card for card in found if card.ours == entity]
        return found[0] if len(found) == 1 else None

    def _allocate(self) -> None:
        if self._paid is not None:
            return
        from app.finance.contracts.payments import allocate
        from app.finance.contracts.service import Registry

        self._paid, self._of_operation = {}, {}
        if not self.contracts or not self.with_paid:
            return
        allocation = allocate(self.session, self.workspace, Registry(self.session, self.workspace), self.contracts)
        for contract_id, found in allocation.paid.items():
            total = ZERO
            for operation, how in found:
                value = Decimal(str(operation.amount_base))
                total += -value if how == "refund" else value
                self._of_operation[operation.id] = (contract_id, how)
            self._paid[contract_id] = total

    def paid(self, contract_id: uuid.UUID) -> Decimal | None:
        self._allocate()
        assert self._paid is not None
        return self._paid.get(contract_id)

    def of_operation(self, operation_id: uuid.UUID) -> tuple[ContractCard, str] | None:
        """К какому договору реестр отнёс операцию и как («number», «auto», «manual», «refund»)."""
        self._allocate()
        assert self._of_operation is not None
        hit = self._of_operation.get(operation_id)
        if hit is None or hit[0] not in self.cards:
            return None
        return self.cards[hit[0]], hit[1]

    def options(self) -> list[dict[str, Any]]:
        """Список для выбора «№Дог.»: номер, клиент, юрлицо, отдел."""
        return [
            {
                "id": str(card.id),
                "number": card.number,
                "customer": card.customer,
                "ours": str(card.ours) if card.ours else None,
                "department": card.department,
            }
            for card in sorted(self.cards.values(), key=lambda item: item.number)
            if card.number
        ]


# ── Справочники одним чтением ────────────────────────────────────────────────


@dataclass
class Book:
    """Справочники компании, собранные один раз на запрос."""

    accounts: dict[uuid.UUID, Account]
    categories: dict[uuid.UUID, Category]
    counterparties: dict[uuid.UUID, str]
    projects: dict[uuid.UUID, str]
    #: Наше юрлицо → короткое имя (код или название): «Фирма» журнала.
    entities: dict[uuid.UUID, str]
    #: Статьи, у которых есть свои статьи, - группы.
    parents: frozenset[uuid.UUID] = frozenset()

    @classmethod
    def load(cls, session: Session, workspace_id: uuid.UUID) -> "Book":
        accounts = {
            item.id: item
            for item in session.scalars(sa.select(Account).where(Account.workspace_id == workspace_id))
        }
        categories = {
            item.id: item
            for item in session.scalars(sa.select(Category).where(Category.workspace_id == workspace_id))
        }
        counterparties = dict(
            session.execute(
                sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace_id)
            ).all()
        )
        projects = dict(
            session.execute(sa.select(Project.id, Project.name).where(Project.workspace_id == workspace_id)).all()
        )
        entities: dict[uuid.UUID, str] = {}
        for entity, name in session.execute(
            sa.select(GroupEntity, Counterparty.name)
            .join(Counterparty, Counterparty.id == GroupEntity.counterparty_id)
            .where(GroupEntity.workspace_id == workspace_id)
        ):
            entities[entity.counterparty_id] = (entity.code or name or "").strip()
        parents = frozenset(item.parent_id for item in categories.values() if item.parent_id)
        return cls(accounts, categories, counterparties, projects, entities, parents)

    def account_name(self, account_id: uuid.UUID | None) -> str:
        account = self.accounts.get(account_id) if account_id else None
        return account.name if account else ""

    def entity_of(self, account_id: uuid.UUID | None) -> uuid.UUID | None:
        account = self.accounts.get(account_id) if account_id else None
        return account.group_entity_id if account else None

    def firm(self, account_id: uuid.UUID | None) -> str:
        entity = self.entity_of(account_id)
        return self.entities.get(entity, "") if entity else ""

    def category_name(self, category_id: uuid.UUID | None) -> str:
        category = self.categories.get(category_id) if category_id else None
        return category.name if category else ""

    def group_name(self, category_id: uuid.UUID | None) -> str:
        category = self.categories.get(category_id) if category_id else None
        if category is None:
            return ""
        if category.parent_id and category.parent_id in self.categories:
            return self.categories[category.parent_id].name
        # Статья верхнего уровня со своими статьями - сама себе группа («Доходы»).
        return category.name if category.id in self.parents else ""


# ── Строки и записи ──────────────────────────────────────────────────────────


def _money(value: Decimal | None) -> str:
    return "" if value is None else format(Decimal(str(value)).quantize(Decimal("0.01")), "f")


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _split_marks(session: Session, operations: Sequence[Operation]) -> dict[uuid.UUID, dict[str, Any]]:
    """Группы дробления: сумма выписки, сумма частей, сколько частей, сходится ли."""
    groups = list({operation.split_group for operation in operations if operation.split_group})
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for chunk in _chunks(groups, 900):
        for group, total, count, parts in session.execute(
            sa.select(
                Operation.split_group,
                sa.func.max(Operation.split_total),
                sa.func.count(Operation.id),
                sa.func.sum(Operation.amount),
            )
            .where(Operation.split_group.in_(chunk), Operation.deleted_at.is_(None))
            .group_by(Operation.split_group)
        ):
            parts_sum = Decimal(str(parts or 0))
            total_value = Decimal(str(total)) if total is not None else parts_sum
            out[group] = {
                "group": str(group),
                "total": _money(total_value),
                "parts": int(count or 0),
                "sum": _money(parts_sum),
                "ok": parts_sum == total_value,
            }
    return out


def _spaced(value: str) -> str:
    """«400000.00» → «400 000» - так дробление подписано в журнале BBC."""
    amount = Decimal(value or "0")
    whole = int(amount)
    text = f"{whole:,}".replace(",", " ")
    cents = amount - whole
    return text + (f",{int(round(cents * 100)):02d}" if cents else "")


def _split_label(mark: dict[str, Any] | None) -> str:
    return f"Общий {_spaced(mark['total'])}" if mark else ""


def _projects_of(session: Session, operation_ids: Sequence[uuid.UUID], book: Book) -> dict[uuid.UUID, list[str]]:
    out: dict[uuid.UUID, list[str]] = defaultdict(list)
    for chunk in _chunks(list(operation_ids), 900):
        for operation_id, project_id in session.execute(
            sa.select(OperationProject.operation_id, OperationProject.project_id).where(
                OperationProject.operation_id.in_(chunk)
            )
        ):
            name = book.projects.get(project_id)
            if name:
                out[operation_id].append(name)
    return out


def lines_of(
    operation: Operation,
    book: Book,
    *,
    splits: dict[uuid.UUID, dict[str, Any]] | None = None,
    projects: dict[uuid.UUID, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Операция → строки журнала BBC: у перевода - по строке на каждый свой счёт.

    `id` строки - id операции; у перевода - с суффиксом стороны («:out» -
    строка выписки счёта, откуда ушли деньги, «:in» - куда пришли). Правка
    любой из двух строк - правка одного перевода.
    """
    pnl = pnl_of(operation)
    mark = (splits or {}).get(operation.split_group) if operation.split_group else None
    base = {
        "pnl": pnl_title(pnl, operation.paid_at) if pnl else "",
        "paid_at": operation.paid_at.isoformat(),
        "counterparty": book.counterparties.get(operation.counterparty_id, "") if operation.counterparty_id else "",
        "split": _split_label(mark),
        "contract_ref": operation.contract_ref or "",
        "category": book.category_name(operation.category_id),
        "details": operation.details or "",
        "project": ", ".join((projects or {}).get(operation.id, [])),
        "group": book.group_name(operation.category_id),
        "comment": operation.comment or "",
        "note": operation.note or "",
        "month": _month_title(month_key(operation.paid_at)),
    }
    sides: list[tuple[str, uuid.UUID | None, str]] = []
    if operation.kind == "income":
        sides.append(("", operation.account_to_id, "income"))
    elif operation.kind == "expense":
        sides.append(("", operation.account_from_id, "expense"))
    else:
        if operation.account_from_id:
            sides.append((":out", operation.account_from_id, "expense"))
        if operation.account_to_id:
            sides.append((":in", operation.account_to_id, "income"))
        if not sides:
            sides.append(("", None, "expense"))
    out = []
    for suffix, account_id, direction in sides:
        firm = book.firm(account_id)
        cells = {
            **base,
            "account": book.account_name(account_id),
            "income": _money(operation.amount) if direction == "income" else "",
            "expense": _money(operation.amount) if direction == "expense" else "",
            "firm": firm,
            "code": f"{firm}|{operation.contract_ref}" if firm and operation.contract_ref else "",
        }
        out.append(
            {
                "id": f"{operation.id}{suffix}",
                "operation_id": str(operation.id),
                "version": operation.version,
                "kind": operation.kind,
                "side": suffix.lstrip(":") or None,
                "cells": cells,
            }
        )
    return out


def record_of(
    operation: Operation,
    book: Book,
    *,
    splits: dict[uuid.UUID, dict[str, Any]],
    projects: dict[uuid.UUID, list[str]],
    contracts: Contracts | None,
) -> dict[str, Any]:
    """Запись «приложенческого» вида: одна операция - одна карточка."""
    pnl = pnl_of(operation)
    category = book.categories.get(operation.category_id) if operation.category_id else None
    own_account = (
        operation.account_to_id
        if operation.kind == "income"
        else (operation.account_from_id or operation.account_to_id)
    )
    contract: dict[str, Any] | None = None
    if contracts is not None:
        allocated = contracts.of_operation(operation.id)
        if allocated is not None:
            card, how = allocated
            contract = {**card.out(contracts.paid(card.id)), "how": how}
        elif operation.contract_ref:
            card = contracts.find(operation.contract_ref, book.entity_of(own_account))
            if card is not None:
                contract = {**card.out(contracts.paid(card.id)), "how": "number"}
    mark = splits.get(operation.split_group) if operation.split_group else None
    half = operation.kind == "transfer" and not (operation.account_from_id and operation.account_to_id)
    no_contract = (
        operation.kind == "income"
        and not operation.contract_ref
        and contract is None
        and (category is None or category.nature == "revenue")
    )
    return {
        "id": str(operation.id),
        "version": operation.version,
        "kind": operation.kind,
        "paid_at": operation.paid_at.isoformat(),
        "amount": _money(operation.amount),
        "currency": operation.currency,
        "account_from": book.account_name(operation.account_from_id),
        "account_from_id": str(operation.account_from_id) if operation.account_from_id else None,
        "account_to": book.account_name(operation.account_to_id),
        "account_to_id": str(operation.account_to_id) if operation.account_to_id else None,
        "firm": book.firm(own_account),
        "counterparty": book.counterparties.get(operation.counterparty_id, "") if operation.counterparty_id else "",
        "category": category.name if category else "",
        "category_id": str(category.id) if category else None,
        "group": book.group_name(operation.category_id),
        "contract_ref": operation.contract_ref or "",
        "contract": contract,
        "pnl": pnl,
        "pnl_title": pnl_title(pnl, operation.paid_at),
        "pnl_differs": bool(pnl) and pnl != month_key(operation.paid_at),
        "details": operation.details or "",
        "project": ", ".join(projects.get(operation.id, [])),
        "comment": operation.comment or "",
        "note": operation.note or "",
        "split": mark,
        "source": operation.source,
        "half": half,
        "attention": [
            flag
            for flag, on in (
                ("uncategorized", operation.kind != "transfer" and category is None),
                ("question", bool(operation.note)),
                ("half", half),
                ("no_contract", no_contract),
                ("split", bool(mark) and not mark["ok"]),
            )
            if on
        ],
    }


# ── Отбор журнала ────────────────────────────────────────────────────────────


@dataclass
class JournalFilter:
    month: str = ""
    date_from: date | None = None
    date_to: date | None = None
    firm: uuid.UUID | None = None
    account_ids: Sequence[uuid.UUID] = ()
    category_ids: Sequence[uuid.UUID] = ()
    kinds: Sequence[str] = ()
    search: str = ""
    attention: str = ""
    contract_ref: str = ""

    def edges(self) -> tuple[date | None, date | None]:
        if self.month and re.fullmatch(r"\d{4}-\d{2}", self.month):
            year, month = int(self.month[:4]), int(self.month[5:])
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        return self.date_from, self.date_to


def _filtered(query, workspace_id: uuid.UUID, flt: JournalFilter, book: Book, *, attention: str | None = None):
    query = query.where(
        Operation.workspace_id == workspace_id, Operation.deleted_at.is_(None), Operation.status == "fact"
    )
    start, end = flt.edges()
    if start:
        query = query.where(Operation.paid_at >= start)
    if end:
        query = query.where(Operation.paid_at <= end)
    accounts = list(flt.account_ids)
    if flt.firm is not None:
        firm_accounts = [account.id for account in book.accounts.values() if account.group_entity_id == flt.firm]
        accounts = [item for item in accounts if item in firm_accounts] if accounts else firm_accounts
        if not accounts:
            return query.where(sa.false())
    if accounts:
        query = query.where(sa.or_(Operation.account_from_id.in_(accounts), Operation.account_to_id.in_(accounts)))
    if flt.category_ids:
        ids = set(flt.category_ids)
        # Группа отбирает и свои статьи: «Доходы» - это и «Доходы Аренда».
        ids |= {item.id for item in book.categories.values() if item.parent_id in ids}
        query = query.where(Operation.category_id.in_(list(ids)))
    if flt.kinds:
        query = query.where(Operation.kind.in_(list(flt.kinds)))
    if flt.contract_ref:
        query = query.where(Operation.contract_ref == flt.contract_ref)
    if flt.search.strip():
        like = f"%{flt.search.strip()}%"
        parties = sa.select(Counterparty.id).where(
            Counterparty.workspace_id == workspace_id, Counterparty.name.ilike(like)
        )
        query = query.where(
            sa.or_(
                Operation.comment.ilike(like),
                Operation.contract_ref.ilike(like),
                Operation.details.ilike(like),
                Operation.note.ilike(like),
                Operation.counterparty_id.in_(parties),
            )
        )
    flag = flt.attention if attention is None else attention
    if flag == "uncategorized":
        query = query.where(Operation.kind != "transfer", Operation.category_id.is_(None))
    elif flag == "question":
        query = query.where(Operation.note != "")
    elif flag == "half":
        query = query.where(
            Operation.kind == "transfer",
            sa.or_(Operation.account_from_id.is_(None), Operation.account_to_id.is_(None)),
        )
    elif flag == "no_contract":
        revenue = [item.id for item in book.categories.values() if item.side == "income" and item.nature == "revenue"]
        query = query.where(
            Operation.kind == "income",
            Operation.contract_ref == "",
            sa.or_(Operation.category_id.is_(None), Operation.category_id.in_(revenue)),
        )
    elif flag == "split":
        query = query.where(Operation.split_group.is_not(None))
    return query


def _sums(session: Session, workspace_id: uuid.UUID, flt: JournalFilter, book: Book) -> dict[str, str]:
    rows = session.execute(
        _filtered(
            sa.select(Operation.kind, sa.func.count(Operation.id), sa.func.coalesce(sa.func.sum(Operation.amount_base), 0)),
            workspace_id,
            flt,
            book,
        ).group_by(Operation.kind)
    ).all()
    by_kind = {kind: (int(count), Decimal(str(total))) for kind, count, total in rows}
    income = by_kind.get("income", (0, ZERO))[1]
    expense = by_kind.get("expense", (0, ZERO))[1]
    return {
        "count": str(sum(count for count, _total in by_kind.values())),
        "income": _money(income),
        "expense": _money(expense),
        "net": _money(income - expense),
        "transfers": _money(by_kind.get("transfer", (0, ZERO))[1]),
    }


def balances(session: Session, workspace_id: uuid.UUID, book: Book, start: date | None, end: date | None) -> list[dict[str, Any]]:
    """Остатки на счетах за период - как лист «Остатки на счетах»: начало, приход, расход, конец.

    Считаются по базе, а не по показанным строкам: остаток, собранный из
    страницы, совпал бы с банком только случайно.
    """

    def flows(*conditions) -> dict[uuid.UUID, list[Decimal]]:
        result: dict[uuid.UUID, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
        base = [
            Operation.workspace_id == workspace_id,
            Operation.deleted_at.is_(None),
            Operation.status == "fact",
            *conditions,
        ]
        for column, index in ((Operation.account_to_id, 0), (Operation.account_from_id, 1)):
            for account_id, total in session.execute(
                sa.select(column, sa.func.sum(Operation.amount)).where(*base, column.is_not(None)).group_by(column)
            ):
                result[account_id][index] += Decimal(str(total or 0))
        return result

    before = flows(Operation.paid_at < start) if start else {}
    inside_conditions = []
    if start:
        inside_conditions.append(Operation.paid_at >= start)
    if end:
        inside_conditions.append(Operation.paid_at <= end)
    inside = flows(*inside_conditions)
    out = []
    for account in sorted(book.accounts.values(), key=lambda item: (item.position, item.name)):
        came_before, went_before = before.get(account.id, [ZERO, ZERO])
        opening = Decimal(str(account.starting_balance or 0)) + came_before - went_before
        came, went = inside.get(account.id, [ZERO, ZERO])
        if not (came or went or opening):
            continue
        out.append(
            {
                "id": str(account.id),
                "name": account.name,
                "firm": book.entities.get(account.group_entity_id, "") if account.group_entity_id else "",
                "opening": _money(opening),
                "income": _money(came),
                "expense": _money(went),
                "closing": _money(opening + came - went),
            }
        )
    return out


def months_of(session: Session, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
    """Месяцы, в которых есть записи журнала, со счётом строк - для полосы месяцев."""
    counts: Counter[str] = Counter()
    for day, count in session.execute(
        sa.select(Operation.paid_at, sa.func.count(Operation.id))
        .where(Operation.workspace_id == workspace_id, Operation.deleted_at.is_(None), Operation.status == "fact")
        .group_by(Operation.paid_at)
    ):
        counts[month_key(day)] += int(count)
    return [{"month": key, "title": _month_title(key), "count": counts[key]} for key in sorted(counts)]


def list_records(
    session: Session,
    workspace: Workspace,
    flt: JournalFilter,
    *,
    limit: int = 400,
    offset: int = 0,
    contracts_visible: bool = True,
) -> dict[str, Any]:
    book = Book.load(session, workspace.id)
    total = int(session.scalar(_filtered(sa.select(sa.func.count(Operation.id)), workspace.id, flt, book)) or 0)
    operations = list(
        session.scalars(
            _filtered(sa.select(Operation), workspace.id, flt, book)
            .order_by(Operation.paid_at.desc(), Operation.created_at.desc(), Operation.id)
            .limit(limit)
            .offset(offset)
        )
    )
    splits = _split_marks(session, operations)
    projects = _projects_of(session, [item.id for item in operations], book)
    contracts = Contracts(session, workspace) if contracts_visible else None
    items = [record_of(item, book, splits=splits, projects=projects, contracts=contracts) for item in operations]
    start, end = flt.edges()
    # Счётчики «требуют внимания» - в пределах того же отбора, но без отбора по самому признаку.
    attention = {
        flag: int(
            session.scalar(
                _filtered(sa.select(sa.func.count(Operation.id)), workspace.id, flt, book, attention=flag)
            )
            or 0
        )
        for flag in ATTENTION
    }
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "sums": _sums(session, workspace.id, flt, book),
        "attention": attention,
        "balances": balances(session, workspace.id, book, start, end),
        "months": months_of(session, workspace.id),
    }


def _labels(session: Session, workspace_id: uuid.UUID) -> list[str]:
    return sorted(
        value
        for (value,) in session.execute(
            sa.select(Operation.period_label)
            .where(Operation.workspace_id == workspace_id, Operation.period_label != "", Operation.deleted_at.is_(None))
            .distinct()
        )
    )


def dictionaries(session: Session, workspace: Workspace, *, contracts_visible: bool = True) -> dict[str, Any]:
    """Списки для выбора в журнале: счета, статьи по сторонам, юрлица, договоры, месяцы ОПиУ."""
    book = Book.load(session, workspace.id)
    months = [item["month"] for item in months_of(session, workspace.id)]
    categories = [
        {
            "id": str(item.id),
            "name": item.name,
            "side": item.side,
            "group": book.group_name(item.id),
            "parent_id": str(item.parent_id) if item.parent_id else None,
        }
        for item in sorted(book.categories.values(), key=lambda value: (value.side, value.position, value.name))
        if item.archived_at is None
    ]
    return {
        "accounts": [
            {
                "id": str(item.id),
                "name": item.name,
                "firm": book.entities.get(item.group_entity_id, "") if item.group_entity_id else "",
                "firm_id": str(item.group_entity_id) if item.group_entity_id else None,
                "number": item.number,
            }
            for item in sorted(book.accounts.values(), key=lambda value: (value.position, value.name))
            if item.archived_at is None
        ],
        "categories": categories,
        "firms": [
            {"id": str(key), "name": value}
            for key, value in sorted(book.entities.items(), key=lambda item: item[1])
            if value
        ],
        "periods": period_options(months) + [{"value": label, "title": label} for label in _labels(session, workspace.id)],
        "contracts": Contracts(session, workspace, with_paid=False).options() if contracts_visible else [],
    }


# ── Справочники по тексту: статья, контрагент, проект, счёт ─────────────────


def _nature_for(side: str, name: str, group: str) -> str:
    """Природа новой статьи из её названия - умолчание, которое правится в «Справочниках».

    Без неё каждая статья из журнала ложилась бы «операционным расходом», и
    «Показатели» считали бы заём доходом, а налог - расходом офиса.
    """
    text = f"{norm(group)} {norm(name)}"
    if side == "income":
        if re.search(r"займ|заём|долг|кредит|капитал|взнос учред", text):
            return "capital"
        if "доход" in text and "проч" not in text:
            return "revenue"
        return "other"
    if side == "expense":
        if "налог" in text:
            return "tax"
        if re.search(r"кредит\s*%|процент", text):
            return "financial"
        if re.search(r"займ|заём|долг|кредит|дивиденд|капитал", text):
            return "capital"
        return "operating"
    return "operating"


class Resolver:
    """Справочники по тексту - с кэшем на всю загрузку и без запроса на строку.

    Загрузка на шесть тысяч строк спрашивала бы базу о статье и контрагенте
    каждой строки: на Railway это минуты. Здесь всё читается один раз, новое
    заводится по первому упоминанию и дальше берётся из памяти.
    """

    def __init__(self, session: Session, workspace: Workspace, *, create: bool = True):
        self.session = session
        self.workspace = workspace
        self.create = create
        self.categories: dict[tuple[str, str], Category] = {}
        self.top: dict[tuple[str, str], Category] = {}
        for item in session.scalars(sa.select(Category).where(Category.workspace_id == workspace.id)):
            key = (item.side, norm(item.name))
            if item.parent_id is None:
                self.top[key] = item
            self.categories.setdefault(key, item)
        self.parties: dict[tuple[str, str], Counterparty] = {}
        self.by_bin: dict[str, Counterparty] = {}
        for item in session.scalars(sa.select(Counterparty).where(Counterparty.workspace_id == workspace.id)):
            self.parties.setdefault((item.role, norm(item.name)), item)
            bin_value = str((item.details or {}).get("bin") or "")
            if bin_value:
                self.by_bin.setdefault(bin_value, item)
        self.projects: dict[str, Project] = {
            norm(item.name): item
            for item in session.scalars(sa.select(Project).where(Project.workspace_id == workspace.id))
        }
        self.accounts: dict[str, Account] = {
            item.normalized_name: item
            for item in session.scalars(sa.select(Account).where(Account.workspace_id == workspace.id))
        }
        self._position = int(
            session.scalar(
                sa.select(sa.func.coalesce(sa.func.max(Category.position), 0)).where(Category.workspace_id == workspace.id)
            )
            or 0
        )

    def _new_category(self, side: str, name: str, parent: Category | None, group: str) -> Category:
        self._position += POSITION_STEP
        item = Category(
            workspace_id=self.workspace.id,
            side=side,
            name=name,
            normalized_name=norm(name),
            parent_id=parent.id if parent else None,
            nature=_nature_for(side, name, group),
            position=self._position,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def category(self, side: str, leaf: str | None, group: str | None = None) -> Category | None:
        """Статья по «Подкатегории» и «Категории»: сначала существующая, потом новая под своей группой."""
        leaf = (leaf or "").strip()
        group = (group or "").strip()
        if not leaf and not group:
            return None
        if not leaf:
            leaf, group = group, ""
        found = self.categories.get((side, norm(leaf)))
        if found is not None or not self.create:
            return found
        parent = None
        if group and norm(group) != norm(leaf):
            parent = self.top.get((side, norm(group)))
            if parent is None:
                parent = self._new_category(side, group, None, group)
                self.top[(side, norm(group))] = parent
                self.categories.setdefault((side, norm(group)), parent)
        item = self._new_category(side, leaf, parent, group or leaf)
        self.categories[(side, norm(leaf))] = item
        if parent is None:
            self.top[(side, norm(leaf))] = item
        return item

    def party(self, name: str | None, role: str, bin_value: str = "") -> Counterparty | None:
        if bin_value and bin_value in self.by_bin:
            return self.by_bin[bin_value]
        clean = (name or "").strip()
        if not clean:
            return None
        found = self.parties.get((role, norm(clean)))
        if found is None and self.create:
            found = Counterparty(
                workspace_id=self.workspace.id,
                role=role,
                name=clean,
                normalized_name=norm(clean),
                details={"bin": bin_value} if bin_value else {},
            )
            self.session.add(found)
            self.session.flush()
            self.parties[(role, norm(clean))] = found
        if found is not None and bin_value and not (found.details or {}).get("bin"):
            found.details = {**(found.details or {}), "bin": bin_value}
            self.by_bin[bin_value] = found
        return found

    def project(self, name: str | None) -> Project | None:
        clean = (name or "").strip()
        if not clean:
            return None
        found = self.projects.get(norm(clean))
        if found is None and self.create:
            found = service.ensure_project(self.session, self.workspace.id, clean)
            if found is not None:
                self.projects[norm(clean)] = found
        return found

    def account(self, name: str | None) -> Account | None:
        clean = (name or "").strip()
        return self.accounts.get(norm(clean)) if clean else None


def _is_transfer_group(group: str | None, leaf: str | None, transfer_names: set[str]) -> bool:
    if group and norm(group) in TRANSFER_GROUPS:
        return True
    return bool(leaf) and (norm(leaf) in transfer_names or norm(leaf) in TRANSFER_GROUPS)


def _transfer_names(session: Session, workspace_id: uuid.UUID) -> set[str]:
    return {
        norm(name)
        for (name,) in session.execute(
            sa.select(Category.name).where(Category.workspace_id == workspace_id, Category.side == "transfer")
        )
    }


# ── Загрузка: разбор файла под журнал ────────────────────────────────────────


def _sheet_rows(data: bytes, file_name: str) -> tuple[list[list[Any]] | None, list[list[Any]] | None]:
    """Шаблон журнала: лист «Журнал» и лист «Справочник» - если это он.

    Общий разбор берёт из книги «лучшую» таблицу, и в шаблоне с длинным
    справочником и короткой записью за день выбрал бы справочник. Шаблон
    узнаётся по имени листа, остальное (выписки, чужие книги) - общим разбором.
    """
    if not data.startswith(b"PK"):
        return None, None
    try:
        import openpyxl

        book = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception:  # noqa: BLE001 - не книга Excel: пусть читает общий разбор
        return None, None
    try:
        names = {norm(sheet.title): sheet for sheet in book.worksheets}
        journal = names.get("журнал")
        if journal is None:
            return None, None
        rows = [list(row) for row in journal.iter_rows(values_only=True)]
        reference = names.get("справочник")
        ref_rows = [list(row) for row in reference.iter_rows(values_only=True)] if reference is not None else None
        return rows, ref_rows
    finally:
        book.close()


_ACCOUNT_HEADS = ("счет", "счёт", "счета", "счёта")
_FIRM_HEADS = ("наше юрлицо", "наши тоо", "фирма", "юрлицо", "наше тоо")
_NUMBER_HEADS = ("номер счета (iban)", "номер счёта (iban)", "номер счета", "номер счёта", "iban")
_OPENING_HEADS = ("остаток на начало", "начальный остаток")


def _reference_accounts(rows: list[list[Any]] | None) -> dict[str, dict[str, str]]:
    """Счета из листа «Справочник» шаблона: юрлицо, номер, остаток на начало."""
    if not rows:
        return {}
    for index, row in enumerate(rows[:5]):
        heads = [norm(cell) for cell in row]
        account_col = next((i for i, head in enumerate(heads) if head in _ACCOUNT_HEADS), None)
        if account_col is None:
            continue

        def near(names: tuple[str, ...]) -> int | None:
            return next((i for i, head in enumerate(heads) if i > account_col and head in names), None)

        firm_col, number_col, opening_col = near(_FIRM_HEADS), near(_NUMBER_HEADS), near(_OPENING_HEADS)
        # Короткое имя юрлица («BBC», «BBCA») в справочнике BBC стоит в колонке
        # сразу за «Наши ТОО» - без заголовка или с «Код».
        code_col = None
        if firm_col is not None and firm_col + 1 < len(heads) and heads[firm_col + 1] in ("", "код", "код юрлица", "кратко"):
            code_col = firm_col + 1
        out: dict[str, dict[str, str]] = {}
        for line in rows[index + 1 :]:

            def at(col: int | None) -> str:
                if col is None or col >= len(line) or line[col] is None:
                    return ""
                return str(line[col]).strip()

            name = at(account_col)
            if name:
                out[norm(name)] = {
                    "name": name,
                    "firm": at(firm_col),
                    "code": at(code_col),
                    "number": at(number_col),
                    "opening": at(opening_col),
                }
        return out
    return {}


_LEGAL_FORM = re.compile(r"^(тоо|ип|ао|llp|too)")


def _entity_for(session: Session, workspace_id: uuid.UUID, *texts: str) -> uuid.UUID | None:
    """Наше юрлицо по названию юрлица или счёта - только если подходит одно.

    «Kaspi Юр. ТОО "BBC HR"» содержит и «BBC HR», и «BBC»: берётся самое
    длинное совпадение, и только если оно одно.
    """
    candidates: list[tuple[str, uuid.UUID]] = []
    for entity, name in session.execute(
        sa.select(GroupEntity, Counterparty.name)
        .join(Counterparty, Counterparty.id == GroupEntity.counterparty_id)
        .where(GroupEntity.workspace_id == workspace_id, GroupEntity.archived_at.is_(None))
    ):
        for label in (name, entity.code, entity.full_name):
            key = _LEGAL_FORM.sub("", party_key(label))
            if len(key) >= 2:
                candidates.append((key, entity.counterparty_id))
    for text in texts:
        if not text:
            continue
        wanted = _LEGAL_FORM.sub("", party_key(text))
        exact = {entity for key, entity in candidates if key == wanted}
        if len(exact) == 1:
            return next(iter(exact))
        inside = [(len(key), entity) for key, entity in candidates if key in wanted]
        if inside:
            longest = max(length for length, _entity in inside)
            best = {entity for length, entity in inside if length == longest}
            if len(best) == 1:
                return next(iter(best))
    return None


def analyze_file(
    session: Session,
    workspace: Workspace,
    data: bytes,
    file_name: str,
    *,
    date_order: str | None = None,
    default_account: str | None = None,
) -> tuple[importing.Preview, dict[str, Any]]:
    """Файл → разбор: шаблон журнала, журнал BBC, выписка любого банка.

    Возвращает разбор общего импорта и решения журнала (новые счета и их
    юрлица) - они лягут в партию вместе со строками.
    """
    accounts = [item.name for item in service.list_accounts(session, workspace.id)]
    numbers = service.account_numbers(session, workspace.id)
    rows, reference = _sheet_rows(data, file_name)

    def read(order: str | None) -> importing.Preview:
        if rows is not None:
            return importing.analyze_rows(
                rows, file_name, accounts, date_order=order, default_account=default_account, account_numbers=numbers
            )
        return importing.analyze(
            data, file_name, accounts, date_order=order, default_account=default_account, account_numbers=numbers
        )

    preview = read(date_order)
    if date_order is None and preview.question and preview.question.get("kind") == "date_order":
        # «пн 01.06.26» сам отвечает на вопрос: 1 июня 2026 - понедельник,
        # 6 января - вторник. День недели в дате - довод, а не догадка.
        order = _order_by_weekday(preview)
        if order is not None:
            preview = read(order)
    source = "template" if rows is not None else ("statement" if preview.bank else "table")
    extra = prepare(session, workspace, preview, reference=_reference_accounts(reference))
    extra["source"] = source
    return preview, extra


_WEEKDAY_DATE = re.compile(
    r"^\s*(пн|вт|ср|чт|пт|сб|вс)\.?,?\s+(\d{1,2})[./-](\d{1,2})[./-](\d{2}|\d{4})\b", re.IGNORECASE
)
_WEEKDAY_INDEX = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}


def _order_by_weekday(preview: importing.Preview) -> str | None:
    """Порядок частей даты по дню недели перед ней - если он сходится только с одним чтением."""
    header = ((preview.mapping or {}).get("columns") or {}).get("paid_at", {}).get("header")
    if not header:
        return None
    fits = {"dmy": 0, "mdy": 0}
    seen = 0
    for row in preview.rows:
        match = _WEEKDAY_DATE.match(str((row.raw or {}).get(header) or ""))
        if not match:
            continue
        weekday, first, second, year_text = match.groups()
        year = int(year_text) + (2000 if len(year_text) == 2 else 0)
        seen += 1
        for order, (day, month) in (("dmy", (int(first), int(second))), ("mdy", (int(second), int(first)))):
            try:
                if date(year, month, day).weekday() == _WEEKDAY_INDEX[weekday.lower()]:
                    fits[order] += 1
            except ValueError:
                pass
    if not seen:
        return None
    matching = [order for order, count in fits.items() if count == seen]
    return matching[0] if len(matching) == 1 else None


def _clear_account_problems(row: importing.ParsedRow, new_accounts: set[str]) -> None:
    """Счёт, который заведётся при принятии, - не замечание строки."""
    left = []
    for problem in row.problems:
        text = problem.get("text", "")
        match = re.match(r"счёт «(.+)» не найден", text)
        if match and norm(match.group(1)) in new_accounts:
            continue
        if problem.get("field") in ("account_to", "account_from") and text.startswith("не указано"):
            continue
        left.append(problem)
    row.problems = left


def prepare(
    session: Session, workspace: Workspace, preview: importing.Preview, *, reference: dict[str, dict[str, str]] | None = None
) -> dict[str, Any]:
    """Разбор → строки журнала: счета, переводы, дробления, договоры, повторы.

    Меняет строки разбора на месте; возвращает решения для партии.
    """
    reference = reference or {}
    known_accounts = {item.normalized_name: item for item in service.list_accounts(session, workspace.id)}
    transfer_names = _transfer_names(session, workspace.id)
    file_accounts: dict[str, str] = {}

    # Счёт строки. В журнале одна колонка «Счёт», и разбор, не найдя счёт в
    # справочнике, оставляет имя только в замечании. Новые счета журнала
    # заводятся при «Принять» - строка из-за них не отложенная.
    for row in preview.rows:
        raw_account = None
        for header, value in (row.raw or {}).items():
            if norm(header) in _ACCOUNT_HEADS and value not in (None, ""):
                raw_account = str(value).strip()
                break
        if raw_account:
            row.values["account"] = raw_account
            if norm(raw_account) not in known_accounts:
                file_accounts.setdefault(norm(raw_account), raw_account)
    new_accounts = set(file_accounts)
    for row in preview.rows:
        if row.state == "skipped":
            continue
        name = row.values.get("account")
        kind = row.values.get("kind")
        if name and norm(name) in new_accounts:
            if kind == "income" and not row.values.get("account_to"):
                row.values["account_to"] = name
            elif kind == "expense" and not row.values.get("account_from"):
                row.values["account_from"] = name
            _clear_account_problems(row, new_accounts)
            if not row.problems and row.values.get("paid_at") and row.values.get("amount") and kind:
                row.state = "imported"
    # Строка выписки без суммы («Продление депозита») - движения денег нет:
    # это не отложенная запись, которую надо чинить, а не запись вовсе.
    for row in preview.rows:
        if row.state == "failed" and not row.values.get("amount") and row.values.get("paid_at"):
            fields = {problem.get("field") for problem in row.problems}
            unreadable = any(problem.get("text", "").startswith("не понял сумму") for problem in row.problems)
            if not unreadable and fields <= {"amount", "kind", "account", "account_to", "account_from"}:
                row.state = "skipped"
                row.problems = [{"field": "", "text": "без суммы - движения денег нет, в журнал не ложится"}]

    # Перемещения между своими счетами: группа «Перемещения» или статья-перевод.
    for row in preview.rows:
        values = row.values
        if row.state != "imported" or values.get("kind") not in ("income", "expense"):
            continue
        group = values.get("category") if values.get("subcategory") else None
        leaf = values.get("subcategory") or values.get("category")
        if _is_transfer_group(group, leaf, transfer_names):
            values["own_transfer"] = "in" if values["kind"] == "income" else "out"
            values["kind"] = "transfer"
            values["counterparty"] = None
            values["transfer_category"] = None if leaf and norm(leaf) in TRANSFER_GROUPS else leaf
    _pair_transfers(preview.rows, known_accounts)
    _mark_splits(preview.rows)

    # Номер договора из назначения платежа - только при единственном договоре.
    contracts = Contracts(session, workspace, with_paid=False)
    entities_cache: dict[str, uuid.UUID | None] = {}
    for row in preview.rows:
        values = row.values
        if row.state != "imported" or values.get("kind") != "income" or values.get("contract_ref"):
            continue
        account_name = values.get("account_to") or ""
        if account_name not in entities_cache:
            known = known_accounts.get(norm(account_name))
            entities_cache[account_name] = (
                known.group_entity_id
                if known
                else _entity_for(session, workspace.id, (reference.get(norm(account_name)) or {}).get("firm", ""), account_name)
            )
        entity = entities_cache[account_name]
        hits = {}
        for key in refs_in_text(values.get("comment") or ""):
            card = contracts.find(key, entity)
            if card is not None:
                hits[card.id] = card
        if len(hits) == 1:
            card = next(iter(hits.values()))
            values["contract_ref"] = card.number
            values["contract_auto"] = "по назначению платежа"

    ready = [row for row in preview.rows if row.state == "imported"]
    for row in ready:
        row.values["external_key"] = _key_of(row.values)
    importing._number_duplicates(ready)
    _mark_repeats(session, workspace, preview.rows, known_accounts)

    accounts_new = []
    firms: dict[str, set[uuid.UUID]] = defaultdict(set)
    for key, name in sorted(file_accounts.items(), key=lambda item: item[1]):
        ref = reference.get(key) or {}
        entity = _entity_for(session, workspace.id, ref.get("code", ""), ref.get("firm", ""), name)
        if entity is not None and ref.get("firm"):
            firms[party_key(ref["firm"])].add(entity)
        accounts_new.append(
            {"name": name, "firm_id": str(entity) if entity else None, "number": ref.get("number", ""), "opening": ref.get("opening", ""),
             "firm": ref.get("firm", "")}
        )
    # У депозита в справочнике BBC кода юрлица нет, а у текущего счёта того же ТОО
    # есть: одно и то же «Наши ТОО» - одно юрлицо, если оно определилось однозначно.
    for item in accounts_new:
        if item["firm_id"] is None and item["firm"]:
            known = firms.get(party_key(item["firm"]), set())
            if len(known) == 1:
                item["firm_id"] = str(next(iter(known)))
    return {"accounts_new": accounts_new}


def _key_of(values: dict[str, Any]) -> str:
    """Отпечаток строки журнала: у перевода - дата, сумма и оба счёта, как в разборе выписок."""
    if values.get("kind") == "transfer":
        parts = [
            "own-transfer",
            str(values.get("paid_at") or ""),
            str(values.get("amount") or ""),
            str(values.get("account_from") or ""),
            str(values.get("account_to") or ""),
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return importing.fingerprint(values)


def _pair_transfers(rows: list[importing.ParsedRow], known: dict[str, Account]) -> None:
    """Две строки одного перевода (выписка каждого счёта) → один перевод.

    Пара - один день, одна сумма, разные стороны и разные счета; при выборе
    из нескольких - сначала счёт того же юрлица. Строка, ставшая второй
    стороной, остаётся в партии с пометкой, какой строке она отдана.
    """
    halves = [row for row in rows if row.state == "imported" and row.values.get("own_transfer") in ("in", "out")]
    by_key: dict[tuple[str, str], list[importing.ParsedRow]] = defaultdict(list)
    for row in halves:
        # Сумма - числом, а не текстом: «614 920,00» в одной выписке и «614920»
        # в другой - одна сумма, и пара не должна расходиться на записи.
        amount = _money(Decimal(str(row.values.get("amount") or 0)))
        by_key[(row.values.get("paid_at") or "", amount)].append(row)

    def account_of(row: importing.ParsedRow) -> str:
        values = row.values
        return (values.get("account_to") if values["own_transfer"] == "in" else values.get("account_from")) or ""

    def firm_of(name: str) -> uuid.UUID | None:
        account = known.get(norm(name or ""))
        return account.group_entity_id if account else None

    for group in by_key.values():
        outs = [row for row in group if row.values["own_transfer"] == "out"]
        ins = [row for row in group if row.values["own_transfer"] == "in"]
        for out in outs:
            if out.values.get("account_to"):
                continue  # перевод с обоими счетами уже из реквизитов выписки
            source = account_of(out)
            choices = [
                row
                for row in ins
                if not row.values.get("paired_with") and not row.values.get("account_from") and account_of(row) != source
            ]
            if not choices:
                continue
            same_firm = [
                row for row in choices if firm_of(account_of(row)) is not None and firm_of(account_of(row)) == firm_of(source)
            ]
            chosen = (same_firm or choices)[0]
            out.values["account_to"] = account_of(chosen)
            out.values["paired_with"] = chosen.line
            if not out.values.get("transfer_category"):
                out.values["transfer_category"] = chosen.values.get("transfer_category")
            if not out.values.get("comment") and chosen.values.get("comment"):
                out.values["comment"] = chosen.values["comment"]
            chosen.values["paired_with"] = out.line
            chosen.state = "skipped"
            chosen.problems.append({"field": "", "text": f"вторая сторона перевода из строки {out.line}"})
    for row in halves:
        row.values.setdefault("account_from", None)
        row.values.setdefault("account_to", None)


_SPLIT_TOTAL = re.compile(r"(\d[\d\s.,  ]*)")


def _mark_splits(rows: list[importing.ParsedRow]) -> None:
    """Части одной суммы выписки: одна метка «Дробление», один день, один счёт."""
    groups: dict[tuple[str, str, str, str], list[importing.ParsedRow]] = defaultdict(list)
    for row in rows:
        mark = (row.values.get("split_mark") or "").strip()
        if not mark or row.state not in ("imported", "failed"):
            continue
        account = row.values.get("account_to") or row.values.get("account_from") or row.values.get("account") or ""
        groups[(norm(mark), row.values.get("paid_at") or "", norm(account), row.values.get("kind") or "")].append(row)
    for (mark, day, account, kind), parts in groups.items():
        if len(parts) < 2:
            continue
        parts_sum = sum((Decimal(str(row.values.get("amount") or 0)) for row in parts), ZERO)
        found = _SPLIT_TOTAL.search(mark)
        money = importing.parse_money(found.group(1)) if found else None
        total = money.value if money and money.value > 0 else parts_sum
        key = hashlib.sha1(f"{mark}|{day}|{account}|{kind}".encode("utf-8")).hexdigest()[:16]
        for row in parts:
            row.values["split_key"] = key
            row.values["split_total"] = str(total)
            if total != parts_sum:
                row.problems.append(
                    {"field": "", "text": f"части дробления дают {_spaced(str(parts_sum))} из {_spaced(str(total))}"}
                )


def _mark_repeats(
    session: Session, workspace: Workspace, rows: list[importing.ParsedRow], known_accounts: dict[str, Account]
) -> None:
    """Строки, которые уже есть в журнале.

    Тот же отпечаток - повторная загрузка того же файла. Но выписка,
    загруженная после журнала, заведённого шаблоном, печатает то же назначение
    иначе, и отпечатки разные. Тогда смотрим по делу: день, счёт, сторона,
    сумма - «возможный повтор», и такая строка не заводится без решения
    человека («Завести всё равно»).
    """
    ready = [row for row in rows if row.state == "imported"]
    if not ready:
        return
    keys = [row.values["external_key"] for row in ready if row.values.get("external_key")]
    known: set[str] = set()
    for chunk in _chunks(keys, 900):
        known.update(
            key
            for (key,) in session.execute(
                sa.select(Operation.external_key).where(
                    Operation.workspace_id == workspace.id,
                    Operation.deleted_at.is_(None),
                    Operation.external_key.in_(chunk),
                )
            )
        )
    days = sorted({row.values["paid_at"] for row in ready if row.values.get("paid_at")})
    existing: Counter[tuple[str, str, str, str]] = Counter()
    if days:
        for paid_at, amount, account_from, account_to in session.execute(
            sa.select(Operation.paid_at, Operation.amount, Operation.account_from_id, Operation.account_to_id).where(
                Operation.workspace_id == workspace.id,
                Operation.deleted_at.is_(None),
                Operation.status == "fact",
                Operation.paid_at >= date.fromisoformat(days[0]),
                Operation.paid_at <= date.fromisoformat(days[-1]),
            )
        ):
            amount_text = _money(amount)
            if account_to:
                existing[(str(account_to), "in", paid_at.isoformat(), amount_text)] += 1
            if account_from:
                existing[(str(account_from), "out", paid_at.isoformat(), amount_text)] += 1
    for row in ready:
        values = row.values
        if values.get("external_key") in known:
            row.state = "duplicate"
            row.problems.append({"field": "", "text": "такая операция уже есть - повторная загрузка того же файла"})
            continue
        amount_text = _money(Decimal(str(values.get("amount") or 0)))
        sides = []
        if values.get("account_to"):
            sides.append(("in", values["account_to"]))
        if values.get("account_from"):
            sides.append(("out", values["account_from"]))
        hit = False
        for direction, name in sides:
            account = known_accounts.get(norm(name))
            if account is None:
                continue
            key = (str(account.id), direction, values.get("paid_at") or "", amount_text)
            if existing[key] > 0:
                existing[key] -= 1
                hit = True
        if hit:
            row.state = "duplicate"
            values["maybe_duplicate"] = True
            row.problems.append(
                {
                    "field": "",
                    "text": "в журнале уже есть запись того же дня на этот счёт с той же суммой - "
                    "похоже, строка уже загружена другим файлом",
                }
            )


# ── Предпросмотр: строки как в журнале ───────────────────────────────────────


def preview_line(values: dict[str, Any]) -> dict[str, str]:
    """Значения строки партии → ячейки журнала, как их покажет таблица."""
    kind = values.get("kind")
    amount = _money(Decimal(str(values["amount"]))) if values.get("amount") else ""
    if kind == "transfer":
        both = bool(values.get("account_from") and values.get("account_to"))
        if both:
            account = f"{values['account_from']} → {values['account_to']}"
        else:
            account = values.get("account_to") or values.get("account_from") or values.get("account") or ""
        incoming = values.get("own_transfer") == "in" and not both
        income, expense = (amount, "") if incoming else ("", amount)
    else:
        account = (values.get("account_to") if kind == "income" else values.get("account_from")) or values.get("account") or ""
        income = amount if kind == "income" else ""
        expense = amount if kind == "expense" else ""
    category = values.get("transfer_category") if kind == "transfer" else (values.get("subcategory") or values.get("category"))
    group = (values.get("category") if values.get("subcategory") else "") or ("Перемещения" if kind == "transfer" else "")
    pnl = ""
    if values.get("period_label"):
        pnl = values["period_label"]
    elif values.get("period_start"):
        start = date.fromisoformat(values["period_start"])
        end = date.fromisoformat(values.get("period_end") or values["period_start"])
        whole_year = (start.month, start.day) == (1, 1) and end == date(start.year, 12, 31)
        pnl = f"{start.year} год" if whole_year else _month_title(month_key(start))
    return {
        "pnl": pnl,
        "paid_at": values.get("paid_at") or "",
        "account": account,
        "income": income,
        "expense": expense,
        "counterparty": values.get("counterparty") or "",
        "split": f"Общий {_spaced(str(values['split_total']))}" if values.get("split_total") else "",
        "contract_ref": values.get("contract_ref") or "",
        "category": category or "",
        "details": values.get("details") or "",
        "project": values.get("project") or "",
        "group": group or "",
        "comment": values.get("comment") or "",
        "note": values.get("note") or "",
    }


def preview_payload(session: Session, workspace: Workspace, batch: ImportBatch) -> dict[str, Any]:
    """Партия загрузки глазами журнала: новые строки и соседние строки журнала.

    Соседи - записи того же периода на тех же счетах: окно показывает, как
    журнал будет выглядеть после «Принять», а не голый список из файла.
    """
    rows = list(session.scalars(sa.select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.line)))
    decisions = batch.decisions or {}
    out_rows = []
    days: list[str] = []
    names: set[str] = set()
    for row in rows:
        values = row.parsed or {}
        if row.state != "skipped" and values.get("paid_at"):
            days.append(values["paid_at"])
            for key in ("account_from", "account_to", "account"):
                if values.get(key):
                    names.add(norm(values[key]))
        out_rows.append(
            {
                "line": row.line,
                "state": row.state,
                "problems": row.problems or [],
                "cells": preview_line(values),
                "kind": values.get("kind"),
                "contract_auto": values.get("contract_auto") or "",
                "paired_with": values.get("paired_with"),
                "maybe_duplicate": bool(values.get("maybe_duplicate")),
                "operation_id": str(row.operation_id) if row.operation_id else None,
            }
        )
    context: list[dict[str, Any]] = []
    if days:
        book = Book.load(session, workspace.id)
        account_ids = [item.id for item in book.accounts.values() if item.normalized_name in names]
        if account_ids:
            operations = list(
                session.scalars(
                    sa.select(Operation)
                    .where(
                        Operation.workspace_id == workspace.id,
                        Operation.deleted_at.is_(None),
                        Operation.status == "fact",
                        Operation.paid_at >= date.fromisoformat(min(days)),
                        Operation.paid_at <= date.fromisoformat(max(days)),
                        sa.or_(Operation.account_from_id.in_(account_ids), Operation.account_to_id.in_(account_ids)),
                    )
                    .order_by(Operation.paid_at, Operation.created_at)
                    .limit(400)
                )
            )
            splits = _split_marks(session, operations)
            for operation in operations:
                context.extend(lines_of(operation, book, splits=splits))
    counts = Counter(row["state"] for row in out_rows)
    return {
        "batch_id": str(batch.id),
        "file_name": batch.file_name,
        "status": batch.status,
        "source": decisions.get("journal_source") or "table",
        "counts": {
            "total": len(out_rows),
            "ready": counts.get("imported", 0),
            "failed": counts.get("failed", 0),
            "skipped": counts.get("skipped", 0),
            "duplicate": counts.get("duplicate", 0),
        },
        "accounts_new": decisions.get("accounts_new") or [],
        "statement_account": decisions.get("statement_account") or {},
        "rows": out_rows,
        "context": context,
        "columns": [
            {"key": column.key, "title": column.title, "kind": column.kind, "width": column.width}
            for column in COLUMNS
            if column.template
        ],
    }


#: Что можно поправить в строке загрузки до принятия.
PREVIEW_FIELDS = (
    "pnl", "paid_at", "account", "income", "expense", "counterparty", "contract_ref", "category",
    "details", "project", "comment", "note", "force",
)


def fix_row(session: Session, workspace: Workspace, batch_id: uuid.UUID, line: int, patch: dict[str, Any]) -> ImportRow:
    """Правка строки загрузки по ячейкам журнала - до «Принять»."""
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.workspace_id != workspace.id:
        raise FinanceError("Загрузка не найдена")
    if batch.status != "preview":
        raise FinanceError("Загрузка уже принята или отклонена - строки правятся в журнале")
    row = session.scalar(sa.select(ImportRow).where(ImportRow.batch_id == batch.id, ImportRow.line == line))
    if row is None:
        raise FinanceError("Строка не найдена")
    unknown = set(patch) - set(PREVIEW_FIELDS)
    if unknown:
        raise FinanceError(f"Неизвестные поля: {', '.join(sorted(unknown))}")
    values = dict(row.parsed or {})
    if "paid_at" in patch:
        day = importing.parse_date(patch["paid_at"], CELL_READING) if str(patch["paid_at"] or "").strip() else None
        if day is None:
            raise FinanceError(f"Не понял дату «{patch['paid_at']}». Пример: 03.09.2026")
        check_date(day, field="Дата")
        values["paid_at"] = day.isoformat()
    if "income" in patch or "expense" in patch:
        income_text = str(patch.get("income") or "").strip()
        expense_text = str(patch.get("expense") or "").strip()
        if income_text and expense_text:
            raise FinanceError("Сумма стоит либо в «Приходе», либо в «Расходе»")
        money = importing.parse_money(income_text or expense_text) if (income_text or expense_text) else None
        if money is None or not money.value:
            raise FinanceError("Без суммы строка не станет записью журнала")
        if money.negative:
            raise FinanceError("Минус в сумме не нужен: направление задаёт колонка «Приход» или «Расход»")
        values["amount"] = str(check_money(money.value))
        new_kind = "income" if income_text else "expense"
        if values.get("kind") == "transfer":
            if not (values.get("account_from") and values.get("account_to")):
                values["own_transfer"] = "in" if new_kind == "income" else "out"
                account = values.get("account_to") or values.get("account_from")
                values["account_to"] = account if new_kind == "income" else None
                values["account_from"] = account if new_kind == "expense" else None
        elif new_kind != values.get("kind"):
            account = values.get("account_to") or values.get("account_from") or values.get("account")
            values["account_to"] = account if new_kind == "income" else None
            values["account_from"] = account if new_kind == "expense" else None
            values["kind"] = new_kind
    if "account" in patch:
        name = str(patch["account"] or "").strip()
        values["account"] = name
        if values.get("kind") == "income":
            values["account_to"] = name or None
        elif values.get("kind") == "expense":
            values["account_from"] = name or None
        elif values.get("kind") == "transfer":
            side = "account_to" if values.get("own_transfer") == "in" else "account_from"
            values[side] = name or None
    for key in ("counterparty", "contract_ref", "details", "project", "comment", "note"):
        if key in patch:
            values[key] = str(patch[key] or "").strip()
            if key == "contract_ref":
                values.pop("contract_auto", None)
    if "category" in patch:
        name = str(patch["category"] or "").strip()
        if values.get("kind") == "transfer":
            values["transfer_category"] = name
        else:
            values["subcategory"] = name
            values["category"] = None
    if "pnl" in patch:
        changes = pnl_changes(patch["pnl"])
        for key in ("period_start", "period_end", "accrued_at"):
            values[key] = changes[key].isoformat() if changes[key] else None
        values["period_label"] = changes["period_label"]
    if patch.get("force"):
        values["force"] = True
        values.pop("maybe_duplicate", None)

    new_accounts = {norm(item["name"]) for item in (batch.decisions or {}).get("accounts_new") or []}
    known = {item.normalized_name for item in service.list_accounts(session, workspace.id)} | new_accounts
    problems: list[dict[str, str]] = []
    if not values.get("paid_at"):
        problems.append({"field": "paid_at", "text": "нет даты платежа"})
    if not values.get("amount"):
        problems.append({"field": "amount", "text": "нет суммы"})
    kind = values.get("kind")
    if kind not in ("income", "expense", "transfer"):
        problems.append({"field": "kind", "text": "не указано, приход это или расход"})
    for key, needed in (("account_from", ("expense",)), ("account_to", ("income",))):
        name = values.get(key)
        if name and norm(name) not in known:
            problems.append({"field": "account", "text": f"счёт «{name}» не найден"})
        elif not name and kind in needed:
            problems.append({"field": "account", "text": "счёт не указан"})
    if kind == "transfer" and not (values.get("account_from") or values.get("account_to")):
        problems.append({"field": "account", "text": "у перевода не указан ни один свой счёт"})
    # Отпечаток - по новым значениям. «Завести всё равно» - своя метка в
    # отпечатке: строка заведётся, повторная загрузка файла её узнает.
    values["external_key"] = _key_of(values) + (f"#force{line}" if values.get("force") else "")
    row.parsed = values
    row.problems = problems
    if row.state == "skipped" and not ({"income", "expense", "paid_at", "account"} & set(patch)):
        pass
    elif problems:
        row.state = "failed"
    elif row.state == "duplicate" and not values.get("force"):
        pass
    else:
        row.state = "imported"
    session.flush()
    return row


# ── Принять загрузку ─────────────────────────────────────────────────────────


def _create_accounts(session: Session, workspace: Workspace, wanted: Sequence[dict[str, Any]]) -> list[str]:
    created = []
    existing = {item.normalized_name for item in service.list_accounts(session, workspace.id, with_archived=True)}
    for item in wanted:
        name = str(item.get("name") or "").strip()
        if not name or norm(name) in existing:
            continue
        opening = importing.parse_money(item.get("opening")) if item.get("opening") else None
        balance = ZERO
        if opening is not None:
            balance = -opening.value if opening.negative else opening.value
        account = service.create_account(session, workspace, name=name, kind="bank", starting_balance=balance)
        number = str(item.get("number") or "").strip()
        if number:
            try:
                service.set_account_number(session, workspace, account.id, number)
            except FinanceError:
                pass
        if item.get("firm_id"):
            account.group_entity_id = uuid.UUID(str(item["firm_id"]))
        existing.add(norm(name))
        created.append(name)
    session.flush()
    return created


def _complete_half(
    values: dict[str, Any],
    account_from: Account | None,
    account_to: Account | None,
    halves: dict[tuple[str, str], list[Operation]],
) -> Operation | None:
    """Вторая сторона перевода, заведённого половинкой, - дописать, а не заводить второй перевод."""
    key = (values.get("paid_at") or "", _money(Decimal(str(values.get("amount") or 0))))
    for operation in halves.get(key, []):
        if operation.deleted_at is not None or operation.kind != "transfer":
            continue
        if operation.account_from_id and operation.account_to_id:
            continue
        if account_from and account_to:
            if operation.account_from_id == account_from.id:
                operation.account_to_id = account_to.id
            elif operation.account_to_id == account_to.id:
                operation.account_from_id = account_from.id
            else:
                continue
        elif account_from and operation.account_to_id and operation.account_to_id != account_from.id:
            operation.account_from_id = account_from.id
        elif account_to and operation.account_from_id and operation.account_from_id != account_to.id:
            operation.account_to_id = account_to.id
        else:
            continue
        operation.version += 1
        return operation
    return None


def accept(
    session: Session, workspace: Workspace, batch_id: uuid.UUID, *, actor: str = "", create_accounts: bool = True
) -> dict[str, Any]:
    """Принять загрузку: завести готовые строки одной пачкой.

    Не через заводку по строке (`service.apply_batch`): там на каждую строку
    уходят свои запросы к базе, и журнал на шесть тысяч строк заводился бы
    минутами. Здесь справочники читаются один раз (`Resolver`), операции
    пишутся одним сбросом, проверки - те же правила, что у одной операции.
    """
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.workspace_id != workspace.id:
        raise FinanceError("Загрузка не найдена")
    if batch.status == "cancelled":
        raise FinanceError("Загрузка отклонена - загрузите файл заново")
    decisions = batch.decisions or {}
    created_accounts = _create_accounts(session, workspace, decisions.get("accounts_new") or []) if create_accounts else []
    resolver = Resolver(session, workspace)
    rows = list(session.scalars(sa.select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.line)))
    known_keys = {
        key
        for (key,) in session.execute(
            sa.select(Operation.external_key).where(
                Operation.workspace_id == workspace.id,
                Operation.deleted_at.is_(None),
                Operation.external_key.is_not(None),
            )
        )
    }
    days = [row.parsed["paid_at"] for row in rows if (row.parsed or {}).get("paid_at")]
    halves: dict[tuple[str, str], list[Operation]] = defaultdict(list)
    if days:
        for operation in session.scalars(
            sa.select(Operation).where(
                Operation.workspace_id == workspace.id,
                Operation.deleted_at.is_(None),
                Operation.kind == "transfer",
                sa.or_(Operation.account_from_id.is_(None), Operation.account_to_id.is_(None)),
                Operation.paid_at >= date.fromisoformat(min(days)),
                Operation.paid_at <= date.fromisoformat(max(days)),
            )
        ):
            halves[(operation.paid_at.isoformat(), _money(operation.amount))].append(operation)
    groups: dict[str, uuid.UUID] = {}
    new_operations: list[Operation] = []
    pending_projects: list[tuple[Operation, uuid.UUID]] = []
    linked: list[tuple[ImportRow, uuid.UUID]] = []
    imported = completed = 0
    for row in rows:
        if row.operation_id is not None or row.state != "imported":
            continue
        values = dict(row.parsed or {})
        key = values.get("external_key")
        if key and key in known_keys:
            row.state = "duplicate"
            continue
        kind = values.get("kind")
        try:
            paid_at = date.fromisoformat(values["paid_at"])
            amount = check_money(Decimal(str(values["amount"])))
            check_date(paid_at, field="Дата")
        except (KeyError, TypeError, ValueError, FinanceError) as exc:
            row.state = "failed"
            row.problems = list(row.problems or []) + [{"field": "", "text": f"строка не разобрана до конца: {exc}"}]
            continue
        account_from = resolver.account(values.get("account_from"))
        account_to = resolver.account(values.get("account_to"))
        problem = ""
        if kind not in ("income", "expense", "transfer"):
            problem = "не указано, приход это или расход"
        elif kind == "income" and account_to is None:
            problem = "не указано, на какой счёт пришли деньги"
        elif kind == "expense" and account_from is None:
            problem = "не указано, с какого счёта ушли деньги"
        elif kind == "transfer" and account_from is None and account_to is None:
            problem = "у перевода не найден ни один свой счёт"
        elif kind == "transfer" and account_from is not None and account_from is account_to:
            problem = "перевод на тот же счёт ничего не меняет"
        if problem:
            row.state = "failed"
            row.problems = list(row.problems or []) + [{"field": "account", "text": problem}]
            continue
        if kind == "transfer":
            done = _complete_half(values, account_from, account_to, halves)
            if done is not None:
                # Ссылка - вместе с остальными, после записи пачки (см. ниже).
                linked.append((row, done.id))
                completed += 1
                continue
            category = (
                resolver.category("transfer", values["transfer_category"], "Перемещения")
                if values.get("transfer_category")
                else None
            )
            party = None
        else:
            leaf = values.get("subcategory") or values.get("category")
            group_name = values.get("category") if values.get("subcategory") else None
            category = resolver.category(kind, leaf, group_name)
            party = resolver.party(
                values.get("counterparty"),
                "client" if kind == "income" else "supplier",
                str(values.get("counterparty_bin") or ""),
            )
        group = groups.setdefault(values["split_key"], uuid.uuid4()) if values.get("split_key") else None

        def day(name: str) -> date | None:
            return date.fromisoformat(values[name]) if values.get(name) else None

        operation = Operation(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            kind=kind,
            status="fact",
            paid_at=paid_at,
            accrued_at=day("accrued_at"),
            period_start=day("period_start"),
            period_end=day("period_end"),
            amount=amount,
            currency=(values.get("currency") or workspace.base_currency or "KZT").upper(),
            amount_base=amount,
            rate=Decimal("1"),
            account_from_id=account_from.id if account_from else None,
            account_to_id=account_to.id if account_to else None,
            category_id=category.id if category else None,
            counterparty_id=party.id if party else None,
            comment=values.get("comment") or "",
            contract_ref=(values.get("contract_ref") or "")[:200],
            period_label=(values.get("period_label") or "")[:200],
            details=values.get("details") or "",
            note=values.get("note") or "",
            split_group=group,
            split_total=Decimal(str(values["split_total"])) if group and values.get("split_total") else None,
            split_state="none",
            source="import",
            external_key=key,
            import_batch_id=batch.id,
            raw=row.raw or {},
            created_by=actor,
            version=1,
        )
        new_operations.append(operation)
        project = resolver.project(values.get("project"))
        if project is not None:
            pending_projects.append((operation, project.id))
        # Ссылку строки загрузки на запись - только после записи самой записи:
        # новая статья или контрагент сбрасывают сессию посреди цикла, и строка
        # со ссылкой на ещё не записанную операцию роняла внешний ключ Postgres.
        linked.append((row, operation.id))
        imported += 1
        if key:
            known_keys.add(key)
        # Половинку, заведённую этой же загрузкой, вторая строка файла не
        # дописывает: строки одного файла - разные строки выписки, их пары уже
        # свёл `_pair_transfers`. Дописка «склеила» бы две строки в одну, и одна
        # из них пропала бы из журнала (журнал BBC, 22.07: два «Пэй - Юр счёт»
        # по 150 000 и перевод с депозита на те же 150 000).
    session.add_all(new_operations)
    session.flush()
    for row, operation_id in linked:
        row.operation_id = operation_id
    for operation, project_id in pending_projects:
        session.add(OperationProject(operation_id=operation.id, project_id=project_id, amount=operation.amount))
        operation.split_state = "exact"
    batch.rows_imported = sum(1 for row in rows if row.operation_id is not None)
    batch.rows_failed = sum(1 for row in rows if row.state == "failed")
    batch.rows_duplicate = sum(1 for row in rows if row.state == "duplicate")
    batch.status = "applied"
    batch.applied_at = datetime.now(timezone.utc)
    session.flush()
    return {
        "imported": imported,
        "completed": completed,
        "failed": batch.rows_failed,
        "duplicate": batch.rows_duplicate,
        "skipped": batch.rows_skipped,
        "total": batch.rows_total,
        "accounts_created": created_accounts,
        "operation_ids": [str(row.operation_id) for row in rows if row.operation_id is not None],
    }


def reject(session: Session, workspace: Workspace, batch_id: uuid.UUID) -> None:
    """Отклонить загрузку: в журнал ничего не легло, партия помечена отменённой."""
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.workspace_id != workspace.id:
        raise FinanceError("Загрузка не найдена")
    if batch.status == "applied":
        raise FinanceError("Загрузка уже принята - записи убираются в журнале")
    batch.status = "cancelled"
    session.flush()


# ── Правка записи ────────────────────────────────────────────────────────────

#: Поля записи, которые правятся из журнала и из табличного вида.
EDIT_FIELDS = (
    "paid_at", "amount", "kind", "account", "account_from", "account_to", "counterparty", "category",
    "contract_ref", "pnl", "details", "project", "comment", "note", "income", "expense",
)


def edit(
    session: Session,
    workspace: Workspace,
    operation_id: uuid.UUID,
    patch: dict[str, Any],
    *,
    version: int | None = None,
    side: str | None = None,
    actor: str = "",
) -> Operation:
    """Правка записи журнала по смыслу полей журнала.

    Сторону менять можно: «Приход» ↔ «Расход» правкой суммы в другой колонке
    (или `kind`), счёт остаётся при записи. Статья-перемещение у прихода или
    расхода делает его половинкой перевода, статья прихода у половинки - снова
    приходом: так табличный вид разрешает передумать, не удаляя строку.
    """
    operation = session.get(Operation, operation_id)
    if operation is None or operation.workspace_id != workspace.id or operation.deleted_at is not None:
        raise FinanceError("Запись не найдена")
    unknown = set(patch) - set(EDIT_FIELDS)
    if unknown:
        raise FinanceError(f"Неизвестные поля: {', '.join(sorted(unknown))}")
    resolver = Resolver(session, workspace)
    changes: dict[str, Any] = {}
    kind = operation.kind
    account_from, account_to = operation.account_from_id, operation.account_to_id
    if kind == "transfer" and side is None and not (account_from and account_to):
        side = "in" if account_to else "out"

    def own_account() -> uuid.UUID | None:
        if kind == "income":
            return account_to
        if kind == "expense":
            return account_from
        return account_to if side == "in" else account_from

    def become(new_kind: str) -> None:
        """Приход ↔ расход ↔ половинка перевода: счёт остаётся при записи."""
        nonlocal kind, account_from, account_to, side
        if new_kind == kind:
            return
        if kind == "transfer" and account_from and account_to:
            raise FinanceError(
                "Это перевод между двумя своими счетами - сначала уберите одну сторону или удалите перевод"
            )
        account = own_account()
        if new_kind == "transfer":
            side = "in" if kind == "income" else "out"
        kind = new_kind
        incoming = kind == "income" or (kind == "transfer" and side == "in")
        account_to = account if incoming else None
        account_from = None if incoming else account

    if "kind" in patch:
        wanted = str(patch["kind"] or "")
        if wanted not in ("income", "expense", "transfer"):
            raise FinanceError("Вид записи - приход, расход или перевод")
        become(wanted)
    if "income" in patch or "expense" in patch:
        income_text = str(patch.get("income") or "").strip()
        expense_text = str(patch.get("expense") or "").strip()
        if income_text and expense_text:
            raise FinanceError("Сумма стоит либо в «Приходе», либо в «Расходе»")
        text = income_text or expense_text
        if not text:
            raise FinanceError("Сумма не может быть пустой: запись без суммы удаляется, а не обнуляется")
        money = importing.parse_money(text)
        if money is None or not money.value:
            raise FinanceError(f"Не понял сумму «{text}»")
        if money.negative:
            raise FinanceError("Минус в сумме не нужен: направление задаёт колонка «Приход» или «Расход»")
        changes["amount"] = money.value
        direction = "income" if income_text else "expense"
        if kind == "transfer":
            wanted_side = "in" if direction == "income" else "out"
            if side and wanted_side != side:
                if account_from and account_to:
                    raise FinanceError(
                        "Строка - сторона перевода: «Приход» у счёта, куда пришли деньги, «Расход» - откуда ушли"
                    )
                # Половинка перевода сменила сторону: деньги на самом деле ушли, а не пришли.
                account = account_to or account_from
                side = wanted_side
                account_to = account if side == "in" else None
                account_from = None if side == "in" else account
        else:
            become(direction)
    if "amount" in patch:
        money = importing.parse_money(patch["amount"])
        if money is None or not money.value or money.negative:
            raise FinanceError(f"Не понял сумму «{patch['amount']}»")
        changes["amount"] = money.value
    if "paid_at" in patch:
        day = importing.parse_date(patch["paid_at"], CELL_READING) if str(patch["paid_at"] or "").strip() else None
        if day is None:
            raise FinanceError(f"Не понял дату «{patch['paid_at']}». Пример: 03.09.2026")
        changes["paid_at"] = day

    def account_by(text: Any) -> uuid.UUID | None:
        name = str(text or "").strip()
        if not name:
            return None
        account = resolver.account(name)
        if account is None:
            from app.finance.grid import _resolve

            account = _resolve(session, workspace, Account, name)
        if account is None:
            names = ", ".join(sorted(item.name for item in resolver.accounts.values()))
            raise FinanceError(f"Счёта «{name}» нет. Есть: {names}")
        return account.id

    if "account" in patch:
        chosen = account_by(patch["account"])
        if chosen is None:
            raise FinanceError("У записи журнала должен быть счёт")
        if kind == "income" or (kind == "transfer" and side == "in"):
            account_to = chosen
        else:
            account_from = chosen
    if "account_from" in patch:
        account_from = account_by(patch["account_from"])
    if "account_to" in patch:
        account_to = account_by(patch["account_to"])

    if "category" in patch:
        name = str(patch["category"] or "").strip()
        if not name:
            changes["category_id"] = None
        else:
            wanted_side = kind if kind in ("income", "expense") else "transfer"
            category = resolver.categories.get((wanted_side, norm(name)))
            if category is None:
                other = [
                    item
                    for (item_side, item_name), item in resolver.categories.items()
                    if item_name == norm(name) and item_side != wanted_side
                ]
                if len(other) == 1 and (other[0].side == "transfer" or kind == "transfer"):
                    # Статья-перемещение у прихода или расхода - половинка перевода;
                    # статья прихода или расхода у половинки - снова приход или расход.
                    become(other[0].side)
                    category = other[0]
                else:
                    # «Арен» в ячейке - это «Аренда», если кандидат один: выпадающий
                    # список листа набранное не дополняет (`grid._resolve`).
                    from app.finance.grid import _resolve

                    category = _resolve(session, workspace, Category, name, side=wanted_side) or resolver.category(
                        wanted_side, name
                    )
            changes["category_id"] = category.id if category else None
    if "counterparty" in patch:
        role = "client" if kind == "income" else "supplier"
        text = str(patch["counterparty"] or "").strip()
        party = None
        if text:
            from app.finance.grid import _resolve

            party = resolver.parties.get((role, norm(text))) or _resolve(session, workspace, Counterparty, text, role=role)
            party = party or resolver.party(text, role)
        changes["counterparty_id"] = party.id if party else None
    for key in ("contract_ref", "details", "comment", "note"):
        if key in patch:
            changes[key] = str(patch[key] or "").strip()
    if "pnl" in patch:
        changes.update(pnl_changes(patch["pnl"]))
    if "project" in patch:
        name = str(patch["project"] or "").strip()
        project = resolver.project(name) if name else None
        amount = changes.get("amount", operation.amount)
        changes["projects"] = [(project.id, Decimal(str(amount)))] if project else []

    if kind != operation.kind:
        if kind == "transfer":
            changes["counterparty_id"] = None
        elif "counterparty_id" not in changes and operation.counterparty_id:
            # Плательщик стал получателем: роль контрагента - другая запись справочника.
            was = session.get(Counterparty, operation.counterparty_id)
            if was is not None:
                party = resolver.party(was.name, "client" if kind == "income" else "supplier")
                changes["counterparty_id"] = party.id if party else None
        if "category_id" not in changes and operation.category_id:
            current = session.get(Category, operation.category_id)
            expected = kind if kind in ("income", "expense") else "transfer"
            if current is not None and current.side != expected:
                changes["category_id"] = None
    changes["kind"] = kind
    changes["account_from_id"] = account_from
    changes["account_to_id"] = account_to
    return service.update_operation(session, workspace, operation_id, changes, version=version, actor=actor)


# ── Дробление ────────────────────────────────────────────────────────────────


def split(
    session: Session,
    workspace: Workspace,
    operation_id: uuid.UUID,
    parts: Sequence[dict[str, Any]],
    *,
    version: int | None = None,
    actor: str = "",
) -> list[Operation]:
    """Разбить запись на части - «Дробление 1 суммы».

    Части обязаны дать сумму выписки до копейки: дробление, которое не
    сходится, - не дробление, а потерянные или придуманные деньги. Первая
    часть - сама запись (её отпечаток остаётся: повторная загрузка выписки
    узнает строку), остальные - новые записи с тем же счётом и днём.
    """
    operation = session.get(Operation, operation_id)
    if operation is None or operation.workspace_id != workspace.id or operation.deleted_at is not None:
        raise FinanceError("Запись не найдена")
    if version is not None and operation.version != version:
        raise service.VersionConflict("Запись уже изменили - обновите журнал и повторите")
    if operation.kind == "transfer":
        raise FinanceError("Перевод между своими счетами не дробится - дробится приход или расход")
    if len(parts) < 2:
        raise FinanceError("Частей должно быть хотя бы две")
    if operation.split_group:
        raise FinanceError("Запись уже часть дробления - правьте части или соберите их обратно")
    total = Decimal(str(operation.amount))
    amounts = []
    for part in parts:
        money = importing.parse_money(part.get("amount"))
        if money is None or money.negative or not money.value:
            raise FinanceError(f"Не понял сумму части «{part.get('amount')}»")
        amounts.append(check_money(money.value))
    if sum(amounts, ZERO) != total:
        raise FinanceError(
            f"Части дают {_spaced(str(sum(amounts, ZERO)))}, а в выписке {_spaced(str(total))} - должно сойтись до копейки"
        )
    group = uuid.uuid4()
    result = []
    for index, (part, amount) in enumerate(zip(parts, amounts)):
        if index == 0:
            target = operation
            target.version += 1
        else:
            target = Operation(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                kind=operation.kind,
                status=operation.status,
                paid_at=operation.paid_at,
                accrued_at=operation.accrued_at,
                period_start=operation.period_start,
                period_end=operation.period_end,
                period_label=operation.period_label,
                amount=amount,
                currency=operation.currency,
                amount_base=amount,
                rate=operation.rate,
                account_from_id=operation.account_from_id,
                account_to_id=operation.account_to_id,
                category_id=operation.category_id,
                counterparty_id=operation.counterparty_id,
                comment=operation.comment,
                contract_ref=operation.contract_ref,
                details=operation.details,
                source=operation.source,
                import_batch_id=operation.import_batch_id,
                raw=operation.raw or {},
                created_by=actor,
                version=1,
            )
            session.add(target)
        target.split_group = group
        target.split_total = total
        target.amount = amount
        target.amount_base = check_money(amount * Decimal(str(operation.rate or 1)))
        session.flush()
        patch = {key: part[key] for key in ("pnl", "contract_ref", "category", "details", "comment", "note") if key in part}
        if patch:
            edit(session, workspace, target.id, patch, actor=actor)
        result.append(target)
    session.flush()
    return result


def unsplit(session: Session, workspace: Workspace, group: uuid.UUID, *, actor: str = "") -> Operation:
    """Собрать части обратно в одну запись на сумму выписки."""
    parts = list(
        session.scalars(
            sa.select(Operation)
            .where(Operation.workspace_id == workspace.id, Operation.split_group == group, Operation.deleted_at.is_(None))
            .order_by(Operation.external_key.is_(None), Operation.created_at, Operation.id)
        )
    )
    if not parts:
        raise FinanceError("Частей не найдено")
    head = parts[0]
    total = (
        Decimal(str(head.split_total))
        if head.split_total is not None
        else sum((Decimal(str(item.amount)) for item in parts), ZERO)
    )
    now = datetime.now(timezone.utc)
    for item in parts[1:]:
        item.deleted_at = now
        item.version += 1
    head.amount = total
    head.amount_base = check_money(total * Decimal(str(head.rate or 1)))
    head.split_group = None
    head.split_total = None
    head.version += 1
    session.flush()
    return head


# ── Шаблон и выгрузка ────────────────────────────────────────────────────────

#: Статьи шаблона для компании, у которой своих ещё нет: общие слова, без
#: названий одной компании.
DEFAULT_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("Оплата от клиентов", "Доходы", "income"),
    ("Прочие доходы", "Доходы", "income"),
    ("% по депозиту", "", "income"),
    ("ФОТ", "", "expense"),
    ("Налоги", "", "expense"),
    ("Аренда офиса", "", "expense"),
    ("Комиссии банка", "", "expense"),
    ("Связь и интернет", "", "expense"),
    ("Маркетинг", "", "expense"),
    ("Возврат клиенту", "", "expense"),
    ("Прочие расходы", "", "expense"),
    ("Текущий счёт - Депозит", "Перемещения", "transfer"),
    ("Между банками", "Перемещения", "transfer"),
)

SIDE_TITLES = {"income": "Приход", "expense": "Расход", "transfer": "Перемещение"}

HOW_TO = (
    ("Дата", "день платежа по выписке: 01.06.2026"),
    ("Счёт", "счёт, по выписке которого строка: из листа «Справочник» или новый - заведётся при загрузке"),
    ("Приход / Расход", "сумма в одной из двух колонок, без минуса"),
    ("ОПиУ период", "месяц, к которому платёж относится в прибыли: «Июнь 2026», «2027 год», «Старые»; пусто - месяц платежа"),
    ("Контрагент", "кто заплатил или кому заплатили"),
    ("Дробление 1 суммы", "одна сумма выписки на несколько строк: у всех частей одна метка, например «Общий 400 000»"),
    ("№Дог.", "номер договора из реестра: оплата ляжет в этот договор"),
    ("Подкатегория", "статья: из списка или новая"),
    ("Категория", "группа статьи; «Перемещения» - перевод между своими счетами, обе стороны можно записать двумя строками"),
    ("ФОТ/Детали", "кому зарплата или за что именно"),
    ("Комментарии", "назначение платежа"),
    ("Доп. комменты / Вопросы", "что уточнить: строка будет видна в журнале среди требующих внимания"),
)


def _template_book(session: Session, workspace: Workspace, operations: Sequence[Operation] | None) -> bytes:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    book = Book.load(session, workspace.id)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Журнал"
    columns = [column for column in COLUMNS if column.template or operations is not None]
    head_fill = PatternFill("solid", fgColor="E8E6DF")
    for index, column in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=index, value=column.title)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(index)].width = max(10, column.width / 7)
    sheet.freeze_panes = "A2"
    sheet.row_dimensions[1].height = 30

    row_index = 2
    if operations:
        splits = _split_marks(session, operations)
        projects = _projects_of(session, [item.id for item in operations], book)
        for operation in operations:
            for line in lines_of(operation, book, splits=splits, projects=projects):
                for index, column in enumerate(columns, start=1):
                    value: Any = line["cells"].get(column.key, "")
                    if column.kind == "date" and value:
                        value = date.fromisoformat(value)
                    elif column.kind == "money" and value:
                        value = float(Decimal(value))
                    cell = sheet.cell(row=row_index, column=index, value=None if value == "" else value)
                    if column.kind == "date":
                        cell.number_format = "DD.MM.YYYY"
                    elif column.kind == "money":
                        cell.number_format = "#,##0.00"
                row_index += 1

    reference = workbook.create_sheet("Справочник")
    categories = [
        (item.name, book.group_name(item.id), item.side)
        for item in sorted(book.categories.values(), key=lambda value: (value.side, value.position, value.name))
        if item.archived_at is None and item.id not in book.parents
    ] or list(DEFAULT_CATEGORIES)
    heads = ["Подкатегория", "Категория", "Сторона", "", "Счёт", "Наше юрлицо", "Номер счёта (IBAN)", "Остаток на начало", "", "ОПиУ период"]
    for index, title in enumerate(heads, start=1):
        if title:
            cell = reference.cell(row=1, column=index, value=title)
            cell.font = Font(bold=True)
            cell.fill = head_fill
    for row, (name, group, side) in enumerate(categories, start=2):
        reference.cell(row=row, column=1, value=name)
        reference.cell(row=row, column=2, value=group or None)
        reference.cell(row=row, column=3, value=SIDE_TITLES.get(side, side))
    accounts = [
        item for item in sorted(book.accounts.values(), key=lambda value: (value.position, value.name)) if item.archived_at is None
    ]
    for row, account in enumerate(accounts, start=2):
        reference.cell(row=row, column=5, value=account.name)
        firm = book.entities.get(account.group_entity_id, "") if account.group_entity_id else ""
        reference.cell(row=row, column=6, value=firm or None)
        reference.cell(row=row, column=7, value=account.number or None)
        balance = reference.cell(row=row, column=8, value=float(account.starting_balance or 0))
        balance.number_format = "#,##0.00"
    months = [item["month"] for item in months_of(session, workspace.id)]
    periods = [item["title"] for item in period_options(months)] + (_labels(session, workspace.id) or ["Старые"])
    for row, title in enumerate(periods, start=2):
        reference.cell(row=row, column=10, value=title)
    for letter, width in (("A", 34), ("B", 18), ("C", 14), ("E", 44), ("F", 22), ("G", 26), ("H", 18), ("J", 26)):
        reference.column_dimensions[letter].width = width

    position = {column.key: index for index, column in enumerate(columns, start=1)}
    validations = [
        ("category", f"=Справочник!$A$2:$A${len(categories) + 1}"),
        ("pnl", f"=Справочник!$J$2:$J${len(periods) + 1}"),
    ]
    if accounts:
        validations.append(("account", f"=Справочник!$E$2:$E${len(accounts) + 1}"))
    last = max(row_index + 500, 2000)
    for key, formula in validations:
        # Список - подсказка, а не замок: новая статья или новый счёт - законная строка.
        rule = DataValidation(type="list", formula1=formula, allow_blank=True, showErrorMessage=False)
        letter = get_column_letter(position[key])
        rule.add(f"{letter}2:{letter}{last}")
        sheet.add_data_validation(rule)
    if operations is None:
        for key, number_format in (("paid_at", "DD.MM.YYYY"), ("income", "#,##0.00"), ("expense", "#,##0.00")):
            letter = get_column_letter(position[key])
            for row in range(2, 502):
                sheet[f"{letter}{row}"].number_format = number_format

    guide = workbook.create_sheet("Как заполнять")
    guide.column_dimensions["A"].width = 26
    guide.column_dimensions["B"].width = 110
    guide.cell(row=1, column=1, value="Колонка").font = Font(bold=True)
    guide.cell(row=1, column=2, value="Что писать").font = Font(bold=True)
    for row, (title, text) in enumerate(HOW_TO, start=2):
        guide.cell(row=row, column=1, value=title)
        guide.cell(row=row, column=2, value=text).alignment = Alignment(wrap_text=True)

    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def template_xlsx(session: Session, workspace: Workspace) -> bytes:
    """Пустой шаблон журнала со справочником компании - заполнить и загрузить."""
    return _template_book(session, workspace, None)


def export_xlsx(session: Session, workspace: Workspace, flt: JournalFilter, *, limit: int = 50000) -> bytes:
    """Журнал по отбору - те же колонки, что у шаблона, плюс производные.

    Выгрузка - тот же шаблон: её можно поправить и загрузить обратно, и
    уже заведённые строки узнаются как повторы.
    """
    book = Book.load(session, workspace.id)
    operations = list(
        session.scalars(
            _filtered(sa.select(Operation), workspace.id, flt, book)
            .order_by(Operation.paid_at, Operation.created_at, Operation.id)
            .limit(limit)
        )
    )
    return _template_book(session, workspace, operations)


# ── Табличный вид ────────────────────────────────────────────────────────────


def grid_columns() -> list[dict[str, Any]]:
    return [
        {
            "key": column.key,
            "title": column.title,
            "kind": column.kind,
            "width": column.width,
            "editable": column.editable,
            "source": column.source,
        }
        for column in COLUMNS
    ]


def grid_options(session: Session, workspace: Workspace, book: Book | None = None) -> dict[str, list[str]]:
    book = book or Book.load(session, workspace.id)
    months = [item["month"] for item in months_of(session, workspace.id)]
    return {
        "accounts": sorted(item.name for item in book.accounts.values() if item.archived_at is None),
        "categories": sorted({item.name for item in book.categories.values() if item.archived_at is None}),
        "counterparties": sorted(set(book.counterparties.values()))[:3000],
        "projects": sorted(book.projects.values()),
        "periods": [item["title"] for item in period_options(months)] + _labels(session, workspace.id),
        "contracts": sorted({card["number"] for card in Contracts(session, workspace, with_paid=False).options()}),
    }


def grid_lines(
    session: Session, workspace: Workspace, operations: Sequence[Operation], book: Book | None = None
) -> list[dict[str, Any]]:
    book = book or Book.load(session, workspace.id)
    splits = _split_marks(session, operations)
    projects = _projects_of(session, [item.id for item in operations], book)
    rows = []
    for operation in operations:
        if operation.deleted_at is None:
            rows.extend(lines_of(operation, book, splits=splits, projects=projects))
    return rows


def grid_payload(session: Session, workspace: Workspace, flt: JournalFilter, *, limit: int = 10000) -> dict[str, Any]:
    """Журнал как лист: строки выписок по порядку дат, как в журнале BBC."""
    book = Book.load(session, workspace.id)
    operations = list(
        session.scalars(
            _filtered(sa.select(Operation), workspace.id, flt, book)
            .order_by(Operation.paid_at, Operation.created_at, Operation.id)
            .limit(limit)
        )
    )
    return {
        "columns": grid_columns(),
        "rows": grid_lines(session, workspace, operations, book),
        "options": grid_options(session, workspace, book),
    }


def stale_line_ids(operation: Operation) -> list[str]:
    """Строки листа, которых у операции больше нет: перевод стал приходом, половинка - целым."""
    base = str(operation.id)
    if operation.kind != "transfer":
        have = {base}
    else:
        have = set()
        if operation.account_from_id:
            have.add(f"{base}:out")
        if operation.account_to_id:
            have.add(f"{base}:in")
        if not have:
            have.add(base)
    return [item for item in (base, f"{base}:out", f"{base}:in") if item not in have]


def append_line(session: Session, workspace: Workspace, cells: dict[str, Any], *, actor: str = "") -> Operation:
    """Новая запись из строки, напечатанной внизу листа: дата, счёт и сумма в «Приходе» или «Расходе»."""
    day = importing.parse_date(cells.get("paid_at"), CELL_READING)
    if day is None:
        raise FinanceError("Без даты строка не станет записью журнала")
    income_text = str(cells.get("income") or "").strip()
    expense_text = str(cells.get("expense") or "").strip()
    if income_text and expense_text:
        raise FinanceError("Сумма стоит либо в «Приходе», либо в «Расходе»")
    money = importing.parse_money(income_text or expense_text) if (income_text or expense_text) else None
    if money is None or not money.value:
        raise FinanceError("Без суммы в «Приходе» или «Расходе» строка не станет записью")
    if money.negative:
        raise FinanceError("Минус в сумме не нужен: направление задаёт колонка «Приход» или «Расход»")
    resolver = Resolver(session, workspace)
    account = resolver.account(str(cells.get("account") or ""))
    if account is None:
        from app.finance.grid import _resolve

        account = _resolve(session, workspace, Account, str(cells.get("account") or ""))
    if account is None:
        raise FinanceError("Укажите счёт - из списка счетов компании")
    kind = "income" if income_text else "expense"
    data = service.OperationInput(
        kind=kind,
        status="fact",
        paid_at=day,
        amount=money.value,
        account_to_id=account.id if kind == "income" else None,
        account_from_id=account.id if kind == "expense" else None,
        source="grid",
    )
    operation = service.create_operation(session, workspace, data, actor=actor)
    rest = {
        key: cells[key]
        for key in ("counterparty", "category", "contract_ref", "pnl", "details", "project", "comment", "note")
        if str(cells.get(key) or "").strip()
    }
    if rest:
        operation = edit(session, workspace, operation.id, rest, actor=actor)
    return operation


def create_record(session: Session, workspace: Workspace, fields: dict[str, Any], *, actor: str = "") -> Operation:
    """Новая запись журнала руками - из карточки: вид, дата, сумма, счёт и всё остальное полями журнала."""
    kind = str(fields.get("kind") or "")
    if kind not in ("income", "expense", "transfer"):
        raise FinanceError("Выберите, приход это, расход или перевод")
    day = importing.parse_date(fields.get("paid_at"), CELL_READING)
    if day is None:
        raise FinanceError("Без даты запись не завести")
    money = importing.parse_money(fields.get("amount"))
    if money is None or not money.value or money.negative:
        raise FinanceError("Сумма - положительное число: направление задаёт вид записи")
    resolver = Resolver(session, workspace)

    def account_of(key: str) -> Account | None:
        name = str(fields.get(key) or "").strip()
        if not name:
            return None
        account = resolver.account(name)
        if account is None:
            raise FinanceError(f"Счёта «{name}» нет - он заводится в «Справочниках» или загрузкой")
        return account

    if kind == "transfer":
        account_from, account_to = account_of("account_from"), account_of("account_to")
    else:
        account = account_of("account")
        if account is None:
            raise FinanceError("Укажите счёт")
        account_from, account_to = (None, account) if kind == "income" else (account, None)
    data = service.OperationInput(
        kind=kind,
        status="fact",
        paid_at=day,
        amount=money.value,
        account_from_id=account_from.id if account_from else None,
        account_to_id=account_to.id if account_to else None,
        source="app",
    )
    operation = service.create_operation(session, workspace, data, actor=actor)
    rest = {
        key: fields[key]
        for key in ("counterparty", "category", "contract_ref", "pnl", "details", "project", "comment", "note")
        if str(fields.get(key) or "").strip()
    }
    if rest:
        operation = edit(session, workspace, operation.id, rest, actor=actor)
    return operation


__all__ = [
    "COLUMNS",
    "Contracts",
    "create_record",
    "JournalFilter",
    "accept",
    "analyze_file",
    "append_line",
    "dictionaries",
    "edit",
    "export_xlsx",
    "fix_row",
    "grid_lines",
    "grid_payload",
    "lines_of",
    "list_records",
    "pnl_changes",
    "pnl_of",
    "preview_payload",
    "ref_key",
    "reject",
    "split",
    "stale_line_ids",
    "template_xlsx",
    "unsplit",
]
