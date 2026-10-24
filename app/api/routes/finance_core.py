"""Ядро KORT (`/finance/core/*`): участники, наши юрлица, налоговые годы.

Права - раздела «Договоры»: участники - стороны договоров, и кто видит
реестр, тот ищет клиента и открывает его досье (договоры в досье - только
открытые ему). Править БИН, полное наименование и написания - правом правки
реестра; сводить участников, собирать написания из сводки и менять налоговый
режим юрлиц - владелец и администратор, как настройку реестра.

Каждое изменение пишет событие журнала само (`core/*`): проверяет
`tests/test_finance_audit.py`.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.routes.finance import _guard, _workspace, require_access
from app.api.routes.finance_contracts import _access, _actor, _raise
from app.finance.auth import Member
from app.finance.core import book as book_module
from app.finance.core import participants, tax
from app.finance.db import finance_session

router = APIRouter(prefix="/finance/core", tags=["finance-core"])

viewer = require_access("contracts", "view")
editor = require_access("contracts", "edit")


def _run(member: Member, action):
    """Вызов ядра в транзакции: отказы - словами и своим кодом."""
    _guard()
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return action(session, workspace, access)
        except Exception as exc:  # noqa: BLE001 - `_raise` сам решает, что словами, а что ошибкой
            _raise(exc)


# ── Участники ────────────────────────────────────────────────────────────────


@router.get("/participants")
def participants_list(
    q: str = Query(default="", max_length=200),
    show: str = Query(default="all", pattern="^(all|own|no_bin)$"),
    limit: int = Query(default=60, ge=1, le=200),
    member: Member = Depends(viewer),
) -> dict[str, Any]:
    """Участники списком или по любому написанию, полному наименованию, началу БИН."""
    return _run(member, lambda s, w, a: participants.listing(s, w, a, q=q, show=show, limit=limit))


@router.get("/participants-overview")
def participants_overview(member: Member = Depends(viewer)) -> dict[str, Any]:
    """Числа плиток: все, наши юрлица, без БИН, двойники, БИН из источников."""
    return _run(member, lambda s, w, a: participants.overview(s, w, a))


@router.get("/participants/duplicates")
def participants_duplicates(member: Member = Depends(viewer)) -> dict[str, Any]:
    """Похоже на одного участника: тот же БИН или то же имя без формы и кавычек."""
    return _run(member, lambda s, w, a: {"groups": participants.duplicates(s, w, a)})


@router.get("/participants/proposals")
def participants_proposals(member: Member = Depends(viewer)) -> dict[str, Any]:
    """БИН из 1С и строк банка в сводке - для участников без БИН. Сводка - уже прочитанная, без ожидания."""

    def action(session, workspace, access):
        book, _error = book_module.current(session, workspace.id, wait=0)
        return {"items": participants.bin_proposals(session, workspace, access, book), "book": book is not None}

    return _run(member, action)


@router.get("/participants/{party_id}")
def participant_dossier(party_id: UUID, member: Member = Depends(viewer)) -> dict[str, Any]:
    """Досье: имена, БИН, договоры у всех наших юрлиц, что требует внимания."""
    return _run(member, lambda s, w, a: participants.dossier(s, w, a, party_id))


class BinIn(BaseModel):
    bin: str = Field(default="", max_length=40)
    source: str = Field(default="manual", pattern="^(manual|registry|bank|1c)$")


@router.put("/participants/{party_id}/bin")
def participant_bin(party_id: UUID, body: BinIn, member: Member = Depends(editor)) -> dict[str, Any]:
    return _run(member, lambda s, w, a: participants.set_bin(s, w, a, _actor(member), party_id, body.bin, source=body.source))


class RejectIn(BaseModel):
    bin: str = Field(min_length=12, max_length=40)


@router.post("/participants/{party_id}/bin/reject")
def participant_bin_reject(party_id: UUID, body: RejectIn, member: Member = Depends(editor)) -> dict[str, Any]:
    return _run(member, lambda s, w, a: participants.reject_bin(s, w, a, _actor(member), party_id, body.bin))


class ParticipantPatch(BaseModel):
    full_name: str | None = Field(default=None, max_length=500)


@router.patch("/participants/{party_id}")
def participant_update(party_id: UUID, body: ParticipantPatch, member: Member = Depends(editor)) -> dict[str, Any]:
    data = {key: value for key, value in body.model_dump().items() if value is not None}
    return _run(member, lambda s, w, a: participants.update(s, w, a, _actor(member), party_id, data))


class AliasIn(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    source: str = Field(default="manual", pattern="^(manual|registry|bank|1c)$")


@router.post("/participants/{party_id}/aliases", status_code=201)
def participant_alias(party_id: UUID, body: AliasIn, member: Member = Depends(editor)) -> dict[str, Any]:
    return _run(member, lambda s, w, a: participants.add_alias(s, w, a, _actor(member), party_id, body.name, body.source))


@router.delete("/participants/{party_id}/aliases/{alias_id}")
def participant_alias_remove(party_id: UUID, alias_id: UUID, member: Member = Depends(editor)) -> dict[str, Any]:
    return _run(member, lambda s, w, a: participants.remove_alias(s, w, a, _actor(member), party_id, alias_id))


class MergeIn(BaseModel):
    keep: UUID
    drop: UUID


@router.post("/participants/merge")
def participants_merge(body: MergeIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Свести двух участников в одного - с точкой восстановления."""
    return _run(member, lambda s, w, a: participants.merge(s, w, a, _actor(member), keep=body.keep, drop=body.drop))


class DistinctIn(BaseModel):
    ids: list[UUID] = Field(min_length=2, max_length=20)


@router.post("/participants/distinct")
def participants_distinct(body: DistinctIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """«Это разные» - группа больше не предлагается в двойниках."""
    return _run(member, lambda s, w, a: participants.mark_distinct(s, w, a, _actor(member), body.ids))


def _book(session, workspace) -> book_module.Book:
    book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
    if book is None:
        raise HTTPException(status_code=424, detail=error or "Книга-сводка компании не подключена или ещё читается")
    return book


@router.get("/participants-harvest")
def harvest_preview(member: Member = Depends(viewer)) -> dict[str, Any]:
    """Что запишется из сводки: написания клиентов и коды наших юрлиц."""
    return _run(member, lambda s, w, a: participants.harvest(s, w, a, _actor(member), _book(s, w), apply=False))


@router.post("/participants-harvest")
def harvest_apply(member: Member = Depends(editor)) -> dict[str, Any]:
    """Записать написания и коды из сводки (только однозначные)."""
    return _run(member, lambda s, w, a: participants.harvest(s, w, a, _actor(member), _book(s, w), apply=True))


# ── Наши юрлица: режим по датам ──────────────────────────────────────────────


class PeriodIn(BaseModel):
    valid_from: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    regime: str = Field(default="general", pattern="^(general|simplified|other)$")
    vat_payer: bool = False
    note: str = Field(default="", max_length=500)


@router.post("/entities/{entity_id}/tax", status_code=201)
def entity_tax_add(entity_id: UUID, body: PeriodIn, member: Member = Depends(editor)) -> dict[str, Any]:
    return _run(member, lambda s, w, a: tax.add_period(s, w, a, _actor(member), entity_id, body.model_dump()))


@router.delete("/entities/{entity_id}/tax/{period_id}")
def entity_tax_remove(entity_id: UUID, period_id: UUID, member: Member = Depends(editor)) -> dict[str, Any]:
    return _run(member, lambda s, w, a: tax.remove_period(s, w, a, _actor(member), entity_id, period_id))


# ── Налоговые годы ───────────────────────────────────────────────────────────


@router.get("/tax-years")
def tax_years(member: Member = Depends(viewer)) -> dict[str, Any]:
    """МРП, порог и ставка: прошлые, текущий и следующий год (прогнозом, пока нет закона)."""

    def action(session, workspace, _access):
        from datetime import date

        known = tax.years(session, workspace.id)
        this = date.today().year
        wanted = sorted(set(known) | {this, this + 1})
        return {"items": [tax.year_of(session, workspace.id, year, known).out() for year in wanted]}

    return _run(member, action)


class YearIn(BaseModel):
    mrp: str = Field(max_length=20)
    threshold_mrp: int = Field(ge=1, le=10_000_000)
    vat_rate: str = Field(max_length=10)
    forecast: bool = False
    note: str = Field(default="", max_length=500)


@router.put("/tax-years/{year}")
def tax_year_set(year: int, body: YearIn, member: Member = Depends(editor)) -> dict[str, Any]:
    return _run(member, lambda s, w, a: tax.set_year(s, w, a, _actor(member), year, body.model_dump()))
