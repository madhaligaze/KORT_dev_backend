"""Базы 1С (`/finance/onec/*`): подключение, чтение, месяц по договорам, решения.

Право - «Подключения» (`integrations`): база 1С подключается там же, где
банки, а месяц по договорам показывает суммы счетов и поступлений - это
деньги, и открывать их вместе с реестром договоров нельзя
(`project-money-visibility`).

Чтение базы - синхронное: человек нажал «Обновить» и ждёт ответа с числами.
Прокси фронта ждёт API до трёх минут; база BBC за год читается за десятки
секунд.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import aliased

from app.api.routes.finance import _actor, _fail, _guard, _workspace, require_access
from app.finance import secretbox
from app.finance.auth import Member
from app.finance.config import finance_settings
from app.finance.contracts.models import Contract
from app.finance.db import finance_session
from app.finance.models import Counterparty
from app.finance.onec import service
from app.finance.onec.client import OnecError
from app.finance.service import FinanceError

router = APIRouter(prefix="/finance/onec", tags=["finance-onec"])


def _refuse(exc: Exception) -> HTTPException:
    if isinstance(exc, FinanceError):
        return _fail(exc)
    return HTTPException(status_code=400, detail=str(exc))


def _names(session, workspace_id: UUID) -> dict[UUID, str]:
    return {UUID(item["id"]): item["name"] for item in service.entities(session, workspace_id)}


@router.get("")
def onec_overview(member: Member = Depends(require_access("integrations"))) -> dict[str, Any]:
    """Подключённые базы, наши юрлица для привязки и готов ли сервер хранить пароли."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        names = _names(session, workspace.id)
        return {
            "bases": [service.base_out(base, names) for base in service.list_bases(session, workspace.id)],
            "entities": service.entities(session, workspace.id),
            "secrets_ready": secretbox.ready(),
            "sync_minutes": finance_settings.onec_sync_minutes,
        }


class BaseIn(BaseModel):
    url: str = Field(min_length=3, max_length=500)
    login: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=500)
    title: str = Field(default="", max_length=200)
    group_entity_id: UUID | None = None


@router.post("/bases", status_code=201)
def onec_connect(body: BaseIn, member: Member = Depends(require_access("integrations", "edit"))) -> dict[str, Any]:
    """Подключить базу: вход проверяется сразу, документы читает «Обновить»."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            base = service.connect(
                session,
                workspace,
                url=body.url,
                login=body.login,
                password=body.password,
                title=body.title,
                group_entity_id=body.group_entity_id,
                actor=_actor(member),
            )
        except (FinanceError, OnecError, secretbox.SecretError) as exc:
            raise _refuse(exc) from exc
        service.write_history(
            session,
            workspace,
            kind="onec.base.create",
            base=base,
            title=f"база 1С «{base.title}» подключена ({base.url})",
            after={"url": base.url, "login": base.login, "organization": base.organization_name},
            actor=_actor(member),
        )
        return service.base_out(base, _names(session, workspace.id))


class BasePatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    group_entity_id: UUID | None = None
    clear_group_entity: bool = False
    organization_ref: str | None = Field(default=None, max_length=64)
    state: str | None = Field(default=None, pattern="^(active|off)$")
    sync_from: str | None = Field(default=None, max_length=10)
    login: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, max_length=500)


@router.patch("/bases/{base_id}")
def onec_update(
    base_id: UUID, body: BasePatch, member: Member = Depends(require_access("integrations", "edit"))
) -> dict[str, Any]:
    _guard()
    changes: dict[str, Any] = {
        key: value
        for key, value in body.model_dump().items()
        if value is not None and key not in ("clear_group_entity", "group_entity_id")
    }
    if body.group_entity_id is not None or body.clear_group_entity:
        changes["group_entity_id"] = body.group_entity_id
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            base, before, after = service.update(session, workspace, base_id, changes)
        except (FinanceError, OnecError, secretbox.SecretError) as exc:
            raise _refuse(exc) from exc
        names = _names(session, workspace.id)
        if "group_entity_id" in after:
            after["group_entity"] = names.get(base.group_entity_id, "") if base.group_entity_id else ""
        service.write_history(
            session,
            workspace,
            kind="onec.base.update",
            base=base,
            title=f"база 1С «{base.title}» изменена",
            before=before,
            after=after,
            actor=_actor(member),
        )
        return service.base_out(base, names)


@router.delete("/bases/{base_id}")
def onec_remove(base_id: UUID, member: Member = Depends(require_access("integrations", "edit"))) -> dict[str, bool]:
    """Отключить базу. Снимок её документов уходит вместе с ней; в 1С ничего не меняется."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            base = service.remove(session, workspace.id, base_id)
        except FinanceError as exc:
            raise _fail(exc) from exc
        service.write_history(
            session,
            workspace,
            kind="onec.base.delete",
            entity_id=base_id,
            title=f"база 1С «{base.title}» отключена",
            before={"url": base.url},
            actor=_actor(member),
        )
        return {"ok": True}


@router.post("/bases/{base_id}/sync")
def onec_sync(base_id: UUID, member: Member = Depends(require_access("integrations", "edit"))) -> dict[str, Any]:
    """Перечитать базу сейчас. Ответ - числа прочитанного или причина отказа."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            service.get_base(session, workspace.id, base_id)
        except FinanceError as exc:
            raise _fail(exc) from exc
        workspace_id = workspace.id
    result = service.sync_base(workspace_id, base_id)
    with finance_session() as session:
        workspace = _workspace(session, member)
        base = service.get_base(session, workspace.id, base_id)
        words = ", ".join(f"{service.KIND_TITLES[kind]}: {count}" for kind, count in sorted(result.counts.items()))
        service.write_history(
            session,
            workspace,
            kind="onec.base.sync",
            base=base,
            title=(
                f"база 1С «{base.title}» прочитана" + (f" - {words}" if words else "")
                if result.ok
                else f"база 1С «{base.title}» не прочиталась: {result.error}"
            ),
            after={"ok": result.ok, "counts": result.counts, "error": result.error},
            actor=_actor(member),
        )
        return {"ok": result.ok, "error": result.error, "base": service.base_out(base, _names(session, workspace.id))}


@router.get("/month")
def onec_month(
    month: str = Query(pattern=r"^\d{4}-\d{2}$"),
    base: UUID | None = None,
    member: Member = Depends(require_access("integrations")),
) -> dict[str, Any]:
    """Месяц по договорам: счёт, оплата, АВР, подписание, ЭСФ и что не сопоставилось."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return service.month_view(session, workspace, month, base_id=base)
        except FinanceError as exc:
            raise _fail(exc) from exc


@router.get("/choices")
def onec_choices(
    base: UUID,
    q: str = Query(default="", max_length=100),
    member: Member = Depends(require_access("integrations")),
) -> dict[str, Any]:
    """Договоры юрлица базы - выбрать, к какому отнести документ."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            item = service.get_base(session, workspace.id, base)
        except FinanceError as exc:
            raise _fail(exc) from exc
        if item.group_entity_id is None:
            return {"items": []}
        customer = aliased(Counterparty)
        query = (
            sa.select(Contract.id, Contract.number, customer.name, Contract.amount)
            .outerjoin(customer, customer.id == Contract.customer_id)
            .where(
                Contract.workspace_id == workspace.id,
                Contract.deleted_at.is_(None),
                Contract.executor_id == item.group_entity_id,
            )
            .order_by(customer.name, Contract.number)
            .limit(30)
        )
        text = q.strip()
        if text:
            like = f"%{text}%"
            query = query.where(sa.or_(Contract.number.ilike(like), customer.name.ilike(like)))
        return {
            "items": [
                {
                    "contract_id": str(contract_id),
                    "number": number,
                    "customer": name or "",
                    "amount": format(amount, "f") if amount is not None else None,
                }
                for contract_id, number, name, amount in session.execute(query)
            ]
        }


class DecideIn(BaseModel):
    action: str = Field(pattern="^(link|none|auto)$")
    contract_id: UUID | None = None


@router.post("/documents/{document_id}/decide")
def onec_decide(
    document_id: UUID, body: DecideIn, member: Member = Depends(require_access("integrations", "edit"))
) -> dict[str, Any]:
    """Человек решает за сопоставление: «к этому договору», «не по договору», «снова сама»."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            doc, contract = service.decide(
                session, workspace, document_id, action=body.action, contract_id=body.contract_id,
                user_id=member.user_id,
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        what = f"{service.KIND_TITLES[doc.kind]} {service.short_number(doc.number)} от {doc.doc_date:%d.%m.%Y}"
        words = {
            "link": f"отнесён к договору {contract.number if contract else ''}".strip(),
            "none": "не по договору реестра",
            "auto": "снова сопоставляется сам",
        }[body.action]
        service.write_history(
            session,
            workspace,
            kind="onec.document.decide",
            entity_id=doc.id,
            title=f"1С: {what} ({doc.party_name}) - {words}",
            after={"action": body.action, "contract_id": str(contract.id) if contract else None},
            actor=_actor(member),
        )
        return service.doc_out(doc)


@router.get("/contracts/{contract_id}")
def onec_contract(contract_id: UUID, member: Member = Depends(require_access("integrations"))) -> dict[str, Any]:
    """Документы 1С одного договора."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {"items": service.contract_documents(session, workspace, contract_id)}
