"""АВР и ЭСФ в ядре: документ месяца договора из KORT, 1С и книги (фаза 5).

Акт выполненных работ (АВР) и электронный счёт-фактура (ЭСФ) - события месяца
договора, как счёт и оплата. Где они сейчас: в 1С юрлица (реализация с датой
подписания покупателем, счёт-фактура, ЭСФ со статусом ИС ЭСФ), в книге-сводке
(«АВР (наша)», «Сумма (наша)», №, дата, «АВР (клиент принял)», «ЭСФ (отпр.)»),
а позже - в самом KORT (документы `act` и `esf`, источник KORT).

Один АВР на договор и месяц - по приоритету источника: KORT, потом 1С, потом
книга. Остальные источники - тень: у месяца АВР есть в 1С, а книга о нём не
знает, или суммы, подписание, ЭСФ разные - строка сверки. Сверка 1С с книгой
идёт только у юрлиц, чья база 1С подключена: где 1С нет, книга - единственный
источник, и сравнивать её не с чем.

Ничего не хранится: месяц АВР 1С считает `onec/match` (реализация - месяц
отчётного периода, счёт-фактура и ЭСФ - за своей реализацией), месяц книги -
её строка.

Из того же - три списка работы: АВР не подписан клиентом; АВР без ЭСФ дольше
срока выписки ЭСФ (`ESF_DAYS` календарных дней после оборота - срок НК РК);
месяц начислен, а АВР нет ни в одном источнике.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts.models import Contract
from app.finance.contracts.service import Access
from app.finance.core.accruals import month_key, month_start
from app.finance.core.models import Document
from app.finance.models import Workspace
from app.finance.onec import match as onec_match
from app.finance.onec.models import OnecBase, OnecDocument
from app.finance.onec.service import short_number

#: Порядок источников АВР: первый найденный - правда месяца.
SOURCES = ("kort", "1c", "book")
SOURCE_TITLES = {"kort": "KORT", "1c": "1С", "book": "книга"}
#: Срок выписки ЭСФ после даты оборота, календарных дней (НК РК).
ESF_DAYS = 15
#: Чем АВР 1С и книги расходятся.
CODES = ("act_no_book", "act_no_1c", "act_amount", "act_signed", "esf")
CODE_TITLES = {
    "act_no_book": "в 1С АВР есть, в книге нет",
    "act_no_1c": "в книге АВР есть, в 1С нет",
    "act_amount": "сумма АВР другая",
    "act_signed": "подписание разное",
    "esf": "ЭСФ в одном источнике",
}

_ZERO = Decimal("0")


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _day(value: date | None) -> str | None:
    return value.isoformat() if value else None


@dataclass
class Act:
    """АВР месяца договора в одном источнике."""

    source: str
    month: str
    number: str = ""
    day: date | None = None
    amount: Decimal | None = None
    #: Клиент принял: дата подписания (1С) или дата «клиент принял» (книга).
    signed: date | None = None
    #: Принят, но дата неизвестна (галочка книги без даты).
    accepted: bool = False
    esf: date | None = None
    #: ЭСФ отправлен, но дата неизвестна (галочка книги без даты).
    esf_sent: bool = False
    esf_state: str = ""
    #: Где лежит: id документа 1С или KORT, строка книги.
    ref: str = ""

    @property
    def is_signed(self) -> bool:
        return self.signed is not None or self.accepted

    @property
    def has_esf(self) -> bool:
        return self.esf is not None or self.esf_sent

    def out(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "month": self.month,
            "number": self.number,
            "date": _day(self.day),
            "amount": _money(self.amount),
            "signed": _day(self.signed),
            "accepted": self.is_signed,
            "esf": _day(self.esf),
            "esf_sent": self.has_esf,
            "esf_state": self.esf_state,
            "ref": self.ref,
        }


@dataclass
class MonthActs:
    month: str
    found: dict[str, Act] = field(default_factory=dict)

    @property
    def chosen(self) -> Act | None:
        return next((self.found[source] for source in SOURCES if source in self.found), None)

    def codes(self) -> list[str]:
        ours, theirs = self.found.get("1c"), self.found.get("book")
        if ours is None and theirs is None:
            return []
        if theirs is None:
            return ["act_no_book"]
        if ours is None:
            return ["act_no_1c"]
        out: list[str] = []
        if ours.amount is not None and theirs.amount is not None and ours.amount != theirs.amount:
            out.append("act_amount")
        if ours.is_signed != theirs.is_signed:
            out.append("act_signed")
        if ours.has_esf != theirs.has_esf:
            out.append("esf")
        return out


class Acts:
    """АВР договоров компании по месяцам - на запрос.

    `book` - прочитанная книга-сводка или `None` (тогда только KORT и 1С);
    `only` - один договор (лента в карточке): строки книги сопоставляются только
    его, а не все полторы тысячи на каждое открытие карточки.
    """

    def __init__(
        self, session: Session, workspace: Workspace, access: Access, book: Any | None, *, today: date, only: Contract | None = None
    ) -> None:
        self.session = session
        self.workspace = workspace
        self.today = today
        self.months: dict[uuid.UUID, dict[str, MonthActs]] = defaultdict(dict)
        #: Наши юрлица, у которых подключена база 1С: у них 1С сверяется с книгой.
        self.onec_entities: set[uuid.UUID] = set()
        self.executors: dict[uuid.UUID, uuid.UUID | None] = dict(
            session.execute(sa.select(Contract.id, Contract.executor_id).where(Contract.workspace_id == workspace.id)).all()
        )
        self.reconciler = None
        self._kort()
        self._onec()
        if book is not None:
            self._book(access, book, only)

    def _slot(self, contract_id: uuid.UUID, month: str) -> MonthActs:
        months = self.months[contract_id]
        if month not in months:
            months[month] = MonthActs(month)
        return months[month]

    def _kort(self) -> None:
        esf: dict[tuple[uuid.UUID, str], Document] = {}
        acts: list[Document] = []
        for doc in self.session.scalars(
            sa.select(Document).where(
                Document.workspace_id == self.workspace.id,
                Document.kind.in_(("act", "esf")),
                Document.source == "kort",
                Document.deleted_at.is_(None),
                Document.contract_id.is_not(None),
                Document.period.is_not(None),
            )
        ):
            if doc.kind == "esf":
                esf[(doc.contract_id, month_key(doc.period))] = doc
            else:
                acts.append(doc)
        for doc in acts:
            month = month_key(doc.period)
            paper = esf.get((doc.contract_id, month))
            signed = (doc.meta or {}).get("signed")
            self._slot(doc.contract_id, month).found["kort"] = Act(
                source="kort", month=month, number=doc.number, day=doc.issued_on, amount=doc.amount,
                signed=date.fromisoformat(signed) if signed else None,
                esf=paper.issued_on if paper is not None else None, ref=str(doc.id),
            )

    def _onec(self) -> None:
        bases = {
            base.id: base
            for base in self.session.scalars(
                sa.select(OnecBase).where(OnecBase.workspace_id == self.workspace.id, OnecBase.state == "active")
            )
        }
        self.onec_entities = {base.group_entity_id for base in bases.values() if base.group_entity_id}
        if not bases:
            return
        docs = list(
            self.session.scalars(
                sa.select(OnecDocument).where(
                    OnecDocument.base_id.in_(list(bases)),
                    OnecDocument.kind.in_(("sale", "sf", "esf")),
                    OnecDocument.deleted.is_(False),
                    OnecDocument.gone.is_(False),
                )
            )
        )
        resolved = onec_match.resolve(docs, bases, onec_match.build_index(self.session, self.workspace.id))
        # ЭСФ - за своим счётом-фактурой, счёт-фактура - за реализацией.
        by_ref = {(doc.base_id, doc.kind, doc.ref): doc for doc in docs}
        paper: dict[uuid.UUID, OnecDocument] = {}
        for doc in docs:
            if doc.kind == "sf" and doc.basis_ref:
                sale = by_ref.get((doc.base_id, "sale", doc.basis_ref))
                if sale is not None:
                    paper.setdefault(sale.id, doc)
        for doc in docs:
            if doc.kind == "esf" and doc.basis_ref:
                invoice = by_ref.get((doc.base_id, "sf", doc.basis_ref))
                sale = by_ref.get((doc.base_id, "sale", invoice.basis_ref)) if invoice is not None and invoice.basis_ref else None
                if sale is not None:
                    paper[sale.id] = doc
        for doc in docs:
            if doc.kind != "sale" or not doc.posted:
                continue
            found = resolved.matches.get(doc.id)
            month = resolved.months.get(doc.id, "")
            if found is None or found.state != "ok" or found.contract_id is None or not month:
                continue
            slot = self._slot(found.contract_id, month)
            if "1c" in slot.found:
                # Два АВР за месяц - сумма обоих; номер и дата - первого.
                act = slot.found["1c"]
                act.amount = (act.amount or _ZERO) + (doc.amount or _ZERO)
                continue
            esf = paper.get(doc.id)
            slot.found["1c"] = Act(
                source="1c", month=month, number=short_number(doc.number), day=doc.doc_date, amount=doc.amount,
                signed=doc.signed_at, esf=esf.doc_date if esf is not None else None,
                esf_state=(esf.status or "") if esf is not None else "", ref=str(doc.id),
            )

    def _book(self, access: Access, book: Any, only: Contract | None) -> None:
        from app.finance.core.shadow import Reconciler

        self.reconciler = Reconciler(self.session, self.workspace, access, book, only=only)
        for contract_id, months in self.reconciler.grouped.items():
            for month, entry in months.items():
                row = next((item for item in entry.rows if item.act_no or item.act_state == "yes" or item.act_sum), None)
                if row is None:
                    continue
                self._slot(contract_id, month).found["book"] = Act(
                    source="book", month=month, number=row.act_no, day=row.act_date, amount=row.act_sum,
                    signed=row.accepted_date, accepted=row.accepted_state == "yes",
                    esf=row.esf_date, esf_sent=row.esf_state == "yes", ref=str(row.line),
                )

    # ── Договор ──────────────────────────────────────────────────────────────

    def contract(self, contract_id: uuid.UUID, *, money: bool = True) -> dict[str, dict[str, Any]]:
        """Месяц → АВР договора: выбранный по приоритету, остальные источники и расхождения.

        `money=False` - без суммы АВР: тем, кому деньги договора не открыты.
        """
        compare = self.executors.get(contract_id) in self.onec_entities
        out: dict[str, dict[str, Any]] = {}
        for month, slot in sorted(self.months.get(contract_id, {}).items()):
            act = slot.chosen.out() if slot.chosen is not None else None
            if act is not None and not money:
                act["amount"] = None
            out[month] = {
                "act": act,
                "sources": [source for source in SOURCES if source in slot.found],
                "codes": slot.codes() if compare else [],
            }
        return out

    # ── Сверка и списки ──────────────────────────────────────────────────────

    def reconcile(self, contracts: dict[uuid.UUID, Contract], *, window: list[str]) -> dict[str, Any]:
        """1С против книги по договору и месяцу - у юрлиц с подключённой 1С."""
        lines: list[dict[str, Any]] = []
        counts = {"months": 0, "same": 0, **{code: 0 for code in CODES}}
        for contract_id, months in self.months.items():
            contract = contracts.get(contract_id)
            if contract is None or contract.executor_id not in self.onec_entities:
                continue
            for month, slot in months.items():
                if window and month not in window:
                    continue
                codes = slot.codes()
                counts["months"] += 1
                if not codes:
                    counts["same"] += 1
                    continue
                for code in codes:
                    counts[code] += 1
                lines.append(
                    {
                        "contract_id": str(contract_id),
                        "month": month,
                        "codes": codes,
                        "onec": slot.found["1c"].out() if "1c" in slot.found else None,
                        "book": slot.found["book"].out() if "book" in slot.found else None,
                    }
                )
        lines.sort(key=lambda line: (line["month"], line["contract_id"]), reverse=True)
        return {"counts": counts, "lines": lines}

    def worklists(self, contracts: dict[uuid.UUID, Contract], accrued: dict[uuid.UUID, set[str]]) -> dict[str, list[dict[str, Any]]]:
        """Не подписаны, без ЭСФ после срока, месяц начислен - АВР нет.

        `accrued` - договор → месяцы с начислением (до прошлого месяца
        включительно): месяц, в котором АВР ещё рано, в список не идёт.
        """
        unsigned: list[dict[str, Any]] = []
        no_esf: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        esf_due = self.today - timedelta(days=ESF_DAYS)
        for contract_id, months in self.months.items():
            if contract_id not in contracts:
                continue
            for month, slot in months.items():
                act = slot.chosen
                if act is None:
                    continue
                if not act.is_signed and act.day is not None and act.day <= self.today:
                    unsigned.append({"contract_id": str(contract_id), "act": act.out(), "days": (self.today - act.day).days})
                if not act.has_esf and act.day is not None and act.day < esf_due:
                    no_esf.append({"contract_id": str(contract_id), "act": act.out(), "days": (self.today - act.day).days - ESF_DAYS})
        for contract_id, months in accrued.items():
            have = self.months.get(contract_id, {})
            for month in sorted(months):
                if month not in have or have[month].chosen is None:
                    missing.append({"contract_id": str(contract_id), "month": month})
        unsigned.sort(key=lambda item: -item["days"])
        no_esf.sort(key=lambda item: -item["days"])
        missing.sort(key=lambda item: (item["month"], item["contract_id"]), reverse=True)
        return {"unsigned": unsigned, "no_esf": no_esf, "missing": missing}


def accrued_months(ledger: Any, contracts: list[Contract], *, since: date, until: date) -> dict[uuid.UUID, set[str]]:
    """Договор → месяцы с начислением больше нуля (`core/accruals.py`)."""
    out: dict[uuid.UUID, set[str]] = {}
    for contract in contracts:
        schedule = ledger.schedule(contract, since=since, until=until)
        months = {item.month for item in schedule.accruals if not item.skipped and (item.amount or _ZERO) > 0}
        if months:
            out[contract.id] = months
    return out


def previous_month(today: date) -> date:
    first = date(today.year, today.month, 1)
    return month_start(month_key(first - timedelta(days=1)))


__all__ = [
    "Act",
    "Acts",
    "CODES",
    "CODE_TITLES",
    "ESF_DAYS",
    "MonthActs",
    "SOURCES",
    "SOURCE_TITLES",
    "accrued_months",
    "previous_month",
]
