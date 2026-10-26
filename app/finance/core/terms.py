"""Условия договора на дату: сумма, стороны, статус, окончание - по соглашениям.

Договор хранит текущие значения; соглашение - «было / стало с даты»
(`ContractAmendment.before/after`). Значение на день D - «стало» последнего
соглашения, вступившего не позже D; до первого соглашения - его «было».
Так считаются и прошлые месяцы (клиента перевели на другое ТОО с июля), и
будущие (цена с января, ещё не применённая).

Отрезок, в котором значение действует сейчас, берётся из самого договора, а
не из записи соглашения: правка «опечатка» (`Mode.fix`) исправляет текущее
значение без соглашения, и записанное «стало» уже устарело. Опечатка в
прошлом отрезке так не выражается - для этого есть соглашение с датой.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Callable, Iterable

from app.finance.contracts.models import Contract
from app.finance.contracts.service import value_of

#: Ключи, у которых бывает «с даты». `status` - с фазы 2 ядра (пауза,
#: возобновление); `end_date` - соглашение о сроке.
TRACKED = ("amount", "executor", "customer", "status", "end_date")


@dataclass(frozen=True)
class Terms:
    """Условия договора, действующие в один день."""

    amount: Decimal | None
    executor_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    status_id: uuid.UUID | None
    #: Фаза статуса (`STATUS_PHASES`): пусто - статус без смысла или не задан.
    phase: str
    end_date: date | None


def _remembered(parse: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Разбор значения - один раз на значение: `Timeline.on` зовут на каждый период каждого договора.

    Ключ - с типом: `1` и `1.0` для словаря одно, а сумма из них пишется по-разному.
    """

    @lru_cache(maxsize=8192)
    def cached(kind: type, value: Any) -> Any:
        return parse(value)

    def read(value: Any) -> Any:
        try:
            return cached(type(value), value)
        except TypeError:  # список или словарь - не ключ кэша
            return parse(value)

    return read


@_remembered
def _uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


@_remembered
def _money(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@_remembered
def _day(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class Timeline:
    """Значения ключей договора по датам - из его соглашений."""

    def __init__(
        self,
        contract: Contract,
        # Строки `contract_amendments` или соглашения журнала (`core/journal.Agreement`):
        # у обоих `contract_id`, `effective_from`, `position`, `before`, `after`, `applied_at`.
        amendments: Iterable[Any],
        phase_of: Callable[[uuid.UUID | None], str],
    ) -> None:
        self.contract = contract
        self.phase_of = phase_of
        #: Значения договора «сейчас» - один раз: `on` зовут на каждый период.
        self.current: dict[str, Any] = {key: value_of(contract, key) for key in TRACKED}
        self.initial: dict[str, Any] = {}
        #: Ключ → [(с какого дня, значение, применено ли к договору)].
        self.steps: dict[str, list[tuple[date, Any, bool]]] = {}
        dated = sorted(
            (item for item in amendments if item.effective_from is not None and item.contract_id == contract.id),
            key=lambda item: (item.effective_from, item.position),
        )
        for item in dated:
            for key, value in (item.after or {}).items():
                if key not in TRACKED:
                    continue
                if key not in self.initial:
                    before = item.before or {}
                    self.initial[key] = before[key] if key in before else value_of(contract, key)
                self.steps.setdefault(key, []).append((item.effective_from, value, item.applied_at is not None))

    def value(self, key: str, day: date) -> Any:
        """Значение ключа в день `day` в виде API (`value_of`)."""
        current = self.current[key] if key in self.current else value_of(self.contract, key)
        steps = self.steps.get(key)
        if not steps:
            return current
        # Отрезок «сейчас» - от последнего применённого соглашения до
        # следующего: в нём значение - то, что стоит в договоре. Соглашение,
        # чей день настал, но которое фоновая задача ещё не применила, - уже
        # следующий отрезок со своим «стало».
        live = 0
        for index, (_since, _value, applied) in enumerate(steps):
            if applied:
                live = index + 1
        start = steps[live - 1][0] if live else None
        end = steps[live][0] if live < len(steps) else None
        if (start is None or day >= start) and (end is None or day < end):
            return current
        value = self.initial[key]
        for since, after, _applied in steps:
            if since <= day:
                value = after
            else:
                break
        return value

    def on(self, day: date) -> Terms:
        status_id = _uuid(self.value("status", day))
        return Terms(
            amount=_money(self.value("amount", day)),
            executor_id=_uuid(self.value("executor", day)),
            customer_id=_uuid(self.value("customer", day)),
            status_id=status_id,
            phase=self.phase_of(status_id) or "",
            end_date=_day(self.value("end_date", day)),
        )

    def changes(self, key: str) -> list[tuple[date, Any]]:
        """Даты, с которых ключ менялся, и новое значение."""
        return [(since, value) for since, value, _applied in self.steps.get(key, [])]


__all__ = ["TRACKED", "Terms", "Timeline"]
