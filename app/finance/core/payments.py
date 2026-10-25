"""Оплаты договоров по месяцам: какой месяц чем оплачен и сколько осталось (фаза 4).

К какому договору оплата - уже решено в двух местах: операции журнала
движения денег - `contracts/payments.allocate` (сторона, счёт юрлица, номер
договора из источника, решение человека), поступления 1С -
`onec/match.resolve` (договор документа). Источник оплат - один на наше
юрлицо (`core/settings.py`): журнал или 1С. Одна и та же оплата есть и в
выписке, и в 1С, и сложить оба источника значило бы удвоить.

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
from app.finance.contracts import payments as contract_payments
from app.finance.contracts.fields import number_key, party_key
from app.finance.contracts.models import Contract, ContractPayment, CounterpartyName
from app.finance.contracts.service import Access, Actor, Registry, ensure_writable, get_contract, people_of, visible_to
from app.finance.core import settings as core_settings
from app.finance.core.accruals import Ledger, month_key, month_start
from app.finance.core.models import Document
from app.finance.layout import norm
from app.finance.models import Account, Category, Counterparty, Operation, Workspace
from app.finance.onec import match as onec_match
from app.finance.onec.match import visual_key
from app.finance.onec.models import OnecBase, OnecDocument
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
    """Оплаты компании по договорам и месяцам - на запрос."""

    def __init__(self, session: Session, workspace: Workspace, *, today: date) -> None:
        self.session = session
        self.workspace = workspace
        self.today = today
        self.settings = core_settings.payments(session, workspace)
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
        self.allocation = contract_payments.allocate(session, workspace, self.registry, self.contracts.values())
        self.by_contract: dict[uuid.UUID, list[Payment]] = defaultdict(list)
        for contract_id, items in self.allocation.paid.items():
            contract = self.contracts.get(contract_id)
            if contract is None or self.settings.source_of(self.ours(contract)) != "journal":
                continue
            for operation, how in items:
                self.by_contract[contract_id].append(self._from_operation(operation, contract_id, how))
        for payment in self._onec():
            self.by_contract[payment.contract_id].append(payment)
        # По первому поступлению вообще, а не первой разнесённой оплате: иначе
        # неразнесённое раньше неё выпадало из «Оплат без договора» как «до учёта».
        firsts = [payment.paid_at for items in self.by_contract.values() for payment in items]
        first_income = session.scalar(
            sa.select(sa.func.min(Operation.paid_at)).where(
                Operation.workspace_id == workspace.id,
                Operation.deleted_at.is_(None),
                Operation.status == "fact",
                Operation.kind == "income",
            )
        )
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

    def _from_operation(self, operation: Operation, contract_id: uuid.UUID, how: str) -> Payment:
        account = operation.account_to_id if operation.kind == "income" else operation.account_from_id
        return Payment(
            key=f"op:{operation.id}",
            source="journal",
            contract_id=contract_id,
            paid_at=operation.paid_at,
            amount=operation.amount_base,
            hint=_hint_of(operation.period_start, operation.period_end, operation.accrued_at),
            how=how,
            payer=self.names.get(operation.counterparty_id, "") if operation.counterparty_id else "",
            comment=operation.comment or "",
            contract_ref=operation.contract_ref or "",
            account=self.accounts.get(account, "") if account else "",
            label=operation.period_label or "",
        )

    def _onec(self) -> list[Payment]:
        """Поступления 1С юрлиц, у которых источник оплат - 1С."""
        wanted = {entity for entity, source in self.settings.sources.items() if source == "1c"}
        if not wanted:
            return []
        bases = {
            base.id: base
            for base in self.session.scalars(
                sa.select(OnecBase).where(
                    OnecBase.workspace_id == self.workspace.id,
                    OnecBase.group_entity_id.in_(wanted),
                    OnecBase.state == "active",
                )
            )
        }
        if not bases:
            return []
        docs = list(
            self.session.scalars(
                sa.select(OnecDocument).where(
                    OnecDocument.base_id.in_(list(bases)),
                    OnecDocument.kind.in_(("payment", "invoice")),
                    OnecDocument.deleted.is_(False),
                    OnecDocument.gone.is_(False),
                )
            )
        )
        resolved = onec_match.resolve(docs, bases, onec_match.build_index(self.session, self.workspace.id))
        invoices = {(doc.base_id, doc.ref): doc for doc in docs if doc.kind == "invoice"}
        out: list[Payment] = []
        for doc in docs:
            if doc.kind != "payment" or not doc.posted or doc.amount is None:
                continue
            found = resolved.matches.get(doc.id)
            if found is None or found.state != "ok" or found.contract_id not in self.contracts:
                continue
            invoice = invoices.get((doc.base_id, doc.basis_ref)) if doc.basis_ref else None
            month = onec_match.month_key(invoice.period_from or invoice.doc_date) if invoice is not None else ""
            out.append(
                Payment(
                    key=f"1c:{doc.id}",
                    source="1c",
                    contract_id=found.contract_id,
                    paid_at=doc.doc_date,
                    amount=doc.amount,
                    hint=(month, month) if month else None,
                    how=found.how,
                    payer=doc.party_name,
                    comment=doc.content or "",
                    contract_ref=doc.contract_number or "",
                    account=bases[doc.base_id].title,
                )
            )
        return out

    def _openings(self) -> dict[uuid.UUID, Document]:
        """Сальдо по начислениям: оплаты гасят его; сальдо по АВР - начало долга по актам (`core/debts.py`)."""
        from app.finance.core import opening as opening_module

        if self.since is None:
            return {}
        return opening_module.current(self.session, self.workspace, self.since, "accruals")

    # ── Договор ──────────────────────────────────────────────────────────────

    def contract(self, contract: Contract, ledger: Ledger | None = None) -> ContractMoney:
        if contract.id in self._cache:
            return self._cache[contract.id]
        # Оплаты раньше начала учёта - до KORT: их место во входящем сальдо. Кроме
        # оплаты за месяц учёта: «ИЮНЬ 2026», оплаченный в мае, - июньская предоплата.
        # Оплаты позже `today` - после дня, на который считается («на дату»).
        first_key = month_key(self.since) if self.since is not None else ""
        payments = [
            payment
            for payment in self.by_contract.get(contract.id, [])
            if payment.paid_at <= self.today
            and (self.since is None or payment.paid_at >= self.since or (payment.hint is not None and payment.hint[1] >= first_key))
        ]
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
            schedule = (ledger or Ledger(self.session, self.workspace.id, [contract])).schedule(
                contract, since=self.since, until=until
            )
            for accrual in schedule.accruals:
                amount = _ZERO if accrual.skipped else (accrual.amount or _ZERO)
                bucket = buckets.setdefault(accrual.month, Bucket(accrual.month, _ZERO, due=accrual.period_from))
                bucket.accrued += amount
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

        Не оплаты - и их нет в списке: переводы со своих юрлиц (плательщик -
        наше юрлицо или ТОО, на чьё имя наш счёт: «ТОО "Big Business
        Consulting"» на счёт «Kaspi Юр. ТОО "Big Business Consulting"»), статьи
        и плательщики, отмеченные человеком «не оплаты по договорам», решённые
        «не по договору». Сколько скрыто и почему - `hidden`. Спорные - с
        договорами, между которыми спор; остальные - с договорами того же
        клиента, того же номера или той же суммы у этого юрлица, если такие есть.
        """
        from app.finance.core.participants import bare_key, legal_tail

        self.hidden = {"own": 0, "ignored": 0, "decided": 0}
        ignored_categories = self.settings.ignored("categories")
        ignored_payers = self.settings.ignored("payers")
        own_keys = {legal_tail(name) for account_id, name in self.accounts.items() if self.account_entity.get(account_id)}
        own_keys |= {bare_key(self.names.get(party_id, "")) for party_id in self.registry.own}
        own_keys.discard("")
        allocated = {operation.id for items in self.allocation.paid.values() for operation, _how in items}
        disputed: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        for contract_id, items in self.allocation.open.items():
            for operation in items:
                disputed[operation.id].add(contract_id)
        decided = set(
            self.session.scalars(
                sa.select(ContractPayment.operation_id).where(ContractPayment.workspace_id == self.workspace.id)
            )
        )
        entity_of_account = self.account_entity
        categories = dict(
            self.session.execute(sa.select(Category.id, Category.name).where(Category.workspace_id == self.workspace.id)).all()
        )
        query = sa.select(Operation).where(
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
        for operation in self.session.scalars(query.order_by(Operation.paid_at.desc())):
            if operation.id in allocated:
                continue
            if operation.id in decided and operation.id not in disputed:
                self.hidden["decided"] += 1
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
    session: Session, workspace: Workspace, access: Access, book: Any, *, today: date, money: Money | None = None
) -> dict[str, Any]:
    """Оплаты KORT против «Сумма Факт Поступ.» книги-сводки - по договору и месяцу.

    Месяц оплат у KORT - куда платёж лёг разнесением (см. описание модуля), у
    книги - строка месяца. Сравнивается и договор целиком за окно книги: месяц
    может разойтись только раскладкой (книга положила двойную оплату в один
    месяц, KORT - в два), а итог по договору - нет; расхождение итога - это
    оплата, которую одна сторона не видит.
    """
    from app.finance.core.shadow import Reconciler

    reconciler = Reconciler(session, workspace, access, book)
    current = month_key(today)
    window = [month for month in reconciler.window if month <= current]
    money = money or Money(session, workspace, today=today)
    contracts_out: dict[str, dict[str, Any]] = {}
    lines: list[dict[str, Any]] = []
    counts = {"contracts": 0, "same": 0, "months": 0, "months_same": 0, **{code: 0 for code in PAID_CODES}}
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
