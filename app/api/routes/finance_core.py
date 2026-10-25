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

from datetime import timedelta
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
        raise HTTPException(status_code=424, detail=error or "Сводная таблица компании не подключена или ещё читается")
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
        raise PermissionError("Сверку со сводной таблицей ведёт владелец или администратор")


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
            return {"book": None, "error": error or "Сводная таблица компании не подключена", "window": [], "counts": {},
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
            return {"book": None, "error": error or "Сводная таблица компании не подключена", "window": [], "counts": {},
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
        from app.finance.core import acts as acts_module
        from app.finance.core import payments as payments_module

        contract = _opened_contract(session, workspace, access, contract_id)
        book, _error = book_module.current(session, workspace.id, wait=0)
        # Оплата и АВР книги - деньги компании: тем, кому скрыта «Оплачено (сводка)», не отдаются.
        money = "summary_paid" not in access.hidden
        today = contracts_service.today()
        out = shadow_module.contract_months(session, workspace, access, contract, book, money=money, on=today)
        out["today"] = today.isoformat()
        # Оплаты KORT по месяцам - тем, кому открыты оплаты по выписке (нужен журнал).
        if access.view and "paid" not in access.hidden:
            out["money"] = payments_module.contract_money(session, workspace, access, contract, today=today)
        # АВР месяца - KORT, 1С, книга; сумма - только с правом на деньги книги.
        acts = acts_module.Acts(session, workspace, access, book, today=today, only=contract)
        out["acts"] = acts.contract(contract.id, money=money)
        return out

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


# ── НДС и порог ──────────────────────────────────────────────────────────────


def _turnover(session, workspace, access, year: int):
    from app.finance.core import vat

    _setup_only(access)
    if not 2000 <= year <= 2100:
        raise HTTPException(status_code=400, detail="Год - от 2000 до 2100")
    return vat.Turnover(session, workspace, access, year, today=contracts_service.today())


def _acts(session, workspace, access) -> dict:
    """АВР книги-сводки по юрлицу и месяцу - уже прочитанная книга, без ожидания Google."""
    from app.finance.core import vat

    book, _error = book_module.current(session, workspace.id, wait=0)
    if book is None:
        return {}
    reconciler = shadow_module.Reconciler(session, workspace, access, book)
    return vat.book_acts(reconciler)


@router.get("/vat")
def vat_year(year: int = Query(ge=2000, le=2100), member: Member = Depends(viewer)) -> dict[str, Any]:
    """Оборот наших юрлиц за год к порогу НДС: факт, прогноз, месяц перехода порога."""
    from app.finance.core import vat

    def action(session, workspace, access):
        turnover = _turnover(session, workspace, access, year)
        return vat.year_out(turnover, turnover.run(acts=_acts(session, workspace, access)))

    return _run(member, action)


@router.get("/vat/clients")
def vat_clients(
    year: int = Query(ge=2000, le=2100), entity: UUID = Query(), member: Member = Depends(viewer)
) -> dict[str, Any]:
    """Договоры юрлица, которые можно перевести: оборот каждого до конца года."""
    return _run(member, lambda s, w, a: {"items": _turnover(s, w, a, year).clients(entity)})


class MoveIn(BaseModel):
    contract_id: UUID
    target: UUID
    since: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class MovesIn(BaseModel):
    year: int = Field(ge=2000, le=2100)
    moves: list[MoveIn] = Field(min_length=1, max_length=500)


def _moves(body: MovesIn) -> list:
    from datetime import date

    from app.finance.core import vat

    return [vat.Move(item.contract_id, item.target, date.fromisoformat(item.since)) for item in body.moves]


@router.post("/vat/preview")
def vat_preview(body: MovesIn, member: Member = Depends(viewer)) -> dict[str, Any]:
    """Перевод до записи: оборот затронутых юрлиц к концу года - до и после. Ничего не пишет."""

    def action(session, workspace, access):
        turnover = _turnover(session, workspace, access, body.year)
        moves = _moves(body)
        before, after = turnover.run(), turnover.run(moves)
        threshold = turnover.tax_year.threshold
        touched = {item.target for item in moves}
        for contract in turnover.contracts:
            if any(item.contract_id == contract.id for item in moves) and contract.executor_id:
                touched.add(contract.executor_id)
        return {
            "entities": [
                {
                    "entity_id": str(entity_id),
                    "code": after[entity_id].code,
                    "name": after[entity_id].name,
                    "before": format(before[entity_id].forecast, "f"),
                    "after": format(after[entity_id].forecast, "f"),
                    "crossing_before": before[entity_id].crossing(threshold),
                    "crossing_after": after[entity_id].crossing(threshold),
                }
                for entity_id in touched
                if entity_id in after
            ]
        }

    return _run(member, action)


@router.post("/vat/transfer")
def vat_transfer(body: MovesIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Перевести договоры к другому нашему юрлицу с дня - пачкой, с точкой восстановления."""

    def action(session, workspace, access):
        _setup_only(access)
        return changes.transfer(session, workspace, access, _actor(member), _moves(body))

    return _run(member, action)


# ── Оплаты ───────────────────────────────────────────────────────────────────


def _entities_out(session, workspace, money) -> list[dict[str, Any]]:
    """Наши юрлица с источником оплат и тем, сколько оплат в каждом источнике."""
    import sqlalchemy as sa

    from app.finance.contracts.models import GroupEntity
    from app.finance.models import Counterparty
    from app.finance.onec.models import OnecBase, OnecDocument

    journal: dict[Any, int] = {}
    for contract_id, items in money.allocation.paid.items():
        contract = money.contracts.get(contract_id)
        entity = money.ours(contract) if contract is not None else None
        if entity is not None:
            journal[entity] = journal.get(entity, 0) + len(items)
    onec = dict(
        session.execute(
            sa.select(OnecBase.group_entity_id, sa.func.count(OnecDocument.id))
            .join(OnecDocument, OnecDocument.base_id == OnecBase.id)
            .where(
                OnecBase.workspace_id == workspace.id,
                OnecBase.group_entity_id.is_not(None),
                OnecDocument.kind == "payment",
                OnecDocument.posted.is_(True),
                OnecDocument.deleted.is_(False),
                OnecDocument.gone.is_(False),
            )
            .group_by(OnecBase.group_entity_id)
        ).all()
    )
    rows = session.execute(
        sa.select(GroupEntity, Counterparty)
        .join(Counterparty, Counterparty.id == GroupEntity.counterparty_id)
        .where(GroupEntity.workspace_id == workspace.id, GroupEntity.archived_at.is_(None))
        .order_by(GroupEntity.position, Counterparty.name)
    ).all()
    return [
        {
            "id": str(entity.counterparty_id),
            "code": entity.code,
            "name": party.name,
            "source": money.settings.source_of(entity.counterparty_id),
            "journal": journal.get(entity.counterparty_id, 0),
            "onec": onec.get(entity.counterparty_id, 0),
        }
        for entity, party in rows
    ]


@router.get("/payments")
def payments_view(member: Member = Depends(viewer)) -> dict[str, Any]:
    """Оплаты в ядре: начало учёта, источник по юрлицам, сверка с книгой, оплаты без договора, входящее сальдо."""
    from app.finance.core import opening as opening_module
    from app.finance.core import payments as payments_module
    from app.finance.core import settings as core_settings

    def action(session, workspace, access):
        _setup_only(access)
        today = contracts_service.today()
        money = payments_module.Money(session, workspace, today=today)
        book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
        out: dict[str, Any] = {
            "settings": core_settings.payments_out(money.settings, since_used=money.since),
            "entities": _entities_out(session, workspace, money),
            "without": money.without_contract(),
            "without_hidden": money.hidden,
            "book": _book_out(book) if book is not None else None,
            "error": "" if book is not None else (error or "Сводная таблица компании не подключена"),
            "shadow": None,
            "opening": None,
        }
        if book is not None:
            out["shadow"] = payments_module.reconcile_paid(session, workspace, access, book, today=today, money=money)
            if money.since is not None:
                out["opening"] = opening_module.plan(session, workspace, access, book, since=money.since)
        return out

    return _run(member, action)


@router.get("/acts")
def acts_view(member: Member = Depends(viewer)) -> dict[str, Any]:
    """АВР и ЭСФ: 1С против книги по договору и месяцу, не подписаны, без ЭСФ после срока, нет АВР."""
    import sqlalchemy as sa

    from app.finance.contracts.models import GroupEntity
    from app.finance.core import acts as acts_module
    from app.finance.core.accruals import Ledger, month_key, month_start
    from app.finance.models import Counterparty

    def action(session, workspace, access):
        _setup_only(access)
        today = contracts_service.today()
        book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
        acts = acts_module.Acts(session, workspace, access, book, today=today)
        visible = shadow_module._visible(session, workspace, access)
        contracts = {item.id: item for item in visible}
        current = month_key(today)
        window = [month for month in (acts.reconciler.window if acts.reconciler else []) if month < current]
        until = acts_module.previous_month(today)
        since = month_start(window[0]) if window else month_start(month_key(until.replace(day=1) - timedelta(days=150)))
        accrued = acts_module.accrued_months(Ledger(session, workspace.id, visible), visible, since=since, until=until)
        shadow = acts.reconcile(contracts, window=window)
        work = acts.worklists(contracts, accrued)
        ids = {line["contract_id"] for line in shadow["lines"]} | {
            item["contract_id"] for items in work.values() for item in items
        }
        names = dict(session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace.id)).all())
        codes = {
            entity_id: code
            for entity_id, code in session.execute(
                sa.select(GroupEntity.counterparty_id, GroupEntity.code).where(GroupEntity.workspace_id == workspace.id)
            )
            if code
        }
        onec = [
            {"id": str(entity_id), "code": codes.get(entity_id, ""), "name": names.get(entity_id, "")}
            for entity_id in sorted(acts.onec_entities, key=lambda item: codes.get(item, names.get(item, "")))
        ]
        return {
            "book": _book_out(book) if book is not None else None,
            "error": "" if book is not None else (error or "Сводная таблица компании не подключена"),
            "window": window,
            "onec": onec,
            **shadow,
            **work,
            "contracts": {
                key: {
                    "id": key,
                    "number": contracts[UUID(key)].number,
                    "customer": names.get(contracts[UUID(key)].customer_id, ""),
                    "executor": codes.get(contracts[UUID(key)].executor_id) or names.get(contracts[UUID(key)].executor_id, ""),
                }
                for key in ids
                if UUID(key) in contracts
            },
            "codes": acts_module.CODE_TITLES,
            "esf_days": acts_module.ESF_DAYS,
        }

    return _run(member, action)


class PaymentSettingsIn(BaseModel):
    #: Начало учёта оплат: дата или `null` - по первой оплате. Не передано - не меняется.
    since: str | None = Field(default="keep", pattern=r"^(keep|\d{4}-\d{2}-\d{2})$")
    entity_id: UUID | None = None
    source: str | None = Field(default=None, pattern="^(journal|1c)$")


@router.put("/payments/settings")
def payments_settings(body: PaymentSettingsIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Начало учёта оплат и откуда оплаты нашего юрлица - журнал или 1С."""
    from datetime import date

    from app.finance.core import settings as core_settings

    since: Any = "keep"
    if "since" in body.model_fields_set:
        since = None if body.since is None else (date.fromisoformat(body.since) if body.since != "keep" else "keep")

    def action(session, workspace, access):
        value = core_settings.set_payments(
            session, workspace, access, _actor(member), since=since, entity_id=body.entity_id, source=body.source
        )
        return core_settings.payments_out(value, since_used=value.since)

    return _run(member, action)


class PaymentIgnoreIn(BaseModel):
    kind: str = Field(pattern="^(categories|payers)$")
    name: str = Field(min_length=1, max_length=500)
    #: `true` - «не оплаты по договорам», `false` - снова оплаты.
    on: bool = True


@router.put("/payments/ignore")
def payments_ignore(body: PaymentIgnoreIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Статья или плательщик целиком - «не оплаты по договорам» (проценты, переводы, продажи маркетплейса)."""
    from app.finance.core import settings as core_settings

    def action(session, workspace, access):
        value = core_settings.set_ignore(session, workspace, access, _actor(member), kind=body.kind, name=body.name, on=body.on)
        return core_settings.payments_out(value, since_used=value.since)

    return _run(member, action)


@router.post("/payments/opening")
def payments_opening(member: Member = Depends(editor)) -> dict[str, Any]:
    """Перенести входящее сальдо договоров на начало учёта из книги - пачкой, с точкой восстановления."""
    from app.finance.core import opening as opening_module
    from app.finance.core import payments as payments_module

    def action(session, workspace, access):
        _setup_only(access)
        money = payments_module.Money(session, workspace, today=contracts_service.today())
        if money.since is None:
            raise HTTPException(status_code=409, detail="Начало учёта оплат не задано и оплат ещё нет")
        return opening_module.apply(session, workspace, access, _actor(member), _book(session, workspace), since=money.since)

    return _run(member, action)


class PaymentDecisionIn(BaseModel):
    action: str = Field(pattern="^(link|none|auto)$")
    contract_id: UUID | None = None


@router.post("/payments/{operation_id}/decide")
def payment_decide(operation_id: UUID, body: PaymentDecisionIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Поступление без договора: отнести к договору, «не по договору» или снять решение."""
    from app.finance.core import payments as payments_module

    return _run(
        member,
        lambda s, w, a: payments_module.decide(s, w, a, _actor(member), operation_id, action=body.action, contract_id=body.contract_id),
    )


class OpeningIn(BaseModel):
    #: Сальдо на начало учёта; `null` - снять.
    amount: str | None = Field(default=None, max_length=40)
    #: По начислениям (его гасят оплаты) или по АВР (начало долга по актам).
    basis: str = Field(default="accruals", pattern="^(accruals|acts)$")


@router.put("/contracts/{contract_id}/opening")
def contract_opening(contract_id: UUID, body: OpeningIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Входящее сальдо одного договора руками - книга его потом не перебивает."""
    from app.finance.core import opening as opening_module
    from app.finance.core import payments as payments_module

    def action(session, workspace, access):
        value = None
        if body.amount is not None:
            value, terms = contracts_service.read_money(body.amount, field="Сальдо")
            if value is None:
                raise HTTPException(status_code=422, detail=f"Сальдо: «{terms}» не похоже на число")
        money = payments_module.Money(session, workspace, today=contracts_service.today())
        if money.since is None:
            raise HTTPException(status_code=409, detail="Начало учёта оплат не задано и оплат ещё нет")
        opening_module.set_one(
            session, workspace, access, _actor(member), contract_id, since=money.since, amount=value, basis=body.basis
        )
        return {"ok": True}

    return _run(member, action)


# ── Итоги: долги ─────────────────────────────────────────────────────────────


def _debts_access(access) -> None:
    # Долг - сумма договора минус оплаты по выписке: кому они скрыты, тому и долг.
    if {"amount", "paid", "remaining"} & access.hidden:
        raise PermissionError("Долги по договорам видит тот, кому открыты сумма договора и оплаты по выписке")


@router.get("/debts")
def debts_view(
    as_of: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    group: str = Query(default="client", pattern="^(client|entity|department|person)$"),
    basis: str = Query(default="accruals", pattern="^(accruals|acts)$"),
    side: str = Query(default="receivable", pattern="^(receivable|payable|intra)$"),
    member: Member = Depends(require_access("reports.debts")),
) -> dict[str, Any]:
    """Долги по договорам на день: нам, наши (покупки), между своими; по начислениям или по АВР; по группам, со старением."""
    from datetime import date

    from app.finance.core import debts as debts_module

    def action(session, workspace, access):
        _debts_access(access)
        day = date.fromisoformat(as_of) if as_of else contracts_service.today()
        book, error = None, ""
        if basis == "acts":
            # АВР книги - часть долга по АВР у юрлиц без 1С: без книги он был бы меньше молча.
            book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
        out = debts_module.Debts(session, workspace, access, day=day, basis=basis, book=book, side=side).out(group)
        out["book"] = _book_out(book) if book is not None else None
        out["error"] = (
            "" if basis == "accruals" or book is not None
            else f"{error or 'Сводная таблица компании не подключена'} - АВР только KORT и 1С"
        )
        return out

    return _run(member, action)


@router.get("/debts/shadow")
def debts_shadow(
    basis: str = Query(default="accruals", pattern="^(accruals|acts)$"), member: Member = Depends(viewer)
) -> dict[str, Any]:
    """Сальдо KORT на конец месяца против «Сальдо Конец» книги-сводки - по начислениям или по АВР."""
    from app.finance.core import debts as debts_module

    def action(session, workspace, access):
        _setup_only(access)
        book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
        if book is None:
            return {"book": None, "error": error or "Сводная таблица компании не подключена", "basis": basis, "window": [],
                    "counts": {}, "contracts": {}, "lines": []}
        out = debts_module.reconcile_saldo(session, workspace, access, book, today=contracts_service.today(), basis=basis)
        return {"book": _book_out(book), "error": "", **out}

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
