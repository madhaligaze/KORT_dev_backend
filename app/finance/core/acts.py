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

АВР и ЭСФ KORT и 1С - факты журнала (`core/journal.py`): месяц АВР 1С при
проведении считает `onec/match` (реализация - месяц отчётного периода,
счёт-фактура и ЭСФ - за своей реализацией). АВР книги - её строка, читается
при запросе: таблица в журнал пока не проводится.

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
from app.finance.core import journal
from app.finance.core.accruals import month_key, month_start
from app.finance.core.models import Fact
from app.finance.models import Workspace
from app.finance.onec.models import OnecBase

#: Порядок источников АВР: первый найденный - правда месяца.
SOURCES = ("kort", "1c", "book")
SOURCE_TITLES = {"kort": "KORT", "1c": "1С", "book": "таблица"}
#: Срок выписки ЭСФ после даты оборота, календарных дней (НК РК).
ESF_DAYS = 15
#: Чем АВР 1С и книги расходятся.
CODES = ("act_no_book", "act_no_1c", "act_amount", "act_signed", "esf")
CODE_TITLES = {
    "act_no_book": "в 1С АВР есть, в таблице нет",
    "act_no_1c": "в таблице АВР есть, в 1С нет",
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
        bases = {
            base.id: base
            for base in session.scalars(
                sa.select(OnecBase).where(OnecBase.workspace_id == workspace.id, OnecBase.state == "active")
            )
        }
        self.onec_entities = {base.group_entity_id for base in bases.values() if base.group_entity_id}
        journal.ensure(session, workspace.id)
        facts = journal.current(session, workspace.id, ("act", "esf"), sources=("kort", "1c"))
        # Документы ядра - записанные в KORT; документы 1С - из баз (ключ `1c:`).
        self._kort([fact for fact in facts if fact.source == "kort" and fact.key.startswith("doc:")])
        self._onec([fact for fact in facts if fact.key.startswith("1c:")])
        if book is not None:
            self._book(access, book, only)

    def _slot(self, contract_id: uuid.UUID, month: str) -> MonthActs:
        months = self.months[contract_id]
        if month not in months:
            months[month] = MonthActs(month)
        return months[month]

    def _kort(self, facts: list[Fact]) -> None:
        esf: dict[tuple[uuid.UUID, str], Fact] = {}
        acts: list[Fact] = []
        for fact in facts:
            if fact.contract_id is None or fact.period is None:
                continue
            if fact.kind == "esf":
                esf[(fact.contract_id, month_key(fact.period))] = fact
            else:
                acts.append(fact)
        for fact in acts:
            month = month_key(fact.period)
            paper = esf.get((fact.contract_id, month))
            data = fact.data or {}
            signed = (data.get("meta") or {}).get("signed")
            self._slot(fact.contract_id, month).found["kort"] = Act(
                source="kort", month=month, number=data.get("number") or "", day=fact.day, amount=fact.amount,
                signed=date.fromisoformat(signed) if signed else None,
                esf=paper.day if paper is not None else None, ref=str(journal.ref_of(fact)),
            )

    def _onec(self, facts: list[Fact]) -> None:
        # ЭСФ - за своим АВР (`act` - ключ реализации): ЭСФ сильнее
        # счёта-фактуры, из нескольких счетов-фактур - первый, из ЭСФ - последний.
        ordered = sorted(facts, key=lambda fact: (fact.day or date.min, fact.key))
        paper: dict[str, Fact] = {}
        for fact in ordered:
            act_key = (fact.data or {}).get("act")
            if fact.kind == "esf" and act_key and (fact.data or {}).get("form") == "sf":
                paper.setdefault(act_key, fact)
        for fact in ordered:
            act_key = (fact.data or {}).get("act")
            if fact.kind == "esf" and act_key and (fact.data or {}).get("form") == "esf":
                paper[act_key] = fact
        for fact in ordered:
            data = fact.data or {}
            if fact.kind != "act" or not data.get("posted"):
                continue
            if data.get("state") != "ok" or fact.contract_id is None or fact.period is None:
                continue
            month = month_key(fact.period)
            slot = self._slot(fact.contract_id, month)
            if "1c" in slot.found:
                # Два АВР за месяц - сумма обоих; номер и дата - первого.
                act = slot.found["1c"]
                act.amount = (act.amount or _ZERO) + (fact.amount or _ZERO)
                continue
            esf = paper.get(fact.key)
            signed = data.get("signed")
            slot.found["1c"] = Act(
                source="1c", month=month, number=data.get("number") or "", day=fact.day, amount=fact.amount,
                signed=date.fromisoformat(signed) if signed else None, esf=esf.day if esf is not None else None,
                esf_state=((esf.data or {}).get("status") or "") if esf is not None else "", ref=str(journal.ref_of(fact)),
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

    def reconcile(
        self, contracts: dict[uuid.UUID, Contract], *, window: list[str], entity: uuid.UUID | None = None
    ) -> dict[str, Any]:
        """1С против таблицы по договору и месяцу - у юрлиц с подключённой 1С.

        Юрлицо месяца - «Наша Фирма» строки таблицы, иначе исполнитель договора:
        месяцы до перевода клиента сверяются с 1С прежнего ТОО. `entity` -
        только месяцы этого юрлица (как считает «Переход»).
        """
        from app.finance.core.shadow import EntityTally, month_owner

        lines: list[dict[str, Any]] = []
        counts = {"months": 0, "same": 0, **{code: 0 for code in CODES}}
        tally = EntityTally()
        grouped = self.reconciler.grouped if self.reconciler is not None else {}
        for contract_id, months in self.months.items():
            contract = contracts.get(contract_id)
            if contract is None:
                continue
            for month, slot in months.items():
                if window and month not in window:
                    continue
                owner = month_owner(contract, None, grouped.get(contract_id, {}).get(month))
                if owner not in self.onec_entities or (entity is not None and owner != entity):
                    continue
                codes = slot.codes()
                counts["months"] += 1
                tally.add(owner, contract_id, not codes, month)
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
        #: `by_entity` - по нашим юрлицам месяца, для «Перехода».
        return {"counts": counts, "lines": lines, "by_entity": tally.out()}

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
        months = {item.month for item in ledger.accruals(contract, since=since, until=until) if not item.skipped and (item.amount or _ZERO) > 0}
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
