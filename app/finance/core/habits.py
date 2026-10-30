"""Привычки денег: как клиент платит и что компания платит каждый месяц - из истории, а не со слов.

Таблица планирует по сроку: счёт выставлен 25-го - деньги 25-го. На деле
клиент, который полгода платит на неделю позже, заплатит на неделю позже и в
этот раз, а «Платежи (план)» коллега заполнял руками, глядя в Журнал за
прошлый месяц. Обе вещи в данных уже есть:

* **привычка клиента** - задержка оплаты месяца против срока: день, когда
  оплаты месяца набрали его начисление, минус срок (`core/cash_calendar.due`).
  Медиана последних месяцев (не больше `KEEP`), сколько их и сколько вовремя.
  Меньше `ENOUGH` месяцев - привычки нет: по двум точкам не судят;
* **повторяющийся расход** - тот же получатель (или статья, если получателя
  нет) того же нашего юрлица в `ENOUGH_MONTHS` из последних `MONTHS` полных
  месяцев: обычный день - медиана, сумма - последнего месяца. Переводы между
  своими и «не оплаты» компании (`core/settings.payments.ignore`) - не расход.

И то и другое - довод, а не решение: привычка двигает дату только в
сценарии «по привычке», повтор становится плановым платежом, когда его
примет человек (`cash.accept`).
"""
from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts.models import GroupEntity
from app.finance.layout import norm
from app.finance.models import Account, Category, Counterparty, Operation, Workspace

_ZERO = Decimal("0")
#: Сколько последних оплаченных месяцев клиента смотреть и сколько нужно для привычки.
KEEP, ENOUGH = 8, 3
#: Повтор расхода: в стольких из последних полных месяцев.
MONTHS, ENOUGH_MONTHS = 3, 2


@dataclass
class Habit:
    """Как клиент платит: задержки месяцев против срока (дней, плюс - позже срока)."""

    lags: list[int] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return len(self.lags) >= ENOUGH

    @property
    def delay(self) -> int:
        """Медиана задержки, дней; 0 - пока привычки нет."""
        return int(round(statistics.median(self.lags))) if self.known else 0

    @property
    def on_time(self) -> int:
        return sum(1 for lag in self.lags if lag <= 0)

    def words(self) -> str:
        if not self.known:
            return f"оплаченных месяцев {len(self.lags)} - привычки ещё нет"
        delay = self.delay
        when = "в срок" if delay == 0 else (f"на {delay} дн. позже срока" if delay > 0 else f"на {-delay} дн. раньше срока")
        return f"обычно платит {when}: вовремя {self.on_time} из {len(self.lags)}"

    def out(self) -> dict[str, Any]:
        return {"delay": self.delay if self.known else None, "months": len(self.lags), "on_time": self.on_time, "words": self.words()}


class Habits:
    """Привычки клиентов компании: ключ клиента → `Habit`."""

    def __init__(self) -> None:
        self._raw: dict[str, list[tuple[date, int]]] = defaultdict(list)
        self._done: dict[str, Habit] | None = None

    def add(self, client: str, due: date | None, paid_on: date | None) -> None:
        if not client or due is None or paid_on is None:
            return
        self._raw[client].append((due, (paid_on - due).days))
        self._done = None

    def _build(self) -> dict[str, Habit]:
        if self._done is None:
            self._done = {
                client: Habit([lag for _due, lag in sorted(items)[-KEEP:]])
                for client, items in self._raw.items()
            }
        return self._done

    def of(self, client: str) -> Habit:
        return self._build().get(client) or Habit()

    def company(self) -> Habit:
        """Привычка всех клиентов разом - когда у клиента своей нет."""
        lags = [lag for habit in self._build().values() for lag in habit.lags]
        return Habit(lags)


def month_paid_on(parts: Iterable[tuple[date, Decimal]], accrued: Decimal) -> date | None:
    """День, когда оплаты месяца (по дням) набрали его начисление; не набрали - `None`."""
    total = _ZERO
    for day, amount in sorted(parts, key=lambda item: item[0]):
        total += amount
        if total >= accrued:
            return day
    return None


# ── Повторяющиеся расходы ────────────────────────────────────────────────────


@dataclass
class Repeat:
    """Расход, который повторяется: кто, чьи деньги, обычный день, сумма."""

    entity: uuid.UUID | None
    key: str
    label: str
    party: str
    counterparty_id: uuid.UUID | None
    category_id: uuid.UUID | None
    day: int
    amount: Decimal
    seen: int
    of: int
    #: Месяцы, в которых расход уже был (`ГГГГ-ММ`).
    months: set[str] = field(default_factory=set)

    def words(self) -> str:
        return f"{self.seen} из {self.of} мес., около {self.day}-го"


def _month(day: date) -> str:
    return f"{day:%Y-%m}"


def _shift_month(day: date, by: int) -> date:
    index = day.year * 12 + day.month - 1 + by
    return date(index // 12, index % 12 + 1, 1)


def repeats(
    session: Session, workspace: Workspace, *, today: date, accounts: dict[uuid.UUID, uuid.UUID | None],
    ignore_categories: set[str], months: int = MONTHS,
) -> list[Repeat]:
    """Расходы, повторявшиеся в последние полные месяцы, по нашим юрлицам.

    `accounts` - счета календаря → наше юрлицо. Перевод на счёт нашего юрлица,
    получатель - наше юрлицо, статья из «не оплат» компании - не расход.
    """
    first = _shift_month(today.replace(day=1), -months)
    full = [_month(_shift_month(first, index)) for index in range(months)]
    own = set(session.scalars(sa.select(GroupEntity.counterparty_id).where(GroupEntity.workspace_id == workspace.id)))
    categories = {
        row.id: row.name for row in session.execute(sa.select(Category.id, Category.name).where(Category.workspace_id == workspace.id))
    }
    names = dict(session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace.id)).all())
    rows = session.execute(
        sa.select(Operation.paid_at, Operation.amount_base, Operation.account_from_id, Operation.account_to_id,
                  Operation.counterparty_id, Operation.category_id, Operation.kind)
        .where(
            Operation.workspace_id == workspace.id, Operation.deleted_at.is_(None), Operation.status == "fact",
            Operation.kind == "expense", Operation.paid_at >= first, Operation.paid_at <= today,
        )
    ).all()
    groups: dict[tuple[uuid.UUID | None, str], dict[str, Any]] = {}
    for paid_at, amount, account_from, _to, party, category, _kind in rows:
        if account_from not in accounts:
            continue
        if party is not None and party in own:
            continue
        category_name = categories.get(category, "") if category else ""
        if category_name and norm(category_name) in ignore_categories:
            continue
        if party is not None:
            key = f"p:{party}"
        elif category is not None:
            key = f"c:{category}"
        else:
            continue
        entity = accounts[account_from]
        group = groups.setdefault((entity, key), {
            "party": party, "category": category, "label": category_name or names.get(party, ""),
            "by_month": defaultdict(lambda: _ZERO), "days": defaultdict(list),
        })
        if not group["category"] and category:
            group["category"], group["label"] = category, category_name or group["label"]
        month = _month(paid_at)
        group["by_month"][month] += Decimal(str(amount or 0))
        group["days"][month].append(paid_at.day)
    out: list[Repeat] = []
    for (entity, key), group in groups.items():
        seen = [month for month in full if group["by_month"].get(month)]
        if len(seen) < ENOUGH_MONTHS:
            continue
        last = seen[-1]
        days = [day for month in seen for day in group["days"][month]]
        party_name = names.get(group["party"], "") if group["party"] else ""
        out.append(Repeat(
            entity=entity, key=key, label=group["label"] or party_name, party=party_name,
            counterparty_id=group["party"], category_id=group["category"], day=int(statistics.median(days)),
            amount=group["by_month"][last].quantize(Decimal("0.01")), seen=len(seen), of=len(full),
            months=set(group["by_month"]),
        ))
    out.sort(key=lambda item: (-item.amount, item.label))
    return out


def proposals(
    found: Iterable[Repeat], *, today: date, end: date, workday: Callable[[date], date], covered: Callable[[Repeat, date], bool],
) -> list[dict[str, Any]]:
    """Повторы → ожидания на месяцы до `end`, которых ещё нет: ни факта в месяце, ни плана рядом."""
    import calendar as month_calendar

    out: list[dict[str, Any]] = []
    for item in found:
        month = today.replace(day=1)
        while month <= end:
            key = _month(month)
            last_day = month_calendar.monthrange(month.year, month.month)[1]
            due = workday(month.replace(day=min(item.day, last_day)))
            if key not in item.months and due >= today and due <= end and not covered(item, due):
                out.append({
                    "key": f"history:{item.entity}:{item.key}:{key}", "entity_id": str(item.entity) if item.entity else None,
                    "day": due.isoformat(), "amount": format(item.amount, "f"), "line": item.label, "party": item.party,
                    "counterparty_id": str(item.counterparty_id) if item.counterparty_id else None,
                    "category_id": str(item.category_id) if item.category_id else None,
                    "words": item.words(),
                })
            month = _shift_month(month, 1)
    return out


def accounts_of(session: Session, workspace_id: uuid.UUID, skip: Iterable[uuid.UUID] = ()) -> dict[uuid.UUID, uuid.UUID | None]:
    """Счета - деньги календаря → наше юрлицо: не архив, не исключённые из отчётов, не снятые компанией."""
    skipped = set(skip)
    return {
        account_id: entity
        for account_id, entity in session.execute(
            sa.select(Account.id, Account.group_entity_id).where(
                Account.workspace_id == workspace_id, Account.archived_at.is_(None), Account.excluded_from_reports.is_(False)
            )
        )
        if account_id not in skipped
    }


__all__ = ["ENOUGH", "Habit", "Habits", "Repeat", "accounts_of", "month_paid_on", "proposals", "repeats"]
