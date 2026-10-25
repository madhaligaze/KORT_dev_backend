"""Итоги ядра: кто сколько должен, с какого дня и чей это долг (фаза 6).

Ничего не хранится - долг считается при чтении из событий на любой день:

* **по начислениям** - входящее сальдо, плюс начисления, чей период начался
  до дня, минус оплаты до дня (`core/payments.py`): что клиент должен по
  договору;
* **по АВР** - входящее сальдо по АВР, плюс АВР месяцев учёта с датой до дня
  (`core/acts.py`: KORT, 1С, книга - по приоритету), минус те же оплаты: что
  клиент должен по выставленным актам. АВР без даты - с первого числа своего
  месяца, как его держит строка книги.

Старение - по неоплаченному остатку после разнесения оплат: по начислениям -
месяцы, как их разнесли оплаты (сначала «за какой месяц», остальное -
самому старому долгу); по АВР - акты по порядку дат. Возраст - дни от начала
периода (даты акта): до 30, 31-60, 61-90, больше 90; остаток входящего
сальдо - «до учёта». Оплаченное вперёд и переплата - аванс, отдельно.

Чей долг. Сначала сторона: «нам должны» - исполнитель наше юрлицо, «мы
должны» - покупка, наше юрлицо только заказчик (оплаты - наши списания), «между
своими» - обе стороны наши (FIN/72: BBCA → BBC на 13,5 млн на копии прода - не
долг клиента). Потом группа: вторая сторона, наше юрлицо, отдел и человек - по
долям договора
(`contracts/shares.py`: процент или сумма к сумме договора). Один отдел (один
человек) без доли - договор его целиком; несколько без долей и остаток после
долей - «не распределено»: делить поровну значило бы угадать. Доли, которые
человеку не открыты (`shares.departments_open`, `shares.people_scope`), не
делятся - договор целиком в «доли закрыты»: иначе долю было бы видно по
долгу.

Сверка: долг KORT на конец месяца против «Сальдо Конец (без АВР)» и «Сальдо
Конец (АВР)» строки книги-сводки. Книга держит оплату в строке того месяца,
к которому её отнесли, а не по дню платежа, поэтому и KORT считает здесь так
же: оплата - в месяце, которому её отдало разнесение; не отданная месяцу
(сальдо, переплата) - в месяце платежа.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts import shares as shares_module
from app.finance.contracts.models import Contract, ContractDepartment, ContractPerson, Department, Employee, GroupEntity
from app.finance.contracts.service import Access, Registry, people_of
from app.finance.core import opening as opening_module
from app.finance.core.accruals import Ledger, month_key, month_start
from app.finance.core.acts import Act, Acts
from app.finance.core.payments import OPENING, ContractMoney, Money, Payment
from app.finance.models import Counterparty, Workspace

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
#: Корзины старения: верхняя граница в днях и ключ; `None` - без границы.
AGES: tuple[tuple[int | None, str], ...] = ((30, "d30"), (60, "d60"), (90, "d90"), (None, "d90plus"))
AGE_TITLES = {
    "opening": "до учёта",
    "d30": "до 30 дней",
    "d60": "31-60 дней",
    "d90": "61-90 дней",
    "d90plus": "больше 90 дней",
}
#: Группы: вторая сторона договора (клиент, у покупки - поставщик), наше
#: юрлицо, отдел, человек.
GROUPS = ("client", "entity", "department", "person")
BASES = ("accruals", "acts")
#: Нам должны, мы должны (покупки), между своими (обе стороны - наши юрлица).
SIDES = ("receivable", "payable", "intra")
#: Особые хозяева доли: нет отдела (человека), не распределено, доли закрыты.
NOBODY, REST, CLOSED = "none", "rest", "closed"
_SPECIAL = {
    "client": {NOBODY: "без второй стороны"},
    "entity": {NOBODY: "без нашего юрлица"},
    "department": {NOBODY: "без отдела", REST: "не распределено", CLOSED: "доли закрыты"},
    "person": {NOBODY: "без сотрудника", REST: "не распределено", CLOSED: "доли закрыты"},
}


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _age_key(days: int) -> str:
    for limit, key in AGES:
        if limit is None or days <= limit:
            return key
    return AGES[-1][1]


@dataclass
class ContractDebt:
    """Долг одного договора на день по одному основанию."""

    contract: Contract
    debt: Decimal
    #: Неоплаченное по возрасту - только положительные остатки.
    aging: dict[str, Decimal] = field(default_factory=dict)
    #: Оплачено вперёд и переплата (положительное число).
    advance: Decimal = _ZERO
    paid: Decimal = _ZERO

    @property
    def owed(self) -> Decimal:
        """Неоплаченное - без вычета аванса: долг = неоплаченное минус аванс."""
        return sum(self.aging.values(), _ZERO)

    def out(self) -> dict[str, Any]:
        return {
            "debt": _money(self.debt),
            "owed": _money(self.owed),
            "aging": {key: _money(value) for key, value in self.aging.items() if value},
            "advance": _money(self.advance) if self.advance else None,
            "paid": _money(self.paid),
        }


def _paid(payments: Iterable[Payment], day: date) -> Decimal:
    return sum((payment.amount for payment in payments if payment.paid_at <= day), _ZERO)


def accruals_debt(money: ContractMoney, *, day: date) -> tuple[Decimal, dict[str, Decimal], Decimal, Decimal] | None:
    """Долг по начислениям, старение, аванс и оплачено; `None` - KORT начислений не считает."""
    if not money.accrues:
        return None
    aging: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    advance = sum((part.amount for part in money.over if part.payment.source != "opening"), _ZERO)
    for key, bucket in money.buckets.items():
        if key != OPENING and bucket.starts > day:
            # Месяц, чей период ещё не начался: оплаченное за него - вперёд.
            advance += sum((part.amount for part in bucket.parts if part.payment.source != "opening"), _ZERO)
            continue
        left = bucket.left
        if left < 0:
            advance += -left
        elif left > 0:
            aging["opening" if key == OPENING else _age_key(max(0, (day - bucket.starts).days))] += left
    return money.debt_on(day), dict(aging), advance, _paid(money.payments, day)


def acts_debt(
    acts: Iterable[Act], opening: Decimal | None, payments: Iterable[Payment], *, day: date, since: date
) -> tuple[Decimal, dict[str, Decimal], Decimal, Decimal] | None:
    """Долг по АВР: сальдо по АВР и акты месяцев учёта до дня, минус оплаты; `None` - ни актов, ни сальдо."""
    first = month_key(since)
    dated = sorted(
        (
            (act.day or month_start(act.month), act.amount)
            for act in acts
            if act.amount is not None and act.month >= first and (act.day or month_start(act.month)) <= day
        ),
        key=lambda item: item[0],
    )
    if not dated and opening is None:
        return None
    paid = _paid(payments, day)
    # Аванс на начало учёта гасит акты, как оплата в первый день.
    credit = paid + (-opening if opening is not None and opening < 0 else _ZERO)
    owed: list[tuple[str, Decimal]] = [("opening", opening)] if opening is not None and opening > 0 else []
    owed.extend((_age_key(max(0, (day - when).days)), amount) for when, amount in dated)
    aging: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for key, amount in owed:
        taken = min(credit, amount) if credit > 0 else _ZERO
        credit -= taken
        if amount - taken > 0:
            aging[key] += amount - taken
    debt = (opening or _ZERO) + sum((amount for _when, amount in dated), _ZERO) - paid
    return debt, dict(aging), max(credit, _ZERO), paid


def fractions(
    session: Session, workspace: Workspace, access: Access, contracts: dict[uuid.UUID, Contract], kind: str
) -> dict[uuid.UUID, list[tuple[str, Decimal]]]:
    """Договор → [(отдел или человек, часть)] с тем, что этому человеку открыто.

    Части в сумме - целое: остаток после долей - `REST` («не распределено»),
    закрытое человеку - `CLOSED`, без отдела (человека) - `NOBODY`. Ими же
    делятся долг (`Debts`) и выручка (`core/revenue.py`).
    """
    model = ContractDepartment if kind == "department" else ContractPerson
    owner_of = (lambda row: row.department_id) if kind == "department" else (lambda row: row.employee_id)
    rows: dict[uuid.UUID, list[Any]] = defaultdict(list)
    ids = list(contracts)
    for start in range(0, len(ids), 500):
        for row in session.scalars(sa.select(model).where(model.contract_id.in_(ids[start : start + 500])).order_by(model.position)):
            rows[row.contract_id].append(row)
    registry: Registry | None = None
    people: dict[uuid.UUID, list[uuid.UUID]] = {}
    co: dict[uuid.UUID, list[uuid.UUID]] = {}
    if kind == "person" and not access.admin:
        registry = Registry(session, workspace)
        people = people_of(session, ids)
        co = shares_module.co_departments(session, ids)
    out: dict[uuid.UUID, list[tuple[str, Decimal]]] = {}
    for contract_id, contract in contracts.items():
        found = rows.get(contract_id, [])
        if not found:
            out[contract_id] = [(NOBODY, _ONE)]
            continue
        alone = found[0] if len(found) == 1 else None
        if alone is not None and ((alone.share_percent is None and alone.share_amount is None) or alone.share_percent == _HUNDRED):
            # Один без доли (или со всей) - договор его целиком, доли тут не видно.
            out[contract_id] = [(str(owner_of(alone)), _ONE)]
            continue
        if kind == "department":
            scope = "all" if shares_module.departments_open(access, contract) else "none"
        elif access.admin:
            scope = "all"
        else:
            scope = shares_module.people_scope(access, contract, registry, people.get(contract_id, []), co.get(contract_id, []))
        parts: list[tuple[str, Decimal]] = []
        given = _ZERO
        for row in found:
            if scope == "own" and owner_of(row) != access.employee_id:
                continue
            if row.share_percent is not None:
                fraction = row.share_percent / _HUNDRED
            elif row.share_amount is not None and contract.amount:
                fraction = row.share_amount / contract.amount
            else:
                continue
            parts.append((str(owner_of(row)), fraction))
            given += fraction
        if scope == "none" or (scope == "own" and not parts):
            out[contract_id] = [(CLOSED, _ONE)]
            continue
        if given < _ONE:
            parts.append((REST if scope == "all" else CLOSED, _ONE - given))
        out[contract_id] = parts
    return out


def group_titles(
    session: Session, workspace: Workspace, group: str, names: dict[uuid.UUID, str], codes: dict[uuid.UUID, str]
) -> dict[str, str]:
    """Ключ группы → подпись: отдел - кодом, человек - именем, сторона - кодом нашего юрлица или именем."""
    if group == "department":
        return {
            str(row.id): row.code or row.title
            for row in session.scalars(sa.select(Department).where(Department.workspace_id == workspace.id))
        }
    if group == "person":
        return {str(row.id): row.full_name for row in session.scalars(sa.select(Employee).where(Employee.workspace_id == workspace.id))}
    # Наше юрлицо - кодом и там, где оно вторая сторона (между своими).
    return {str(key): codes.get(key) or name for key, name in names.items()}


def entity_codes(session: Session, workspace: Workspace) -> dict[uuid.UUID, str]:
    return {
        entity_id: code
        for entity_id, code in session.execute(
            sa.select(GroupEntity.counterparty_id, GroupEntity.code).where(GroupEntity.workspace_id == workspace.id)
        )
        if code
    }


def _chosen(acts: Acts, contract_id: uuid.UUID) -> list[Act]:
    return [slot.chosen for slot in acts.months.get(contract_id, {}).values() if slot.chosen is not None]


def side_of(contract: Contract, is_own) -> str | None:
    """Чей долг по договору: `receivable` - нам должны (исполнитель - наше юрлицо),
    `payable` - мы должны (покупка: заказчик - наше юрлицо), `intra` - обе стороны
    наши; `None` - ни одной нашей стороны."""
    executor = contract.executor_id is not None and is_own(contract.executor_id)
    customer = contract.customer_id is not None and is_own(contract.customer_id)
    if executor and customer:
        return "intra"
    if executor:
        return "receivable"
    if customer:
        return "payable"
    return None


class Debts:
    """Долги видимых договоров компании на день - на запрос.

    `book` - прочитанная сводная таблица (АВР таблицы для долга по АВР) или
    `None`: тогда АВР только KORT и 1С. `side` - чьи долги в группах и итогах
    (`SIDES`); итоги каждой стороны - `sides()`.
    """

    def __init__(
        self,
        session: Session,
        workspace: Workspace,
        access: Access,
        *,
        day: date,
        basis: str = "accruals",
        book: Any | None = None,
        side: str = "receivable",
    ) -> None:
        if basis not in BASES:
            raise ValueError("основание долга - по начислениям или по АВР")
        if side not in SIDES:
            raise ValueError("сторона долга - нам должны, мы должны или между своими")
        from app.finance.core.shadow import _visible

        self.session = session
        self.workspace = workspace
        self.access = access
        self.day = day
        self.basis = basis
        self.side = side
        visible = _visible(session, workspace, access)
        self.money = Money(session, workspace, today=day)
        self.since = self.money.since
        acts = Acts(session, workspace, access, book, today=day) if basis == "acts" else None
        openings = opening_module.current(session, workspace, self.since, "acts") if acts is not None and self.since else {}
        self.names = dict(
            session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace.id)).all()
        )
        self.codes = entity_codes(session, workspace)
        ledger = Ledger(session, workspace.id, visible)
        #: Все стороны: договор → долг; `items` - только выбранной стороны.
        self.every: dict[str, dict[uuid.UUID, ContractDebt]] = {key: {} for key in SIDES}
        #: Договоры, долг которых KORT не знает, по сторонам: начислений не считает
        #: (по начислениям) или нет ни АВР, ни сальдо по АВР (по АВР).
        self.unknowns: dict[str, int] = {key: 0 for key in SIDES}
        for contract in visible:
            side_key = side_of(contract, self.money.registry.is_own)
            if side_key is None:
                continue
            money = self.money.contract(contract, ledger)
            if money.since is None:
                continue
            if acts is None:
                found = accruals_debt(money, day=day)
            else:
                doc = openings.get(contract.id)
                found = acts_debt(
                    _chosen(acts, contract.id), doc.amount if doc is not None else None, money.payments,
                    day=day, since=money.since,
                )
            if found is None:
                self.unknowns[side_key] += 1
                continue
            debt, aging, advance, paid = found
            if not debt and not aging and not advance:
                continue
            self.every[side_key][contract.id] = ContractDebt(contract, debt, aging, advance, paid)
        self.items = self.every[side]
        self.unknown = self.unknowns[side]

    # ── Доли ─────────────────────────────────────────────────────────────────

    def _fractions(self, kind: str) -> dict[uuid.UUID, list[tuple[str, Decimal]]]:
        return fractions(self.session, self.workspace, self.access, {cid: item.contract for cid, item in self.items.items()}, kind)

    # ── Выдача ───────────────────────────────────────────────────────────────

    def _titles(self, group: str) -> dict[str, str]:
        return group_titles(self.session, self.workspace, group, self.names, self.codes)

    def _parties(self, contract: Contract) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        """(вторая сторона, наше юрлицо): у покупки вторая сторона - исполнитель."""
        if self.side == "payable":
            return contract.executor_id, contract.customer_id
        return contract.customer_id, contract.executor_id

    def _label(self, party: uuid.UUID | None) -> str:
        return (self.codes.get(party) or self.names.get(party, "")) if party else ""

    def rows(self, group: str) -> list[dict[str, Any]]:
        """Долги, сгруппированные: вторая сторона, наше юрлицо, отдел, человек."""
        if group not in GROUPS:
            raise ValueError("группа - клиент, юрлицо, отдел или человек")
        titles = self._titles(group)
        special = _SPECIAL[group]
        fractions = self._fractions(group) if group in ("department", "person") else {}
        groups: dict[str, dict[str, Any]] = {}
        for contract_id, item in self.items.items():
            contract = item.contract
            party, ours = self._parties(contract)
            if group == "client":
                owners = [(str(party) if party else NOBODY, _ONE)]
            elif group == "entity":
                owners = [(str(ours) if ours else NOBODY, _ONE)]
            else:
                owners = fractions.get(contract_id, [(NOBODY, _ONE)])
            for owner, fraction in owners:
                entry = groups.get(owner)
                if entry is None:
                    entry = groups[owner] = {
                        "key": owner,
                        "title": special.get(owner) or titles.get(owner) or "-",
                        "special": owner in special,
                        "debt": _ZERO,
                        "owed": _ZERO,
                        "advance": _ZERO,
                        "aging": defaultdict(lambda: _ZERO),
                        "contracts": [],
                    }
                entry["debt"] += item.debt * fraction
                entry["owed"] += item.owed * fraction
                entry["advance"] += item.advance * fraction
                for key, amount in item.aging.items():
                    entry["aging"][key] += amount * fraction
                part = item.out() if fraction == _ONE else ContractDebt(
                    contract,
                    item.debt * fraction,
                    {key: amount * fraction for key, amount in item.aging.items()},
                    item.advance * fraction,
                    item.paid * fraction,
                ).out()
                entry["contracts"].append(
                    {
                        "id": str(contract_id),
                        "number": contract.number,
                        #: Вторая сторона (клиент, у покупки - поставщик) и наше юрлицо.
                        "party": self._label(party),
                        "entity": self._label(ours),
                        #: Доля в процентах, когда в группе не весь договор.
                        "fraction": format((fraction * _HUNDRED).quantize(Decimal("0.01")), "f") if fraction != _ONE else None,
                        "whole": _money(item.debt) if fraction != _ONE else None,
                        **part,
                    }
                )
        out: list[dict[str, Any]] = []
        for entry in groups.values():
            entry["contracts"].sort(
                key=lambda row: (-Decimal(row["owed"] or "0"), -Decimal(row["advance"] or "0"), row["number"])
            )
            out.append(
                {
                    **entry,
                    "debt": _money(entry["debt"]),
                    "owed": _money(entry["owed"]),
                    "advance": _money(entry["advance"]) if entry["advance"] else None,
                    "aging": {key: _money(value) for key, value in entry["aging"].items() if value},
                }
            )
        # Особые («не распределено», «доли закрыты») - в конце, остальные - по
        # неоплаченному, потом по авансу.
        out.sort(key=lambda row: (row["special"], -Decimal(row["owed"]), -Decimal(row["advance"] or "0"), row["title"]))
        return out

    def totals(self) -> dict[str, Any]:
        aging: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        for item in self.items.values():
            for key, amount in item.aging.items():
                aging[key] += amount
        return {
            "debt": _money(sum((item.debt for item in self.items.values()), _ZERO)),
            "owed": _money(sum(aging.values(), _ZERO)),
            "advance": _money(sum((item.advance for item in self.items.values()), _ZERO)),
            "aging": {key: _money(value) for key, value in aging.items() if value},
            # Договоры, по которым должны, - одно число с «Сегодня»; только с авансом - отдельно.
            "contracts": sum(1 for item in self.items.values() if item.owed > 0),
            "advance_contracts": sum(1 for item in self.items.values() if item.advance > 0 and not item.owed),
            "unknown": self.unknown,
        }

    def sides(self) -> dict[str, dict[str, Any]]:
        """Итог каждой стороны - для переключателя «Нам должны · Мы должны · Между своими»."""
        return {
            key: {
                "owed": _money(sum((item.owed for item in items.values()), _ZERO)),
                "advance": _money(sum((item.advance for item in items.values()), _ZERO)),
                "contracts": sum(1 for item in items.values() if item.owed > 0),
                "unknown": self.unknowns[key],
            }
            for key, items in self.every.items()
        }

    def out(self, group: str) -> dict[str, Any]:
        return {
            "as_of": self.day.isoformat(),
            "basis": self.basis,
            "side": self.side,
            "group": group,
            "since": self.since.isoformat() if self.since else None,
            "ages": [{"key": key, "title": title} for key, title in AGE_TITLES.items()],
            "sides": self.sides(),
            "totals": self.totals(),
            "rows": self.rows(group),
        }


# ── Сверка с сальдо книги ────────────────────────────────────────────────────


def _attributed(money: ContractMoney) -> dict[str, Decimal]:
    """Месяц → оплачено за него, как держит книга: по разнесению, не отданное месяцу - по месяцу платежа."""
    first = month_key(money.since) if money.since else ""
    out: dict[str, Decimal] = defaultdict(lambda: _ZERO)

    def by_date(payment: Payment) -> str:
        return max(month_key(payment.paid_at), first)

    for key, bucket in money.buckets.items():
        for part in bucket.parts:
            if part.payment.source == "opening":
                continue
            out[by_date(part.payment) if key == OPENING else key] += part.amount
    for part in money.over:
        if part.payment.source != "opening":
            out[by_date(part.payment)] += part.amount
    return out


def _running(start: Decimal, plus: dict[str, Decimal], minus: dict[str, Decimal], months: list[str]) -> dict[str, Decimal]:
    running = start
    out: dict[str, Decimal] = {}
    for key in sorted(set(plus) | set(minus) | set(months)):
        running += plus.get(key, _ZERO) - minus.get(key, _ZERO)
        out[key] = running
    return out


def closing_accruals(money: ContractMoney, months: list[str]) -> dict[str, Decimal]:
    """Сальдо на конец каждого месяца по начислениям: сальдо начала, начислено и оплачено до месяца включительно."""
    accrued = {key: bucket.accrued for key, bucket in money.buckets.items() if key != OPENING}
    return _running(money.opening or _ZERO, accrued, _attributed(money), months)


def closing_acts(money: ContractMoney, acts: Iterable[Act], opening: Decimal | None, months: list[str]) -> dict[str, Decimal]:
    """Сальдо на конец каждого месяца по АВР: сальдо начала, АВР и оплачено до месяца включительно."""
    first = month_key(money.since) if money.since else ""
    acted: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for act in acts:
        if act.amount is not None and act.month >= first:
            acted[act.month] += act.amount
    return _running(opening or _ZERO, acted, _attributed(money), months)


def reconcile_saldo(
    session: Session, workspace: Workspace, access: Access, book: Any, *, today: date, basis: str = "accruals"
) -> dict[str, Any]:
    """«Сальдо Конец» книги против сальдо KORT по договору и месяцу - с начала учёта до текущего месяца."""
    from app.finance.core.shadow import Reconciler

    if basis not in BASES:
        raise ValueError("основание сальдо - по начислениям или по АВР")
    column = "closing_accruals" if basis == "accruals" else "closing_acts"
    reconciler = Reconciler(session, workspace, access, book)
    money = Money(session, workspace, today=today)
    current = month_key(today)
    first = month_key(money.since) if money.since else ""
    window = [month for month in reconciler.window if first <= month <= current] if first else []
    acts = Acts(session, workspace, access, book, today=today) if basis == "acts" else None
    openings = opening_module.current(session, workspace, money.since, "acts") if acts is not None and money.since else {}
    lines: list[dict[str, Any]] = []
    counts = {"contracts": 0, "same": 0, "months": 0, "months_same": 0}
    contracts_out: dict[str, Any] = {}
    for contract in reconciler.contracts:
        months = reconciler.grouped.get(contract.id, {})
        if not months or not window:
            continue
        item = money.contract(contract, reconciler.ledger)
        if item.since is None:
            continue
        if acts is None:
            if not item.accrues:
                continue
            ours = closing_accruals(item, window)
        else:
            doc = openings.get(contract.id)
            ours = closing_acts(item, _chosen(acts, contract.id), doc.amount if doc is not None else None, window)
        rows: list[dict[str, Any]] = []
        for month in window:
            entry = months.get(month)
            if entry is None:
                continue
            values = [getattr(row, column) for row in entry.rows if getattr(row, column) is not None]
            if not values:
                continue
            theirs = sum(values, _ZERO)
            kort = ours.get(month, _ZERO)
            same = kort == theirs
            counts["months"] += 1
            counts["months_same"] += int(same)
            rows.append({"month": month, "kort": _money(kort), "book": _money(theirs), "same": same})
        if not rows:
            continue
        counts["contracts"] += 1
        if all(row["same"] for row in rows):
            counts["same"] += 1
            continue
        contracts_out[str(contract.id)] = {
            "id": str(contract.id),
            "number": contract.number,
            "customer": reconciler.names.get(contract.customer_id, ""),
            "executor": reconciler.party_label(contract.executor_id),
        }
        last = rows[-1]
        lines.append(
            {
                "contract_id": str(contract.id),
                "months": rows,
                "diff": _money(Decimal(last["kort"]) - Decimal(last["book"])),
                "first": next(row["month"] for row in rows if not row["same"]),
            }
        )
    lines.sort(key=lambda line: -abs(Decimal(line["diff"] or "0")))
    return {"basis": basis, "window": window, "counts": counts, "contracts": contracts_out, "lines": lines}


__all__ = [
    "AGES",
    "AGE_TITLES",
    "BASES",
    "ContractDebt",
    "Debts",
    "GROUPS",
    "SIDES",
    "side_of",
    "accruals_debt",
    "acts_debt",
    "closing_accruals",
    "closing_acts",
    "reconcile_saldo",
]
