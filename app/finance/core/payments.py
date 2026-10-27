"""Оплаты договоров по месяцам: какой месяц чем оплачен и сколько осталось (фаза 4).

Оплаты - факты журнала (`core/journal.py`, вид `payment`). К какому договору
оплата, решено при проведении: операции журнала движения денег -
`contracts/payments.allocate` (сторона, счёт юрлица, номер договора из
источника, решение человека), поступления 1С - `onec/match.resolve` (договор
документа). Источник оплат - один на наше юрлицо (`core/settings.py`): журнал
или 1С. Одна и та же оплата есть и в выписке, и в 1С, и сложить оба источника
значило бы удвоить.

К какому месяцу - здесь, при чтении, ничего не храня:

1. «За какой месяц» из источника - период начисления операции («ИЮНЬ 2026»
   в журнале), у поступления 1С - месяц счёта, который оно оплачивает:
   платёж сначала гасит начисления этих месяцев, старые первыми, но не
   больше начисленного. В месяцах подсказки начислений нет вовсе - платёж
   ложится туда целиком: так его отнёс источник, и расхождение с условиями
   договора видно в этом месяце, а не размазано по чужим. Месяц раньше
   начала учёта оплат - это оплата входящего сальдо.
2. Остаток - самому старому долгу: входящее сальдо договора (`opening` на
   начало учёта оплат), затем начисления по порядку, хоть и будущие - это
   предоплата.
3. Что сверх всех начислений - «сверх начислений»: переплата.

Договор, у которого KORT начислений не считает (сумма условием, не задано,
как начислять, нет суммы), долга не имеет - он неизвестен; его оплаты лежат
по месяцам подсказки, а без неё - по месяцу платежа.

Платежи гасят по дате: пришедший раньше гасит раньше. Начисления раньше
начала учёта оплат (`payments.since`) не ждут оплат в KORT - их долг на этот
день и есть входящее сальдо.

Возвраты клиенту (расход по договору продажи) пока не вычитаются: книга
держит их отдельной колонкой, а журнал - расходом без договора.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts.fields import number_key, party_key
from app.finance.contracts.models import Contract, ContractPayment, CounterpartyName
from app.finance.contracts.service import Access, Actor, Registry, ensure_writable, get_contract, people_of, visible_to
from app.finance.core import journal
from app.finance.core import settings as core_settings
from app.finance.core.accruals import Ledger, month_key, month_start
from app.finance.core.models import Fact
from app.finance.layout import norm
from app.finance.models import Account, Category, Counterparty, Operation, Workspace
from app.finance.onec.match import visual_key
from app.finance.onec.models import OnecBase
from app.finance.service import FinanceError

_ZERO = Decimal("0")
#: Насколько вперёд от сегодня (и от последней оплаты) предоплата может гасить месяцы.
AHEAD_MONTHS = 24
#: Месяц входящего сальдо в разнесении - раньше любого месяца.
OPENING = "opening"
#: Сколько договоров юрлица с той же суммой ещё кандидаты «та же сумма»; больше - не довод.
SAME_SUM_MAX = 3


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _shift(key: str, by: int) -> str:
    index = int(key[:4]) * 12 + int(key[5:7]) - 1 + by
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


@dataclass(frozen=True)
class Payment:
    """Оплата договора из источника - операция журнала или поступление 1С."""

    key: str
    source: str
    contract_id: uuid.UUID
    paid_at: date
    amount: Decimal
    #: «За какие месяцы» по источнику: первый и последний ключ месяца.
    hint: tuple[str, str] | None
    #: Как найден договор: `auto`, `number`, `manual` (журнал); у 1С - `number`, `client`…
    how: str
    payer: str = ""
    comment: str = ""
    contract_ref: str = ""
    account: str = ""
    #: Период словами источника, когда это не месяц («Старые (…)»): не за месяцы учёта.
    label: str = ""

    def out(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source,
            "paid_at": self.paid_at.isoformat(),
            "amount": _money(self.amount),
            "hint": list(self.hint) if self.hint else None,
            "label": self.label,
            "how": self.how,
            "payer": self.payer,
            "comment": self.comment[:200],
            "contract_ref": self.contract_ref,
            "account": self.account,
        }


@dataclass
class Part:
    payment: Payment
    amount: Decimal
    #: `hint` - по «за какой месяц», `order` - самому старому долгу, `date` - по
    #: месяцу платежа (договор без начислений), `over` - сверх начислений,
    #: `old` - за период до учёта, сверх входящего сальдо.
    by: str


@dataclass
class Bucket:
    """Долг, который гасят оплаты: входящее сальдо или начисление месяца."""

    month: str
    accrued: Decimal
    parts: list[Part] = field(default_factory=list)
    #: Срок оплаты - «Период с» начисления (у сальдо и месяца без начисления - нет).
    due: date | None = None
    #: Наше юрлицо месяца - исполнитель на день периода: после перевода клиента
    #: старые месяцы остаются долгом прежнему ТОО (`core/debts.py`).
    executor: uuid.UUID | None = None

    @property
    def paid(self) -> Decimal:
        return sum((part.amount for part in self.parts), _ZERO)

    @property
    def left(self) -> Decimal:
        return self.accrued - self.paid

    @property
    def starts(self) -> date:
        """С какого дня начисление - долг: «Период с» или первое число месяца."""
        return self.due or month_start(self.month)

    def out(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "accrued": _money(self.accrued),
            "paid": _money(self.paid),
            "debt": _money(self.left),
            "parts": [
                {"key": part.payment.key, "paid_at": part.payment.paid_at.isoformat(), "amount": _money(part.amount), "by": part.by}
                for part in self.parts
            ],
        }


@dataclass
class ContractMoney:
    contract_id: uuid.UUID
    since: date | None
    opening: Decimal | None
    buckets: dict[str, Bucket]
    over: list[Part]
    payments: list[Payment]
    #: Считает ли KORT начисления договора; нет - долга не знает.
    accrues: bool = True

    def paid_in(self, month: str) -> Decimal:
        bucket = self.buckets.get(month)
        return bucket.paid if bucket is not None else _ZERO

    @property
    def paid(self) -> Decimal:
        return sum((payment.amount for payment in self.payments), _ZERO)

    def debt_on(self, day: date) -> Decimal:
        """Долг на день: входящее сальдо и начисления с началом периода до дня, минус оплаты до дня."""
        owed = self.opening or _ZERO
        owed += sum(
            (bucket.accrued for key, bucket in self.buckets.items() if key != OPENING and bucket.starts <= day),
            _ZERO,
        )
        return owed - sum((payment.amount for payment in self.payments if payment.paid_at <= day), _ZERO)

    def out(self, *, today: date) -> dict[str, Any]:
        months = [bucket.out() for key, bucket in sorted(self.buckets.items()) if key != OPENING]
        opening = self.buckets.get(OPENING)
        return {
            "since": self.since.isoformat() if self.since else None,
            "today": today.isoformat(),
            "opening": opening.out() if opening is not None else None,
            "months": months,
            "over": [
                {"key": part.payment.key, "paid_at": part.payment.paid_at.isoformat(), "amount": _money(part.amount)}
                for part in self.over
            ],
            "over_total": _money(sum((part.amount for part in self.over), _ZERO)),
            "payments": [payment.out() for payment in sorted(self.payments, key=lambda item: (item.paid_at, item.key), reverse=True)],
            "paid": _money(self.paid),
            "accrues": self.accrues,
            "debt": _money(self.debt_on(today)) if self.accrues else None,
        }


def _hint_of(start: date | None, end: date | None, accrued: date | None) -> tuple[str, str] | None:
    if start is not None:
        return month_key(start), month_key(end or start)
    if accrued is not None:
        return month_key(accrued), month_key(accrued)
    return None


def allocate_months(
    payments: list[Payment], buckets: dict[str, Bucket]
) -> list[Part]:
    """Разнести оплаты одного договора по долгам (см. описание модуля). Возвращает «сверх начислений»."""
    ordered = sorted(payments, key=lambda item: (item.paid_at, item.key))
    left = {payment.key: payment.amount for payment in ordered}
    # Возврат покупателю - оплата со знаком минус: он забирает назад часть
    # прежних оплат, самых поздних до него. Раньше разнесение отбрасывало минус,
    # и переплата, которую клиенту вернули, гасила следующие месяцы (полигон, О6).
    for index, payment in enumerate(ordered):
        if payment.amount >= 0:
            continue
        back = -payment.amount
        left[payment.key] = _ZERO
        for earlier in reversed(ordered[:index]):
            if back <= 0:
                break
            cut = min(back, max(left[earlier.key], _ZERO))
            left[earlier.key] -= cut
            back -= cut
    keys = sorted(buckets, key=lambda key: "" if key == OPENING else key)

    def take(payment: Payment, bucket: Bucket, by: str) -> None:
        amount = min(left[payment.key], bucket.left)
        if amount > 0:
            bucket.parts.append(Part(payment, amount, by))
            left[payment.key] -= amount

    for payment in ordered:
        if payment.hint is None:
            continue
        first, last = payment.hint
        # Месяц без суммы начисления (договор без суммы, условием) - не «месяц с
        # начислением»: иначе подсказка упиралась в ноль и оплата «за июль»
        # уезжала в месяц платежа (TAUSUGUR AGRO на копии, 25.10.2026).
        inside = [key for key in keys if key != OPENING and first <= key <= last and buckets[key].accrued > 0]
        if not inside:
            # В месяцах подсказки начислений нет - платёж там, куда его отнёс источник.
            buckets.setdefault(first, Bucket(first, _ZERO)).parts.append(Part(payment, left[payment.key], "hint"))
            left[payment.key] = _ZERO
            continue
        for key in inside:
            if left[payment.key] <= 0:
                break
            take(payment, buckets[key], "hint")
    for payment in ordered:
        # «Старые (…)» - не за месяцы учёта: гасит только входящее сальдо.
        allowed = [OPENING] if payment.label and payment.hint is None else keys
        for key in allowed:
            if left[payment.key] <= 0:
                break
            if key in buckets:
                take(payment, buckets[key], "order")
    return [
        Part(payment, left[payment.key], "old" if payment.label and payment.hint is None else "over")
        for payment in ordered
        if left[payment.key] > 0
    ]


class Money:
    """Оплаты компании по договорам и месяцам - на запрос.

    `only` - только оплаты этих договоров (регистр пересчитывает несколько):
    годится, когда начало учёта задал человек - иначе оно от первой оплаты
    всей компании, и `only` не действует. У такого `Money` оплаты без договора
    и спорные - не по всей компании: ему - только `contract`.
    """

    def __init__(self, session: Session, workspace: Workspace, *, today: date, only: Iterable[uuid.UUID] | None = None) -> None:
        self.session = session
        self.workspace = workspace
        self.today = today
        self.settings = core_settings.payments(session, workspace)
        scope = list(only) if only is not None and self.settings.since is not None else None
        self.registry = Registry(session, workspace)
        self.contracts = {
            item.id: item
            for item in session.scalars(
                sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
            )
        }
        self.names = dict(
            session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace.id)).all()
        )
        self.accounts = dict(
            session.execute(sa.select(Account.id, Account.name).where(Account.workspace_id == workspace.id)).all()
        )
        self.account_entity = dict(
            session.execute(sa.select(Account.id, Account.group_entity_id).where(Account.workspace_id == workspace.id)).all()
        )
        #: Сколько поступлений «без договора» скрыто и почему - после `without_contract`.
        self.hidden: dict[str, int] = {}
        #: Операции журнала денег, отнесённые к договору, - при любом источнике оплат юрлица.
        self.journal_paid: dict[uuid.UUID, list[Fact]] = defaultdict(list)
        #: Спорная операция → договоры, между которыми спор.
        self.disputed: dict[uuid.UUID, set[uuid.UUID]] = {}
        #: Возвращённые поступления (`contracts/payments.returned_pairs`) и обеспечительные платежи - не оплаты.
        self.returned: set[uuid.UUID] = set()
        self.deposits: set[uuid.UUID] = set()
        self.by_contract: dict[uuid.UUID, list[Payment]] = defaultdict(list)
        wanted = {entity for entity, source in self.settings.sources.items() if source == "1c"}
        self._bases: dict[str, str] | None = None
        first_income: date | None = None
        journal.ensure(session, workspace.id)
        for fact in journal.current(session, workspace.id, ("payment",), contracts=scope):
            data = fact.data or {}
            if fact.source == "journal":
                if data.get("direction") == "in" and (first_income is None or fact.day < first_income):
                    first_income = fact.day
                if data.get("open"):
                    self.disputed[journal.ref_of(fact)] = {uuid.UUID(item) for item in data["open"]}
                if data.get("returned"):
                    self.returned.add(journal.ref_of(fact))
                if data.get("deposit"):
                    self.deposits.add(journal.ref_of(fact))
                if fact.contract_id is None:
                    continue
                self.journal_paid[fact.contract_id].append(fact)
                contract = self.contracts.get(fact.contract_id)
                if contract is None or self.settings.source_of(self.ours(contract)) != "journal":
                    continue
                self.by_contract[fact.contract_id].append(self._from_journal(fact))
            elif fact.source == "1c":
                # Поступления 1С - только у юрлиц, чей источник оплат 1С.
                if (
                    fact.entity_id not in wanted
                    or not data.get("posted")
                    or fact.amount is None
                    or data.get("state") != "ok"
                    or fact.contract_id not in self.contracts
                ):
                    continue
                self.by_contract[fact.contract_id].append(self._from_onec(fact))
        # По первому поступлению вообще, а не первой разнесённой оплате: иначе
        # неразнесённое раньше неё выпадало из «Оплат без договора» как «до учёта».
        firsts = [payment.paid_at for items in self.by_contract.values() for payment in items]
        if first_income is not None:
            firsts.append(first_income)
        first = min(firsts, default=None)
        self.since: date | None = self.settings.since or (date(first.year, first.month, 1) if first else None)
        self.openings = self._openings()
        self._cache: dict[uuid.UUID, ContractMoney] = {}

    # ── Источники ────────────────────────────────────────────────────────────

    def ours(self, contract: Contract) -> uuid.UUID | None:
        if contract.executor_id and self.registry.is_own(contract.executor_id):
            return contract.executor_id
        if contract.customer_id and self.registry.is_own(contract.customer_id):
            return contract.customer_id
        return None

    def _from_journal(self, fact: Fact) -> Payment:
        """Оплата из факта журнала денег: плательщик и счёт - по нынешним именам."""
        data = fact.data or {}
        months = data.get("months")
        account = data.get("account")
        return Payment(
            key=fact.key,
            source="journal",
            contract_id=fact.contract_id,
            paid_at=fact.day,
            amount=fact.amount,
            hint=(months[0], months[1]) if months else None,
            how=data.get("how") or "",
            payer=self.names.get(fact.party_id, "") if fact.party_id else "",
            comment=data.get("comment") or "",
            contract_ref=data.get("contract_ref") or "",
            account=self.accounts.get(uuid.UUID(account), "") if account else "",
            label=data.get("label") or "",
        )

    def _from_onec(self, fact: Fact) -> Payment:
        """Поступление 1С из факта: счёт - название базы, месяц - счёта, который оно оплачивает."""
        if self._bases is None:
            self._bases = {
                str(base_id): title
                for base_id, title in self.session.execute(
                    sa.select(OnecBase.id, OnecBase.title).where(OnecBase.workspace_id == self.workspace.id)
                )
            }
        data = fact.data or {}
        months = data.get("months")
        return Payment(
            key=fact.key,
            source="1c",
            contract_id=fact.contract_id,
            paid_at=fact.day,
            amount=fact.amount,
            hint=(months[0], months[1]) if months else None,
            how=data.get("how") or "",
            payer=data.get("payer") or "",
            comment=data.get("comment") or "",
            contract_ref=data.get("contract_ref") or "",
            account=self._bases.get(data.get("base") or "", ""),
        )

    def _openings(self) -> dict[uuid.UUID, Fact]:
        """Сальдо по начислениям: оплаты гасят его; сальдо по АВР - начало долга по актам (`core/debts.py`)."""
        if self.since is None:
            return {}
        return journal.openings(self.session, self.workspace.id, self.since, "accruals")

    # ── Договор ──────────────────────────────────────────────────────────────

    def _counted(self, payment: Payment) -> bool:
        """Оплата - в учёте KORT на `today`.

        Оплаты раньше начала учёта - до KORT: их место во входящем сальдо. Кроме
        оплаты за месяц учёта: «ИЮНЬ 2026», оплаченный в мае, - июньская
        предоплата. Оплаты позже `today` - после дня, на который считается («на дату»).
        """
        first_key = month_key(self.since) if self.since is not None else ""
        return payment.paid_at <= self.today and (
            self.since is None or payment.paid_at >= self.since or (payment.hint is not None and payment.hint[1] >= first_key)
        )

    def paid_of(self, contract_id: uuid.UUID) -> Decimal:
        """Сколько оплачено по договору в учёте KORT - без раскладки по месяцам («Оплачено» реестра)."""
        return sum((payment.amount for payment in self.by_contract.get(contract_id, []) if self._counted(payment)), _ZERO)

    def contract(self, contract: Contract, ledger: Ledger | None = None) -> ContractMoney:
        if contract.id in self._cache:
            return self._cache[contract.id]
        payments = [payment for payment in self.by_contract.get(contract.id, []) if self._counted(payment)]
        opening_doc = self.openings.get(contract.id)
        opening = opening_doc.amount if opening_doc is not None else None
        buckets: dict[str, Bucket] = {}
        over: list[Part] = []
        if self.since is not None:
            credit: list[Payment] = []
            if opening is not None and opening > 0:
                buckets[OPENING] = Bucket(OPENING, opening)
            elif opening is not None and opening < 0:
                # Аванс на начало учёта гасит начисления, как оплата в первый день.
                credit.append(
                    Payment(
                        key=f"opening:{contract.id}", source="opening", contract_id=contract.id, paid_at=self.since,
                        amount=-opening, hint=None, how="opening",
                    )
                )
            last = max([self.today, *(payment.paid_at for payment in payments)])
            until = month_start(_shift(month_key(last), AHEAD_MONTHS))
            accruals = (ledger or Ledger(self.session, self.workspace.id, [contract])).accruals(
                contract, since=self.since, until=until
            )
            for accrual in accruals:
                amount = _ZERO if accrual.skipped else (accrual.amount or _ZERO)
                bucket = buckets.setdefault(
                    accrual.month, Bucket(accrual.month, _ZERO, due=accrual.period_from, executor=accrual.executor_id)
                )
                bucket.accrued += amount
            if OPENING in buckets:
                # Сальдо на начало учёта - долг тому, кто был исполнителем тогда: первому месяцу учёта.
                first_month = next((key for key in sorted(buckets) if key != OPENING), None)
                buckets[OPENING].executor = buckets[first_month].executor if first_month else contract.executor_id
            accrues = any(bucket.accrued > 0 for key, bucket in buckets.items() if key != OPENING)
            first = month_key(self.since)
            # «За май», когда учёт с июня, - оплата входящего сальдо: её гасит порядок.
            placed = [
                payment if payment.hint is None or payment.hint[1] >= first else replace(payment, hint=None)
                for payment in payments
            ]
            over = allocate_months(credit + placed, buckets)
            if not accrues:
                # Начислений KORT не считает: оплата - в месяце платежа, а не «сверх».
                # «Старые (…)» и аванс на начало - не за месяцы учёта, они остаются в стороне.
                kept: list[Part] = []
                for part in over:
                    if part.payment.source == "opening" or part.by == "old":
                        kept.append(part)
                        continue
                    month = month_key(part.payment.paid_at)
                    buckets.setdefault(month, Bucket(month, _ZERO)).parts.append(Part(part.payment, part.amount, "date"))
                over = [part for part in kept if part.payment.source != "opening"]
            # Будущие месяцы без оплат - не долг и не строка: только то, что начислено к сегодня или оплачено.
            current = month_key(self.today)
            buckets = {
                key: bucket for key, bucket in buckets.items() if key == OPENING or key <= current or bucket.parts
            }
        else:
            accrues = True
            over = [Part(payment, payment.amount, "over") for payment in payments]
        money = ContractMoney(contract.id, self.since, opening, buckets, over, payments, accrues)
        self._cache[contract.id] = money
        return money

    def all(self, contracts: Iterable[Contract]) -> dict[uuid.UUID, ContractMoney]:
        items = list(contracts)
        ledger = Ledger(self.session, self.workspace.id, items)
        return {item.id: self.contract(item, ledger) for item in items}

    # ── Без договора ─────────────────────────────────────────────────────────

    def without_contract(self, *, since: date | None = None) -> list[dict[str, Any]]:
        """Поступления журнала, не отнесённые ни к одному договору, - с кандидатами.

        Не оплаты - и их нет в списке: возвращённые плательщику той же суммой
        (`contracts/payments.returned_pairs`), обеспечительные платежи
        (`is_deposit`), переводы со своих юрлиц (плательщик -
        наше юрлицо или ТОО, на чьё имя наш счёт: «ТОО "Big Business
        Consulting"» на счёт «Kaspi Юр. ТОО "Big Business Consulting"»), статьи
        и плательщики, отмеченные человеком «не оплаты по договорам», решённые
        «не по договору». Сколько скрыто и почему - `hidden`. Спорные - с
        договорами, между которыми спор; остальные - с договорами того же
        клиента, того же номера или той же суммы у этого юрлица, если такие есть.
        """
        from app.finance.core.participants import bare_key, legal_tail

        self.hidden = {"own": 0, "ignored": 0, "decided": 0, "returned": 0, "deposit": 0}
        ignored_categories = self.settings.ignored("categories")
        ignored_payers = self.settings.ignored("payers")
        own_keys = {legal_tail(name) for account_id, name in self.accounts.items() if self.account_entity.get(account_id)}
        own_keys |= {bare_key(self.names.get(party_id, "")) for party_id in self.registry.own}
        own_keys.discard("")
        allocated = {journal.ref_of(fact) for items in self.journal_paid.values() for fact in items}
        disputed = self.disputed
        decided = set(
            self.session.scalars(
                sa.select(ContractPayment.operation_id).where(ContractPayment.workspace_id == self.workspace.id)
            )
        )
        entity_of_account = self.account_entity
        categories = dict(
            self.session.execute(sa.select(Category.id, Category.name).where(Category.workspace_id == self.workspace.id)).all()
        )
        # Без `raw` - строка выписки списку не нужна.
        query = sa.select(Operation).options(sa.orm.defer(Operation.raw)).where(
            Operation.workspace_id == self.workspace.id,
            Operation.deleted_at.is_(None),
            Operation.status == "fact",
            Operation.kind == "income",
        )
        start = since or self.since
        if start is not None:
            query = query.where(Operation.paid_at >= start)
        by_number: dict[str, list[Contract]] = defaultdict(list)
        by_customer: dict[uuid.UUID, list[Contract]] = defaultdict(list)
        for contract in self.contracts.values():
            if contract.number:
                by_number[number_key(contract.number)].append(contract)
            if contract.customer_id:
                by_customer[contract.customer_id].append(contract)
        name_keys: dict[str, set[uuid.UUID]] = defaultdict(set)
        for party_id, name in self.names.items():
            name_keys[party_key(name)].add(party_id)
            name_keys[visual_key(name)].add(party_id)
        # Написания - тоже: банк-посредник, общий для нескольких клиентов, сторону
        # не называет, но кандидатами эти клиенты быть должны.
        for normalized, name, party_id in self.session.execute(
            sa.select(CounterpartyName.normalized, CounterpartyName.name, CounterpartyName.counterparty_id).where(
                CounterpartyName.workspace_id == self.workspace.id
            )
        ):
            name_keys[normalized].add(party_id)
            name_keys[visual_key(name)].add(party_id)
        out: list[dict[str, Any]] = []
        for operation in self.session.scalars(query.order_by(Operation.paid_at.desc(), Operation.id)):
            if operation.id in allocated:
                continue
            if operation.id in decided and operation.id not in disputed:
                self.hidden["decided"] += 1
                continue
            if operation.id in self.returned:
                self.hidden["returned"] += 1
                continue
            if operation.id in self.deposits:
                self.hidden["deposit"] += 1
                continue
            entity = entity_of_account.get(operation.account_to_id) if operation.account_to_id else None
            if entity is not None and self.settings.source_of(entity) != "journal":
                continue
            payer = self.names.get(operation.counterparty_id, "") if operation.counterparty_id else ""
            if (operation.counterparty_id and self.registry.is_own(operation.counterparty_id)) or (
                payer and (legal_tail(payer) in own_keys or bare_key(payer) in own_keys)
            ):
                self.hidden["own"] += 1
                continue
            category = categories.get(operation.category_id, "") if operation.category_id else ""
            if (category and norm(category) in ignored_categories) or (payer and norm(payer) in ignored_payers):
                self.hidden["ignored"] += 1
                continue
            candidates: dict[uuid.UUID, str] = {cid: "спор" for cid in disputed.get(operation.id, set())}
            if not candidates:
                ref = number_key(operation.contract_ref) if operation.contract_ref else ""
                for contract in by_number.get(ref, []) if ref else []:
                    candidates.setdefault(contract.id, "номер")
                same = name_keys.get(party_key(payer), set()) | name_keys.get(visual_key(payer), set()) if payer else set()
                for party_id in same:
                    for contract in by_customer.get(party_id, []):
                        candidates.setdefault(contract.id, "клиент")
                if entity is not None:
                    same_sum = [
                        contract
                        for contract in self.contracts.values()
                        if contract.executor_id == entity
                        and contract.amount is not None
                        and contract.amount == operation.amount_base
                        and contract.customer_id is not None
                    ]
                    # «Та же сумма» у пятидесяти договоров (50 000) ничего не говорит.
                    if len(same_sum) <= SAME_SUM_MAX:
                        for contract in same_sum:
                            candidates.setdefault(contract.id, "сумма")
            ranked = sorted(candidates.items(), key=lambda item: ("спор", "номер", "клиент", "сумма").index(item[1]))[:6]
            out.append(
                {
                    "operation_id": str(operation.id),
                    "paid_at": operation.paid_at.isoformat(),
                    "amount": _money(operation.amount_base),
                    "payer": payer,
                    "account": self.accounts.get(operation.account_to_id, "") if operation.account_to_id else "",
                    "entity_id": str(entity) if entity else None,
                    "category": category,
                    "comment": (operation.comment or "")[:240],
                    "contract_ref": operation.contract_ref or "",
                    "hint": list(_hint_of(operation.period_start, operation.period_end, operation.accrued_at) or ()) or None,
                    "disputed": operation.id in disputed,
                    "candidates": [
                        {
                            "id": str(cid),
                            "number": self.contracts[cid].number,
                            "customer": self.names.get(self.contracts[cid].customer_id, ""),
                            "executor_id": str(self.contracts[cid].executor_id) if self.contracts[cid].executor_id else None,
                            "why": why,
                        }
                        for cid, why in ranked
                        if cid in self.contracts
                    ],
                }
            )
        return out


def _visible(session: Session, workspace: Workspace, access: Access, contracts: Iterable[Contract]) -> list[Contract]:
    items = list(contracts)
    registry = Registry(session, workspace)
    people = people_of(session, [item.id for item in items])
    return [item for item in items if visible_to(item, registry, access, people.get(item.id, []))]


# ── Сверка с книгой ──────────────────────────────────────────────────────────

#: Чем оплаты месяца расходятся с книгой: суммой, есть только в книге, есть только у KORT.
PAID_CODES = ("paid", "paid_no_kort", "paid_no_book")


def _book_paid(month: Any) -> Decimal | None:
    values = [row.paid for row in month.rows if row.paid is not None] if month is not None else []
    return sum(values, _ZERO) if values else None


def reconcile_paid(
    session: Session, workspace: Workspace, access: Access, book: Any, *, today: date, money: Money | None = None,
    entity: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Оплаты KORT против «Сумма Факт Поступ.» книги-сводки - по договору и месяцу.

    Месяц оплат у KORT - куда платёж лёг разнесением (см. описание модуля), у
    книги - строка месяца. Сравнивается и договор целиком за окно книги: месяц
    может разойтись только раскладкой (книга положила двойную оплату в один
    месяц, KORT - в два), а итог по договору - нет; расхождение итога - это
    оплата, которую одна сторона не видит.

    `entity` - только месяцы, где это наше юрлицо исполнитель (как считает «Переход»).
    """
    from app.finance.core.shadow import EntityTally, Reconciler, month_owner

    reconciler = Reconciler(session, workspace, access, book)
    current = month_key(today)
    window = [month for month in reconciler.window if month <= current]
    money = money or Money(session, workspace, today=today)
    contracts_out: dict[str, dict[str, Any]] = {}
    lines: list[dict[str, Any]] = []
    counts = {"contracts": 0, "same": 0, "months": 0, "months_same": 0, **{code: 0 for code in PAID_CODES}}
    tally = EntityTally()
    for contract in reconciler.contracts:
        book_months = reconciler.grouped.get(contract.id, {})
        item = money.contract(contract, reconciler.ledger)
        months: list[dict[str, Any]] = []
        kort_total = book_total = _ZERO
        for month in window:
            kort = item.paid_in(month)
            theirs = _book_paid(book_months.get(month))
            if not kort and not theirs:
                continue
            owner = month_owner(contract, item.buckets.get(month), book_months.get(month))
            if entity is not None and owner != entity:
                continue
            kort_total += kort
            book_total += theirs or _ZERO
            if kort and not theirs:
                code = "paid_no_book"
            elif theirs and not kort:
                code = "paid_no_kort"
            elif kort != theirs:
                code = "paid"
            else:
                code = ""
            counts["months"] += 1
            if code:
                counts[code] += 1
            else:
                counts["months_same"] += 1
            tally.add(owner, contract.id, not code, month)
            months.append({"month": month, "kort": _money(kort), "book": _money(theirs), "code": code})
        if not months:
            continue
        counts["contracts"] += 1
        same = kort_total == book_total
        if same:
            counts["same"] += 1
        if same and not any(row["code"] for row in months):
            continue
        # Совпавшие договоры не отдаются - только числом.
        contracts_out[str(contract.id)] = {
            "id": str(contract.id),
            "number": contract.number,
            "customer": reconciler.names.get(contract.customer_id, ""),
            "executor": reconciler.party_label(contract.executor_id),
            "kort": _money(kort_total),
            "book": _money(book_total),
            "same": same,
            "opening_paid": _money(item.buckets[OPENING].paid) if OPENING in item.buckets else None,
            "over": _money(sum((part.amount for part in item.over), _ZERO)) if item.over else None,
        }
        lines.append({"contract_id": str(contract.id), "months": months, "diff": _money(kort_total - book_total)})
    lines.sort(key=lambda line: -abs(Decimal(line["diff"] or "0")))
    return {
        "window": window,
        "since": money.since.isoformat() if money.since else None,
        "counts": counts,
        "contracts": contracts_out,
        "lines": lines,
        #: По нашим юрлицам месяца - для «Перехода».
        "by_entity": tally.out(),
    }


def _check_money(access: Access) -> None:
    if not access.view or "paid" in access.hidden:
        raise PermissionError("Оплаты по договорам вам не открыты - нужен доступ к журналу")


def contract_money(session: Session, workspace: Workspace, access: Access, contract: Contract, *, today: date) -> dict[str, Any]:
    """Оплаты договора по месяцам - для ленты в карточке."""
    _check_money(access)
    money = Money(session, workspace, today=today)
    return money.contract(contract).out(today=today)


def decide(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    operation_id: uuid.UUID,
    *,
    action: str,
    contract_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Решение по поступлению из списка «без договора»: к договору, «не по договору», снять решение."""
    _check_money(access)
    if not access.edit:
        raise PermissionError("Разносить оплаты вам не открыто")
    if action not in ("link", "none", "auto"):
        raise FinanceError("Ждём «link», «none» или «auto»")
    operation = session.get(Operation, operation_id)
    if operation is None or operation.workspace_id != workspace.id or operation.deleted_at is not None:
        raise FinanceError("Такой операции в журнале нет")
    if operation.kind not in ("income", "expense"):
        raise FinanceError("Перевод между своими счетами - не оплата по договору")
    registry = Registry(session, workspace)
    contract = None
    if action == "link":
        if contract_id is None:
            raise FinanceError("К какому договору - не сказано")
        contract = get_contract(session, workspace, contract_id)
        ensure_writable(session, registry, access, contract)
    row = session.scalar(
        sa.select(ContractPayment).where(
            ContractPayment.workspace_id == workspace.id, ContractPayment.operation_id == operation_id
        )
    )
    before = {"contract_id": str(row.contract_id) if row and row.contract_id else None, "decided": row is not None}
    if action == "auto":
        if row is not None:
            session.delete(row)
    else:
        target = contract.id if contract is not None else None
        if row is None:
            session.add(
                ContractPayment(
                    workspace_id=workspace.id, operation_id=operation_id, contract_id=target, created_by=actor.user_id
                )
            )
        else:
            row.contract_id, row.created_by = target, actor.user_id
    session.flush()
    words = {
        "link": f"к договору {contract.number or 'без номера'}" if contract is not None else "",
        "none": "не по договору",
        "auto": "разнесение снова по выписке",
    }
    # Не `entity="operation"`: такую запись журнал отменял бы как правку полей операции.
    history.write(
        session,
        workspace,
        kind=f"core.payment.{action}",
        entity="payment",
        entity_id=operation.id,
        title=f"оплата {operation.paid_at:%d.%m.%Y} на {_money(operation.amount_base)} - {words[action]}",
        before=before,
        after={"action": action, "contract_id": str(contract.id) if contract is not None else None},
        actor=actor.email,
    )
    return {"operation_id": str(operation.id), "action": action, "contract_id": str(contract.id) if contract else None}


__all__ = [
    "AHEAD_MONTHS",
    "Bucket",
    "ContractMoney",
    "Money",
    "OPENING",
    "Part",
    "Payment",
    "allocate_months",
    "contract_money",
    "decide",
]
