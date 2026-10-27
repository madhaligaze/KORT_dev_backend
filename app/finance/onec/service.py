"""Базы 1С компании: подключение, чтение, решения человека, месяц по договорам.

Чтение базы - сеть, и держать на ней транзакцию учёта незачем: настройки
берутся одной короткой сессией, 1С читается без базы KORT, прочитанное
пишется второй сессией (`sync_base`). Поэтому «Обновить» у одного человека не
держит блокировки другим, сколько бы 1С ни думала.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history, secretbox
from app.finance.contracts.models import Contract, GroupEntity, ListValue
from app.finance.contracts.setup import remember_alias
from app.finance.db import finance_session
from app.finance.models import Counterparty, Workspace
from app.finance.onec import match as match_module
from app.finance.onec import sync as sync_module
from app.finance.onec.client import Client, Connection, OnecError, check_host, normalize_url
from app.finance.onec.models import OnecBase, OnecDocument
from app.finance.service import FinanceError

log = logging.getLogger(__name__)

KIND_TITLES = {
    "invoice": "счёт на оплату",
    "sale": "АВР",
    "sf": "счёт-фактура",
    "esf": "ЭСФ",
    "payment": "поступление",
    "refund": "возврат покупателю",
}
#: Фазы статуса, при которых абонентский договор ждёт документы в месяце.
WORKING_PHASES = ("active", "in_progress")


# ── Базы ─────────────────────────────────────────────────────────────────────


def list_bases(session: Session, workspace_id: uuid.UUID) -> list[OnecBase]:
    return list(
        session.scalars(
            sa.select(OnecBase).where(OnecBase.workspace_id == workspace_id).order_by(OnecBase.created_at)
        )
    )


def get_base(session: Session, workspace_id: uuid.UUID, base_id: uuid.UUID) -> OnecBase:
    base = session.get(OnecBase, base_id)
    if base is None or base.workspace_id != workspace_id:
        raise FinanceError("База 1С не найдена")
    return base


def entities(session: Session, workspace_id: uuid.UUID) -> list[dict[str, str]]:
    """Наши юрлица реестра - к ним привязываются базы."""
    rows = session.execute(
        sa.select(GroupEntity.counterparty_id, Counterparty.name, GroupEntity.bin)
        .join(Counterparty, Counterparty.id == GroupEntity.counterparty_id)
        .where(GroupEntity.workspace_id == workspace_id, GroupEntity.archived_at.is_(None))
        .order_by(GroupEntity.position, Counterparty.name)
    ).all()
    return [{"id": str(party_id), "name": name, "bin": (bin_value or "").strip()} for party_id, name, bin_value in rows]


def base_out(base: OnecBase, names: dict[uuid.UUID, str] | None = None) -> dict[str, Any]:
    names = names or {}
    return {
        "id": str(base.id),
        "title": base.title,
        "url": base.url,
        "login": base.login,
        "state": base.state,
        "group_entity_id": str(base.group_entity_id) if base.group_entity_id else None,
        "group_entity": names.get(base.group_entity_id, "") if base.group_entity_id else "",
        "organization_ref": base.organization_ref,
        "organization_name": base.organization_name,
        "organization_bin": base.organization_bin,
        "organizations": base.organizations or [],
        "sync_from": base.sync_from.isoformat() if base.sync_from else None,
        "last_sync_at": base.last_sync_at.isoformat() if base.last_sync_at else None,
        "last_ok_at": base.last_ok_at.isoformat() if base.last_ok_at else None,
        "last_error": base.last_error or "",
        "warnings": base.warnings or [],
        "counts": base.counts or {},
    }


def _entity_by_bin(session: Session, workspace_id: uuid.UUID, bin_value: str) -> uuid.UUID | None:
    """Наше юрлицо с тем же БИН - привязка сама, когда сомнений нет."""
    if not bin_value:
        return None
    found = list(
        session.scalars(
            sa.select(GroupEntity.counterparty_id).where(
                GroupEntity.workspace_id == workspace_id,
                GroupEntity.archived_at.is_(None),
                GroupEntity.bin == bin_value,
            )
        )
    )
    return found[0] if len(found) == 1 else None


def connect(
    session: Session,
    workspace: Workspace,
    *,
    url: str,
    login: str,
    password: str,
    title: str = "",
    group_entity_id: uuid.UUID | None = None,
    actor: str = "",
) -> OnecBase:
    """Подключить базу: проверить вход сразу и запомнить организации базы."""
    address = normalize_url(url)
    check_host(address)
    login = (login or "").strip()
    if not login or not password:
        raise FinanceError("Нужны логин и пароль автоматического REST-сервиса базы")
    exists = session.scalar(
        sa.select(OnecBase.id).where(OnecBase.workspace_id == workspace.id, OnecBase.url == address)
    )
    if exists is not None:
        raise FinanceError("Эта база 1С уже подключена")
    if group_entity_id is not None:
        _check_entity(session, workspace.id, group_entity_id)
    secret = secretbox.seal(password)
    found = sync_module.organizations(Client(Connection(address, login, password)))
    base = OnecBase(
        workspace_id=workspace.id,
        title=(title or "").strip(),
        url=address,
        login=login,
        secret=secret,
        group_entity_id=group_entity_id,
        organizations=found,
        sync_from=sync_module.default_since(),
        created_by=actor,
    )
    if len(found) == 1:
        base.organization_ref = found[0]["ref"]
        base.organization_name = found[0]["name"]
        base.organization_bin = found[0]["bin"]
        if base.group_entity_id is None:
            base.group_entity_id = _entity_by_bin(session, workspace.id, found[0]["bin"])
    if not base.title:
        base.title = base.organization_name or address.rsplit("/", 1)[-1]
    session.add(base)
    session.flush()
    return base


def _check_entity(session: Session, workspace_id: uuid.UUID, entity_id: uuid.UUID) -> None:
    entity = session.get(GroupEntity, entity_id)
    if entity is None or entity.workspace_id != workspace_id:
        raise FinanceError("Такого юрлица среди наших нет")


def update(
    session: Session,
    workspace: Workspace,
    base_id: uuid.UUID,
    changes: dict[str, Any],
) -> tuple[OnecBase, dict[str, Any], dict[str, Any]]:
    """Поправить базу: юрлицо, организацию, название, вход, состояние, окно чтения."""
    base = get_base(session, workspace.id, base_id)
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    def put(name: str, value: Any, shown: Any = None) -> None:
        old = getattr(base, name)
        if old == value:
            return
        before[name] = str(old) if old is not None else None
        after[name] = shown if shown is not None else (str(value) if value is not None else None)
        setattr(base, name, value)

    if "title" in changes:
        title = str(changes["title"] or "").strip()
        if not title:
            raise FinanceError("У базы должно быть название")
        put("title", title)
    if "group_entity_id" in changes:
        raw = changes["group_entity_id"]
        entity_id = uuid.UUID(str(raw)) if raw else None
        if entity_id is not None:
            _check_entity(session, workspace.id, entity_id)
        put("group_entity_id", entity_id)
    if "organization_ref" in changes:
        ref = str(changes["organization_ref"] or "")
        chosen = next((item for item in base.organizations or [] if item["ref"] == ref), None)
        if chosen is None:
            raise FinanceError("Такой организации в базе нет - обновите базу и выберите снова")
        put("organization_ref", ref)
        base.organization_name, base.organization_bin = chosen["name"], chosen["bin"]
    if "state" in changes:
        state = str(changes["state"])
        if state not in ("active", "off"):
            raise FinanceError("Состояние бывает «active» или «off»")
        put("state", state)
    if "sync_from" in changes:
        try:
            since = date.fromisoformat(str(changes["sync_from"]))
        except ValueError as exc:
            raise FinanceError("С какой даты читать: ждём дату вида 2026-01-01") from exc
        if since.year < 2000:
            raise FinanceError("Читать раньше 2000 года нечего")
        put("sync_from", since)
    if "login" in changes or "password" in changes:
        login = str(changes.get("login") or base.login).strip()
        password = str(changes.get("password") or "")
        if not password:
            raise FinanceError("Новый вход - вместе с паролем")
        sync_module.organizations(Client(Connection(base.url, login, password)))
        if login != base.login:
            before["login"], after["login"] = base.login, login
        base.login = login
        base.secret = secretbox.seal(password)
        after["password"] = "заменён"
    session.flush()
    return base, before, after


def remove(session: Session, workspace_id: uuid.UUID, base_id: uuid.UUID) -> OnecBase:
    base = get_base(session, workspace_id, base_id)
    session.execute(sa.delete(OnecDocument).where(OnecDocument.base_id == base.id))
    session.delete(base)
    session.flush()
    return base


@dataclass(frozen=True)
class SyncResult:
    ok: bool
    error: str
    counts: dict[str, int]
    warnings: list[str]


def sync_base(workspace_id: uuid.UUID, base_id: uuid.UUID) -> SyncResult:
    """Перечитать базу (см. описание модуля): настройки → 1С → запись."""
    with finance_session() as session:
        base = get_base(session, workspace_id, base_id)
        try:
            connection = sync_module.connection_of(base)
        except secretbox.SecretError as exc:
            base.last_error, base.last_sync_at = str(exc), datetime.now(timezone.utc)
            return SyncResult(False, str(exc), {}, [])
        chosen_ref, since = base.organization_ref, base.sync_from
    snapshot: sync_module.Snapshot | None = None
    error = ""
    try:
        client = Client(connection)
        found = sync_module.organizations(client)
        with finance_session() as session:
            base = get_base(session, workspace_id, base_id)
            base.organizations = found
            chosen_ref = sync_module.choose_organization(base, found)
        snapshot = sync_module.read(client, organization_ref=chosen_ref, since=since)
    except OnecError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - чужая система не роняет учёт
        log.exception("1С: чтение базы %s", base_id)
        error = f"Чтение 1С оборвалось: {exc.__class__.__name__}"
    with finance_session() as session:
        base = get_base(session, workspace_id, base_id)
        base.last_sync_at = datetime.now(timezone.utc)
        if snapshot is None:
            base.last_error = error
            return SyncResult(False, error, dict(base.counts or {}), list(base.warnings or []))
        chosen = next((item for item in snapshot.organizations if item["ref"] == chosen_ref), None)
        if chosen is not None:
            base.organization_ref, base.organization_name, base.organization_bin = (
                chosen["ref"], chosen["name"], chosen["bin"],
            )
            if base.group_entity_id is None:
                base.group_entity_id = _entity_by_bin(session, base.workspace_id, chosen["bin"])
        counts = sync_module.apply(session, base, snapshot, since=since)
        base.counts, base.warnings, base.last_error = counts, snapshot.warnings, ""
        base.last_ok_at = base.last_sync_at
        return SyncResult(True, "", counts, snapshot.warnings)


def sync_due(minutes: int) -> int:
    """Фоновый проход: перечитать включённые базы, которые давно не читались."""
    if minutes <= 0:
        return 0
    now = datetime.now(timezone.utc)
    with finance_session() as session:
        due = [
            (base.workspace_id, base.id)
            for base in session.scalars(sa.select(OnecBase).where(OnecBase.state == "active"))
            if base.last_sync_at is None or (now - base.last_sync_at).total_seconds() >= minutes * 60
        ]
    done = 0
    for workspace_id, base_id in due:
        result = sync_base(workspace_id, base_id)
        done += 1 if result.ok else 0
    return done


# ── Документы ────────────────────────────────────────────────────────────────


def short_number(number: str) -> str:
    """«00000000444» → «444»: так номер пишут люди и книги."""
    text = (number or "").strip()
    stripped = text.lstrip("0")
    return stripped if stripped and stripped.isdigit() else text


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(Decimal("0.01")), "f")


def doc_out(doc: OnecDocument, found: match_module.Match | None = None) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "kind": doc.kind,
        "number": short_number(doc.number),
        "date": doc.doc_date.isoformat(),
        "amount": _money(doc.amount),
        "posted": doc.posted,
        "party": doc.party_name or doc.party_full_name,
        "party_bin": doc.party_bin,
        "contract_number": doc.contract_number,
        "signed_at": doc.signed_at.isoformat() if doc.signed_at else None,
        "status": doc.status,
        "reg_number": doc.reg_number,
        "period_from": doc.period_from.isoformat() if doc.period_from else None,
        "period_to": doc.period_to.isoformat() if doc.period_to else None,
        "content": (doc.content or "")[:300],
        "base_id": str(doc.base_id),
        "how": found.how if found else "",
        "manual": doc.manual,
    }


def _contract_brief(session: Session, ids: set[uuid.UUID], index: match_module.Index) -> dict[uuid.UUID, dict[str, Any]]:
    if not ids:
        return {}
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for contract in session.scalars(sa.select(Contract).where(Contract.id.in_(ids))):
        out[contract.id] = {
            "contract_id": str(contract.id),
            "number": contract.number,
            "customer": index.names.get(contract.customer_id, "") if contract.customer_id else "",
            "executor": index.names.get(contract.executor_id, "") if contract.executor_id else "",
            "amount": _money(contract.amount),
            "billing": contract.billing,
        }
    return out


def _documents(session: Session, workspace_id: uuid.UUID, base_ids: set[uuid.UUID]) -> list[OnecDocument]:
    if not base_ids:
        return []
    return list(
        session.scalars(
            sa.select(OnecDocument).where(
                OnecDocument.workspace_id == workspace_id,
                OnecDocument.base_id.in_(base_ids),
                OnecDocument.deleted.is_(False),
                OnecDocument.gone.is_(False),
            )
        )
    )


def _working(session: Session, workspace_id: uuid.UUID, entity_ids: set[uuid.UUID], month: str) -> list[Contract]:
    """Абонентские договоры наших юрлиц, действующие в месяце, - ждут документов."""
    if not entity_ids:
        return []
    year, number = (int(part) for part in month.split("-"))
    start = date(year, number, 1)
    end = date(year + (number == 12), number % 12 + 1, 1)
    phases = {
        value.id
        for value in session.scalars(sa.select(ListValue).where(ListValue.workspace_id == workspace_id, ListValue.field_key == "status"))
        if (value.meaning or {}).get("phase") in WORKING_PHASES
    }
    if not phases:
        return []
    return list(
        session.scalars(
            sa.select(Contract).where(
                Contract.workspace_id == workspace_id,
                Contract.deleted_at.is_(None),
                Contract.billing == "month",
                Contract.executor_id.in_(entity_ids),
                Contract.status_id.in_(phases),
                sa.or_(Contract.signed_at.is_(None), Contract.signed_at < end),
                sa.or_(Contract.end_date.is_(None), Contract.end_date >= start),
            )
        )
    )


def month_view(
    session: Session, workspace: Workspace, month: str, *, base_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Месяц по договорам: счёт, оплата, АВР, подписание, ЭСФ - как колонки книг BBC."""
    try:
        year, number = (int(part) for part in month.split("-"))
        date(year, number, 1)
    except ValueError as exc:
        raise FinanceError("Месяц - вида 2026-09") from exc
    bases = {base.id: base for base in list_bases(session, workspace.id) if base_id is None or base.id == base_id}
    docs = _documents(session, workspace.id, set(bases))
    index = match_module.build_index(session, workspace.id)
    resolved = match_module.resolve(docs, bases, index)

    rows: dict[uuid.UUID, dict[str, Any]] = {}
    unmatched: list[dict[str, Any]] = []
    for doc in sorted(docs, key=lambda item: (item.doc_date, item.number)):
        if resolved.months.get(doc.id) != month:
            continue
        found = resolved.matches[doc.id]
        if found.state == "ok" and found.contract_id is not None:
            row = rows.setdefault(found.contract_id, {"docs": {kind: [] for kind in KIND_TITLES}})
            row["docs"][doc.kind].append(doc_out(doc, found))
        elif found.state != "none":
            unmatched.append({**doc_out(doc, found), "state": found.state, "candidates": [str(item) for item in found.candidates]})

    entity_ids = {base.group_entity_id for base in bases.values() if base.group_entity_id}
    waiting = {contract.id for contract in _working(session, workspace.id, entity_ids, month)} - set(rows)
    for contract_id in waiting:
        rows[contract_id] = {"docs": {kind: [] for kind in KIND_TITLES}}
    briefs = _contract_brief(
        session,
        set(rows) | {uuid.UUID(item) for entry in unmatched for item in entry["candidates"]},
        index,
    )
    out_rows = []
    for contract_id, row in rows.items():
        brief = briefs.get(contract_id)
        if brief is None:
            continue
        docs_of = row["docs"]
        sales = docs_of["sale"]
        signed = [item["signed_at"] for item in sales if item["signed_at"]]
        paid = sum((Decimal(item["amount"]) for item in docs_of["payment"] if item["amount"]), Decimal("0"))
        out_rows.append(
            {
                **brief,
                "invoices": docs_of["invoice"],
                "payments": docs_of["payment"],
                "paid": _money(paid) if docs_of["payment"] else None,
                "sales": sales,
                "signed_at": max(signed) if sales and len(signed) == len(sales) else None,
                # ЭСФ - документ ЭСФ, а где его в 1С нет - счёт-фактура: так
                # ведёт база, где ЭСФ выписывают на портале, а в 1С ставят
                # счёт-фактуру с датой выписки.
                "esf": docs_of["esf"] or docs_of["sf"],
                "esf_kind": "esf" if docs_of["esf"] else ("sf" if docs_of["sf"] else ""),
            }
        )
    out_rows.sort(key=lambda item: (item["customer"].lower(), item["number"]))
    candidates = {str(key): value for key, value in briefs.items()}
    return {
        "month": month,
        "rows": out_rows,
        "unmatched": unmatched,
        "candidates": candidates,
        "totals": {
            "contracts": len(out_rows),
            "invoices": sum(1 for row in out_rows if row["invoices"]),
            "paid": sum(1 for row in out_rows if row["payments"]),
            "sales": sum(1 for row in out_rows if row["sales"]),
            "signed": sum(1 for row in out_rows if row["signed_at"]),
            "esf": sum(1 for row in out_rows if row["esf"]),
            "unmatched": len(unmatched),
        },
        "months": sorted({value for value in resolved.months.values() if value}, reverse=True),
    }


def contract_documents(session: Session, workspace: Workspace, contract_id: uuid.UUID) -> list[dict[str, Any]]:
    """Документы 1С одного договора - для карточки."""
    bases = {base.id: base for base in list_bases(session, workspace.id)}
    docs = _documents(session, workspace.id, set(bases))
    index = match_module.build_index(session, workspace.id)
    resolved = match_module.resolve(docs, bases, index)
    out = [
        {**doc_out(doc, resolved.matches[doc.id]), "month": resolved.months.get(doc.id, "")}
        for doc in docs
        if resolved.matches[doc.id].contract_id == contract_id
    ]
    out.sort(key=lambda item: (item["date"], item["kind"]), reverse=True)
    return out


def decide(
    session: Session,
    workspace: Workspace,
    document_id: uuid.UUID,
    *,
    action: str,
    contract_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
) -> tuple[OnecDocument, Contract | None]:
    """Решение человека: `link` - к договору, `none` - не по договору, `auto` - снять."""
    doc = session.get(OnecDocument, document_id)
    if doc is None or doc.workspace_id != workspace.id:
        raise FinanceError("Документ 1С не найден")
    contract: Contract | None = None
    if action == "link":
        if contract_id is None:
            raise FinanceError("К какому договору отнести - не указано")
        contract = session.get(Contract, contract_id)
        if contract is None or contract.workspace_id != workspace.id or contract.deleted_at is not None:
            raise FinanceError("Такого договора в реестре нет")
        doc.manual, doc.manual_contract_id = "link", contract.id
        # Написание клиента из 1С запоминается за стороной договора: следующие
        # документы этого клиента найдут договор сами.
        if contract.customer_id is not None and doc.party_name:
            remember_alias(session, workspace.id, contract.customer_id, doc.party_name, source="1c")
    elif action == "none":
        doc.manual, doc.manual_contract_id = "none", None
    elif action == "auto":
        doc.manual, doc.manual_contract_id = "", None
    else:
        raise FinanceError("Ждём «link», «none» или «auto»")
    doc.manual_by, doc.manual_at = user_id, datetime.now(timezone.utc)
    session.flush()
    return doc, contract


def write_history(session: Session, workspace: Workspace, *, kind: str, base: OnecBase | None = None, title: str,
                  before: dict[str, Any] | None = None, after: dict[str, Any] | None = None,
                  entity_id: uuid.UUID | None = None, actor: str = "") -> None:
    history.write(
        session,
        workspace,
        kind=kind,
        entity="onec",
        entity_id=entity_id or (base.id if base is not None else None),
        title=title,
        before=before,
        after=after,
        actor=actor,
    )


__all__ = [
    "KIND_TITLES",
    "SyncResult",
    "base_out",
    "connect",
    "contract_documents",
    "decide",
    "doc_out",
    "entities",
    "get_base",
    "list_bases",
    "month_view",
    "remove",
    "short_number",
    "sync_base",
    "sync_due",
    "update",
    "write_history",
]
