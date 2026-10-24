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
from app.finance.contracts import service as contracts_service
from app.finance.core import book as book_module
from app.finance.core import changes, participants, tax
from app.finance.core import shadow as shadow_module
from app.finance.db import finance_session

router = APIRouter(prefix="/finance/core", tags=["finance-core"])

viewer = require_access("contracts", "view")
editor = require_access("contracts", "edit")


def _run(member: Member, action):
    """Вызов ядра в транзакции: отказы - словами и своим кодом.

    `_raise` бросает 4xx сам, а «нужен режим правки» и конфликт поля
    возвращает ответом - его надо вернуть, иначе клиент получал `null` с 200.
    """
    _guard()
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return action(session, workspace, access)
        except Exception as exc:  # noqa: BLE001 - `_raise` сам решает, что словами, а что ошибкой
            return _raise(exc)


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


# ── Начисления и сверка с книгой-сводкой ─────────────────────────────────────


def _setup_only(access) -> None:
    # Сверка показывает все договоры компании с деньгами книги - это работа
    # того, кто переводит учёт в KORT: владельца или администратора.
    if not access.setup:
        raise PermissionError("Сверку с книгой-сводкой ведёт владелец или администратор")


def _book_out(book: book_module.Book) -> dict[str, Any]:
    return {
        "title": book.title,
        "worksheet": book.worksheet,
        "read_at": book.read_at.isoformat(),
        "url": f"https://docs.google.com/spreadsheets/d/{book.spreadsheet_id}" if book.spreadsheet_id else "",
        "drift": book.drift,
    }


@router.get("/shadow")
def shadow_view(member: Member = Depends(viewer)) -> dict[str, Any]:
    """Сверка начислений KORT с книгой-сводкой: совпало, расходится, предложения, нет в реестре."""

    def action(session, workspace, access):
        _setup_only(access)
        book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
        if book is None:
            return {"book": None, "error": error or "Книга-сводка компании не подключена", "window": [], "counts": {},
                    "contracts": {}, "lines": [], "proposals": [], "unknown": []}
        reconciler = shadow_module.Reconciler(session, workspace, access, book)
        return {"book": _book_out(book), "error": "", **reconciler.out(reconciler.run(), money=True)}

    return _run(member, action)


@router.get("/shadow/bills")
def shadow_bills(member: Member = Depends(viewer)) -> dict[str, Any]:
    """«Не выставлены» KORT против списка книги: совпало, срок другой, только в книге, только у KORT."""
    from app.finance.core import billing

    def action(session, workspace, access):
        _setup_only(access)
        book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
        if book is None:
            return {"book": None, "error": error or "Книга-сводка компании не подключена", "window": [], "counts": {},
                    "lines": [], "contracts": {}}
        out = billing.reconcile_bills(session, workspace, access, book, today=contracts_service.today())
        return {"book": _book_out(book), "error": "", **out}

    return _run(member, action)


class ApplyIn(BaseModel):
    contract_id: UUID
    kind: str = Field(max_length=40)
    value: dict[str, Any] = Field(default_factory=dict)
    option: str = Field(default="", max_length=20)


@router.post("/shadow/apply")
def shadow_apply(body: ApplyIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Записать одно предложение сверки - обычным изменением договора."""
    return _run(
        member,
        lambda s, w, a: {
            "ok": True,
            "text": changes.apply_proposal(s, w, a, _actor(member), body.contract_id, body.kind, body.value, option=body.option),
        },
    )


class ApplyAllIn(BaseModel):
    kind: str = Field(pattern="^(billing|period_day|billing_from)$")


@router.post("/shadow/apply-all")
def shadow_apply_all(body: ApplyAllIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Все предложения простого вида разом - с точкой восстановления."""

    def action(session, workspace, access):
        _setup_only(access)
        return changes.apply_all(session, workspace, access, _actor(member), _book(session, workspace), body.kind)

    return _run(member, action)


class FromBookIn(BaseModel):
    key: str = Field(min_length=8, max_length=40)


@router.post("/shadow/create", status_code=201)
def shadow_create(body: FromBookIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Договор книги, которого нет в реестре, - завести по его строкам."""

    def action(session, workspace, access):
        _setup_only(access)
        contract = changes.create_from_book(session, workspace, access, _actor(member), _book(session, workspace), body.key)
        return {"id": str(contract.id), "number": contract.number}

    return _run(member, action)


def _opened_contract(session, workspace, access, contract_id: UUID):
    contract = contracts_service.get_contract(session, workspace, contract_id)
    registry = contracts_service.Registry(session, workspace)
    people = contracts_service.people_of(session, [contract.id]).get(contract.id, [])
    if not contracts_service.visible_to(contract, registry, access, people):
        raise contracts_service.NotFound("Договор не найден")
    return contract


@router.get("/contracts/{contract_id}/months")
def contract_months(contract_id: UUID, member: Member = Depends(viewer)) -> dict[str, Any]:
    """Лента месяцев договора: начисление KORT и строка книги за месяц."""

    def action(session, workspace, access):
        contract = _opened_contract(session, workspace, access, contract_id)
        book, _error = book_module.current(session, workspace.id, wait=0)
        # Оплата и АВР книги - деньги компании: тем, кому скрыта «Оплачено (сводка)», не отдаются.
        money = "summary_paid" not in access.hidden
        return shadow_module.contract_months(
            session, workspace, access, contract, book, money=money, on=contracts_service.today()
        )

    return _run(member, action)


class AdjustmentIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    amount: str | None = Field(default=None, max_length=40)
    skip: bool = False
    reason: str = Field(default="", max_length=500)


@router.post("/contracts/{contract_id}/adjustments", status_code=201)
def contract_adjustment(contract_id: UUID, body: AdjustmentIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Поправка начисления месяца: другая сумма или «не начислять», с причиной."""

    def action(session, workspace, access):
        doc = changes.set_adjustment(
            session, workspace, access, _actor(member), contract_id, body.month,
            amount=body.amount, skip=body.skip, reason=body.reason,
        )
        return {"id": str(doc.id)}

    return _run(member, action)


@router.delete("/contracts/{contract_id}/adjustments/{document_id}")
def contract_adjustment_remove(contract_id: UUID, document_id: UUID, member: Member = Depends(editor)) -> dict[str, Any]:
    def action(session, workspace, access):
        changes.remove_adjustment(session, workspace, access, _actor(member), contract_id, document_id)
        return {"ok": True}

    return _run(member, action)


class StatusFromIn(BaseModel):
    status_id: UUID
    effective_from: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    summary: str = Field(default="", max_length=300)


@router.post("/contracts/{contract_id}/status-from", status_code=201)
def contract_status_from(contract_id: UUID, body: StatusFromIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Статус договора с даты: приостановка, возобновление, расторжение."""
    from datetime import date

    def action(session, workspace, access):
        amendment = changes.status_from(
            session, workspace, access, _actor(member), contract_id, body.status_id,
            date.fromisoformat(body.effective_from), summary=body.summary,
        )
        return {"id": str(amendment.id)}

    return _run(member, action)
