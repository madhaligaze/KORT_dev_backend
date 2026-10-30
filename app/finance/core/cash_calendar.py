"""Платёжный календарь ядра: сколько денег будет у каждого нашего юрлица на каждый день.

Всё здесь выводится при чтении, хранится только слово человека
(`core/cash_plans.py`: плановый платёж, обещание, настройки):

* **остаток** - счета наших юрлиц, которые компания считает деньгами календаря
  (`habits.accounts_of`): начальный остаток плюс факт журнала до дня. Перевод
  между своими юрлицами - расход у одного и приход у другого: в итоге группы
  ноль, у юрлица - настоящие деньги;
* **дни до сегодня - факт**, сегодня и дальше - ожидания. Остаток на утро
  сегодня - факт; сегодняшний столбец - факт за день и то, что ещё ждём;
* **поступления по договорам** - неоплаченный остаток каждого месяца договора.
  Откуда остаток - звено «Переход» оплат юрлица (`core/switch.py`): пока
  правда - сводная таблица компании, строки таблицы («Сумма договора» минус
  «Поступило»), после перехода - расчёт KORT (`registers`: начисления и
  разнесённые оплаты). Компания без таблицы - сразу KORT. Будущие месяцы -
  начисления KORT до конца окна, а не «следующий месяц» руками. Покупки -
  тем же правилом расходом заказчику, договоры между своими - обоим;
* **срок** (`due`) - условие договора (`calendar.terms`: число месяца или
  дней после начала периода), иначе срок компании, иначе начало периода
  (срок счёта); у строки таблицы - её день счёта (колонка ядра `bill_day`). Перенос на
  рабочий день - пакет страны (выходные и праздники) с поправками компании;
* **долг** - срок прошёл, денег нет. В остаток идёт только по обещанию
  клиента (`promise`): дата и сумма; без обещания или с нарушенным - «без
  даты». Долг старше `old_after` дней - отдельной строкой;
* **плановые платежи** сводятся с фактом при чтении (то же юрлицо и
  направление, сумма до тенге или тот же получатель ±5%, факт за 5 дней до
  плана - 10 после, операция - один раз). Прошедший неоплаченный расход
  переносится на сегодня: обязательство не исчезает от того, что день прошёл;
* **сценарии**: `terms` - по условиям (как таблица); `habit` - по привычке
  клиентов (`core/habits.py`: медиана задержки) и с непринятыми повторами
  расходов журнала; `cash` - только расходы, поступлений не ждём;
* **разрыв** - первый день, когда остаток юрлица ниже его неснижаемого
  (`calendar.reserve`, по умолчанию ноль), с подсказками словами: с какого
  юрлица перевести, какие долги собрать, какие платежи сдвинуть. Решает
  человек.

Строки календаря - данными: поступления - «вид услуги» договора × по графику /
долг / старый долг; плановые - строка словами компании или статья. Какие строки
считать в остатке - параметр вопроса (`exclude`, как флажки таблицы).
"""
from __future__ import annotations

import calendar as month_calendar
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import packs
from app.finance.contracts.fields import party_key
from app.finance.contracts.models import Contract
from app.finance.contracts.service import Access, Registry
from app.finance.core import cash_plans, habits as habits_module
from app.finance.core.accruals import month_key, month_start
from app.finance.core.models import CashPlan
from app.finance.core.payments import OPENING
from app.finance.models import Category, Counterparty, Operation, Workspace

_ZERO = Decimal("0")
SCENARIOS = ("terms", "habit", "cash")
SCENARIO_TITLES = {"terms": "по условиям", "habit": "по привычке клиентов", "cash": "только расходы"}
#: Окно по умолчанию - пять недель, как у таблицы коллеги.
DAYS = 35
MAX_DAYS = 400
NOBODY = "none"


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _clamp(year: int, month: int, day: int) -> date:
    return date(year, month, min(max(day, 1), month_calendar.monthrange(year, month)[1]))


@dataclass
class Item:
    """Ожидание календаря: приход или расход нашего юрлица на день (пусто - без даты)."""

    direction: str
    entity: uuid.UUID | None
    day: date | None
    amount: Decimal
    line: str
    source: str
    #: `expected` - ждём, `overdue` - срок прошёл, денег нет, `promised` - по обещанию,
    #: `broken` - обещание нарушено, `carried` - прошедший расход перенесён на сегодня,
    #: `habit` - дата по привычке клиента, `proposed` - повтор журнала, не принят.
    state: str = "expected"
    #: Срок по условиям, до переноса и привычки.
    due: date | None = None
    title: str = ""
    client: str = ""
    kind: str = ""
    contract_id: uuid.UUID | None = None
    month: str = ""
    plan_id: uuid.UUID | None = None
    note: str = ""
    age: int | None = None

    def out(self, names: dict[uuid.UUID, str]) -> dict[str, Any]:
        return {
            "direction": self.direction, "entity": str(self.entity) if self.entity else None,
            "entity_title": names.get(self.entity, "") if self.entity else "",
            "day": self.day.isoformat() if self.day else None, "due": self.due.isoformat() if self.due else None,
            "amount": _money(self.amount), "line": self.line, "source": self.source, "state": self.state,
            "title": self.title, "client": self.client, "kind": self.kind,
            "contract_id": str(self.contract_id) if self.contract_id else None, "month": self.month,
            "plan_id": str(self.plan_id) if self.plan_id else None, "note": self.note, "age": self.age,
        }


@dataclass
class Line:
    key: str
    title: str
    direction: str
    order: int
    included: bool = True


@dataclass
class Board:
    """Числа одного юрлица (или всей группы) по дням."""

    days: list[date]
    opening: list[Decimal] = field(default_factory=list)
    closing: list[Decimal] = field(default_factory=list)
    fact_in: list[Decimal] = field(default_factory=list)
    fact_out: list[Decimal] = field(default_factory=list)
    lines: dict[str, list[Decimal]] = field(default_factory=dict)
    later: dict[str, Decimal] = field(default_factory=dict)
    no_date: dict[str, Decimal] = field(default_factory=dict)

    def put(self, line: str, index: int, amount: Decimal) -> None:
        self.lines.setdefault(line, [_ZERO] * len(self.days))[index] += amount


class Calendar:
    """Платёжный календарь компании с `start` по `end` на день `today` - один раз на запрос."""

    def __init__(
        self, session: Session, workspace: Workspace, access: Access, *, start: date, end: date, today: date,
        scenario: str = "terms", exclude: Sequence[str] = (), book: Any | None = None,
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("сценарий - по условиям, по привычке клиентов или только расходы")
        if end < start:
            raise ValueError("конец окна раньше начала")
        if (end - start).days > MAX_DAYS:
            raise ValueError(f"окно календаря - не больше {MAX_DAYS} дней")
        self.session, self.workspace, self.access = session, workspace, access
        # Окно в будущем считается с сегодняшнего утра: остаток на его первый день -
        # факт до сегодня и ожидания между сегодня и началом окна. Показываются дни окна.
        self.shown = start
        self.start, self.end, self.today = min(start, today), end, today
        self.scenario = scenario
        self.exclude = tuple(item for item in exclude if item)
        self.book = book
        self.settings = cash_plans.settings_of(session, workspace)
        self.country = packs.country_of(workspace)
        years = range(min(start, today).year - 1, end.year + 2)
        self.off = self.country.days_off(years, self.settings.holidays_off, self.settings.holidays_working)
        self.registry = Registry(session, workspace)
        self.entities = cash_plans._own_entities(session, workspace.id)
        self.names = dict(
            session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace.id)).all()
        )
        self.titles = {**self.names, **self.entities}
        self.accounts = habits_module.accounts_of(session, workspace.id, self.settings.skip_accounts)
        self.items: list[Item] = []
        self.done: list[dict[str, Any]] = []
        self.early: list[dict[str, Any]] = []
        self.lines: dict[str, Line] = {}
        self.sources: dict[str, str] = {}
        self.habits = habits_module.Habits()
        self.notes: list[str] = []
        self.proposals: list[dict[str, Any]] = []

    # ── Срок ─────────────────────────────────────────────────────────────────

    def workday(self, day: date) -> date:
        return self.country.workday(day, self.off)

    def due(self, contract_id: uuid.UUID | None, base: date | None) -> date | None:
        """Срок оплаты месяца по условиям, до переноса на рабочий день."""
        if base is None:
            return None
        terms = self.settings.contract_terms.get(contract_id) if contract_id else None
        if terms and "day" in terms:
            return _clamp(base.year, base.month, terms["day"])
        if terms and "days" in terms:
            return base + timedelta(days=terms["days"])
        return base + timedelta(days=self.settings.terms_days)

    # ── Строки ───────────────────────────────────────────────────────────────

    def _line(self, key: str, title: str, direction: str, order: int) -> str:
        if key not in self.lines:
            self.lines[key] = Line(key, title, direction, order, not any(key.startswith(prefix) for prefix in self.exclude))
        return key

    def _contract_line(self, direction: str, kind: str, state: str, intra: bool) -> str:
        kind = kind or "договоры"
        if intra:
            return self._line(f"{direction}:intra", "между своими юрлицами", direction, 40)
        words = {"schedule": "по графику", "debt": f"долг до {self.settings.old_after} дней",
                 "old": f"долг больше {self.settings.old_after} дней"}[state]
        order = {"schedule": 10, "debt": 20, "old": 30}[state]
        return self._line(f"{direction}:{state}:{kind}", f"{kind} - {words}", direction, order)

    def _plan_line(self, direction: str, label: str, source: str = "plan") -> str:
        label = label.strip() or ("поступления" if direction == "in" else "прочие расходы")
        if source == "habit":
            return self._line(f"{direction}:habit:{label}", f"{label} - повтор журнала", direction, 70)
        return self._line(f"{direction}:plan:{label}", label, direction, 50 if direction == "in" else 60)

    # ── Поступления и платежи по договорам ───────────────────────────────────

    def _contracts(self) -> None:
        from app.finance.core import book as book_module
        from app.finance.core import registers, switch
        from app.finance.core.debts import side_of
        from app.finance.core.shadow import _visible

        visible = _visible(self.session, self.workspace, self.access)
        by_id = {item.id: item for item in visible}
        on_kort = switch.kort_entities(self.session, self.workspace.id, "payments")
        table_entities: set[uuid.UUID] = set()
        table_last = ""
        table_contracts: set[uuid.UUID] = set()
        if self.book is not None:
            matcher = book_module.Matcher(self.session, self.workspace.id)
            finder = cash_plans.Entities(self.session, self.workspace.id)
            for row, match in book_module.rows_of(self.book, matcher):
                entity = matcher.entity_by_code.get(cash_plans.norm(row.own)) or finder.find(row.own)
                if entity is None:
                    continue
                table_entities.add(entity)
                if entity in on_kort:
                    continue
                if row.month and row.month > table_last:
                    table_last = row.month
                contract = by_id.get(match.contract_id) if match.state == "found" else None
                if match.state == "found" and contract is None:
                    continue  # договор есть, но спрашивающему не открыт
                if contract is not None:
                    table_contracts.add(contract.id)
                self._table_row(row, entity, contract)
        for entity in self.entities:
            self.sources[str(entity)] = "table" if entity in table_entities and entity not in on_kort else "kort"
        found = registers.totals(self.session, self.workspace, visible, day=self.today)
        ledger = None
        current = month_key(self.today)
        last_month = month_key(self.end)
        for contract in visible:
            side = side_of(contract, self.registry.is_own)
            if side is None:
                continue
            item = found.settlements.get(contract.id)
            if item is None or not item.accrues:
                continue
            kind = self._kind(contract)
            client_id = contract.customer_id if side != "payable" else contract.executor_id
            client = self.names.get(client_id, "") if client_id else ""
            for key, line in item.lines.items():
                entity = line.executor or contract.executor_id
                if self._from_table(entity, table_entities, on_kort):
                    continue
                left = line.left
                if left <= 0:
                    continue
                if key == OPENING:
                    self._contract_items(contract, side, kind, client, None, "", left, line.executor, note="долг до начала учёта")
                    continue
                self._contract_items(contract, side, kind, client, line.starts, key, left, line.executor)
            # Будущие месяцы до конца окна - начисления KORT (у юрлица на таблице - после её последнего месяца).
            first_future = max(current, table_last if contract.id in table_contracts else "")
            if last_month <= first_future:
                continue
            if ledger is None:
                ledger = found.ledger
            future = month_start(first_future)
            next_month = date(future.year + (future.month // 12), future.month % 12 + 1, 1)
            for accrual in ledger.accruals(contract, since=next_month, until=month_start(last_month)):
                if accrual.month <= first_future or accrual.skipped or not accrual.amount:
                    continue
                paid = item.lines[accrual.month].paid if accrual.month in item.lines else _ZERO
                entity = accrual.executor_id or contract.executor_id
                if contract.id not in table_contracts and self._from_table(entity, table_entities, on_kort):
                    continue
                left = accrual.amount - paid
                if left > 0:
                    self._contract_items(contract, side, kind, client, accrual.period_from, accrual.month, left, accrual.executor_id)

    @staticmethod
    def _from_table(entity: uuid.UUID | None, table_entities: set[uuid.UUID], on_kort: set[uuid.UUID]) -> bool:
        return entity is not None and entity in table_entities and entity not in on_kort

    def _kind(self, contract: Contract) -> str:
        value = self.registry.values.get(contract.type_id) if contract.type_id else None
        return value.value if value is not None else ""

    def _contract_items(
        self, contract: Contract, side: str, kind: str, client: str, base: date | None, month: str, amount: Decimal,
        executor: uuid.UUID | None, note: str = "",
    ) -> None:
        due = self.due(contract.id, base)
        common = dict(due=due, client=client, kind=kind, contract_id=contract.id, month=month, note=note,
                      title=(contract.number or "").strip())
        if side in ("receivable", "intra"):
            self.items.append(Item("in", executor or contract.executor_id, due, amount, "", "contract", **common))
        if side in ("payable", "intra"):
            self.items.append(Item("out", contract.customer_id, due, amount, "", "contract", **common))
        if side == "intra":
            self.items[-1].state = self.items[-2].state = "intra"

    def _table_row(self, row: Any, entity: uuid.UUID, contract: Contract | None) -> None:
        amount = row.amount
        if amount is None or amount <= 0:
            return
        paid = row.paid if row.paid is not None else sum((part.amount for part in row.payments), _ZERO)
        base = self._table_base(row)
        client_key = party_key(row.customer)
        due = self.due(contract.id if contract else None, base)
        if due is not None and paid >= amount:
            self.habits.add(client_key, self.workday(due), habits_module.month_paid_on(
                ((part.paid_on, part.amount) for part in row.payments if part.paid_on), amount))
        if due is not None and row.payments:
            first = min((part.paid_on for part in row.payments if part.paid_on), default=None)
            if first is not None and first < self.workday(due) and first >= self.start:
                self.early.append({"entity": str(entity), "client": row.customer, "number": row.number, "month": row.month,
                                   "due": self.workday(due).isoformat(), "paid_on": first.isoformat(), "amount": _money(paid)})
        left = amount - paid
        if left <= 0:
            return
        intra = contract is not None and contract.customer_id is not None and self.registry.is_own(contract.customer_id)
        common = dict(due=due, client=row.customer, kind=row.kind or (self._kind(contract) if contract else ""),
                      contract_id=contract.id if contract else None, month=row.month, title=row.number,
                      note="" if row.month else "долг до начала учёта")
        self.items.append(Item("in", entity, due, left, "", "table", **common))
        if intra:
            self.items[-1].state = "intra"
            self.items.append(Item("out", contract.customer_id, due, left, "", "table", **{**common, "kind": common["kind"]}))
            self.items[-1].state = "intra"

    @staticmethod
    def _table_base(row: Any) -> date | None:
        """Срок строки таблицы: день счёта (`bill_day`) - дата или число месяца строки; иначе день начала периода."""
        from app.finance.core import book as book_module

        if not row.month:
            return None
        year, month = int(row.month[:4]), int(row.month[5:7])
        text = (row.bill_day or "").strip()
        as_date = book_module.date_of(text) if text else None
        if as_date is not None:
            return as_date
        digits = text.replace(".", "").strip()
        if digits.isdigit() and 1 <= int(digits) <= 31:
            return _clamp(year, month, int(digits))
        if row.period_from is not None:
            return _clamp(year, month, row.period_from.day)
        return date(year, month, 1)

    # ── Привычки клиентов из KORT ────────────────────────────────────────────

    def _kort_habits(self) -> None:
        """Задержки оплат месяцев из живого расчёта KORT (`Money`): нужны сценарию «по привычке»."""
        from app.finance.core.debts import side_of
        from app.finance.core.payments import Money
        from app.finance.core.shadow import _visible

        visible = [item for item in _visible(self.session, self.workspace, self.access)
                   if side_of(item, self.registry.is_own) == "receivable"]
        if not visible:
            return
        money = Money(self.session, self.workspace, today=self.today)
        for contract in visible:
            client = party_key(self.names.get(contract.customer_id, "")) if contract.customer_id else ""
            for key, bucket in money.contract(contract).buckets.items():
                if key == OPENING or bucket.accrued <= 0 or bucket.left > 0:
                    continue
                due = self.due(contract.id, bucket.starts)
                paid_on = habits_module.month_paid_on(((part.payment.paid_at, part.amount) for part in bucket.parts), bucket.accrued)
                if due is not None:
                    self.habits.add(client, self.workday(due), paid_on)

    # ── Даты, долги, обещания ────────────────────────────────────────────────

    def _place(self) -> list[CashPlan]:
        """Срок → день ожидания; долг → по обещанию или без даты. Возвращает обещания."""
        plans = cash_plans.active(self.session, self.workspace.id)
        promises: dict[uuid.UUID, list[CashPlan]] = defaultdict(list)
        for plan in plans:
            if plan.kind == "promise" and plan.contract_id is not None:
                promises[plan.contract_id].append(plan)
        overdue: dict[uuid.UUID | None, list[Item]] = defaultdict(list)
        company = self.habits.company()
        for item in self.items:
            if item.source not in ("contract", "table"):
                continue
            intra = item.state == "intra"
            day = self.workday(item.due) if item.due is not None else None
            if day is not None and day >= self.today:
                item.day, item.state = day, "expected"
                if self.scenario == "habit" and item.direction == "in" and not intra:
                    habit = self.habits.of(party_key(item.client))
                    delay = habit.delay if habit.known else company.delay
                    if delay > 0:
                        item.day, item.state = self.workday(day + timedelta(days=delay)), "habit"
                item.line = self._contract_line(item.direction, item.kind, "schedule", intra)
                continue
            item.age = (self.today - day).days if day is not None else None
            old = item.age is None or item.age > self.settings.old_after
            item.line = self._contract_line(item.direction, item.kind, "old" if old else "debt", intra)
            item.day, item.state = None, "overdue"
            if item.direction == "out":
                # Наш долг поставщику - обязательство сегодня, а не «когда-нибудь».
                item.day, item.state = self.today, "carried"
                continue
            overdue[item.contract_id].append(item)
        for contract_id, items in overdue.items():
            given = sorted(promises.get(contract_id, []), key=lambda plan: plan.day or date.max) if contract_id else []
            items.sort(key=lambda item: (item.due or date.min))
            for plan in given:
                want = plan.amount if plan.amount is not None else sum((item.amount for item in items if item.state == "overdue"), _ZERO)
                for index, item in enumerate(list(items)):
                    if want <= 0:
                        break
                    if item.state != "overdue":
                        continue
                    if item.amount > want:
                        rest = Item(**{**item.__dict__, "amount": item.amount - want})
                        items.insert(index + 1, rest)
                        self.items.append(rest)
                        item.amount = want
                    want -= item.amount
                    item.plan_id, item.note = plan.id, plan.note or item.note
                    if plan.day is not None and plan.day >= self.today:
                        item.day, item.state = plan.day, "promised"
                    else:
                        item.state = "broken"
                if want > 0 and plan.amount is not None:
                    self.notes.append(f"обещано больше долга на {_money(want)}: {self._label(contract_id)}")
            if self.scenario == "habit":
                for item in items:
                    if item.state != "overdue" or item.due is None:
                        continue
                    habit = self.habits.of(party_key(item.client))
                    if habit.known and habit.delay > 0:
                        guess = self.workday(item.due + timedelta(days=habit.delay))
                        if guess >= self.today:
                            item.day, item.state = guess, "habit"
        return plans

    def _label(self, contract_id: uuid.UUID | None) -> str:
        if contract_id is None:
            return "без договора"
        contract = self.session.get(Contract, contract_id)
        return (contract.number or "договор без номера") if contract else "договор"

    # ── Плановые платежи и факт ──────────────────────────────────────────────

    def _facts(self) -> list[Any]:
        rows = self.session.execute(
            sa.select(Operation.id, Operation.kind, Operation.paid_at, Operation.amount_base, Operation.account_from_id,
                      Operation.account_to_id, Operation.counterparty_id, Operation.category_id)
            .where(Operation.workspace_id == self.workspace.id, Operation.deleted_at.is_(None), Operation.status == "fact",
                   Operation.paid_at <= max(self.today, self.end))
        ).all()
        return rows

    def _plans(self, plans: list[CashPlan], facts: list[Any], all_accounts: dict[uuid.UUID, uuid.UUID | None]) -> None:
        from app.finance.core.debts import side_of

        categories = dict(self.session.execute(sa.select(Category.id, Category.name).where(Category.workspace_id == self.workspace.id)).all())
        payable_by_plan: set[uuid.UUID] = set()
        pool: dict[tuple[uuid.UUID | None, str], list[Any]] = defaultdict(list)
        lo = self.today - timedelta(days=400)
        for row in facts:
            if row.paid_at < lo or row.paid_at > self.today:
                continue
            if row.kind == "income" and row.account_to_id in all_accounts:
                pool[(all_accounts[row.account_to_id], "in")].append(row)
            elif row.kind == "expense" and row.account_from_id in all_accounts:
                pool[(all_accounts[row.account_from_id], "out")].append(row)
        used: set[uuid.UUID] = set()
        pinned = {plan.operation_id for plan in plans if plan.operation_id}
        payments = [plan for plan in plans if plan.kind == "payment"]
        payments.sort(key=lambda plan: plan.day or date.max)
        for plan in payments:
            if plan.contract_id is not None:
                contract = self.session.get(Contract, plan.contract_id)
                if contract is not None and side_of(contract, self.registry.is_own) == "payable":
                    payable_by_plan.add(contract.id)
            label = plan.line or categories.get(plan.category_id, "") or plan.title or plan.party
            line = self._plan_line(plan.direction, label)
            base = dict(direction=plan.direction, entity=plan.entity_id, amount=plan.amount or _ZERO, line=line, source="plan",
                        due=plan.day, title=plan.title or plan.party, client=plan.party or self.names.get(plan.counterparty_id, ""),
                        plan_id=plan.id, note=plan.note, contract_id=plan.contract_id)
            if plan.operation_id is not None:
                self.done.append({"plan_id": str(plan.id), "operation_id": str(plan.operation_id), "how": "человек"})
                continue
            match = self._match(plan, pool.get((plan.entity_id, plan.direction), []), used | pinned) if plan.day else None
            if match is not None:
                used.add(match.id)
                self.done.append({"plan_id": str(plan.id), "operation_id": str(match.id), "paid_on": match.paid_at.isoformat(),
                                  "amount": _money(Decimal(str(match.amount_base))), "how": "сведено"})
                if plan.day and match.paid_at < plan.day:
                    self.early.append({"plan_id": str(plan.id), "entity": str(plan.entity_id) if plan.entity_id else None,
                                       "title": label, "due": plan.day.isoformat(), "paid_on": match.paid_at.isoformat(),
                                       "amount": _money(plan.amount)})
                continue
            if plan.day is None:
                self.items.append(Item(day=None, state="overdue", **base))
            elif plan.day >= self.today:
                self.items.append(Item(day=plan.day, state="expected", **base))
            elif plan.direction == "out":
                self.items.append(Item(day=self.today, state="carried", age=(self.today - plan.day).days, **base))
            else:
                self.items.append(Item(day=None, state="overdue", age=(self.today - plan.day).days, **base))
        if payable_by_plan:
            # Покупку ведут плановые платежи человека - её месяцы не двоить.
            self.items = [item for item in self.items
                          if not (item.source in ("contract", "table") and item.direction == "out" and item.contract_id in payable_by_plan)]
        # Ожидания журнала (повторения и планы старого журнала) - тоже плановые платежи.
        for row in self.session.execute(
            sa.select(Operation.id, Operation.kind, Operation.paid_at, Operation.amount_base, Operation.account_from_id,
                      Operation.account_to_id, Operation.category_id, Operation.comment, Operation.counterparty_id)
            .where(Operation.workspace_id == self.workspace.id, Operation.deleted_at.is_(None), Operation.status == "plan",
                   Operation.paid_at <= self.end, Operation.kind.in_(("income", "expense")))
        ):
            account = row.account_to_id if row.kind == "income" else row.account_from_id
            if account not in self.accounts:
                continue
            direction = "in" if row.kind == "income" else "out"
            label = categories.get(row.category_id, "") or (row.comment or "")[:60]
            amount = Decimal(str(row.amount_base or 0))
            common = dict(direction=direction, entity=self.accounts[account], amount=amount, line=self._plan_line(direction, label),
                          source="journal", due=row.paid_at, title=(row.comment or "")[:120],
                          client=self.names.get(row.counterparty_id, "") if row.counterparty_id else "")
            if row.paid_at >= self.today:
                self.items.append(Item(day=row.paid_at, state="expected", **common))
            elif direction == "out":
                self.items.append(Item(day=self.today, state="carried", age=(self.today - row.paid_at).days, **common))
            else:
                self.items.append(Item(day=None, state="overdue", age=(self.today - row.paid_at).days, **common))

    @staticmethod
    def _match(plan: CashPlan, candidates: list[Any], used: set[uuid.UUID]) -> Any | None:
        """Факт к плановому платежу: сумма до тенге или тот же получатель ±5%; дата -5..+10; ближе - лучше."""
        if plan.amount is None or plan.day is None:
            return None
        best, best_rank = None, None
        lo, hi = plan.day - timedelta(days=cash_plans.MATCH_BEFORE), plan.day + timedelta(days=cash_plans.MATCH_AFTER)
        for row in candidates:
            if row.id in used or row.paid_at < lo or row.paid_at > hi:
                continue
            amount = Decimal(str(row.amount_base or 0))
            same_party = plan.counterparty_id is not None and row.counterparty_id == plan.counterparty_id
            if amount == plan.amount:
                quality = 0 if same_party else 1
            elif same_party and plan.amount and abs(amount - plan.amount) <= plan.amount * cash_plans.MATCH_SHARE:
                quality = 2
            else:
                continue
            rank = (quality, abs((row.paid_at - plan.day).days))
            if best_rank is None or rank < best_rank:
                best, best_rank = row, rank
        return best

    # ── Повторы журнала ──────────────────────────────────────────────────────

    def _repeats(self, plans: list[CashPlan]) -> None:
        from app.finance.core import settings as core_settings

        ignore = core_settings.payments(self.session, self.workspace).ignored("categories")
        found = habits_module.repeats(self.session, self.workspace, today=self.today, accounts=self.accounts, ignore_categories=ignore)
        planned = [plan for plan in plans if plan.kind == "payment" and plan.direction == "out" and plan.day]

        def covered(repeat: habits_module.Repeat, due: date) -> bool:
            for plan in planned:
                if plan.entity_id != repeat.entity or abs((plan.day - due).days) > 10:
                    continue
                if repeat.counterparty_id and plan.counterparty_id == repeat.counterparty_id:
                    return True
                if repeat.category_id and plan.category_id == repeat.category_id:
                    return True
                if cash_plans.norm(plan.line or plan.title) in (cash_plans.norm(repeat.label), cash_plans.norm(repeat.party)):
                    return True
            return False

        self.proposals = habits_module.proposals(found, today=self.today, end=self.end, workday=self.workday, covered=covered)
        if self.scenario == "habit":
            for item in self.proposals:
                entity = cash_plans._uuid(item["entity_id"])
                self.items.append(Item(
                    "out", entity, date.fromisoformat(item["day"]), Decimal(item["amount"]),
                    self._plan_line("out", item["line"], "habit"), "habit", state="proposed", due=date.fromisoformat(item["day"]),
                    title=item["line"], client=item["party"], note=item["words"],
                ))

    # ── Сборка ───────────────────────────────────────────────────────────────

    def build(self) -> dict[str, Any]:
        days = [self.start + timedelta(days=index) for index in range((self.end - self.start).days + 1)]
        index_of = {day: position for position, day in enumerate(days)}
        all_accounts = habits_module.accounts_of(self.session, self.workspace.id)
        self._contracts()
        if self.scenario == "habit":
            self._kort_habits()
        plans = self._place()
        facts = self._facts()
        self._plans(plans, facts, all_accounts)
        self._repeats(plans)
        if self.scenario == "cash":
            for item in self.items:
                if item.direction == "in" and item.day is not None:
                    item.note = (item.note + " · " if item.note else "") + "в сценарии «только расходы» не ждём"
        boards: dict[uuid.UUID | None, Board] = {}

        def board(entity: uuid.UUID | None) -> Board:
            if entity not in boards:
                boards[entity] = Board(days)
            return boards[entity]

        # Остаток на утро первого дня и факт по дням.
        opening: dict[uuid.UUID | None, Decimal] = defaultdict(lambda: _ZERO)
        from app.finance.models import Account

        for account_id, start_balance in self.session.execute(
            sa.select(Account.id, Account.starting_balance).where(Account.workspace_id == self.workspace.id)
        ):
            if account_id in self.accounts:
                opening[self.accounts[account_id]] += Decimal(str(start_balance or 0))
        fact_lines: dict[tuple[uuid.UUID | None, str, int], Decimal] = defaultdict(lambda: _ZERO)
        last_fact = min(self.today, self.end)
        for row in facts:
            amount = Decimal(str(row.amount_base or 0))
            moves = []
            if row.account_to_id in self.accounts and row.kind in ("income", "transfer"):
                moves.append((self.accounts[row.account_to_id], amount))
            if row.account_from_id in self.accounts and row.kind in ("expense", "transfer"):
                moves.append((self.accounts[row.account_from_id], -amount))
            if row.kind == "transfer" and len(moves) == 2 and moves[0][0] == moves[1][0]:
                continue  # перевод между своими счетами одного юрлица
            for entity, signed in moves:
                if row.paid_at < self.start:
                    opening[entity] += signed
                elif row.paid_at <= last_fact:
                    board(entity)
                    fact_lines[(entity, "in" if signed > 0 else "out", index_of[row.paid_at])] += abs(signed)
        for entity in set(opening) | set(self.accounts.values()) | {item.entity for item in self.items}:
            board(entity)
        for entity, target in boards.items():
            target.fact_in = [fact_lines.get((entity, "in", position), _ZERO) for position in range(len(days))]
            target.fact_out = [fact_lines.get((entity, "out", position), _ZERO) for position in range(len(days))]
        # Ожидания по дням и строкам.
        for item in self.items:
            target = board(item.entity)
            if item.day is None:
                target.no_date[item.line] = target.no_date.get(item.line, _ZERO) + item.amount
                continue
            if item.day > self.end:
                target.later[item.line] = target.later.get(item.line, _ZERO) + item.amount
                continue
            if item.day < max(self.today, self.start):
                continue
            target.put(item.line, index_of[item.day], item.amount)
        counted = {key for key, line in self.lines.items() if line.included}
        for entity, target in boards.items():
            running = opening[entity]
            for position, day in enumerate(days):
                target.opening.append(running)
                running += target.fact_in[position] - target.fact_out[position]
                if day >= self.today:
                    for key, values in target.lines.items():
                        if key not in counted:
                            continue
                        line = self.lines[key]
                        if line.direction == "in" and self.scenario == "cash":
                            continue
                        running += values[position] if line.direction == "in" else -values[position]
                target.closing.append(running)
        group = self._group(boards, days)
        gaps = self._gaps(boards)
        return self._out(days, boards, group, gaps)

    def _group(self, boards: dict[uuid.UUID | None, Board], days: list[date]) -> Board:
        total = Board(days)
        size = len(days)
        total.opening = [sum((item.opening[i] for item in boards.values()), _ZERO) for i in range(size)]
        total.closing = [sum((item.closing[i] for item in boards.values()), _ZERO) for i in range(size)]
        total.fact_in = [sum((item.fact_in[i] for item in boards.values()), _ZERO) for i in range(size)]
        total.fact_out = [sum((item.fact_out[i] for item in boards.values()), _ZERO) for i in range(size)]
        for item in boards.values():
            for key, values in item.lines.items():
                for position, value in enumerate(values):
                    total.put(key, position, value)
            for target, source in ((total.no_date, item.no_date), (total.later, item.later)):
                for key, value in source.items():
                    target[key] = target.get(key, _ZERO) + value
        return total

    # ── Разрыв ───────────────────────────────────────────────────────────────

    def _gaps(self, boards: dict[uuid.UUID | None, Board]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        lows: dict[uuid.UUID | None, Decimal] = {}
        for entity, target in boards.items():
            reserve = self.settings.reserve.get(entity, _ZERO) if entity else _ZERO
            future = [(day, value) for day, value in zip(target.days, target.closing) if day >= self.today]
            if not future:
                continue
            lows[entity] = min(value for _day, value in future) - reserve
        for entity, target in boards.items():
            if entity is None:
                continue
            reserve = self.settings.reserve.get(entity, _ZERO)
            future = [(day, value) for day, value in zip(target.days, target.closing) if day >= self.today]
            first = next(((day, value) for day, value in future if value < reserve), None)
            if first is None:
                continue
            low_day, low = min(((day, value) for day, value in future if day >= first[0]), key=lambda pair: pair[1])
            deficit = reserve - low
            back = next((day for day, value in future if day > low_day and value >= reserve), None)
            hints: list[dict[str, Any]] = []
            for other, spare in sorted(lows.items(), key=lambda pair: -pair[1]):
                if other is None or other == entity or spare <= 0:
                    continue
                hints.append({"kind": "transfer", "from": str(other), "from_title": self.entities.get(other, ""),
                              "amount": _money(min(spare, deficit)),
                              "text": f"перевести {_money(min(spare, deficit))} с {self.entities.get(other, '')} до {first[0]:%d.%m}"
                                      + ("" if spare >= deficit else " - закроет часть")})
                if len(hints) >= 3:
                    break
            debts = sorted((item for item in self.items if item.entity == entity and item.direction == "in" and item.day is None),
                           key=lambda item: -item.amount)
            gathered = _ZERO
            for item in debts:
                if gathered >= deficit or len([hint for hint in hints if hint["kind"] == "collect"]) >= 5:
                    break
                gathered += item.amount
                hints.append({"kind": "collect", "amount": _money(item.amount), "client": item.client, "contract_id": str(item.contract_id) if item.contract_id else None,
                              "text": (f"договориться о дате с «{item.client}» ({item.title or 'без договора'}) - долг {_money(item.amount)}"
                                       if item.source in ("contract", "table") else f"уточнить, когда придёт «{item.title or item.client}» - {_money(item.amount)}")})
            moves = sorted((item for item in self.items if item.entity == entity and item.direction == "out" and item.day is not None
                            and self.today <= item.day <= first[0]), key=lambda item: -item.amount)[:5]
            for item in moves:
                hints.append({"kind": "move", "amount": _money(item.amount), "day": item.day.isoformat(), "plan_id": str(item.plan_id) if item.plan_id else None,
                              "text": f"сдвинуть «{item.title or self.lines[item.line].title}» {item.day:%d.%m} - {_money(item.amount)}"})
            out.append({
                "entity": str(entity), "entity_title": self.entities.get(entity, ""), "day": first[0].isoformat(),
                "balance": _money(first[1]), "low_day": low_day.isoformat(), "low": _money(low), "reserve": _money(reserve),
                "deficit": _money(deficit), "back": back.isoformat() if back else None, "hints": hints,
            })
        out.sort(key=lambda gap: gap["day"])
        return out

    # ── Ответ ────────────────────────────────────────────────────────────────

    def _board_out(self, target: Board, keys: list[str]) -> dict[str, Any]:
        skip = (self.shown - self.start).days

        def series(values: list[Decimal]) -> list[str | None]:
            return [_money(value) for value in values[skip:]]

        return {
            "opening": series(target.opening), "closing": series(target.closing),
            "fact_in": series(target.fact_in), "fact_out": series(target.fact_out),
            "lines": [
                {"key": key, "values": series(target.lines.get(key, [_ZERO] * len(target.days))),
                 "no_date": _money(target.no_date.get(key)) if target.no_date.get(key) else None,
                 "later": _money(target.later.get(key)) if target.later.get(key) else None}
                for key in keys if key in target.lines or key in target.no_date or key in target.later
            ],
        }

    def _out(self, days: list[date], boards: dict[uuid.UUID | None, Board], group: Board, gaps: list[dict[str, Any]]) -> dict[str, Any]:
        keys = sorted(self.lines, key=lambda key: (self.lines[key].direction != "in", self.lines[key].order, self.lines[key].title))
        order = sorted(boards, key=lambda entity: (entity is None, self.entities.get(entity, "") if entity else ""))
        items = [item for item in self.items if item.day is None or item.day >= self.shown]
        items.sort(key=lambda item: (item.day or date.max, item.direction, -item.amount))
        return {
            "from": self.shown.isoformat(), "to": self.end.isoformat(), "today": self.today.isoformat(),
            "scenario": self.scenario, "scenario_title": SCENARIO_TITLES[self.scenario], "exclude": list(self.exclude),
            "days": [day.isoformat() for day in days if day >= self.shown],
            "days_off": [day.isoformat() for day in days if day >= self.shown and (day.weekday() in self.country.weekend or day in self.off)],
            "lines": [{"key": key, "title": self.lines[key].title, "direction": self.lines[key].direction,
                       "included": self.lines[key].included} for key in keys],
            "entities": [
                {"id": str(entity) if entity else NOBODY,
                 "title": self.entities.get(entity, "") if entity else "без юрлица",
                 "source": self.sources.get(str(entity), "kort") if entity else None,
                 **self._board_out(boards[entity], keys)}
                for entity in order
            ],
            "total": self._board_out(group, keys),
            "gaps": gaps,
            "items": [item.out(self.titles) for item in items],
            "done": self.done, "early": self.early, "proposals": self.proposals,
            "promises": self._promises_out(),
            "settings": self.settings.out(), "notes": self.notes,
        }

    def _promises_out(self) -> dict[str, Any]:
        broken = [item for item in self.items if item.state == "broken"]
        kept = [item for item in self.items if item.state == "promised"]
        return {
            "kept": len(kept), "broken": len(broken),
            "broken_amount": _money(sum((item.amount for item in broken), _ZERO)),
            "promised_amount": _money(sum((item.amount for item in kept), _ZERO)),
        }


def window(today: date, start: date | None = None, end: date | None = None, days: int | None = None) -> tuple[date, date]:
    """Окно по умолчанию: с понедельника этой недели пять недель (как у таблицы)."""
    first = start or (today - timedelta(days=today.weekday()))
    last = end or (first + timedelta(days=(days or DAYS) - 1))
    return first, last


def build(
    session: Session, workspace: Workspace, access: Access, *, today: date, start: date | None = None, end: date | None = None,
    days: int | None = None, scenario: str = "terms", exclude: Iterable[str] = (), book: Any | None = None,
) -> dict[str, Any]:
    first, last = window(today, start, end, days)
    return Calendar(session, workspace, access, start=first, end=last, today=today, scenario=scenario,
                    exclude=tuple(exclude), book=book).build()


__all__ = ["Calendar", "DAYS", "Item", "SCENARIOS", "build", "window"]
