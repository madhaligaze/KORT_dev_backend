"""Вход из 1С (шаг 4 этапа 2): компания с 1С - от подключения до «Нам должны».

До этого шага 1С только сверялась: документы базы сопоставлялись с
договорами, заведёнными из таблиц, а БИН клиента предлагался по одному. У
компании без таблиц договоров нет - и 1С ей ничего не давала. Здесь из одной
базы (и ОСВ по 1210 файлом) KORT заводит модель шагами, как «Первые шаги»
сводной таблицы (`core/onboarding.py`): каждый шаг до записи показывает
числа, пачка договоров и сальдо возвращается точкой восстановления.

1. **Наше юрлицо** - организация базы по БИН; нет такого юрлица - заводится.
2. **Участники** - контрагенты документов базы. Один БИН - один участник, все
   написания карточек 1С становятся его написаниями (полигон, У1, У5, У6). Нет
   участника с этим БИН, но есть ровно один без БИН с таким же написанием -
   ему записывается БИН (У3). Иначе - новый участник с БИН. Два участника под
   одно написание - не трогаем, это решает человек.
3. **Договоры** - договоры документов, которых в KORT нет. Условия - по
   актам: месяцами - «в месяц» с самой частой суммой (опечатка в одном акте -
   Д6 - сумму не сдвигает), сумма, державшаяся два месяца и дольше, -
   соглашение «сумма с даты» (Д5); один акт или разовые акты на «Основной
   договор» - разовый договор на каждый акт (Д15). Месяцы без актов внутри
   ряда - пауза, предложением (Д8); акты кончились раньше прошлого месяца -
   вопрос «договор закончился?» (Д9): без ответа такой договор не заводится,
   иначе он начислял бы месяцы, которых не было.
4. **Начало учёта и сальдо** - с первого числа периода ОСВ; сальдо договора -
   дебет минус кредит на начало (аванс со знаком минус, С3).
5. **Оплаты** - поступления 1С становятся источником оплат юрлица.

Слов BBC модуль не знает: всё - из документов 1С и стандартного отчёта.
"""
from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts import restore
from app.finance.contracts import setup as contracts_setup
from app.finance.contracts.fields import number_key, party_key
from app.finance.contracts.models import Contract, GroupEntity
from app.finance.contracts.service import Access, Actor, Registry
from app.finance.core import bin as bins
from app.finance.core import opening as opening_module
from app.finance.core import settings as core_settings
from app.finance.core.accruals import month_key, month_start
from app.finance.models import Counterparty, Workspace
from app.finance.onec import match as onec_match
from app.finance.onec.models import OnecBase, OnecDocument
from app.finance.onec.osv import Osv
from app.finance.service import FinanceError

_ZERO = Decimal("0")
#: Подпись договора 1С по умолчанию - без номера.
MAIN = "Основной договор"
#: Сколько месяцев подряд должна держаться новая сумма, чтобы стать «с даты».
STEADY = 2
#: Пометки карточек 1С, которые не имя клиента: «Таза Су (не использовать)».
_STALE = ("не использовать", "не исп.", "удален", "удалён", "старый", "дубль")


class StartError(FinanceError):
    """Шаг «из 1С» нельзя сделать - с текстом для человека."""


def _require(access: Access) -> None:
    if not access.setup:
        raise PermissionError("Вход из 1С проходит владелец или администратор")


def _money(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _month_end(day: date) -> date:
    following = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
    return following - timedelta(days=1)


def _next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


# ── Участники ────────────────────────────────────────────────────────────────


@dataclass
class Party:
    """Контрагент базы: одна запись на БИН (или на карточку 1С без годного БИН)."""

    key: str
    bin: str
    names: Counter = field(default_factory=Counter)
    full_names: Counter = field(default_factory=Counter)
    refs: set[str] = field(default_factory=set)
    #: Что сделает шаг: same | alias | bin | create | dispute.
    action: str = ""
    party_id: uuid.UUID | None = None
    note: str = ""

    @property
    def name(self) -> str:
        """Имя для KORT: самое частое написание без пометок «не использовать»."""
        clean = [name for name, _count in self.names.most_common() if not any(mark in name.lower() for mark in _STALE)]
        return clean[0] if clean else self.names.most_common(1)[0][0]

    @property
    def full_name(self) -> str:
        return self.full_names.most_common(1)[0][0] if self.full_names else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "bin": self.bin, "name": self.name, "names": [name for name, _ in self.names.most_common()],
            "full_name": self.full_name, "action": self.action, "party_id": str(self.party_id) if self.party_id else None,
            "note": self.note,
        }


# ── Договоры ─────────────────────────────────────────────────────────────────


@dataclass
class Act:
    number: str
    day: date
    start: date | None
    end: date | None
    amount: Decimal

    @property
    def monthly(self) -> bool:
        if not self.start or not self.end:
            return False
        return self.start.day == 1 and self.end == _month_end(self.start)


@dataclass
class Draft:
    """Договор из 1С, которого нет в KORT: что о нём знают документы и что заведём."""

    key: str
    party_key: str
    number: str
    signed: date | None
    acts: list[Act] = field(default_factory=list)
    docs: int = 0
    billing: str = ""
    amount: Decimal | None = None
    billing_from: date | None = None
    #: Смены суммы: (с какого числа, сумма) - соглашения «сумма с даты».
    changes: list[tuple[date, Decimal]] = field(default_factory=list)
    #: Месяцы без актов внутри ряда: (с, по) - пауза предложением.
    pauses: list[tuple[date, date]] = field(default_factory=list)
    #: Акты кончились раньше прошлого месяца: последний день последнего акта.
    last_day: date | None = None
    outliers: list[str] = field(default_factory=list)
    #: Разовый договор одного акта «Основного договора».
    act: Act | None = None
    contract_id: uuid.UUID | None = None

    @property
    def label(self) -> str:
        if self.act is not None:
            return f"по акту № {self.act.number} от {self.act.day:%d.%m.%Y}"
        return self.number or MAIN

    def to_dict(self, parties: dict[str, Party]) -> dict[str, Any]:
        party = parties.get(self.party_key)
        return {
            "key": self.key,
            "label": self.label,
            "number": self.number,
            "customer": party.name if party else "",
            "signed": self.signed.isoformat() if self.signed else None,
            "billing": self.billing,
            "amount": _money(self.amount),
            "billing_from": self.billing_from.isoformat() if self.billing_from else None,
            "changes": [{"from": day.isoformat(), "amount": _money(amount)} for day, amount in self.changes],
            "pauses": [{"from": start.isoformat(), "to": end.isoformat()} for start, end in self.pauses],
            "last_day": self.last_day.isoformat() if self.last_day else None,
            "outliers": self.outliers,
            "acts": len(self.acts),
            "contract_id": str(self.contract_id) if self.contract_id else None,
        }


def _runs(months: list[tuple[date, Decimal]]) -> tuple[list[tuple[date, Decimal]], list[str]]:
    """Ряд сумм по месяцам → (с какого месяца, сумма) без одиночных выбросов.

    Месяц, чья сумма не такая, как у обоих соседей, а соседи между собой
    равны, - опечатка акта, а не новая сумма (Д6): условия она не меняет и
    остаётся расхождением документа.
    """
    outliers: list[str] = []
    clean: list[tuple[date, Decimal]] = []
    for index, (month, amount) in enumerate(months):
        before = months[index - 1][1] if index > 0 else None
        after = months[index + 1][1] if index + 1 < len(months) else None
        if before is not None and after is not None and before == after and amount != before:
            outliers.append(f"{month:%m.%Y}: акт на {format(amount, 'f')} при {format(before, 'f')} до и после")
            continue
        clean.append((month, amount))
    runs: list[tuple[date, Decimal, int]] = []
    for month, amount in clean:
        if runs and runs[-1][1] == amount:
            runs[-1] = (runs[-1][0], amount, runs[-1][2] + 1)
        else:
            runs.append((month, amount, 1))
    # Сумма, продержавшаяся меньше `STEADY` месяцев не в начале ряда, - не смена,
    # а случайность; она растворяется в соседней.
    steady: list[tuple[date, Decimal]] = []
    for index, (month, amount, count) in enumerate(runs):
        if index and count < STEADY:
            continue
        if steady and steady[-1][1] == amount:
            continue
        steady.append((month, amount))
    return steady, outliers


def shape(draft: Draft, today: date) -> None:
    """Условия договора по его актам: как начислять, сумма, смены суммы, паузы, конец."""
    acts = sorted(draft.acts, key=lambda act: (act.start or act.day, act.day))
    monthly = [act for act in acts if act.monthly]
    if len(monthly) >= 2:
        by_month: dict[date, Decimal] = {}
        for act in monthly:
            by_month[act.start] = by_month.get(act.start, _ZERO) + act.amount
        months = sorted(by_month.items())
        runs, draft.outliers = _runs(months)
        draft.billing = "month"
        draft.billing_from = months[0][0]
        draft.amount = runs[0][1]
        draft.changes = runs[1:]
        present = {month for month, _ in months}
        cursor = months[0][0]
        gap_start: date | None = None
        while cursor <= months[-1][0]:
            if cursor not in present and gap_start is None:
                gap_start = cursor
            if cursor in present and gap_start is not None:
                draft.pauses.append((gap_start, cursor - timedelta(days=1)))
                gap_start = None
            cursor = _next_month(cursor)
        # Акт месяца выписывают в его конце: акта прошлого месяца ещё может не
        # быть, а позапрошлого - уже должен быть. Последний акт раньше - ряд
        # кончился, и KORT спросит, закончился ли договор.
        due = month_start(month_key(today))
        due = date(due.year - (due.month == 1), (due.month - 2) % 12 + 1, 1)
        if months[-1][0] < due:
            draft.last_day = _month_end(months[-1][0])
        return
    draft.billing = "total"
    draft.amount = sum((act.amount for act in acts), _ZERO) if acts else None
    first = acts[0] if acts else None
    draft.billing_from = (first.start or first.day) if first else None


# ── Источник: документы базы ─────────────────────────────────────────────────


@dataclass
class Source:
    """Что база 1С знает о юрлице: участники, договоры и их документы."""

    base: OnecBase
    entity_id: uuid.UUID | None
    parties: dict[str, Party]
    drafts: dict[str, Draft]
    matched: dict[str, uuid.UUID]


def _party_key_of(doc: OnecDocument) -> str:
    number = bins.digits(doc.party_bin)
    return f"bin:{number}" if bins.checksum_ok(number) else f"ref:{doc.party_ref or party_key(doc.party_name)}"


def read_source(session: Session, workspace: Workspace, base: OnecBase, *, today: date) -> Source:
    docs = list(
        session.scalars(
            sa.select(OnecDocument).where(
                OnecDocument.base_id == base.id, OnecDocument.gone.is_(False), OnecDocument.deleted.is_(False)
            ).order_by(OnecDocument.doc_date, OnecDocument.ref)
        )
    )
    entity_id = base.group_entity_id
    index = onec_match.build_index(session, workspace.id)
    parties: dict[str, Party] = {}
    by_ref: dict[str, str] = {}
    for doc in docs:
        if not doc.party_ref and not doc.party_name:
            continue
        key = _party_key_of(doc)
        party = parties.setdefault(key, Party(key=key, bin=key[4:] if key.startswith("bin:") else ""))
        if doc.party_name:
            party.names[doc.party_name.strip()] += 1
        if doc.party_full_name:
            party.full_names[doc.party_full_name.strip()] += 1
        if doc.party_ref:
            party.refs.add(doc.party_ref)
            by_ref[doc.party_ref] = key
    # Договор документа: ключ 1С, у «Основного договора» - вместе с клиентом.
    drafts: dict[str, Draft] = {}
    matched: dict[str, uuid.UUID] = {}
    votes: dict[str, Counter] = {}
    for doc in docs:
        if doc.kind not in ("sale", "invoice", "payment"):
            continue
        key_party = _party_key_of(doc)
        contract_key = doc.contract_ref or f"main:{key_party}"
        if not doc.contract_number:
            contract_key = f"main:{key_party}"
        found = onec_match.match_own(doc, entity_id, index) if entity_id else None
        if found is not None and found.state == "ok" and found.contract_id:
            votes.setdefault(contract_key, Counter())[found.contract_id] += 1
        draft = drafts.setdefault(
            contract_key,
            Draft(key=contract_key, party_key=key_party, number=doc.contract_number or "", signed=doc.contract_date),
        )
        draft.docs += 1
        if doc.kind == "sale" and doc.amount is not None:
            draft.acts.append(Act(doc.number, doc.doc_date, doc.period_from, doc.period_to, doc.amount))
    for key, counter in votes.items():
        matched[key] = counter.most_common(1)[0][0]
    # Разовые акты «Основного договора» - договор на каждый акт (Д15); акты
    # месяцами на «Основном договоре» - один договор «в месяц» (Д3).
    out: dict[str, Draft] = {}
    for key, draft in drafts.items():
        if key in matched:
            draft.contract_id = matched[key]
            out[key] = draft
            continue
        if key.startswith("main:") and draft.acts and sum(1 for act in draft.acts if act.monthly) < 2:
            for act in draft.acts:
                single = Draft(key=f"{key}:{act.number}:{act.day:%Y%m%d}", party_key=draft.party_key, number="",
                               signed=act.day, acts=[act], docs=1, act=act)
                shape(single, today)
                out[single.key] = single
            continue
        shape(draft, today)
        out[key] = draft
    return Source(base, entity_id, parties, out, matched)


# ── План и шаги ──────────────────────────────────────────────────────────────


def _base(session: Session, workspace: Workspace, base_id: uuid.UUID) -> OnecBase:
    base = session.get(OnecBase, base_id)
    if base is None or base.workspace_id != workspace.id:
        raise StartError("Такой базы 1С нет")
    return base


def _owner_of_bin(session: Session, workspace: Workspace, number: str) -> Counterparty | None:
    return session.scalar(
        sa.select(Counterparty).where(
            Counterparty.workspace_id == workspace.id, Counterparty.bin == number, Counterparty.archived_at.is_(None)
        )
    )


def resolve_parties(session: Session, workspace: Workspace, source: Source) -> None:
    """Что шаг «Участники» сделает с каждым контрагентом базы - без записи."""
    registry = Registry(session, workspace)
    own = set(registry.own)
    for party in source.parties.values():
        if party.bin:
            owner = _owner_of_bin(session, workspace, party.bin)
            if owner is not None:
                from app.finance.contracts.models import CounterpartyName

                known = {party_key(owner.name)} | set(
                    session.scalars(sa.select(CounterpartyName.normalized).where(CounterpartyName.counterparty_id == owner.id))
                )
                party.party_id = owner.id
                party.action = "alias" if any(party_key(name) not in known for name in party.names) else "same"
                continue
        candidates: set[uuid.UUID] = set()
        for name in list(party.names) + list(party.full_names):
            resolved = registry.resolve_party(name, slot="customer", create=False)
            if resolved.party is not None:
                candidates.add(resolved.party.id)
            for item in resolved.ambiguous:
                candidates.add(item.id)
        candidates -= own
        if len(candidates) > 1:
            party.action, party.note = "dispute", "под это написание подходят несколько участников"
            continue
        if len(candidates) == 1:
            found = registry.parties[next(iter(candidates))]
            party.party_id = found.id
            if party.bin and not found.bin:
                party.action = "bin"
            elif party.bin and found.bin != party.bin:
                party.action, party.note = "dispute", f"у участника «{found.name}» другой БИН {found.bin}"
            else:
                party.action = "alias"
            continue
        party.action = "create"


def plan(
    session: Session, workspace: Workspace, access: Access, base_id: uuid.UUID, *, today: date, osv: Osv | None = None,
    answers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Что KORT заведёт из базы 1С - по шагам, с числами до записи."""
    _require(access)
    base = _base(session, workspace, base_id)
    source = read_source(session, workspace, base, today=today)
    resolve_parties(session, workspace, source)
    entity = session.get(GroupEntity, base.group_entity_id) if base.group_entity_id else None
    actions = Counter(party.action for party in source.parties.values())
    drafts = [draft for draft in source.drafts.values() if draft.contract_id is None]
    answers = answers or {}
    questions = [draft for draft in drafts if draft.last_day and draft.key not in answers]
    settings = core_settings.payments(session, workspace)
    openings = _osv_openings(session, workspace, source, osv) if osv is not None else None
    return {
        "base": {"id": str(base.id), "title": base.title, "organization": base.organization_name, "bin": base.organization_bin},
        "entity": {"id": str(entity.counterparty_id) if entity else None, "name": base.organization_name, "bin": base.organization_bin},
        "participants": {
            "counts": dict(actions),
            "items": [party.to_dict() for party in sorted(source.parties.values(), key=lambda item: item.name)],
        },
        "contracts": {
            "known": len(source.drafts) - len(drafts),
            "new": len(drafts) - len(questions),
            "questions": [draft.to_dict(source.parties) for draft in questions],
            "items": [draft.to_dict(source.parties) for draft in sorted(drafts, key=lambda item: (item.party_key, item.label))],
        },
        "since": {
            "current": settings.since.isoformat() if settings.since else None,
            "proposed": osv.start.isoformat() if osv and osv.start else None,
        },
        "openings": openings,
        "payments": {"source": settings.source_of(base.group_entity_id) if base.group_entity_id else None},
    }


def add_entity(session: Session, workspace: Workspace, access: Access, actor: Actor, base_id: uuid.UUID) -> dict[str, Any]:
    """Шаг 1: наше юрлицо базы - по БИН организации; нет - завести и привязать к базе."""
    _require(access)
    base = _base(session, workspace, base_id)
    if base.group_entity_id:
        return {"done": 0, "id": str(base.group_entity_id)}
    number = bins.digits(base.organization_bin)
    owner = _owner_of_bin(session, workspace, number) if bins.checksum_ok(number) else None
    if owner is not None and session.get(GroupEntity, owner.id) is not None:
        base.group_entity_id = owner.id
        session.flush()
        return {"done": 0, "id": str(owner.id), "linked": True}
    name = (base.organization_name or "").strip()
    if not name:
        raise StartError("В базе 1С у организации нет названия - заведите юрлицо в «Участниках»")
    entity = contracts_setup.add_entity(session, workspace, name=name, bin=number if bins.checksum_ok(number) else "")
    base.group_entity_id = entity.counterparty_id
    session.flush()
    history.write(
        session, workspace, kind="core.onec_start", entity="group_entities", entity_id=entity.counterparty_id,
        title=f"из 1С заведено наше юрлицо «{name}»", after={"bin": number}, actor=actor.email,
    )
    return {"done": 1, "id": str(entity.counterparty_id)}


def add_participants(session: Session, workspace: Workspace, access: Access, actor: Actor, base_id: uuid.UUID, *, today: date) -> dict[str, Any]:
    """Шаг 2: участники из контрагентов базы - по БИН, с написаниями 1С."""
    from app.finance.core import participants

    _require(access)
    base = _base(session, workspace, base_id)
    source = read_source(session, workspace, base, today=today)
    resolve_parties(session, workspace, source)
    registry = Registry(session, workspace)
    counts: Counter = Counter()
    for party in source.parties.values():
        if party.action == "dispute":
            counts["dispute"] += 1
            continue
        if party.action == "create":
            resolved = registry.resolve_party(party.name, slot="customer")
            target = resolved.party
            if target is None:
                counts["dispute"] += 1
                continue
            if party.bin and not target.bin:
                target.bin, target.kind = party.bin, bins.kind_of(party.bin, target.name)
            if party.full_name and not getattr(target, "full_name", ""):
                target.full_name = party.full_name
            party.party_id = target.id
        elif party.action == "bin":
            target = registry.parties[party.party_id]
            target.bin, target.kind = party.bin, bins.kind_of(party.bin, target.name)
        assert party.party_id is not None
        for name in list(party.names) + list(party.full_names):
            participants.remember(session, workspace, party.party_id, name, source="1c")
        counts[party.action] += 1
    session.flush()
    if counts:
        history.write(
            session, workspace, kind="core.onec_start", entity="counterparties", entity_id=None,
            title=(
                f"участники из 1С: заведено {counts['create']}, БИН записан {counts['bin']}, "
                f"написания {counts['alias']}" + (f", спорных {counts['dispute']}" if counts["dispute"] else "")
            ),
            after=dict(counts), actor=actor.email,
        )
    return {"done": counts["create"] + counts["bin"] + counts["alias"], "counts": dict(counts)}


def _status(session: Session, workspace: Workspace, phase: str) -> uuid.UUID | None:
    from app.finance.core.changes import status_by_phase

    value = status_by_phase(session, workspace.id, phase)
    return value.id if value is not None else None


def add_contracts(
    session: Session, workspace: Workspace, access: Access, actor: Actor, base_id: uuid.UUID, *, today: date,
    answers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Шаг 3: договоры из 1С пачкой - с соглашениями «сумма с даты» и паузами, с точкой восстановления.

    `answers`: ключ черновика → `{"end": "ГГГГ-ММ-ДД"}` (договор закончился) или
    `{"active": true}` (идёт). Черновик с вопросом без ответа не заводится.
    `{"pause": false}` - паузу не ставить.
    """
    from app.finance.contracts.service import Mode, create, patch
    from app.finance.core.changes import status_from

    _require(access)
    if not access.edit:
        raise PermissionError("Заводить договоры можно с правом правки реестра")
    base = _base(session, workspace, base_id)
    if not base.group_entity_id:
        raise StartError("Сначала - наше юрлицо базы")
    source = read_source(session, workspace, base, today=today)
    resolve_parties(session, workspace, source)
    answers = answers or {}
    active, suspended = _status(session, workspace, "active"), _status(session, workspace, "suspended")
    terminated, fulfilled = _status(session, workspace, "terminated"), _status(session, workspace, "fulfilled")
    ids: list[str] = []
    waiting: list[str] = []
    for draft in sorted(source.drafts.values(), key=lambda item: (item.party_key, item.label)):
        if draft.contract_id is not None:
            continue
        party = source.parties.get(draft.party_key)
        if party is None or party.party_id is None or draft.amount is None:
            continue
        answer = answers.get(draft.key) or {}
        if draft.last_day and not answer:
            waiting.append(draft.key)
            continue
        values: dict[str, Any] = {
            "number": draft.number,
            "customer": {"id": str(party.party_id)},
            "executor": {"id": str(base.group_entity_id)},
            "billing": draft.billing,
            "amount": format(draft.amount, "f"),
        }
        if draft.signed:
            values["signed_at"] = draft.signed.isoformat()
        if draft.billing_from:
            values["billing_from"] = draft.billing_from.isoformat()
            if draft.billing == "month":
                values["period_day"] = draft.billing_from.day
        if draft.billing == "total" and fulfilled:
            values["status"] = {"id": str(fulfilled)}
            values["end_date"] = (draft.act.end or draft.act.day).isoformat() if draft.act else None
        elif answer.get("end") and terminated:
            values["status"] = {"id": str(terminated)}
            values["end_date"] = str(answer["end"])
        elif active:
            values["status"] = {"id": str(active)}
        values = {key: value for key, value in values.items() if value is not None}
        contract = create(session, workspace, access, actor, values)
        for day, amount in draft.changes:
            mode = Mode(kind="from_date", effective_from=day, summary="сумма по актам 1С")
            patch(session, workspace, access, actor, contract.id, {"amount": format(amount, "f")}, known_seq=None, mode=mode)
        if draft.pauses and answer.get("pause", True) and suspended and active:
            for start, end in draft.pauses:
                status_from(session, workspace, access, actor, contract.id, suspended, start, summary="нет актов 1С")
                status_from(session, workspace, access, actor, contract.id, active, end + timedelta(days=1), summary="акты 1С снова")
        ids.append(str(contract.id))
    if not ids:
        return {"done": 0, "waiting": waiting, "point": None}
    title = f"из 1С заведено договоров: {len(ids)}"
    point = restore._point(session, workspace, actor, "contracts_create", title, {"ids": ids})
    history.write(
        session, workspace, kind="core.onec_start", entity="contracts", entity_id=None,
        title=title, after={"count": len(ids), "waiting": len(waiting)}, actor=actor.email,
    )
    return {"done": len(ids), "waiting": waiting, "point": restore._point_ref(point)}


def _osv_openings(session: Session, workspace: Workspace, source: Source, osv: Osv) -> dict[str, Any]:
    """Строки ОСВ → договоры KORT: сколько сальдо ляжет и что не нашло договора."""
    wanted, missing = osv_wanted(session, workspace, source.base, osv)
    return {
        "since": osv.start.isoformat() if osv.start else None,
        "contracts": len(wanted),
        "debt": _money(sum(wanted.values(), _ZERO)),
        "missing": missing,
        "problems": osv.problems,
    }


def osv_wanted(session: Session, workspace: Workspace, base: OnecBase, osv: Osv) -> tuple[dict[uuid.UUID, Decimal], list[dict[str, Any]]]:
    """Договор KORT → сальдо на начало по ОСВ; строки без договора - списком.

    Договор ищется у участника строки (по написанию, 1С их уже записала) по
    номеру; «Основной договор» - единственный договор клиента без номера, а у
    клиента с одним договором - он (С2: сальдо к договору реестра).
    """
    registry = Registry(session, workspace)
    contracts = list(
        session.scalars(
            sa.select(Contract).where(
                Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None), Contract.executor_id == base.group_entity_id
            )
        )
    )
    by_party: dict[uuid.UUID, list[Contract]] = {}
    for contract in contracts:
        if contract.customer_id:
            by_party.setdefault(contract.customer_id, []).append(contract)
    wanted: dict[uuid.UUID, Decimal] = {}
    missing: list[dict[str, Any]] = []
    for line in osv.lines:
        if not line.opening:
            continue
        party = registry.resolve_party(line.party, slot="customer", create=False).party
        own = by_party.get(party.id, []) if party is not None else []
        if line.number:
            found = [item for item in own if item.number_key == number_key(line.number)]
        else:
            found = [item for item in own if not item.number_key] or (own if len(own) == 1 else [])
        if len(found) != 1:
            missing.append({"party": line.party, "contract": line.contract, "opening": _money(line.opening), "line": line.line,
                            "why": "у клиента нет такого договора" if not found else "подходят несколько договоров"})
            continue
        wanted[found[0].id] = wanted.get(found[0].id, _ZERO) + line.opening
    return wanted, missing


def start_since(
    session: Session, workspace: Workspace, access: Access, actor: Actor, base_id: uuid.UUID, osv: Osv
) -> dict[str, Any]:
    """Шаг 4: начать учёт с первого числа периода ОСВ и перенести сальдо договоров - точкой восстановления."""
    _require(access)
    base = _base(session, workspace, base_id)
    if osv.start is None:
        raise StartError("В ОСВ не назван период - с какого дня сальдо, не понять")
    settings = core_settings.payments(session, workspace)
    day = settings.since
    if day is None:
        day = osv.start
        core_settings.set_payments(session, workspace, access, actor, since=day)
    elif day != osv.start:
        raise StartError(f"Учёт уже идёт с {day:%d.%m.%Y}, а ОСВ - на {osv.start:%d.%m.%Y}: нужна ОСВ на начало учёта")
    wanted, missing = osv_wanted(session, workspace, base, osv)
    moved = opening_module.apply_wanted(
        session, workspace, access, actor, {"accruals": wanted}, since=day, source="1c",
        title=f"входящее сальдо на {day:%d.%m.%Y} из ОСВ 1С",
    )
    return {"since": day.isoformat(), "missing": missing, **moved}


def stored_osv(session: Session, workspace: Workspace, base_id: uuid.UUID) -> Osv | None:
    """ОСВ, загруженная к базе раньше (`core_settings.data.onec_osv`)."""
    from app.finance.core.models import CoreSettings

    row = session.get(CoreSettings, workspace.id)
    data = ((row.data if row is not None else None) or {}).get("onec_osv") or {}
    item = data.get(str(base_id))
    return Osv.from_store(item) if item else None


def keep_osv(session: Session, workspace: Workspace, access: Access, actor: Actor, base_id: uuid.UUID, osv: Osv) -> dict[str, Any]:
    """Запомнить ОСВ к базе: шаги «из 1С» берут сальдо из неё, файл второй раз не нужен."""
    from datetime import datetime, timezone

    from app.finance.core.models import CoreSettings

    _require(access)
    base = _base(session, workspace, base_id)
    row = session.get(CoreSettings, workspace.id)
    if row is None:
        row = CoreSettings(workspace_id=workspace.id, data={})
        session.add(row)
    data = dict(row.data or {})
    kept = dict(data.get("onec_osv") or {})
    kept[str(base.id)] = osv.to_store()
    data["onec_osv"] = kept
    row.data = data
    row.updated_by = actor.user_id
    row.updated_at = datetime.now(timezone.utc)
    session.flush()
    history.write(
        session, workspace, kind="core.onec_start", entity="core_settings", entity_id=None,
        title=f"ОСВ 1С к базе «{base.title}»: {len(osv.lines)} договоров на {osv.start:%d.%m.%Y}" if osv.start else "ОСВ 1С без периода",
        after={"file": osv.file, "contracts": len(osv.lines)}, actor=actor.email,
    )
    return osv.to_dict()


def use_payments(session: Session, workspace: Workspace, access: Access, actor: Actor, base_id: uuid.UUID) -> dict[str, Any]:
    """Шаг 5: поступления 1С - источник оплат юрлица базы."""
    _require(access)
    base = _base(session, workspace, base_id)
    if not base.group_entity_id:
        raise StartError("Сначала - наше юрлицо базы")
    core_settings.set_payments(session, workspace, access, actor, entity_id=base.group_entity_id, source="1c")
    return {"done": 1}


__all__ = [
    "Act", "Draft", "MAIN", "Party", "Source", "StartError", "add_contracts", "add_entity", "add_participants",
    "keep_osv", "osv_wanted", "plan", "read_source", "resolve_parties", "shape", "start_since", "stored_osv", "use_payments",
]
