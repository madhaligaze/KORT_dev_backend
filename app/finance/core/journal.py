"""Журнал фактов: всё, что случилось с договором, - в одной форме (этап 2, шаг 1).

До журнала факты о договоре лежали в шести таблицах своей формы, и каждый итог
собирал их по-своему: долги и выручка читали операции журнала денег через
разнесение, документы 1С - через сопоставление, отдельно документы ядра и
соглашения. Теперь эти таблицы - хранилища подключений: выписки и журнал
денег, 1С, то, что человек записал в KORT. Факты из них **проводятся** в
журнал (`Fact`), как документ 1С проводится в регистры, а итоги ядра - долги,
выручка, оборот и НДС - читают только журнал.

Что проводится
──────────────
* операция журнала денег (`op:`) - оплата: поступление всегда, и без договора
  (из таких - «Оплаты без договора» и начало учёта), списание - если отнесено
  к договору или спорно. Договор - по разнесению (`contracts/payments.allocate`),
  «за какие месяцы» - период операции;
* документ 1С (`1c:`): поступление - оплата, счёт на оплату - счёт,
  реализация - АВР, счёт-фактура и ЭСФ - ЭСФ со ссылкой на свой АВР. Договор и
  месяц - по сопоставлению (`onec/match.resolve`);
* документ ядра (`doc:`): поправка, счёт, АВР, ЭСФ, входящее сальдо, «верно у
  KORT» (решение человека);
* соглашение к договору (`amend:`), решения «эта оплата по этому договору»
  (`cp:`) и «этот документ 1С по этому договору» (`1cm:`).

Договор оплаты и документа 1С - вывод правил, а не слово источника. Правило
дало другой ответ (завели второй договор клиента, поправили номер) - факт
проводится заново новой версией, и видно, с какой записи оплата легла на
другой договор.

Неизменяемость
──────────────
Строка журнала не правится. Проведение сравнивает то, что должно быть, с
действующими фактами по ключу: совпало - ничего; изменилось - новая версия
(`replaces` - прежняя, у прежней `ended_seq`); пропало из хранилища - пометка
«снят» (`void`). Всё записанное за одно проведение получает один номер `seq`
(счётчик компании `facts`) - курсор «что изменилось после N».

Когда проводится
────────────────
Перед чтением, если хранилища менялись. События сессии (`register`) замечают
запись в таблицы-источники и в реестр, от которого зависит разнесение, и
перед коммитом сдвигают счётчик `facts.inputs`; проведение помнит, при каком
его значении проведено (`facts.posted`). Совпадают - журнал свежий. Своя
незакоммиченная правка видна сразу: читатель после неё проводит заново в той
же транзакции, и коммит уносит правку вместе с её фактами. Первое чтение
компании в новом процессе проводит заново, не веря метке: старый процесс на
выкладке ещё пишет и счётчика не знает.

Части - `PARTS`: `kort` (документы ядра и соглашения - дёшево), `journal`
(разнесение операций и решения человека по ним), `1c` (сопоставление
документов 1С). Начислениям нужны только соглашения и поправки: правка одного
договора не стоит им разнесения всего журнала денег.

Что проводится заново (шаг 2, 27.10.2026)
──────────────────────────────────────────
Не весь журнал, а то, что изменилось. Писатель вместе со сдвигом счётчика
кладёт в очередь (`FactInput`), что именно правил (`_dirt`): операцию или
решение по ней - эту операцию; документ или соглашение - договор; форму
реестра - всю часть. Разнесение одной операции не зависит от других
операций: только от неё, решения человека и формы реестра - сторон,
номера, даты, окончания и суммы договора (`SHAPE`), написаний и БИН
участников, юрлиц счетов, наших юрлиц. Сопоставление 1С читает те же колонки
договора. Поэтому правка примечания, статуса, отдела или доли договора
журнал не трогает вовсе, новая выписка проводит свои операции, а решение
«эта оплата по этому договору» - одну. Значение счётчика без строки в очереди
(писал старый процесс на выкладке) - проводится всё.
"""
from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.finance.core.models import Fact, FactInput

PARTS = ("kort", "journal", "1c")
#: Ключи каждой части: проведение части сравнивает и снимает только свои.
PREFIXES = {"kort": ("doc:", "amend:"), "journal": ("op:", "cp:"), "1c": ("1c:", "1cm:")}
#: Ключи, которые проводятся по договору: у документа ядра и соглашения договор не меняется.
SCOPED = ("doc:", "amend:")
#: Ключ очереди «вся часть»: правка, после которой ключи не назвать (форма реестра, 1С).
WHOLE = uuid.UUID(int=0)
#: Колонки договора, по которым выписка и 1С находят договор: форма реестра.
SHAPE = frozenset({"executor_id", "customer_id", "number", "number_key", "signed_at", "end_date", "amount", "deleted_at"})
#: Колонки участника, по которым выписка и 1С узнают сторону.
PARTY = frozenset({"name", "normalized_name", "bin", "details", "merged_into_id", "archived_at"})
#: Больше стольких операций разом (загрузили выписку) - дешевле провести журнал денег целиком.
_OPERATIONS_MAX = 400
#: Счётчики компании: номер проведения, правки хранилищ, при какой правке проведено.
SEQ, INPUTS, POSTED = "facts", "facts.inputs", "facts.posted"
#: Вид документа ядра → вид факта: «верно у KORT» - решение человека.
DOCUMENT_FACTS = {"adjustment": "adjustment", "bill": "bill", "act": "act", "esf": "esf", "opening": "opening", "verified": "decision"}
#: Документ 1С → вид факта.
ONEC_FACTS = {
    "payment": "payment", "refund": "payment", "invoice": "bill", "sale": "act", "sf": "esf", "esf": "esf",
    # Покупка: акт поставщика - АВР договора покупки, оплата поставщику - его оплата.
    "purchase": "act", "supplier_payment": "payment",
}

_PENDING = "finance.journal.pending"
_FRESH = "finance.journal.fresh"
_OBSERVED = "finance.journal.observed"
_BULK = "finance.journal.bulk"
_SEEN = "finance.journal.seen"
_POSTED_HERE = "finance.journal.posted"
_SCOPED_DONE = "finance.journal.scoped"
#: Регистры итогов, построенные этой сессией (`core/registers.of`): запись в хранилища их сбрасывает.
REGISTERS = "finance.registers"
#: Больше стольких договоров документы ядра проводятся целиком, а не по договорам.
_SCOPE_MAX = 300
_TRACKED = "_finance_journal"
#: Колонки, правка только которых массовым `UPDATE` фактов не меняет: номера опроса и порядок строк.
_QUIET = {"seq", "position", "field_seq"}
_CHUNK = 500
#: Первое число замка проведения: отличает его от чужих замков `pg_advisory_xact_lock`.
_LOCK_CLASS = 4100
#: Компании, чей журнал этот процесс уже проводил сам. Первое чтение компании в
#: новом процессе проводит заново, не веря метке: на выкладке старый процесс ещё
#: дописывает правки, а счётчик правок он не знает.
_CHECKED: set[uuid.UUID] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _month_start(key: str) -> date:
    return date(int(key[:4]), int(key[5:7]), 1)


def _plain(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """Данные факта: пустое (нет значения, пустая строка, пустой список) не пишется - `False` и 0 пишутся."""
    return {key: _plain(value) for key, value in data.items() if value is not None and value != "" and value != [] and value != {}}


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _id(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


# ── Черновик факта ───────────────────────────────────────────────────────────


@dataclass
class Draft:
    """Каким факт должен быть по хранилищу - до сравнения с журналом."""

    key: str
    kind: str
    source: str
    day: date | None = None
    period: date | None = None
    amount: Decimal | None = None
    contract_id: uuid.UUID | None = None
    party_id: uuid.UUID | None = None
    entity_id: uuid.UUID | None = None
    data: dict[str, Any] = field(default_factory=dict)
    author: uuid.UUID | None = None
    void: bool = False

    def signature(self) -> str:
        return _signature(
            self.kind, self.source, self.day, self.period, self.amount, self.contract_id, self.party_id,
            self.entity_id, _clean(self.data), self.author, self.void,
        )


def _signature(
    kind: str, source: str, day: date | None, period: date | None, amount: Decimal | None,
    contract_id: uuid.UUID | None, party_id: uuid.UUID | None, entity_id: uuid.UUID | None,
    data: dict[str, Any], author: uuid.UUID | None, void: bool,
) -> str:
    return json.dumps(
        [kind, source, _plain(day), _plain(period), _money(amount), _id(contract_id), _id(party_id), _id(entity_id),
         data, _id(author), bool(void)],
        sort_keys=True, ensure_ascii=False,
    )


def signature_of(fact: Fact) -> str:
    return _signature(
        fact.kind, fact.source, fact.day, fact.period, fact.amount, fact.contract_id, fact.party_id,
        fact.entity_id, fact.data or {}, fact.author, fact.void,
    )


# ── Проведение: хранилища → черновики ───────────────────────────────────────


def _months(start: date | None, end: date | None, accrued: date | None) -> list[str] | None:
    """«За какие месяцы» операции: период, иначе месяц начисления."""
    if start is not None:
        return [_month_key(start), _month_key(end or start)]
    if accrued is not None:
        return [_month_key(accrued), _month_key(accrued)]
    return None


def _kort_drafts(session: Session, workspace: Any, contracts: set[uuid.UUID] | None = None) -> dict[str, Draft]:
    """Документы ядра и соглашения; `contracts` - только этих договоров."""
    from app.finance.contracts.models import ContractAmendment
    from app.finance.core.models import Document

    out: dict[str, Draft] = {}
    documents = sa.select(Document).where(Document.workspace_id == workspace.id, Document.deleted_at.is_(None))
    amendments = sa.select(ContractAmendment).where(ContractAmendment.workspace_id == workspace.id)
    if contracts is not None:
        documents = documents.where(Document.contract_id.in_(list(contracts)))
        amendments = amendments.where(ContractAmendment.contract_id.in_(list(contracts)))
    for doc in session.scalars(documents):
        kind = DOCUMENT_FACTS[doc.kind]
        key = f"doc:{doc.id}"
        out[key] = Draft(
            key=key, kind=kind, source=doc.source, day=doc.issued_on, period=doc.period, amount=doc.amount,
            contract_id=doc.contract_id, author=doc.updated_by or doc.created_by,
            data={
                "what": "verified" if doc.kind == "verified" else None,
                "number": doc.number, "state": doc.state, "skip": doc.skip, "reason": doc.reason,
                "source_key": doc.source_key, "meta": doc.meta or None,
            },
        )
    for row in session.scalars(amendments):
        key = f"amend:{row.id}"
        out[key] = Draft(
            key=key, kind="agreement", source="book" if row.origin == "book" else "kort", day=row.effective_from,
            contract_id=row.contract_id, author=row.created_by,
            data={
                "effect": row.effect, "before": row.before or None, "after": row.after or None, "number": row.number,
                "signed": row.signed_at, "summary": row.summary, "origin": row.origin, "position": row.position or None,
                "applied": row.applied_at is not None,
            },
        )
    return out


def _journal_drafts(session: Session, workspace: Any, operations: set[uuid.UUID] | None = None) -> dict[str, Draft]:
    """Операции журнала денег - оплаты с договором по разнесению - и решения человека по ним.

    `operations` - только эти операции: разнесение одной не зависит от других.
    """
    from app.finance.contracts import payments as contract_payments
    from app.finance.contracts.models import Contract, ContractPayment
    from app.finance.contracts.service import Registry
    from app.finance.models import Account, Operation

    registry = Registry(session, workspace)
    contracts = list(
        session.scalars(sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None)))
    )
    allocation = contract_payments.allocate(session, workspace, registry, contracts, operations=operations)
    paid: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
    #: Разделённая оплата (О2): операция → (договор, часть).
    parts: dict[uuid.UUID, list[tuple[uuid.UUID, Decimal]]] = defaultdict(list)
    for contract_id, items in allocation.paid.items():
        for operation, how in items:
            if how == "split":
                parts[operation.id].append((contract_id, operation.amount_base))
            else:
                paid[operation.id] = (contract_id, how)
    sale = {contract.id for contract in contracts if registry.is_own(contract.executor_id)}
    returned = allocation.returned
    deposits = allocation.deposits
    disputed: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for contract_id, items in allocation.open.items():
        for operation in items:
            disputed[operation.id].add(contract_id)
    entity_of_account = dict(
        session.execute(sa.select(Account.id, Account.group_entity_id).where(Account.workspace_id == workspace.id)).all()
    )
    out: dict[str, Draft] = {}
    # Колонками, а не строками операций: строка тащит `raw` выписки, а его здесь не читают.
    query = sa.select(
        Operation.id, Operation.kind, Operation.paid_at, Operation.amount_base, Operation.counterparty_id,
        Operation.account_to_id, Operation.account_from_id, Operation.period_start, Operation.period_end,
        Operation.accrued_at, Operation.period_label, Operation.comment, Operation.contract_ref,
    ).where(
        Operation.workspace_id == workspace.id,
        Operation.deleted_at.is_(None),
        Operation.status == "fact",
        Operation.kind.in_(("income", "expense")),
    )
    decisions = sa.select(ContractPayment).where(ContractPayment.workspace_id == workspace.id)
    if operations is not None:
        query = query.where(Operation.id.in_(list(operations) or [WHOLE]))
        decisions = decisions.where(ContractPayment.operation_id.in_(list(operations) or [WHOLE]))
    def amount_of(contract_id: uuid.UUID | None, kind: str, amount: Decimal) -> Decimal:
        # Встречное направление - возврат: у продажи списание клиенту, у покупки - поступление от поставщика (О6).
        if contract_id is None:
            return amount
        expected = "income" if contract_id in sale else "expense"
        return amount if kind == expected else -amount

    for operation in session.execute(query):
        found = paid.get(operation.id)
        candidates = disputed.get(operation.id)
        refund = allocation.refunds.get(operation.id)
        if operation.kind == "expense" and found is None and not candidates and not refund and operation.id not in parts:
            # Списание без договора - не оплата: зарплата, налоги, переводы.
            continue
        account = operation.account_to_id if operation.kind == "income" else operation.account_from_id
        data = {
            "direction": "in" if operation.kind == "income" else "out",
            "account": account, "how": found[1] if found else None,
            "months": _months(operation.period_start, operation.period_end, operation.accrued_at),
            "label": operation.period_label, "comment": operation.comment, "contract_ref": operation.contract_ref,
            "open": sorted(str(item) for item in candidates) if candidates else None,
            "third": True if operation.id in allocation.third else None,
            "refund": str(refund) if refund else None,
            "returned": str(returned[operation.id]) if operation.id in returned else None,
            "deposit": True if operation.id in deposits else None,
        }
        entity_id = entity_of_account.get(account) if account else None
        if operation.id in parts:
            # Факт на каждую часть: «объяснить» и регистры видят часть у её договора.
            for contract_id, amount in parts[operation.id]:
                key = f"op:{operation.id}:{contract_id}"
                out[key] = Draft(
                    key=key, kind="payment", source="journal", day=operation.paid_at,
                    amount=amount_of(contract_id, operation.kind, amount), contract_id=contract_id,
                    party_id=operation.counterparty_id, entity_id=entity_id, data={**data, "how": "split"},
                )
            continue
        key = f"op:{operation.id}"
        out[key] = Draft(
            key=key, kind="payment", source="journal", day=operation.paid_at,
            amount=amount_of(found[0] if found else None, operation.kind, operation.amount_base),
            contract_id=found[0] if found else None, party_id=operation.counterparty_id, entity_id=entity_id, data=data,
        )
    for row in session.scalars(decisions):
        key = f"cp:{row.operation_id}"
        out[key] = Draft(
            key=key, kind="decision", source="kort", day=row.created_at.date() if row.created_at else None,
            contract_id=row.contract_id, author=row.created_by,
            data={"what": "payment", "operation": row.operation_id, "action": "link" if row.contract_id else "none"},
        )
    return out


def _onec_drafts(session: Session, workspace: Any) -> dict[str, Draft]:
    """Документы включённых баз 1С и решения человека по ним."""
    from app.finance.onec import match as onec_match
    from app.finance.onec.models import OnecBase, OnecDocument
    from app.finance.onec.service import short_number

    bases = {
        base.id: base
        for base in session.scalars(
            sa.select(OnecBase).where(OnecBase.workspace_id == workspace.id, OnecBase.state == "active")
        )
    }
    if not bases:
        return {}
    docs = list(
        session.scalars(
            sa.select(OnecDocument).where(
                OnecDocument.base_id.in_(list(bases)),
                OnecDocument.deleted.is_(False),
                OnecDocument.gone.is_(False),
            )
        )
    )
    resolved = onec_match.resolve(docs, bases, onec_match.build_index(session, workspace.id))
    by_ref = {(doc.base_id, doc.kind, doc.ref): doc for doc in docs}
    out: dict[str, Draft] = {}
    for doc in docs:
        found = resolved.matches[doc.id]
        month = resolved.months.get(doc.id, "")
        base = bases[doc.base_id]
        data: dict[str, Any] = {
            "posted": doc.posted, "state": found.state, "how": found.how, "number": short_number(doc.number),
            "base": base.id, "payer": doc.party_name, "bin": doc.party_bin, "contract_ref": doc.contract_number,
        }
        if doc.kind == "payment":
            invoice = by_ref.get((doc.base_id, "invoice", doc.basis_ref)) if doc.basis_ref else None
            paid_for = onec_match.month_key(invoice.period_from or invoice.doc_date) if invoice is not None else ""
            data.update({"direction": "in", "comment": doc.content, "months": [paid_for, paid_for] if paid_for else None})
        elif doc.kind == "refund":
            # Возврат покупателю - оплата договора со знаком минус: долг растёт обратно.
            data.update({"direction": "out", "refund": True, "comment": doc.content, "months": None})
        elif doc.kind == "supplier_payment":
            data.update({"direction": "out", "comment": doc.content, "months": None, "side": "purchase"})
        elif doc.kind == "sale":
            data["signed"] = doc.signed_at
        elif doc.kind == "purchase":
            data["side"] = "purchase"
        elif doc.kind in ("sf", "esf"):
            invoice = by_ref.get((doc.base_id, "sf", doc.basis_ref)) if doc.kind == "esf" and doc.basis_ref else None
            sale_ref = doc.basis_ref if doc.kind == "sf" else (invoice.basis_ref if invoice is not None else "")
            sale = by_ref.get((doc.base_id, "sale", sale_ref)) if sale_ref else None
            data.update({"form": doc.kind, "act": f"1c:{sale.id}" if sale is not None else None, "status": doc.status, "reg": doc.reg_number})
        key = f"1c:{doc.id}"
        out[key] = Draft(
            key=key, kind=ONEC_FACTS[doc.kind], source="1c", day=doc.doc_date,
            period=_month_start(month) if month else None,
            amount=-doc.amount if doc.kind == "refund" and doc.amount is not None else doc.amount,
            contract_id=found.contract_id if found.state == "ok" else None, entity_id=base.group_entity_id, data=data,
        )
        if doc.manual:
            decision = f"1cm:{doc.id}"
            out[decision] = Draft(
                key=decision, kind="decision", source="kort", day=doc.manual_at.date() if doc.manual_at else None,
                contract_id=doc.manual_contract_id, author=doc.manual_by,
                data={"what": "1c", "document": doc.id, "action": doc.manual},
            )
    return out


BUILDERS = {"kort": _kort_drafts, "journal": _journal_drafts, "1c": _onec_drafts}


# ── Проведение: черновики → журнал ───────────────────────────────────────────


def post(
    session: Session,
    workspace: Any,
    parts: Iterable[str] = PARTS,
    *,
    contracts: Iterable[uuid.UUID] | None = None,
    operations: Iterable[uuid.UUID] | None = None,
) -> dict[str, int]:
    """Провести части в журнал: новое, изменённое, снятое. Возвращает, сколько чего записано.

    `contracts` - только документы ядра и соглашения этих договоров (часть
    `kort`): так начисления одного договора не ждут проведения всей компании.
    Договор у документа и соглашения не меняется, поэтому сравнивать с
    журналом можно по договору. `operations` - только эти операции и решения
    по ним (часть `journal`): разнесение одной операции от других не зависит.
    """
    from app.finance.contracts.fields import bump

    chosen = [part for part in PARTS if part in set(parts)]
    wanted: dict[str, Draft] = {}
    scope = set(contracts) if contracts is not None else None
    picked = set(operations) if operations is not None else None
    if scope is not None:
        if chosen != ["kort"]:
            raise ValueError("по договорам проводятся только документы ядра и соглашения")
        wanted.update(_kort_drafts(session, workspace, scope))
        prefixes = list(SCOPED)
    elif picked is not None:
        if chosen != ["journal"]:
            raise ValueError("по операциям проводится только журнал денег")
        from app.finance.contracts.payments import widen

        # Одинаковые оплаты на одинаковые договоры решаются вместе (`_pair_twins`).
        picked = widen(session, workspace.id, picked)
        wanted.update(_journal_drafts(session, workspace, picked))
        prefixes = list(PREFIXES["journal"])
    else:
        for part in chosen:
            wanted.update(BUILDERS[part](session, workspace))
        prefixes = [prefix for part in chosen for prefix in PREFIXES[part]]
    # Строками, а не объектами: сравнению нужны поля подписи, а объектов ORM
    # на журнал денег - тысячи (27.10.2026).
    query = sa.select(
        Fact.id, Fact.key, Fact.kind, Fact.source, Fact.day, Fact.period, Fact.amount, Fact.contract_id,
        Fact.party_id, Fact.entity_id, Fact.data, Fact.author, Fact.void,
    ).where(
        Fact.workspace_id == workspace.id,
        Fact.ended_seq.is_(None),
        sa.or_(*(Fact.key.startswith(prefix) for prefix in prefixes)),
    )
    if scope is not None:
        # И по ключу: если документ всё же сменил договор, его прежняя версия
        # найдётся, а не останется второй действующей.
        query = query.where(sa.or_(Fact.contract_id.in_(list(scope)), Fact.key.in_(list(wanted) or [""])))
    if picked is not None:
        keys = [f"{prefix}{item}" for item in picked for prefix in prefixes]
        current = {}
        for start in range(0, len(keys), _CHUNK):
            current.update({fact.key: fact for fact in session.execute(query.where(Fact.key.in_(keys[start : start + _CHUNK])))})
        # Части разделённых оплат (`op:<операция>:<договор>`) редки - их ключи не назвать заранее.
        names = {str(item) for item in picked}
        for fact in session.execute(query.where(Fact.key.like("op:%:%"))):
            if fact.key.split(":")[1] in names:
                current[fact.key] = fact
    else:
        current = {fact.key: fact for fact in session.execute(query)}
    writes: list[tuple[Draft, Fact | None]] = []
    counts = {"new": 0, "changed": 0, "void": 0}
    for key, draft in wanted.items():
        old = current.pop(key, None)
        if old is not None and signature_of(old) == draft.signature():
            continue
        writes.append((draft, old))
        counts["new" if old is None or old.void else "changed"] += 1
    for key, old in current.items():
        if old.void:
            continue
        writes.append((Draft(key=key, kind=old.kind, source=old.source, contract_id=old.contract_id, void=True), old))
        counts["void"] += 1
    if not writes:
        return counts
    seq = bump(session, workspace.id, SEQ)
    ended = [old.id for _draft, old in writes if old is not None]
    for start in range(0, len(ended), _CHUNK):
        # Сначала закрыть прежние версии: действующая версия ключа - одна (уникальный индекс).
        session.execute(
            sa.update(Fact)
            .where(Fact.id.in_(ended[start : start + _CHUNK]))
            .values(ended_seq=seq)
            .execution_options(synchronize_session=False)
        )
    now = _now()
    rows = [
        {
            "id": uuid.uuid4(), "workspace_id": workspace.id, "seq": seq, "key": draft.key, "kind": draft.kind,
            "source": draft.source, "day": draft.day, "period": draft.period, "amount": draft.amount,
            "contract_id": draft.contract_id, "party_id": draft.party_id, "entity_id": draft.entity_id,
            "data": _clean(draft.data), "author": draft.author, "recorded_at": now,
            "replaces": old.id if old is not None else None, "void": draft.void, "ended_seq": None,
        }
        for draft, old in writes
    ]
    for start in range(0, len(rows), _CHUNK):
        session.execute(sa.insert(Fact.__table__), rows[start : start + _CHUNK])
    # Прежние версии могли лежать в карте сессии с пустым `ended_seq`.
    for _draft, old in writes:
        if old is not None:
            loaded = session.identity_map.get(session.identity_key(Fact, old.id))
            if loaded is not None:
                session.expire(loaded)
    counts["seq"] = seq
    return counts


# ── Свежесть журнала ─────────────────────────────────────────────────────────


def _counter(session: Session, workspace_id: uuid.UUID, name: str) -> int | None:
    from app.finance.contracts.models import Counter

    value = session.scalar(sa.select(Counter.value).where(Counter.workspace_id == workspace_id, Counter.name == name))
    return int(value) if value is not None else None


def _set_counter(session: Session, workspace_id: uuid.UUID, name: str, value: int) -> None:
    from app.finance.contracts.models import Counter

    table = Counter.__table__
    done = session.execute(
        sa.update(table).where(table.c.workspace_id == workspace_id, table.c.name == name).values(value=value)
    ).rowcount
    if done:
        return
    try:
        with session.begin_nested():
            session.execute(sa.insert(table).values(workspace_id=workspace_id, name=name, value=value))
    except sa.exc.IntegrityError:
        session.execute(sa.update(table).where(table.c.workspace_id == workspace_id, table.c.name == name).values(value=value))


def _lock(session: Session, workspace_id: uuid.UUID) -> None:
    """Одно проведение компании за раз: второе ждёт и видит записанное первым."""
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        sa.text("SELECT pg_advisory_xact_lock(:a, :b)"),
        {"a": _LOCK_CLASS, "b": (workspace_id.int & 0x7FFFFFFF)},
    )


def ensure(
    session: Session, workspace_id: uuid.UUID, parts: Iterable[str] = PARTS, *, contracts: Iterable[uuid.UUID] | None = None
) -> None:
    """Провести части, если хранилища менялись после проведения (см. описание модуля).

    `contracts` с одной частью `kort` - начислениям этих договоров: проводятся
    только их документы и соглашения. Пачка решений сверки иначе проводила бы
    документы всей компании на каждый договор.
    """
    if session.new or session.dirty or session.deleted:
        session.flush()
    info = session.info
    fresh: set[str] = info.setdefault(_FRESH, {}).setdefault(workspace_id, set())
    missing = [part for part in PARTS if part in set(parts) and part not in fresh]
    if not missing:
        return
    scope: set[uuid.UUID] | None = None
    if contracts is not None and missing == ["kort"]:
        done: set[uuid.UUID] = info.setdefault(_SCOPED_DONE, {}).setdefault(workspace_id, set())
        scope = set(contracts) - done
        if not scope:
            return
        if len(scope) > _SCOPE_MAX:
            # Много договоров - дешевле провести документы компании целиком.
            scope = None
    tracked = getattr(type(session), _TRACKED, False)
    pending: dict[str, set[uuid.UUID]] = info.setdefault(_PENDING, {}).get(workspace_id) or {}
    # Не верить очереди: слежения нет, была массовая правка без объектов или
    # компания в этом процессе ещё не проводилась (старый процесс на выкладке
    # пишет, не зная очереди).
    blind = not tracked or bool(info.get(_BULK)) or workspace_id not in _CHECKED
    # Своя незакоммиченная правка: метка общая для всех сессий, её правку она ещё не знает.
    own = blind or any(pending.values())
    observed: dict[uuid.UUID, int] = info.setdefault(_OBSERVED, {})
    if not own:
        inputs = _counter(session, workspace_id, INPUTS) or 0
        if _counter(session, workspace_id, POSTED) == inputs:
            fresh.update(PARTS)
            return
    _lock(session, workspace_id)
    inputs = _counter(session, workspace_id, INPUTS) or 0
    posted = _counter(session, workspace_id, POSTED) or 0
    if not own and posted == inputs:
        # Пока ждали замка, провёл другой запрос.
        fresh.update(PARTS)
        return
    from app.finance.models import Workspace

    workspace = session.get(Workspace, workspace_id)
    if scope is not None:
        post(session, workspace, ["kort"], contracts=scope)
        info.setdefault(_SCOPED_DONE, {}).setdefault(workspace_id, set()).update(scope)
        return
    observed.setdefault(workspace_id, inputs)
    work = {part: {WHOLE} for part in PARTS} if blind else _queued(session, workspace_id, posted, inputs)
    for part, refs in pending.items():
        work.setdefault(part, set()).update(refs)
    # Метку «проведено до N» ставит только то проведение, после которого свежи
    # все части: оно проводит и очередь частей, проведённых этой сессией
    # раньше, - иначе чужая правка между двумя проведениями осталась бы за меткой.
    closing = fresh.union(missing).issuperset(PARTS)
    for part in PARTS:
        if part in missing or (closing and work.get(part)):
            _post_part(session, workspace, part, work.get(part) or set())
            fresh.add(part)
            pending.pop(part, None)
    if fresh.issuperset(PARTS):
        # Весь журнал проведён после всех правок этой сессии: коммит унесёт их вместе.
        mark = observed.pop(workspace_id)
        _set_counter(session, workspace_id, POSTED, mark)
        session.execute(
            sa.delete(FactInput).where(FactInput.workspace_id == workspace_id, FactInput.inputs <= mark)
            .execution_options(synchronize_session=False)
        )
        info.get(_PENDING, {}).pop(workspace_id, None)
        # Проверенной компания станет с коммитом: откат унесёт и проведение.
        info.setdefault(_POSTED_HERE, set()).add(workspace_id)


def _queued(session: Session, workspace_id: uuid.UUID, posted: int, inputs: int) -> dict[str, set[uuid.UUID]]:
    """Что записано в очередь правками после проведённой: часть → ключи (`WHOLE` - вся часть).

    Номер правки без строки в очереди - правку писал тот, кто очереди не знает
    (старый процесс на выкладке): тогда не угадывать, что он менял, а
    провести всё.
    """
    if inputs <= posted:
        return {}
    out: dict[str, set[uuid.UUID]] = {}
    numbers: set[int] = set()
    for number, part, ref in session.execute(
        sa.select(FactInput.inputs, FactInput.part, FactInput.ref).where(
            FactInput.workspace_id == workspace_id, FactInput.inputs > posted, FactInput.inputs <= inputs
        )
    ):
        numbers.add(int(number))
        out.setdefault(part, set()).add(ref)
    if len(numbers) < inputs - posted:
        return {part: {WHOLE} for part in PARTS}
    return out


def _post_part(session: Session, workspace: Any, part: str, refs: set[uuid.UUID]) -> None:
    """Провести часть по ключам очереди: вся часть, операции или договоры."""
    if not refs:
        return
    if WHOLE in refs:
        post(session, workspace, [part])
    elif part == "journal" and len(refs) <= _OPERATIONS_MAX:
        post(session, workspace, ["journal"], operations=refs)
    elif part == "kort" and len(refs) <= _SCOPE_MAX:
        post(session, workspace, ["kort"], contracts=refs)
    else:
        post(session, workspace, [part])


# ── События сессии ───────────────────────────────────────────────────────────


def _inputs() -> tuple[type, ...]:
    """Таблицы, от которых зависят факты: хранилища и реестр, по которому идёт разнесение."""
    from app.finance.contracts.models import Contract, ContractAmendment, ContractPayment, CounterpartyName, GroupEntity
    from app.finance.core.models import Document
    from app.finance.models import Account, Counterparty, Operation
    from app.finance.onec.models import OnecBase, OnecDocument

    return (
        Operation, Account, Counterparty, CounterpartyName, GroupEntity, Contract, ContractAmendment,
        ContractPayment, Document, OnecBase, OnecDocument,
    )


def _changed(obj: Any, names: frozenset[str]) -> bool:
    attrs = sa.inspect(obj).attrs
    return any(name in attrs and attrs[name].history.has_changes() for name in names)


def _was(obj: Any, name: str) -> list[Any]:
    """Прежние значения поля в этой правке (договор документа сменили)."""
    return list(sa.inspect(obj).attrs[name].history.deleted or ())


def _dirt(obj: Any, how: str) -> list[tuple[str, uuid.UUID]]:
    """Что правка объекта (`new`, `dirty`, `deleted`) делает несвежим: часть журнала и ключ."""
    from app.finance.contracts.models import Contract, ContractAmendment, ContractPayment, CounterpartyName, GroupEntity
    from app.finance.core.models import Document
    from app.finance.models import Account, Counterparty, Operation
    from app.finance.onec.models import OnecBase, OnecDocument

    shape = [("journal", WHOLE), ("1c", WHOLE)]
    if isinstance(obj, Operation):
        from app.finance.contracts.payments import is_refund

        # Возврат клиенту делает возвращённым прежнее поступление - другую
        # операцию (`contracts/payments.returned_pairs`): такая правка проводит
        # разнесение целиком. Возвраты редки, остальные списания - своим ключом.
        kinds = [obj.kind, *(_was(obj, "kind") if how == "dirty" else ())]
        comments = [obj.comment, *(_was(obj, "comment") if how == "dirty" else ())]
        if any(is_refund(kind, comment) for kind in kinds for comment in comments):
            return [("journal", WHOLE)]
        # Близнецы операции (`contracts/payments.widen`) ищутся по её стороне и
        # сумме: стёртую или сменившую их - прежних близнецов уже не найти.
        if how == "deleted" or (how == "dirty" and _changed(obj, frozenset({"counterparty_id", "amount_base"}))):
            return [("journal", WHOLE)]
        return [("journal", obj.id)]
    if isinstance(obj, ContractPayment):
        # Снятое решение могло быть прецедентом для оплат того же плательщика за
        # другого (У7, `contracts/payments.widen`): их уже не найти - целиком.
        return [("journal", WHOLE)] if how == "deleted" else [("journal", obj.operation_id)]
    if isinstance(obj, (Document, ContractAmendment)):
        ids = {obj.contract_id, *(_was(obj, "contract_id") if how == "dirty" else ())}
        out = [("kort", item if item is not None else WHOLE) for item in ids]
        if isinstance(obj, ContractAmendment) and "executor" in {obj.effect, *(_was(obj, "effect") if how == "dirty" else ())}:
            # Исполнители из соглашений «с даты» видны разнесению оплат и
            # сопоставлению 1С (`contracts/payments.former_executors`): перевод
            # клиента на другое ТОО будущим числом или его отмена меняют, к
            # какому договору ложатся оплаты и документы, хотя сам договор не тронут.
            out += shape
        return out
    if isinstance(obj, Contract):
        if how == "dirty":
            # Примечание, статус, отдел, свои поля - разнесению и 1С всё равно.
            return shape if _changed(obj, SHAPE) else []
        return [*shape, ("kort", obj.id)]
    if isinstance(obj, Counterparty):
        # Новый участник - ещё ничья сторона: операции с ним проводятся своими ключами.
        if how == "new" or (how == "dirty" and not _changed(obj, PARTY)):
            return []
        return shape
    if isinstance(obj, Account):
        if how == "new" or (how == "dirty" and not _changed(obj, frozenset({"group_entity_id"}))):
            return []
        return [("journal", WHOLE)]
    if isinstance(obj, (CounterpartyName, GroupEntity)):
        return shape
    if isinstance(obj, OnecBase):
        # Чтение базы раз в полчаса пишет время, числа и ошибку чтения - факты
        # от них не зависят: только от того, включена ли база и чьё она юрлицо.
        if how == "dirty" and not _changed(obj, frozenset({"state", "group_entity_id"})):
            return []
        return [("1c", WHOLE)]
    if isinstance(obj, OnecDocument):
        return [("1c", WHOLE)]
    return []


def _mark(session: Session, dirt: dict[uuid.UUID, dict[str, set[uuid.UUID]]]) -> None:
    info = session.info
    pending = info.setdefault(_PENDING, {})
    for workspace_id, parts in dirt.items():
        mine = pending.setdefault(workspace_id, {})
        fresh = info.get(_FRESH, {}).get(workspace_id)
        done = info.get(_SCOPED_DONE, {}).get(workspace_id)
        for part, refs in parts.items():
            mine.setdefault(part, set()).update(refs)
            if fresh is not None:
                fresh.discard(part)
            if part == "kort" and done is not None:
                if WHOLE in refs:
                    done.clear()
                else:
                    done.difference_update(refs)
        info.get(_OBSERVED, {}).pop(workspace_id, None)


def _after_flush(session: Session, _context: Any) -> None:
    from app.finance.core.models import RegisterState
    from app.finance.models import ActionLog

    # Запомненные регистры - до любой правки этой сессии (условия договора,
    # настройки оплат, смыслы статусов): дальше их надо проверить заново.
    if REGISTERS in session.info and any(
        not isinstance(obj, RegisterState) for obj in (*session.new, *session.dirty, *session.deleted)
    ):
        session.info.pop(REGISTERS, None)
    inputs = _inputs()
    dirt: dict[uuid.UUID, dict[str, set[uuid.UUID]]] = {}
    seen: set[uuid.UUID] = session.info.setdefault(_SEEN, set())

    def note(obj: Any, how: str) -> None:
        if obj.workspace_id is None:
            return
        seen.add(obj.workspace_id)
        for part, ref in _dirt(obj, how):
            dirt.setdefault(obj.workspace_id, {}).setdefault(part, set()).add(ref)

    for obj in session.new:
        if isinstance(obj, inputs):
            note(obj, "new")
        elif isinstance(obj, ActionLog) and obj.workspace_id is not None:
            # Компания, о которой эта сессия писала журнал действий: на неё же
            # ложатся массовые правки без объектов в сессии.
            seen.add(obj.workspace_id)
    for obj in session.dirty:
        if isinstance(obj, inputs) and session.is_modified(obj, include_collections=False):
            note(obj, "dirty")
    for obj in session.deleted:
        if isinstance(obj, inputs):
            note(obj, "deleted")
    if dirt:
        _mark(session, dirt)


def _on_execute(state: Any) -> None:
    """Массовые `UPDATE`/`DELETE` по таблицам-источникам: объектов в сессии нет, компания неизвестна."""
    if not (state.is_update or state.is_delete):
        return
    mapper = state.bind_mapper
    if mapper is None or not issubclass(mapper.class_, _inputs()):
        return
    if state.is_update:
        values = getattr(state.statement, "_values", None) or {}
        names = {getattr(column, "key", str(column)) for column in values}
        if names and names <= _QUIET:
            return
    session = state.session
    session.info[_BULK] = True
    for key in (_FRESH, _OBSERVED, _SCOPED_DONE):
        session.info.get(key, {}).clear()


def _before_commit(session: Session) -> None:
    if session.in_nested_transaction():
        # Savepoint (первый `bump` счётчика, вложенная пачка): правки ещё не
        # унесены, очередь пишет корневой коммит.
        return
    if session.new or session.dirty or session.deleted:
        session.flush()
    info = session.info
    targets = {
        workspace_id: {part: refs for part, refs in parts.items() if refs}
        for workspace_id, parts in (info.get(_PENDING) or {}).items()
        if any(parts.values())
    }
    if info.get(_BULK):
        seen = info.get(_SEEN) or set()
        if not seen:
            from app.finance.contracts.models import Counter

            seen = set(session.scalars(sa.select(Counter.workspace_id).where(Counter.name == POSTED)))
        for workspace_id in seen:
            targets[workspace_id] = {part: {WHOLE} for part in PARTS}
    if not targets:
        return
    from app.finance.contracts.fields import bump

    for workspace_id, parts in targets.items():
        number = bump(session, workspace_id, INPUTS)
        rows = [
            {"workspace_id": workspace_id, "inputs": number, "part": part, "ref": ref}
            for part, refs in parts.items()
            for ref in ({WHOLE} if WHOLE in refs else refs)
        ]
        for start in range(0, len(rows), _CHUNK):
            session.execute(sa.insert(FactInput.__table__), rows[start : start + _CHUNK])


def _forget(session: Session, *_args: Any) -> None:
    for key in (_PENDING, _FRESH, _OBSERVED, _BULK, _SEEN, _POSTED_HERE, _SCOPED_DONE, REGISTERS):
        session.info.pop(key, None)


def _after_commit(session: Session) -> None:
    if session.in_nested_transaction():
        # SQLAlchemy зовёт это событие и на savepoint: забыть правки сессии
        # здесь значило бы не сдвинуть счётчик правок при корневом коммите.
        return
    _CHECKED.update(session.info.get(_POSTED_HERE) or ())
    _forget(session)


def _after_end(session: Session, transaction: Any) -> None:
    """Корневая транзакция кончилась (откат, закрытие): правки сессии больше не её."""
    if transaction.parent is None:
        _forget(session)


def register(factory: sessionmaker[Session]) -> None:
    """Повесить слежение за хранилищами на фабрику сессий «Финансов» (один раз на фабрику)."""
    if getattr(factory.class_, _TRACKED, False):
        return
    event.listen(factory, "after_flush", _after_flush)
    event.listen(factory, "do_orm_execute", _on_execute)
    event.listen(factory, "before_commit", _before_commit)
    event.listen(factory, "after_commit", _after_commit)
    # Не `after_rollback`: его зовут и на откат savepoint (гонка первого `bump`).
    event.listen(factory, "after_transaction_end", _after_end)
    setattr(factory.class_, _TRACKED, True)


# ── Чтение ───────────────────────────────────────────────────────────────────


def current(
    session: Session,
    workspace_id: uuid.UUID,
    kinds: Iterable[str],
    *,
    contracts: Iterable[uuid.UUID] | None = None,
    sources: Iterable[str] | None = None,
) -> list[Fact]:
    """Действующие факты видов `kinds` (без снятых) - по порядку записи."""
    base = sa.select(Fact).where(
        Fact.workspace_id == workspace_id,
        Fact.ended_seq.is_(None),
        Fact.void.is_(False),
        Fact.kind.in_(list(kinds)),
    )
    if sources is not None:
        base = base.where(Fact.source.in_(list(sources)))
    if contracts is None:
        return list(session.scalars(base.order_by(Fact.seq, Fact.key)))
    ids = list(contracts)
    out: list[Fact] = []
    for start in range(0, len(ids), _CHUNK):
        out.extend(session.scalars(base.where(Fact.contract_id.in_(ids[start : start + _CHUNK]))))
    out.sort(key=lambda fact: (fact.seq, fact.key))
    return out


def ref_of(fact: Fact) -> uuid.UUID:
    """Строка хранилища, из которой проведён факт: операция, документ 1С или ядра, соглашение.

    Часть разделённой оплаты - `op:<операция>:<договор>`: строка - операция.
    """
    return uuid.UUID(fact.key.split(":")[1])


@dataclass(frozen=True)
class Agreement:
    """Соглашение из журнала - в том виде, в каком его читает `core/terms.Timeline`."""

    id: uuid.UUID
    contract_id: uuid.UUID | None
    effective_from: date | None
    position: int
    before: dict[str, Any]
    after: dict[str, Any]
    #: Применено ли к договору: значение договора уже «стало» (не дата - знак).
    applied_at: bool | None


@dataclass(frozen=True)
class Adjustment:
    """Поправка начисления месяца из журнала; `id` - документ ядра, по нему её правят и снимают."""

    id: uuid.UUID
    contract_id: uuid.UUID | None
    period: date | None
    amount: Decimal | None
    skip: bool
    reason: str


def agreements(session: Session, workspace_id: uuid.UUID, contracts: Iterable[uuid.UUID]) -> list[Agreement]:
    out = []
    for fact in current(session, workspace_id, ("agreement",), contracts=contracts):
        data = fact.data or {}
        out.append(
            Agreement(
                id=ref_of(fact), contract_id=fact.contract_id, effective_from=fact.day,
                position=int(data.get("position") or 0), before=dict(data.get("before") or {}),
                after=dict(data.get("after") or {}), applied_at=True if data.get("applied") else None,
            )
        )
    return out


def adjustments(session: Session, workspace_id: uuid.UUID, contracts: Iterable[uuid.UUID]) -> list[Adjustment]:
    return [
        Adjustment(
            id=ref_of(fact), contract_id=fact.contract_id, period=fact.period, amount=fact.amount,
            skip=bool((fact.data or {}).get("skip")), reason=str((fact.data or {}).get("reason") or ""),
        )
        for fact in current(session, workspace_id, ("adjustment",), contracts=contracts)
    ]


def basis_of(fact: Fact) -> str:
    """Основание сальдо: по начислениям или по АВР (`core/opening.py`)."""
    return str(((fact.data or {}).get("meta") or {}).get("basis") or "accruals")


def openings(session: Session, workspace_id: uuid.UUID, since: date, basis: str = "accruals") -> dict[uuid.UUID, Fact]:
    """Входящее сальдо договоров на начало учёта - одного основания."""
    ensure(session, workspace_id, ("kort",))
    return {
        fact.contract_id: fact
        for fact in current(session, workspace_id, ("opening",))
        if fact.contract_id is not None and fact.period == since and basis_of(fact) == basis
    }


__all__ = [
    "Adjustment",
    "Agreement",
    "Draft",
    "PARTS",
    "adjustments",
    "agreements",
    "basis_of",
    "current",
    "ensure",
    "openings",
    "post",
    "ref_of",
    "register",
    "signature_of",
]
