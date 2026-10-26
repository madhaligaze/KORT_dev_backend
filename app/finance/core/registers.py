"""Регистры итогов (этап 2, шаг 2): хранимый итог журнала фактов и условий договоров.

До регистров долг, выручка и оборот считались заново на каждый запрос:
начисления каждого договора из условий на каждый день периода, разнесение
всех оплат по месяцам. На копии прода это 0,5-0,7 с на экран (27.10.2026), и
считалось одно и то же для «Долгов», «Выручки», «Сегодня», «Группы» и досье
юрлица. Регистр хранит выведенное по договорам:

* **начисления** (`RegisterAccrual`) - период за периодом от начала договора
  до горизонта (декабрь следующего года): сумма с поправкой, «не начислять»,
  исполнитель и заказчик на день периода;
* **расчёты по месяцам** (`RegisterMonth`) - начислено, разнесено оплат на
  месяц (всего и платежами), с какого дня месяц - долг, наше юрлицо месяца;
  оплачено за месяц, как держит таблица, и поступило в месяце;
* **договор** (`RegisterContract`) - входящее сальдо и что его погасило,
  переплата, оплачено в учёте.

Регистр - не вторая правда. Строки - вывод, руками не правятся и
пересчитываются сами. У договора есть подпись входов (`_signature`): колонки
условий и версия его фактов в журнале (последний номер проведения, в котором
факт договора записан, сменён или снят). Подпись другая - договор
пересчитывается целиком. Есть и общая подпись компании (`RegisterState`):
начало учёта и источники оплат, наши юрлица, смыслы статусов, месяц, версия
расчёта (`REVISION`) - сменилась она, пересчитывается всё.

Пересчёт - договор целиком, а не «от изменённой даты», как в 1С: оплата гасит
самый старый долг, и октябрьская оплата меняет строку июня. Дешевле договора
единицы нет: месяцев у него десятки. Дата всё же есть: оплата, внесённая
будущим числом, в учёт до своего дня не входит, и договор помнит, до какого
дня его строки верны (`valid_until`).

Регистр ведётся на сегодня. Долг на прошлый день, лента договора с кусками
оплат, сверки с таблицей - живым расчётом (`core/payments.Money`), и итог
считается одной функцией из той же сводки договора (`Settlement`): числа
регистра и живого расчёта не могут разойтись выводом, только входами.
АВР в регистре нет: у АВР таблицы её строка читается при запросе (шаг 3).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts.models import Contract, GroupEntity, ListValue
from app.finance.core import journal
from app.finance.core import settings as core_settings
from app.finance.core.accruals import Accrual, Ledger, month_key, month_start
from app.finance.core.journal import Adjustment
from app.finance.core.models import Fact, RegisterAccrual, RegisterContract, RegisterMonth, RegisterState
from app.finance.core.payments import OPENING, ContractMoney, Money
from app.finance.models import Workspace

#: Версия расчёта итогов: правка правил начислений или разнесения - поднять,
#: и регистры всех компаний пересчитаются при первом чтении.
REVISION = 1
_ZERO = Decimal("0")
_FAR = date(1900, 1, 1)
_CHUNK = 500
#: До стольких договоров к пересчёту оплаты читаются только их; больше - все разом.
_FEW = 50
#: Первое число замка пересчёта (у журнала - 4100).
_LOCK_CLASS = 4101
#: Колонки договора, из которых считаются его начисления и чьи оплаты.
TERMS = (
    "billing", "period_day", "billing_from", "signed_at", "amount", "status_id", "end_date", "executor_id", "customer_id",
)


def horizon(today: date) -> date:
    """До какого месяца хранятся начисления: декабрь следующего года (прогноз оборота и выручки)."""
    return date(today.year + 1, 12, 1)


# ── Сводка договора: из неё итог считается и в регистре, и вживую ────────────


@dataclass
class Line:
    """Месяц, который гасят оплаты: начисление месяца или входящее сальдо (`OPENING`)."""

    month: str
    accrued: Decimal
    #: С какого дня начисление - долг: «Период с» или первое число месяца.
    starts: date
    executor: uuid.UUID | None
    #: Разнесено на месяц всего и из этого - платежами (без аванса на начало учёта).
    paid: Decimal
    cash: Decimal

    @property
    def left(self) -> Decimal:
        return self.accrued - self.paid


@dataclass
class Settlement:
    """Расчёты договора на день: то из `ContractMoney`, что нужно итогам ядра."""

    contract_id: uuid.UUID
    since: date | None
    opening: Decimal | None
    accrues: bool
    lines: dict[str, Line] = field(default_factory=dict)
    #: Сверх всех начислений - платежами.
    over: Decimal = _ZERO
    #: Оплачено в учёте на день.
    paid: Decimal = _ZERO
    #: Месяц → оплачено за него, как держит таблица (`core/debts._attributed`).
    attributed: dict[str, Decimal] = field(default_factory=dict)
    #: Месяц → поступило по дню платежа.
    received: dict[str, Decimal] = field(default_factory=dict)

    @classmethod
    def of(cls, money: ContractMoney) -> "Settlement":
        lines: dict[str, Line] = {}
        for key, bucket in money.buckets.items():
            lines[key] = Line(
                month=key,
                accrued=bucket.accrued,
                starts=bucket.starts if key != OPENING else _FAR,
                executor=bucket.executor,
                paid=bucket.paid,
                cash=sum((part.amount for part in bucket.parts if part.payment.source != "opening"), _ZERO),
            )
        first = month_key(money.since) if money.since else ""
        attributed: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        # Как держит таблица: оплата - в месяце, которому её отдало разнесение;
        # не отданная месяцу (сальдо, переплата) - в месяце платежа, не раньше учёта.
        for key, bucket in money.buckets.items():
            for part in bucket.parts:
                if part.payment.source == "opening":
                    continue
                attributed[max(month_key(part.payment.paid_at), first) if key == OPENING else key] += part.amount
        for part in money.over:
            if part.payment.source != "opening":
                attributed[max(month_key(part.payment.paid_at), first)] += part.amount
        received: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        for payment in money.payments:
            received[month_key(payment.paid_at)] += payment.amount
        return cls(
            contract_id=money.contract_id,
            since=money.since,
            opening=money.opening,
            accrues=money.accrues,
            lines=lines,
            over=sum((part.amount for part in money.over if part.payment.source != "opening"), _ZERO),
            paid=sum((payment.amount for payment in money.payments), _ZERO),
            attributed={key: value for key, value in attributed.items() if value},
            received={key: value for key, value in received.items() if value},
        )

    def debt_on(self, day: date) -> Decimal:
        """Долг на день: сальдо и начисления с началом периода до дня, минус оплаченное в учёте."""
        owed = self.opening or _ZERO
        owed += sum((line.accrued for key, line in self.lines.items() if key != OPENING and line.starts <= day), _ZERO)
        return owed - self.paid

    def accrued_by_month(self) -> dict[str, Decimal]:
        return {key: line.accrued for key, line in self.lines.items() if key != OPENING}


# ── Начисления из регистра ───────────────────────────────────────────────────


class StoredLedger:
    """Начисления из регистра - то же, что `Ledger.accruals`, без пересчёта условий на каждый период.

    Окно за горизонтом регистра и договор, которого в регистре нет, - живым
    расчётом.
    """

    def __init__(self, session: Session, workspace_id: uuid.UUID, contracts: Iterable[Contract], rows: dict[uuid.UUID, list[Accrual]], *, known: set[uuid.UUID], until: date) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.contracts = list(contracts)
        self.rows = rows
        self.known = known
        self.until = until
        self._live: Ledger | None = None

    def live(self) -> Ledger:
        if self._live is None:
            self._live = Ledger(self.session, self.workspace_id, self.contracts)
        return self._live

    def accruals(self, contract: Contract, *, since: date, until: date) -> list[Accrual]:
        if until > self.until or contract.id not in self.known:
            return self.live().accruals(contract, since=since, until=until)
        first, last = month_key(since), month_key(until)
        return [item for item in self.rows.get(contract.id, []) if first <= item.month <= last]


# ── Регистры компании ────────────────────────────────────────────────────────


def _hash(value: Any) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _versions(session: Session, workspace_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """Договор → последний номер проведения, в котором его факт записан, сменён или снят."""
    touched = sa.case((Fact.ended_seq > Fact.seq, Fact.ended_seq), else_=Fact.seq)
    return {
        contract_id: int(value)
        for contract_id, value in session.execute(
            sa.select(Fact.contract_id, sa.func.max(touched))
            .where(Fact.workspace_id == workspace_id, Fact.contract_id.is_not(None))
            .group_by(Fact.contract_id)
        )
    }


def _signature(row: Any, version: int | None) -> str:
    return _hash([[getattr(row, name) for name in TERMS], version or 0])


def _insert(session: Session, table: sa.Table, rows: list[dict[str, Any]]) -> None:
    """Строки регистра - в Postgres через `COPY`: построчная вставка 14 тысяч строк стоила 1,4 с на копии."""
    if not rows:
        return
    if session.get_bind().dialect.name != "postgresql":
        session.execute(sa.insert(table), rows)
        return
    columns = list(rows[0])
    raw = session.connection().connection.dbapi_connection
    name = f'"{table.schema}"."{table.name}"' if table.schema else f'"{table.name}"'
    with raw.cursor() as cursor:
        with cursor.copy(f"COPY {name} ({', '.join(columns)}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row([row[column] for column in columns])


def _lock(session: Session, workspace_id: uuid.UUID) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(sa.text("SELECT pg_advisory_xact_lock(:a, :b)"), {"a": _LOCK_CLASS, "b": workspace_id.int & 0x7FFFFFFF})


class Registers:
    """Регистры компании, свежие на `today`: при чтении пересчитываются договоры, чьи входы сменились.

    `recomputed` - сколько договоров пересчитано этим чтением.
    """

    def __init__(self, session: Session, workspace: Workspace, *, today: date) -> None:
        self.session = session
        self.workspace = workspace
        self.today = today
        self.until = horizon(today)
        journal.ensure(session, workspace.id)
        self.recomputed = self._refresh()
        state = session.get(RegisterState, workspace.id)
        self.since: date | None = state.since if state is not None else None
        self._settlements: dict[uuid.UUID, Settlement] | None = None
        self._accruals: dict[uuid.UUID, list[Accrual]] | None = None
        self._known: set[uuid.UUID] | None = None

    # ── Свежесть ─────────────────────────────────────────────────────────────

    def _company(self, since: date | None) -> str:
        """Подпись того, что меняет итоги всех договоров разом."""
        session, workspace_id = self.session, self.workspace.id
        settings = core_settings.payments(session, self.workspace)
        own = sorted(
            str(item)
            for item in session.scalars(
                sa.select(GroupEntity.counterparty_id).where(GroupEntity.workspace_id == workspace_id, GroupEntity.archived_at.is_(None))
            )
        )
        phases = sorted(
            (str(row.id), str((row.meaning or {}).get("phase") or ""))
            for row in session.scalars(
                sa.select(ListValue).where(ListValue.workspace_id == workspace_id, ListValue.field_key == "status")
            )
        )
        sources = sorted((str(key), value) for key, value in settings.sources.items())
        return _hash([REVISION, month_key(self.today), self.until, since, sources, own, phases])

    def _plan(self, money: Money | None = None) -> tuple[str, date | None, int, list[Any], list[uuid.UUID], list[uuid.UUID], Money | None]:
        """Что пересчитать: подпись компании, начало учёта, номер журнала, договоры, устаревшие, удалённые.

        `money` - оплаты компании, уже прочитанные этим запросом: не читать второй раз.
        """
        session, workspace_id = self.session, self.workspace.id
        from app.finance.contracts.fields import current

        settings = core_settings.payments(session, self.workspace)
        state = session.get(RegisterState, workspace_id)
        facts_seq = current(session, workspace_id, journal.SEQ) or 0
        since = settings.since
        if since is None:
            # Начало учёта - по первой оплате: пока журнал не менялся, оно то же.
            if state is not None and state.signature and state.facts_seq == facts_seq:
                since = state.since
            else:
                money = money or Money(session, self.workspace, today=self.today)
                since = money.since
        company = self._company(since)
        rows = list(
            session.execute(
                sa.select(Contract.id, *(getattr(Contract, name) for name in TERMS)).where(
                    Contract.workspace_id == workspace_id, Contract.deleted_at.is_(None)
                )
            )
        )
        versions = _versions(session, workspace_id)
        stored = {
            contract_id: (signature, valid_until)
            for contract_id, signature, valid_until in session.execute(
                sa.select(RegisterContract.contract_id, RegisterContract.signature, RegisterContract.valid_until).where(
                    RegisterContract.workspace_id == workspace_id
                )
            )
        }
        every = state is None or state.signature != company
        stale = []
        for row in rows:
            signature, valid_until = stored.get(row.id, (None, None))
            if every or signature != _signature(row, versions.get(row.id)) or (valid_until is not None and valid_until < self.today):
                stale.append(row.id)
        alive = {row.id for row in rows}
        gone = [contract_id for contract_id in stored if contract_id not in alive]
        return company, since, facts_seq, rows, stale, gone, money

    def _refresh(self) -> int:
        company, since, facts_seq, rows, stale, gone, money = self._plan()
        state = self.session.get(RegisterState, self.workspace.id)
        if not stale and not gone and state is not None and state.signature == company:
            return 0
        _lock(self.session, self.workspace.id)
        # Пока ждали замка, другой запрос мог пересчитать то же.
        company, since, facts_seq, rows, stale, gone, money = self._plan(money)
        state = self.session.get(RegisterState, self.workspace.id)
        if not stale and not gone and state is not None and state.signature == company:
            return 0
        versions = _versions(self.session, self.workspace.id)
        by_id = {row.id: row for row in rows}
        # Оплаты - только после версий фактов: прочитанные раньше могли бы
        # оказаться старее подписи, и договор не пересчитался бы. Новее - не
        # страшно: подпись разойдётся, и следующее чтение пересчитает.
        self._write(stale, gone, None, {key: _signature(by_id[key], versions.get(key)) for key in stale})
        if state is None:
            state = RegisterState(workspace_id=self.workspace.id)
            self.session.add(state)
        state.signature, state.day, state.since, state.facts_seq = company, self.today, since, facts_seq
        self.session.flush()
        return len(stale)

    def _write(self, stale: list[uuid.UUID], gone: list[uuid.UUID], money: Money | None, signatures: dict[uuid.UUID, str]) -> None:
        session, workspace_id = self.session, self.workspace.id
        dropped = [*stale, *gone]
        for model in (RegisterContract, RegisterMonth, RegisterAccrual):
            for start in range(0, len(dropped), _CHUNK):
                session.execute(
                    sa.delete(model)
                    .where(model.workspace_id == workspace_id, model.contract_id.in_(dropped[start : start + _CHUNK]))
                    .execution_options(synchronize_session=False)
                )
        if not stale:
            return
        if money is None:
            # Несколько договоров - их оплаты, а не все оплаты компании (начало учёта задано).
            money = Money(session, self.workspace, today=self.today, only=stale if len(stale) <= _FEW else None)
        contracts: list[Contract] = []
        for start in range(0, len(stale), _CHUNK):
            contracts.extend(session.scalars(sa.select(Contract).where(Contract.id.in_(stale[start : start + _CHUNK]))))
        ledger = Ledger(session, workspace_id, contracts)
        heads: list[dict[str, Any]] = []
        months: list[dict[str, Any]] = []
        periods: list[dict[str, Any]] = []
        for contract in contracts:
            item = Settlement.of(money.contract(contract, ledger))
            future = [payment.paid_at for payment in money.by_contract.get(contract.id, []) if payment.paid_at > self.today]
            opening = item.lines.get(OPENING)
            heads.append(
                {
                    "workspace_id": workspace_id, "contract_id": contract.id, "signature": signatures[contract.id],
                    # Оплата будущим числом войдёт в учёт в свой день - до него строки верны.
                    "valid_until": min(future) - timedelta(days=1) if future else None,
                    "accrues": item.accrues, "opening": item.opening,
                    "opening_paid": opening.paid if opening is not None else _ZERO,
                    "opening_cash": opening.cash if opening is not None else _ZERO,
                    "opening_executor": opening.executor if opening is not None else None,
                    "over": item.over, "paid": item.paid,
                }
            )
            keys = {key for key in item.lines if key != OPENING} | set(item.attributed) | set(item.received)
            for key in keys:
                line = item.lines.get(key)
                months.append(
                    {
                        "workspace_id": workspace_id, "contract_id": contract.id, "month": month_start(key),
                        "bucket": line is not None, "accrued": line.accrued if line else _ZERO,
                        "starts": line.starts if line else None, "executor_id": line.executor if line else None,
                        "paid": line.paid if line else _ZERO, "cash": line.cash if line else _ZERO,
                        "attributed": item.attributed.get(key, _ZERO), "received": item.received.get(key, _ZERO),
                    }
                )
            for accrual in ledger.accruals(contract, since=_FAR, until=self.until):
                adjustment = accrual.adjustment
                periods.append(
                    {
                        "workspace_id": workspace_id, "contract_id": contract.id, "period_from": accrual.period_from,
                        "month": month_start(accrual.month), "period_to": accrual.period_to, "base": accrual.base,
                        "amount": accrual.amount, "executor_id": accrual.executor_id, "customer_id": accrual.customer_id,
                        "adjustment_id": adjustment.id if adjustment is not None else None, "skipped": accrual.skipped,
                    }
                )
        for model, rows in ((RegisterContract, heads), (RegisterMonth, months), (RegisterAccrual, periods)):
            _insert(session, model.__table__, rows)

    # ── Чтение ───────────────────────────────────────────────────────────────

    def settlements(self) -> dict[uuid.UUID, Settlement]:
        """Договор → расчёты на сегодня."""
        if self._settlements is not None:
            return self._settlements
        session, workspace_id = self.session, self.workspace.id
        out: dict[uuid.UUID, Settlement] = {}
        for head in session.execute(
            sa.select(
                RegisterContract.contract_id, RegisterContract.accrues, RegisterContract.opening, RegisterContract.opening_paid,
                RegisterContract.opening_cash, RegisterContract.opening_executor, RegisterContract.over, RegisterContract.paid,
            ).where(RegisterContract.workspace_id == workspace_id)
        ):
            item = Settlement(
                contract_id=head.contract_id, since=self.since, opening=head.opening, accrues=head.accrues,
                over=head.over, paid=head.paid,
            )
            if head.opening is not None and head.opening > 0:
                item.lines[OPENING] = Line(OPENING, head.opening, _FAR, head.opening_executor, head.opening_paid, head.opening_cash)
            out[head.contract_id] = item
        for row in session.execute(
            sa.select(
                RegisterMonth.contract_id, RegisterMonth.month, RegisterMonth.bucket, RegisterMonth.accrued, RegisterMonth.starts,
                RegisterMonth.executor_id, RegisterMonth.paid, RegisterMonth.cash, RegisterMonth.attributed, RegisterMonth.received,
            ).where(RegisterMonth.workspace_id == workspace_id)
        ):
            item = out.get(row.contract_id)
            if item is None:
                continue
            key = month_key(row.month)
            if row.bucket:
                item.lines[key] = Line(key, row.accrued, row.starts, row.executor_id, row.paid, row.cash)
            if row.attributed:
                item.attributed[key] = row.attributed
            if row.received:
                item.received[key] = row.received
        self._settlements = out
        return out

    def ledger(self, contracts: Iterable[Contract]) -> StoredLedger:
        """Начисления договоров из регистра (`accruals`), за горизонтом - живым расчётом."""
        if self._accruals is None:
            rows: dict[uuid.UUID, list[Accrual]] = defaultdict(list)
            for row in self.session.execute(
                sa.select(RegisterAccrual).where(RegisterAccrual.workspace_id == self.workspace.id)
                .order_by(RegisterAccrual.contract_id, RegisterAccrual.period_from)
            ).scalars():
                rows[row.contract_id].append(
                    Accrual(
                        contract_id=row.contract_id, month=month_key(row.month), period_from=row.period_from,
                        period_to=row.period_to, base=row.base, amount=row.amount, executor_id=row.executor_id,
                        customer_id=row.customer_id,
                        # Причина поправки - у живого расчёта (лента договора); итогам нужно «не начислять».
                        adjustment=(
                            Adjustment(row.adjustment_id, row.contract_id, row.month, row.amount if not row.skipped else None, row.skipped, "")
                            if row.adjustment_id is not None
                            else None
                        ),
                    )
                )
            self._accruals = dict(rows)
            self._known = set(
                self.session.scalars(sa.select(RegisterContract.contract_id).where(RegisterContract.workspace_id == self.workspace.id))
            )
        return StoredLedger(self.session, self.workspace.id, contracts, self._accruals, known=self._known or set(), until=self.until)


# ── Итоги на день: регистр или живой расчёт ──────────────────────────────────


class Totals:
    """Расчёты и начисления договоров на день: `stored` - из регистра (день - сегодня), иначе живым расчётом.

    Начисления (`ledger`) - по первому обращению: долгам хватает расчётов.
    """

    def __init__(self, since: date | None, settlements: dict[uuid.UUID, Settlement], ledger: Callable[[], Any], stored: bool) -> None:
        self.since = since
        self.settlements = settlements
        self.stored = stored
        self._ledger = ledger
        self._built: Any = None

    @property
    def ledger(self) -> Any:
        if self._built is None:
            self._built = self._ledger()
        return self._built


def of(session: Session, workspace: Workspace, *, today: date) -> Registers:
    """Регистры компании на сегодня - один раз на запрос.

    «Группа» и досье юрлица читают и оборот, и долги. Запись в хранилища в
    этой же сессии сбрасывает запомненное (`journal.REGISTERS`): правку
    регистр увидит при следующем чтении.
    """
    cache: dict[tuple[uuid.UUID, date], Registers] = session.info.setdefault(journal.REGISTERS, {})
    key = (workspace.id, today)
    if key not in cache:
        cache[key] = Registers(session, workspace, today=today)
    return cache[key]


def totals(session: Session, workspace: Workspace, contracts: Iterable[Contract], *, day: date) -> Totals:
    """Итоги договоров на день. Регистр ведётся на сегодня; другой день - живым расчётом из журнала."""
    from app.finance.contracts.service import today

    items = list(contracts)
    if day == today():
        registers = of(session, workspace, today=day)
        found = registers.settlements()
        settlements = {item.id: found[item.id] for item in items if item.id in found}
        missing = [item for item in items if item.id not in found]
        if missing:
            # Договор завели другой транзакцией после пересчёта: не пропустить, а досчитать.
            money = Money(session, workspace, today=day, only=[item.id for item in missing])
            ledger = Ledger(session, workspace.id, missing)
            settlements.update({item.id: Settlement.of(money.contract(item, ledger)) for item in missing})
        return Totals(registers.since, settlements, lambda: registers.ledger(items), True)
    money = Money(session, workspace, today=day)
    ledger = Ledger(session, workspace.id, items)
    return Totals(money.since, {item.id: Settlement.of(money.contract(item, ledger)) for item in items}, lambda: ledger, False)


def ledger_on(session: Session, workspace: Workspace, contracts: Iterable[Contract], *, day: date) -> Any:
    """Начисления договоров: из регистра, если день - сегодня (регистр свежий), иначе - `Ledger`."""
    from app.finance.contracts.service import today

    items = list(contracts)
    if day == today():
        return of(session, workspace, today=day).ledger(items)
    return Ledger(session, workspace.id, items)


__all__ = ["Line", "REVISION", "Registers", "Settlement", "StoredLedger", "Totals", "horizon", "ledger_on", "of", "totals"]
