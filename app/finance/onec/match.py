"""К какому договору реестра и к какому месяцу относится документ 1С.

Считается при показе и не хранится - как оплаты по выписке
(`contracts/payments.py`): поменяли номер или сторону договора в реестре -
ответ меняется сразу. Хранится только решение человека
(`OnecDocument.manual*`).

Договор
───────
База привязана к нашему юрлицу, поэтому кандидаты - договоры, где это юрлицо
исполнитель. Дальше - по правилу проекта «не угадывать»:

1. Решение человека - главнее всего.
2. Счёт-фактура и ЭСФ идут за своей реализацией, поступление - за счётом из
   расшифровки платежа: бухгалтер уже связал их в 1С.
3. Номер договора 1С совпал с номером в реестре и клиент тот же (ключ имени,
   подтверждённое написание или БИН) - договор найден. Тот же номер, а клиент
   записан иначе - допускается то, что люди делают с именами, когда номер уже
   сузил выбор: одно имя внутри другого (от пяти знаков), латиница вместо
   кириллицы («Завод ATEX» и «Завод "АТЕХ"»), пояснение в скобках, одна-две
   опечатки («INDUSTRIALL RECYCLING»). Номер есть только у другого клиента -
   не наш договор, «номер у другого клиента».
4. Номера нет («Основной договор») - договор клиента, если он у юрлица один;
   несколько - тот, чей срок содержит дату документа (как в выписке: месяц до
   начала, два после окончания); не решилось - спорный.
5. Номер в 1С есть, а в реестре у этого клиента другой - не угадываем, даже
   если договор у клиента один: показываем его кандидатом, решает человек.
   Исключение - разница только в разделителях («ОБОиНО-62» и «ОБОиНО/62»)
   у того же клиента: это тот же номер, записанный другой рукой.
6. Того же номера с тем же клиентом нет у юрлица базы, но есть у другого
   нашего юрлица - «договор записан на другое юрлицо»: расхождение реестра и
   1С, решает человек.

«й» и «ё» в ключах сравниваются как «и» и «е»: в реестре BBC часть имён
записана с «й», разложенной на «и» и надстрочный знак, ключ стороны знак
выбрасывает, и «Теплобетонстрой» реестра не совпадал с «Теплобетонстрой» 1С.

Месяц
─────
Месяц услуги, а не дата бумаги: счёт за октябрь BBC выставляет 29 сентября.
Счёт - по периоду «за период с … по …» в содержании, иначе по дате; АВР - по
отчётному периоду; счёт-фактура и ЭСФ - по своей реализации; поступление -
по счёту, который оно оплачивает, иначе по дате платежа.
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts.fields import number_key, party_key
from app.finance.contracts.models import Contract, CounterpartyName, GroupEntity
from app.finance.models import Counterparty
from app.finance.onec.models import OnecBase, OnecDocument
from app.finance.onec.sync import PURCHASE_KINDS

#: Короче - не ищем одно имя внутри другого: «ТОО А» нашлось бы везде.
MIN_CONTAINED = 5
WINDOW_BEFORE = timedelta(days=31)
WINDOW_AFTER = timedelta(days=62)

_LETTERS = str.maketrans({"й": "и", "ё": "е"})
#: Латинские буквы, неотличимые от кириллицы, - в кириллицу (только для
#: сравнения при совпавшем номере: «ATEX» → «атех»).
_LOOKALIKE = str.maketrans("abcehkmoptxy", "авсенкмортху")
_PARENS = re.compile(r"\([^)]*\)")
_FORMS = re.compile(r"^(тоо|ип|ао|ооо|чк|кх)")
_NUMBER_JUNK = re.compile(r"[^0-9a-zа-я]+")


def fold(key: str) -> str:
    """Ключ стороны без разницы «й/и», «ё/е» (см. описание модуля)."""
    return key.translate(_LETTERS)


@lru_cache(maxsize=65536)
def side_key(name: str) -> str:
    # Одно имя - в сотнях документов и операций: ключ - один раз.
    return fold(party_key(unicodedata.normalize("NFC", name or "")))


def loose_key(name: str) -> str:
    """Имя для сравнения при совпавшем номере: без скобок, формы и с кириллицей вместо латиницы-двойника."""
    key = side_key(_PARENS.sub(" ", unicodedata.normalize("NFC", name or "")))
    return _FORMS.sub("", key).translate(_LOOKALIKE)


def visual_key(name: str) -> str:
    """Ключ стороны, в котором латиница-двойник - кириллица: «ТОО ECO Bazar» = «ТОО ЕСО Bazar».

    Не нечёткое сравнение: это одна и та же строка на глаз, банк просто набрал
    её другой раскладкой. Форма и всё остальное имя остаются, как в `side_key`.
    """
    return side_key(name).translate(_LOOKALIKE)


@lru_cache(maxsize=65536)
def number_loose(number: str) -> str:
    """Номер без разделителей: «ОБОиНО-62» = «ОБОиНО/62»."""
    return _NUMBER_JUNK.sub("", number_key(number))


def _distance(a: str, b: str, limit: int) -> int:
    """Опечаток между строками (замена, вставка, пропуск, перестановка соседних)."""
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        best = current[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                value = min(value, previous2[j - 2] + 1)
            current[j] = value
            best = min(best, value)
        if best > limit:
            return limit + 1
        previous2, previous = previous, current
    return previous[len(b)]


def _near(a: str, b: str) -> bool:
    """Одно имя внутри другого (от пяти знаков) или одна-две опечатки."""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    if len(short) >= MIN_CONTAINED and short in long:
        return True
    if len(short) < MIN_CONTAINED:
        return False
    limit = 1 if len(long) < 12 else 2
    return abs(len(a) - len(b)) <= limit and _distance(a, b, limit) <= limit


@dataclass(frozen=True)
class Match:
    """Что известно о договоре документа.

    `state`: `ok` - найден (`how`: `manual`, `basis`, `invoice`, `number`,
    `client`, `window`); `none` - человек решил «не по договору»;
    `other_client` - номер есть только у другого клиента; `ambiguous` -
    подходят несколько; `missing` - у клиента нет договора с этим юрлицом;
    `other_entity` - договор с тем же номером и клиентом записан на другое
    наше юрлицо; `no_entity` - база не привязана к юрлицу реестра.
    """

    state: str
    contract_id: uuid.UUID | None = None
    how: str = ""
    candidates: tuple[uuid.UUID, ...] = ()


@dataclass
class ContractInfo:
    id: uuid.UUID
    executor_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    number: str
    number_key: str
    signed_at: date | None
    end_date: date | None
    amount: Decimal | None

    def holds(self, day: date) -> bool:
        if self.signed_at is not None and day < self.signed_at - WINDOW_BEFORE:
            return False
        if self.end_date is not None and day > self.end_date + WINDOW_AFTER:
            return False
        return True


@dataclass
class Index:
    """Договоры и стороны компании - один раз на запрос."""

    contracts: dict[uuid.UUID, ContractInfo] = field(default_factory=dict)
    by_executor: dict[uuid.UUID, list[ContractInfo]] = field(default_factory=dict)
    #: Договоры, где сторона - заказчик: у нашего юрлица это покупки.
    by_customer: dict[uuid.UUID, list[ContractInfo]] = field(default_factory=dict)
    party_keys: dict[uuid.UUID, set[str]] = field(default_factory=dict)
    #: Ключи для сравнения при совпавшем номере (`loose_key`).
    party_loose: dict[uuid.UUID, set[str]] = field(default_factory=dict)
    party_bins: dict[uuid.UUID, set[str]] = field(default_factory=dict)
    names: dict[uuid.UUID, str] = field(default_factory=dict)


def build_index(session: Session, workspace_id: uuid.UUID) -> Index:
    index = Index()
    rows = session.execute(
        sa.select(
            Contract.id, Contract.executor_id, Contract.customer_id, Contract.number, Contract.number_key,
            Contract.signed_at, Contract.end_date, Contract.amount,
        ).where(Contract.workspace_id == workspace_id, Contract.deleted_at.is_(None))
    ).all()
    from app.finance.contracts.payments import former_executors

    # Документы прежнего ТОО - тоже документы договора: клиента перевели на
    # другое наше юрлицо с даты, а его акты до неё остались в базе прежнего.
    former = former_executors(session, workspace_id)
    for row in rows:
        info = ContractInfo(*row)
        index.contracts[info.id] = info
        for owner in {info.executor_id, *former.get(info.id, ())} - {None}:
            index.by_executor.setdefault(owner, []).append(info)
        if info.customer_id is not None:
            index.by_customer.setdefault(info.customer_id, []).append(info)
    party_ids = {info.customer_id for info in index.contracts.values() if info.customer_id} | {
        info.executor_id for info in index.contracts.values() if info.executor_id
    }
    if party_ids:
        for party in session.scalars(sa.select(Counterparty).where(Counterparty.id.in_(party_ids))):
            index.names[party.id] = party.name
            index.party_keys.setdefault(party.id, set()).add(side_key(party.name))
            index.party_loose.setdefault(party.id, set()).add(loose_key(party.name))
            # БИН - колонка участника (ядро, 24.10.2026); `details.bin` - прежнее место.
            bin_value = str(party.bin or (party.details or {}).get("bin") or "").strip()
            if bin_value:
                index.party_bins.setdefault(party.id, set()).add(bin_value)
        for party_id, normalized, name in session.execute(
            sa.select(CounterpartyName.counterparty_id, CounterpartyName.normalized, CounterpartyName.name).where(
                CounterpartyName.workspace_id == workspace_id, CounterpartyName.counterparty_id.in_(party_ids)
            )
        ):
            index.party_keys.setdefault(party_id, set()).update({fold(normalized), side_key(name)})
            index.party_loose.setdefault(party_id, set()).add(loose_key(name))
        for party_id, bin_value in session.execute(
            sa.select(GroupEntity.counterparty_id, GroupEntity.bin).where(GroupEntity.workspace_id == workspace_id)
        ):
            if bin_value and bin_value.strip():
                index.party_bins.setdefault(party_id, set()).add(bin_value.strip())
    for keys in (*index.party_keys.values(), *index.party_loose.values()):
        keys.discard("")
    return index


def doc_keys(doc: OnecDocument) -> set[str]:
    return {key for key in (side_key(doc.party_name), side_key(doc.party_full_name)) if key}


def same_client(doc: OnecDocument, party_id: uuid.UUID | None, index: Index) -> bool:
    if party_id is None:
        return False
    if doc_keys(doc) & index.party_keys.get(party_id, set()):
        return True
    return bool(doc.party_bin) and doc.party_bin in index.party_bins.get(party_id, set())


def loose_client(doc: OnecDocument, party_id: uuid.UUID | None, index: Index) -> bool:
    """Тот же клиент, записанный иначе (пункт 3) - только когда номер уже совпал."""
    if same_client(doc, party_id, index):
        return True
    if party_id is None:
        return False
    mine = {key for key in (loose_key(doc.party_name), loose_key(doc.party_full_name)) if key}
    return any(_near(a, b) for a in mine for b in index.party_loose.get(party_id, set()))


def _narrow(candidates: list[ContractInfo], doc: OnecDocument) -> list[ContractInfo]:
    if len(candidates) < 2:
        return candidates
    fitting = [item for item in candidates if item.holds(doc.doc_date)] or candidates
    if len(fitting) == 1:
        return fitting
    if doc.amount is not None:
        exact = [item for item in fitting if item.amount == doc.amount]
        if len(exact) == 1:
            return exact
    dated = [item for item in fitting if item.signed_at is not None]
    return dated if len(dated) == 1 else fitting


def _elsewhere(doc: OnecDocument, entity_id: uuid.UUID, index: Index) -> list[ContractInfo]:
    """Тот же номер с тем же клиентом у другого нашего юрлица (пункт 6)."""
    wanted = number_loose(doc.contract_number)
    return [
        item
        for executor_id, items in index.by_executor.items()
        if executor_id != entity_id
        for item in items
        if number_loose(item.number) == wanted and loose_client(doc, item.customer_id, index)
    ]


def match_own(doc: OnecDocument, entity_id: uuid.UUID | None, index: Index) -> Match:
    """Договор по самому документу - номер договора 1С и клиент (пункты 3-5).

    Покупка (акт поставщика, оплата ему): наше юрлицо - заказчик договора, а
    «клиент» документа - исполнитель.
    """
    if entity_id is None:
        return Match("no_entity")
    if doc.kind in PURCHASE_KINDS:
        return _match_purchase(doc, entity_id, index)
    mine = index.by_executor.get(entity_id, [])
    key = number_key(doc.contract_number)
    if key:
        same_number = [item for item in mine if item.number_key == key]
        if same_number:
            strict = _narrow([item for item in same_number if same_client(doc, item.customer_id, index)], doc)
            if len(strict) == 1:
                return Match("ok", strict[0].id, "number")
            if len(strict) > 1:
                return Match("ambiguous", candidates=tuple(item.id for item in strict))
            loose = _narrow([item for item in same_number if loose_client(doc, item.customer_id, index)], doc)
            if len(loose) == 1:
                return Match("ok", loose[0].id, "number")
            if len(loose) > 1:
                return Match("ambiguous", candidates=tuple(item.id for item in loose))
            return Match("other_client", candidates=tuple(item.id for item in same_number))
    clients = [item for item in mine if same_client(doc, item.customer_id, index)]
    if key:
        # Тот же номер другой рукой («ОБОиНО-62» и «ОБОиНО/62») у того же клиента.
        spelled = [item for item in clients if number_loose(item.number) == number_loose(doc.contract_number)]
        if len(spelled) == 1:
            return Match("ok", spelled[0].id, "number")
        elsewhere = _elsewhere(doc, entity_id, index)
        if elsewhere:
            return Match("other_entity", candidates=tuple(item.id for item in elsewhere))
        # Номер в 1С есть, а у клиента в реестре другой - решает человек.
        return Match("missing", candidates=tuple(item.id for item in clients))
    if not clients:
        return Match("missing")
    chosen = _narrow(clients, doc)
    if len(chosen) == 1:
        return Match("ok", chosen[0].id, "client" if len(clients) == 1 else "window")
    return Match("ambiguous", candidates=tuple(item.id for item in chosen))


def _match_purchase(doc: OnecDocument, entity_id: uuid.UUID, index: Index) -> Match:
    """Договор покупки: у нашего юрлица-заказчика, исполнитель - контрагент документа."""
    mine = [item for item in index.by_customer.get(entity_id, []) if same_client(doc, item.executor_id, index)]
    key = number_key(doc.contract_number)
    if key:
        mine = [item for item in mine if item.number_key == key or number_loose(item.number) == number_loose(doc.contract_number)]
    if not mine:
        return Match("missing")
    chosen = _narrow(mine, doc)
    if len(chosen) == 1:
        return Match("ok", chosen[0].id, "number" if key else "client")
    return Match("ambiguous", candidates=tuple(item.id for item in chosen))


@dataclass
class Resolved:
    """Документы базы с договорами и месяцами - ответ сопоставления."""

    matches: dict[uuid.UUID, Match] = field(default_factory=dict)
    months: dict[uuid.UUID, str] = field(default_factory=dict)


def month_key(day: date | None) -> str:
    return f"{day:%Y-%m}" if day else ""


def resolve(documents: Iterable[OnecDocument], bases: dict[uuid.UUID, OnecBase], index: Index) -> Resolved:
    """Договор и месяц каждого документа (см. описание модуля)."""
    docs = list(documents)
    by_ref: dict[tuple[uuid.UUID, str, str], OnecDocument] = {(doc.base_id, doc.kind, doc.ref): doc for doc in docs}
    out = Resolved()

    def entity_of(doc: OnecDocument) -> uuid.UUID | None:
        base = bases.get(doc.base_id)
        return base.group_entity_id if base is not None else None

    def manual(doc: OnecDocument) -> Match | None:
        if doc.manual == "link" and doc.manual_contract_id in index.contracts:
            return Match("ok", doc.manual_contract_id, "manual")
        if doc.manual == "none":
            return Match("none", how="manual")
        return None

    def basis(doc: OnecDocument) -> OnecDocument | None:
        if not doc.basis_ref:
            return None
        kind = {"sf": "sale", "esf": "sf", "payment": "invoice"}.get(doc.kind)
        return by_ref.get((doc.base_id, kind, doc.basis_ref)) if kind else None

    def match_of(doc: OnecDocument, depth: int = 0) -> Match:
        if doc.id in out.matches:
            return out.matches[doc.id]
        found = manual(doc)
        if found is None and depth < 3:
            parent = basis(doc)
            if parent is not None:
                inherited = match_of(parent, depth + 1)
                if inherited.state == "ok":
                    found = Match("ok", inherited.contract_id, "invoice" if doc.kind == "payment" else "basis")
        if found is None:
            found = match_own(doc, entity_of(doc), index)
        out.matches[doc.id] = found
        return found

    def month_of(doc: OnecDocument, depth: int = 0) -> str:
        if doc.id in out.months:
            return out.months[doc.id]
        value = ""
        if doc.kind in ("invoice", "sale", "purchase"):
            value = month_key(doc.period_from or doc.doc_date)
        elif depth < 3:
            parent = basis(doc)
            value = month_of(parent, depth + 1) if parent is not None else ""
        value = value or month_key(doc.doc_date)
        out.months[doc.id] = value
        return value

    for doc in docs:
        match_of(doc)
        month_of(doc)
    return out


__all__ = ["Index", "Match", "Resolved", "build_index", "match_own", "month_key", "resolve", "same_client", "visual_key"]
