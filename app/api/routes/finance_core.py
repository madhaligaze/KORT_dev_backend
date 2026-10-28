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

from datetime import date, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
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


def _entity_code(session, workspace, entity: UUID | None) -> str:
    """Код нашего юрлица для фильтра сверки «по ТОО» (`entity`); чужой или несуществующий - отказ словами."""
    if entity is None:
        return ""
    from app.finance.contracts.models import GroupEntity
    from app.finance.service import FinanceError

    row = session.get(GroupEntity, entity)
    if row is None or row.workspace_id != workspace.id:
        raise FinanceError("Такого нашего юрлица нет")
    return row.code or ""


def _book_out(book: book_module.Book) -> dict[str, Any]:
    return {
        "title": book.title,
        "worksheet": book.worksheet,
        "read_at": book.read_at.isoformat(),
        "url": f"https://docs.google.com/spreadsheets/d/{book.spreadsheet_id}" if book.spreadsheet_id else "",
        "drift": book.drift,
    }


@router.get("/shadow")
def shadow_view(entity: UUID | None = Query(default=None), member: Member = Depends(viewer)) -> dict[str, Any]:
    """Сверка начислений KORT с книгой-сводкой: совпало, расходится, предложения, нет в реестре.

    `entity` - одно наше юрлицо, как его считает «Переход» (`Shadow.of_entity`).
    """

    def action(session, workspace, access):
        _setup_only(access)
        code = _entity_code(session, workspace, entity)
        book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
        if book is None:
            return {"book": None, "error": error or "Сводная таблица компании не подключена", "window": [], "counts": {},
                    "contracts": {}, "lines": [], "proposals": [], "unknown": []}
        reconciler = shadow_module.Reconciler(session, workspace, access, book)
        shadow = reconciler.run()
        if entity is not None:
            shadow = shadow.of_entity(entity, code)
        return {"book": _book_out(book), "error": "", **reconciler.out(shadow, money=True)}

    return _run(member, action)


@router.get("/shadow/bills")
def shadow_bills(entity: UUID | None = Query(default=None), member: Member = Depends(viewer)) -> dict[str, Any]:
    """«Не выставлены» KORT против списка книги: совпало, срок другой, только в книге, только у KORT."""
    from app.finance.core import billing

    def action(session, workspace, access):
        _setup_only(access)
        _entity_code(session, workspace, entity)
        book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
        if book is None:
            return {"book": None, "error": error or "Сводная таблица компании не подключена", "window": [], "counts": {},
                    "lines": [], "contracts": {}}
        out = billing.reconcile_bills(session, workspace, access, book, today=contracts_service.today(), entity=entity)
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


class VerifyIn(BaseModel):
    contract_id: UUID
    month: str = Field(pattern=r"^\d{4}-\d{2}$")


@router.post("/shadow/verify", status_code=201)
def shadow_verify(body: VerifyIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """«Верно у KORT» для месяца договора: ошиблась таблица, условия договора не меняются."""

    def action(session, workspace, access):
        _setup_only(access)
        doc, text = changes.verify_month(
            session, workspace, access, _actor(member), _book(session, workspace), body.contract_id, body.month
        )
        return {"ok": True, "id": str(doc.id), "text": text}

    return _run(member, action)


class VerifyProposalIn(BaseModel):
    contract_id: UUID
    months: list[str] = Field(min_length=1, max_length=36)


@router.post("/shadow/verify-proposal", status_code=201)
def shadow_verify_proposal(body: VerifyProposalIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """«Верно у KORT» вместо предложения: ошиблась таблица, договор под неё не подгоняется."""

    def action(session, workspace, access):
        _setup_only(access)
        result = changes.verify_proposal(
            session, workspace, access, _actor(member), _book(session, workspace), body.contract_id, body.months
        )
        return {"ok": True, **result}

    return _run(member, action)


@router.delete("/shadow/verify/{document_id}")
def shadow_unverify(document_id: UUID, member: Member = Depends(editor)) -> dict[str, Any]:
    """Снять «верно у KORT»: месяц снова сверяется с таблицей."""

    def action(session, workspace, access):
        _setup_only(access)
        return {"ok": True, "text": changes.unverify(session, workspace, access, _actor(member), document_id)}

    return _run(member, action)


class VerifySlipsIn(BaseModel):
    entity: UUID | None = None


@router.post("/shadow/verify-slips")
def shadow_verify_slips(body: VerifySlipsIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """«Верно у KORT» разом - месяцы, где таблица пропустила день периода или посчитала его дважды."""

    def action(session, workspace, access):
        _setup_only(access)
        code = _entity_code(session, workspace, body.entity)
        return changes.verify_slips(
            session, workspace, access, _actor(member), _book(session, workspace), entity=body.entity, code=code
        )

    return _run(member, action)


@router.delete("/shadow/verify-batch/{batch}")
def shadow_unverify_batch(batch: UUID, member: Member = Depends(editor)) -> dict[str, Any]:
    """Вернуть пачку «верно у KORT» целиком."""

    def action(session, workspace, access):
        _setup_only(access)
        return changes.unverify_batch(session, workspace, access, _actor(member), str(batch))

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


@router.get("/contracts/{contract_id}/facts")
def contract_facts(contract_id: UUID, history: bool = Query(default=False), member: Member = Depends(viewer)) -> dict[str, Any]:
    """Что было с договором: факты журнала одним списком и долг на сегодня по кускам - каждый со ссылкой на факт."""

    def action(session, workspace, access):
        from app.finance.core import calls

        contract = _opened_contract(session, workspace, access, contract_id)
        out = calls.contract_facts(session, workspace, access, contract, history=history)
        out["explain"] = calls.explain(session, workspace, access, contract, day=contracts_service.today())
        return out

    return _run(member, action)


@router.get("/facts")
def facts_changes(
    since: str = Query(default="0", max_length=300),
    limit: int = Query(default=500, ge=1, le=2000),
    member: Member = Depends(viewer),
) -> dict[str, Any]:
    """Факты журнала после курсора - новые, новые версии, снятые: «подписаться» выборкой."""
    from app.finance.core import calls

    return _run(member, lambda s, w, a: calls.changes(s, w, a, since=since, limit=limit))


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
        return vat.year_out(turnover, turnover.run(acts=_acts(session, workspace, access)), vat.vat_hints(session, workspace, year))

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
                    # Плательщику порог не нужен - ему важен НДС до и после перевода.
                    "status": after[entity_id].status,
                    "vat_before": format(before[entity_id].vat, "f"),
                    "vat_after": format(after[entity_id].vat, "f"),
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
    for contract_id, items in money.journal_paid.items():
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
def payments_view(entity: UUID | None = Query(default=None), member: Member = Depends(viewer)) -> dict[str, Any]:
    """Оплаты в ядре: начало учёта, источник по юрлицам, сверка с книгой, оплаты без договора, входящее сальдо.

    `entity` сужает только сверку с таблицей - до месяцев одного нашего юрлица, как в «Переходе».
    """
    from app.finance.core import opening as opening_module
    from app.finance.core import payments as payments_module
    from app.finance.core import settings as core_settings

    def action(session, workspace, access):
        _setup_only(access)
        _entity_code(session, workspace, entity)
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
            out["shadow"] = payments_module.reconcile_paid(session, workspace, access, book, today=today, money=money, entity=entity)
            if money.since is not None:
                out["opening"] = opening_module.plan(session, workspace, access, book, since=money.since)
        return out

    return _run(member, action)


@router.get("/acts")
def acts_view(entity: UUID | None = Query(default=None), member: Member = Depends(viewer)) -> dict[str, Any]:
    """АВР и ЭСФ: 1С против книги по договору и месяцу, не подписаны, без ЭСФ после срока, нет АВР.

    `entity` - одно наше юрлицо: сверка - его месяцы, списки - его договоры (исполнитель сейчас).
    """
    import sqlalchemy as sa

    from app.finance.contracts.models import GroupEntity
    from app.finance.core import acts as acts_module
    from app.finance.core.accruals import month_key, month_start
    from app.finance.core.registers import ledger_on
    from app.finance.models import Counterparty

    def action(session, workspace, access):
        _setup_only(access)
        _entity_code(session, workspace, entity)
        today = contracts_service.today()
        book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
        acts = acts_module.Acts(session, workspace, access, book, today=today)
        visible = shadow_module._visible(session, workspace, access)
        contracts = {item.id: item for item in visible}
        # Списки «не подписаны / без ЭСФ / нет АВР» - по договорам юрлица; сверка 1С - по его месяцам.
        mine = {key: item for key, item in contracts.items() if item.executor_id == entity} if entity is not None else contracts
        current = month_key(today)
        window = [month for month in (acts.reconciler.window if acts.reconciler else []) if month < current]
        until = acts_module.previous_month(today)
        since = month_start(window[0]) if window else month_start(month_key(until.replace(day=1) - timedelta(days=150)))
        accrued = acts_module.accrued_months(ledger_on(session, workspace, visible, day=today), visible, since=since, until=until)
        shadow = acts.reconcile(contracts, window=window, entity=entity)
        work = acts.worklists(mine, {key: months for key, months in accrued.items() if key in mine})
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


@router.get("/revenue")
def revenue_view(
    year: int = Query(ge=2000, le=2100),
    group: str = Query(default="entity", pattern="^(entity|client|department|person)$"),
    side: str = Query(default="receivable", pattern="^(receivable|intra)$"),
    member: Member = Depends(require_access(("reports.profit", "reports.cash"))),
) -> dict[str, Any]:
    """Выручка по договорам по месяцам года: начислено, АВР, оплачено за месяц, поступило - по группам."""
    from app.finance.core import revenue as revenue_module

    def action(session, workspace, access):
        _debts_access(access)
        # АВР таблицы - часть «АВР» у юрлиц без 1С: без неё колонка была бы меньше молча.
        book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
        out = revenue_module.Revenue(
            session, workspace, access, year=year, today=contracts_service.today(), book=book, side=side
        ).out(group)
        out["book"] = _book_out(book) if book is not None else None
        out["error"] = "" if book is not None else f"{error or 'Сводная таблица компании не подключена'} - АВР только KORT и 1С"
        return out

    return _run(member, action)


# ── Словарь колонок сводной таблицы (фаза 9, R14) ────────────────────────────


@router.get("/book/columns")
def book_columns_view(member: Member = Depends(viewer)) -> dict[str, Any]:
    """Шапка сводной таблицы и как в ней нашлась каждая колонка ядра."""

    def action(session, workspace, access):
        _setup_only(access)
        return book_module.layout_of(session, workspace.id)

    return _run(member, action)


class BookColumnIn(BaseModel):
    key: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    #: `true` - читать колонку и под этим названием, `false` - больше не читать.
    on: bool = True


@router.put("/book/columns")
def book_columns_set(body: BookColumnIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Название колонки компании для колонки ядра - и сразу перечитать таблицу с ним."""
    from app.finance.core import settings as core_settings

    def action(session, workspace, access):
        known = {column.key: column.title for column in book_module.COLUMNS}
        core_settings.set_book_column(
            session, workspace, access, _actor(member), key=body.key, name=body.name, on=body.on, known=known
        )
        session.flush()
        return book_module.layout_of(session, workspace.id)

    return _run(member, action)


# ── Слова компании в колонках таблицы (R12, R13) ─────────────────────────────


def _table_words(session, workspace) -> dict[str, Any]:
    """Значения «Счет» и «Вида Услуги» из прочитанной таблицы - со смыслом и откуда он."""
    from collections import Counter

    from app.finance import bills_bot
    from app.finance.contracts import bills as bills_module
    from app.finance.core import settings as core_settings
    from app.finance.layout import norm

    words = core_settings.table_words(session, workspace.id)
    out: dict[str, Any] = {
        "titles": core_settings.WORD_TITLES,
        "meanings": {"bill": list(core_settings.BILL_MEANINGS), "kind": list(core_settings.KIND_MEANINGS)},
        "seen": {"bill": [], "kind": []},
        "error": "",
    }
    slot = bills_module.get(session, workspace.id)
    if slot is None:
        out["error"] = "Сводная таблица не подключена"
        return out
    with bills_module._guard:
        board, error = slot.board, slot.error
    if board is None:
        out["error"] = error or "Сводная таблица ещё читается - попробуйте через минуту"
        return out
    rows = [*board.bills, *board.done]
    for text, count in Counter(" ".join(bill.text.split()) for bill in rows if bill.text.strip()).most_common():
        key = norm(text)
        if key in words["bill"]:
            meaning, how = words["bill"][key], "company"
        else:
            state = bills_module.state_of(text)
            meaning = "issued" if state is None else state
            how = "unknown" if state == bills_module.UNDATED else "kort"
        out["seen"]["bill"].append({"text": text, "rows": count, "meaning": meaning, "how": how})
    for text, count in Counter(" ".join(bill.kind.split()) for bill in rows if bill.kind.strip()).most_common():
        key = norm(text)
        if key in words["kind"]:
            meaning, how = words["kind"][key], "company"
        else:
            kind = bills_bot.kind_of_words(text)
            meaning = "" if kind == bills_bot.UNKNOWN else kind
            how = "unknown" if kind == bills_bot.UNKNOWN else "kort"
        out["seen"]["kind"].append({"text": text, "rows": count, "meaning": meaning, "how": how})
    return out


@router.get("/table-words")
def table_words_view(member: Member = Depends(viewer)) -> dict[str, Any]:
    """Слова компании в колонках «Счет» и «Вид Услуги»: что встречается в таблице и как KORT это понимает."""

    def action(session, workspace, access):
        _setup_only(access)
        return _table_words(session, workspace)

    return _run(member, action)


class TableWordIn(BaseModel):
    part: str = Field(pattern="^(bill|kind)$")
    word: str = Field(min_length=1, max_length=200)
    #: Пусто - снять смысл: слово снова понимается так, как KORT понимает сам.
    meaning: str = Field(default="", max_length=20)


@router.put("/table-words")
def table_words_set(body: TableWordIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Дать слову колонки смысл - список «Не выставлены», бот и шаблон понимают его сразу. Пишет журнал."""
    from app.finance.core import settings as core_settings
    from app.finance.core import switch

    def action(session, workspace, access):
        core_settings.set_table_word(session, workspace, access, _actor(member), part=body.part, word=body.word, meaning=body.meaning)
        session.flush()
        switch.forget(workspace.id)
        return _table_words(session, workspace)

    return _run(member, action)


# ── Карта группы ─────────────────────────────────────────────────────────────


@router.get("/group")
def group_view(year: int = Query(ge=2000, le=2100), member: Member = Depends(viewer)) -> dict[str, Any]:
    """Наши ТОО и что между ними: оборот и НДС, договоры между своими, переводы клиентов, люди с выплатами из нескольких ТОО."""
    from app.finance.core import group_map

    def action(session, workspace, access):
        # Выплаты людям и обороты всех ТОО - то, что видит владелец или администратор.
        _setup_only(access)
        return group_map.group_map(session, workspace, access, year=year, today=contracts_service.today())

    return _run(member, action)


@router.get("/entities/{entity_id}/numbers")
def entity_numbers(entity_id: UUID, member: Member = Depends(viewer)) -> dict[str, Any]:
    """Досье нашего ТОО «в цифрах»: НДС и оборот за год, кто ему должен."""
    from app.finance.core import group_map

    def action(session, workspace, access):
        _setup_only(access)
        return group_map.entity_numbers(session, workspace, access, entity_id, today=contracts_service.today())

    return _run(member, action)


# ── Переход с таблицы на KORT (фаза 10) ──────────────────────────────────────


@router.get("/switch")
def switch_sources(member: Member = Depends(viewer)) -> dict[str, Any]:
    """Звенья, их названия и какое звено какого юрлица уже ведётся в KORT - без сверок."""
    from app.finance.core import switch

    def action(session, workspace, access):
        _setup_only(access)
        return {
            "links": [{"key": key, "title": switch.LINK_TITLES[key], "switchable": key in switch.SWITCHABLE} for key in switch.LINKS],
            "sources": switch.sources(session, workspace.id),
        }

    return _run(member, action)


@router.get("/switch/{link}")
def switch_link(link: str, member: Member = Depends(viewer)) -> dict[str, Any]:
    """Одно звено по нашим юрлицам: месяцев сверено, совпало, строк таблицы без договора, откуда сейчас.

    По звену - свой запрос: сверка каждого звена читает всю таблицу, и экран
    показывает готовое звено, не дожидаясь остальных.
    """
    from app.finance.core import switch

    def action(session, workspace, access):
        _setup_only(access)
        return switch.link_tally(session, workspace, access, link, today=contracts_service.today())

    return _run(member, action)


@router.get("/switch/{link}/{entity_id}/preview")
def switch_preview(
    link: str, entity_id: UUID, source: str = Query(pattern="^(book|kort)$"), member: Member = Depends(viewer)
) -> dict[str, Any]:
    """Что станет после перехода звена юрлица: счета - какие появятся, уйдут, сменят срок;
    оплаты - у каких договоров «Оплачено» другое и на сколько."""
    from app.finance.core import switch
    from app.finance.service import FinanceError

    def action(session, workspace, access):
        _setup_only(access)
        if link == "bills":
            return switch.preview_bills(session, workspace.id, entity_id, source, today=contracts_service.today())
        if link == "payments":
            return switch.preview_payments(session, workspace.id, entity_id, source, today=contracts_service.today())
        raise FinanceError("Это звено пока ведётся только в таблице")

    return _run(member, action)


class SwitchIn(BaseModel):
    #: `kort` - звено юрлица ведётся в KORT, `book` - снова из таблицы.
    source: str = Field(pattern="^(book|kort)$")


@router.put("/switch/{link}/{entity_id}")
def switch_set(link: str, entity_id: UUID, body: SwitchIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Перевести звено юрлица на KORT или вернуть на таблицу. Пишет журнал (`core.settings`).

    Список счетов, число у пункта и бот читают источник при каждом обращении
    (`bills.current`), так что переход виден сразу, без нового чтения таблицы.
    """
    from app.finance.core import switch

    def action(session, workspace, access):
        out = switch.set_source(session, workspace, access, _actor(member), link=link, entity_id=entity_id, source=body.source)
        switch.forget(workspace.id)
        return {"sources": out}

    return _run(member, action)


# ── Первые шаги: от сводной таблицы до «Долгов» (фаза 9) ─────────────────────


@router.get("/start")
def start_plan(member: Member = Depends(viewer)) -> dict[str, Any]:
    """Что KORT заведёт по сводной таблице: юрлица, договоры, начало учёта и сальдо - и долги после."""
    from app.finance.contracts.summary import source_of
    from app.finance.core import onboarding

    def action(session, workspace, access):
        _setup_only(access)
        connected = source_of(session, workspace.id) is not None
        if not connected:
            return {"connected": False, "book": None, "error": ""}
        book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
        if book is None:
            return {"connected": True, "book": None, "error": error or "Сводная таблица ещё читается - попробуйте через минуту"}
        return {"connected": True, "error": "", **onboarding.plan(session, workspace, access, book, today=contracts_service.today())}

    return _run(member, action)


class StartIn(BaseModel):
    #: Начало учёта оплат для шага `since`, если его ещё нет; пусто - предложенное по таблице.
    since: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


@router.post("/start/{step}")
def start_step(step: str, body: StartIn | None = None, member: Member = Depends(editor)) -> dict[str, Any]:
    """Шаг первых шагов: `entities` - наши юрлица по кодам, `contracts` - договоры по строкам, `since` - начало учёта и сальдо."""
    from datetime import date as date_type

    from app.finance.core import onboarding
    from app.finance.service import FinanceError

    def action(session, workspace, access):
        _setup_only(access)
        if step not in onboarding.STEPS:
            raise FinanceError("Такого шага нет")
        book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
        if book is None:
            raise FinanceError(error or "Сводная таблица не подключена")
        actor = _actor(member)
        if step == "entities":
            return onboarding.add_entities(session, workspace, access, actor, book)
        if step == "contracts":
            return onboarding.add_contracts(session, workspace, access, actor, book)
        since = date_type.fromisoformat(body.since) if body is not None and body.since else None
        return onboarding.start_since(session, workspace, access, actor, book, today=contracts_service.today(), since=since)

    return _run(member, action)


# ── Виды: пустые разделы ─────────────────────────────────────────────────────


@router.get("/usage")
def sections_usage(
    member: Member = Depends(require_access(("contracts", "journal", "table", "reports.cash", "reports.profit", "reports.debts"))),
) -> dict[str, Any]:
    """Разделы колонки, где у компании нет ни одной строки: они уходят в «Ещё» и остаются в поиске."""
    from app.finance.core import usage

    def action(session, workspace, _access):
        return {"empty": usage.empty_sections(session, workspace)}

    _guard()
    with finance_session() as session:
        return action(session, _workspace(session, member), None)


# ── «Найти или сделать» ──────────────────────────────────────────────────────


@router.get("/search/contracts")
def search_contracts(q: str = Query(default="", max_length=200), member: Member = Depends(viewer)) -> dict[str, Any]:
    """Договоры, открытые человеку, по номеру (как его ни пиши) и клиенту - для окна Ctrl+K."""
    from app.finance.core import search

    return _run(member, lambda s, w, a: {"items": search.contracts(s, w, a, q)})


# ── «Сегодня» ────────────────────────────────────────────────────────────────


def _today_card(member: Member, card: str, check=None) -> dict[str, Any]:
    from app.finance.core import today as today_module

    def action(session, workspace, access):
        if check is not None:
            check(access)
        return today_module.BUILDERS[card](session, workspace, access, today=contracts_service.today())

    return _run(member, action)


@router.get("/today/debts")
def today_debts(member: Member = Depends(require_access("reports.debts"))) -> dict[str, Any]:
    """«Сегодня»: нам должны - неоплаченное, старше 90 дней, крупные должники."""
    return _today_card(member, "debts", _debts_access)


@router.get("/today/payments")
def today_payments(member: Member = Depends(viewer)) -> dict[str, Any]:
    """«Сегодня»: поступления без договора."""
    return _today_card(member, "payments", _setup_only)


@router.get("/today/acts")
def today_acts(member: Member = Depends(viewer)) -> dict[str, Any]:
    """«Сегодня»: АВР не подписаны, ЭСФ не выписаны, месяц без АВР."""
    return _today_card(member, "acts", _setup_only)


@router.get("/today/vat")
def today_vat(member: Member = Depends(require_access("reports.summary"))) -> dict[str, Any]:
    """«Сегодня»: режим НДС не записан, порог пройден или скоро."""
    return _today_card(member, "vat")


@router.get("/today/shadow")
def today_shadow(member: Member = Depends(viewer)) -> dict[str, Any]:
    """«Сегодня»: сверка начислений с таблицей - предложения и договоры, которых нет в реестре."""
    return _today_card(member, "shadow", _setup_only)


@router.get("/today/participants")
def today_participants(member: Member = Depends(viewer)) -> dict[str, Any]:
    """«Сегодня»: двойники и БИН к подтверждению."""
    return _today_card(member, "participants")


@router.get("/debts/shadow")
def debts_shadow(
    basis: str = Query(default="accruals", pattern="^(accruals|acts)$"),
    entity: UUID | None = Query(default=None),
    member: Member = Depends(viewer),
) -> dict[str, Any]:
    """Сальдо KORT на конец месяца против «Сальдо Конец» книги-сводки - по начислениям или по АВР."""
    from app.finance.core import debts as debts_module

    def action(session, workspace, access):
        _setup_only(access)
        _entity_code(session, workspace, entity)
        book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
        if book is None:
            return {"book": None, "error": error or "Сводная таблица компании не подключена", "basis": basis, "window": [],
                    "counts": {}, "contracts": {}, "lines": []}
        out = debts_module.reconcile_saldo(
            session, workspace, access, book, today=contracts_service.today(), basis=basis, entity=entity
        )
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


# ── Приёмник ─────────────────────────────────────────────────────────────────


def _intake_file(file: UploadFile) -> tuple[bytes, str]:
    from app.finance.config import finance_settings

    limit = int(finance_settings.import_max_mb * 1024 * 1024)
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"Файл больше {finance_settings.import_max_mb} МБ")
    return data, file.filename or "файл"


def _intake_answers(raw: str) -> dict[str, Any]:
    import json

    try:
        value = json.loads(raw or "{}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Ответы о листах не прочитались") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Ответы о листах - объект")
    return value


@router.post("/intake")
def intake_describe(file: UploadFile = File(...), member: Member = Depends(editor)) -> dict[str, Any]:
    """«Предложить» для файла: что значит строка каждого листа, колонки, вопросы и отказы. Ничего не пишет.

    Словарь компании и разметка, запомненная раньше, - тоже здесь: размеченный
    однажды лист называет свою форму сам (`core/excel.describe`).
    """
    from app.finance import heap
    from app.finance.core import excel

    data, name = _intake_file(file)
    content = [data]
    del data

    def action(session, workspace, _access):
        return excel.describe(session, workspace, content.pop(), name)

    try:
        return _run(member, action)
    finally:
        content.clear()
        heap.trim()


@router.post("/intake/plan")
def intake_plan(file: UploadFile = File(...), answers: str = Form("{}"), member: Member = Depends(editor)) -> dict[str, Any]:
    """Что таблица положит в модель по разметке человека - и где она расходится с KORT. Ничего не пишет."""
    from app.finance import heap
    from app.finance.core import excel

    data, name = _intake_file(file)
    given = _intake_answers(answers)
    try:
        return _run(member, lambda s, w, a: excel.plan(s, w, data, name, given, today=contracts_service.today(), access=a))
    finally:
        heap.trim()


@router.post("/intake/apply")
def intake_apply(file: UploadFile = File(...), answers: str = Form("{}"), member: Member = Depends(editor)) -> dict[str, Any]:
    """Положить таблицу в модель: участники, договоры, сальдо - пачкой с точкой восстановления; разметка запоминается."""
    from app.finance import heap
    from app.finance.core import excel

    data, name = _intake_file(file)
    given = _intake_answers(answers)
    try:
        return _run(member, lambda s, w, a: excel.apply(s, w, a, _actor(member), data, name, given, today=contracts_service.today()))
    finally:
        heap.trim()


# ── Вход из 1С ───────────────────────────────────────────────────────────────


@router.get("/onec-start/{base_id}")
def onec_start_plan(base_id: UUID, member: Member = Depends(viewer)) -> dict[str, Any]:
    """Что KORT заведёт из базы 1С - шагами, с числами до записи."""
    from app.finance.core import onec_start

    def action(session, workspace, access):
        osv = onec_start.stored_osv(session, workspace, base_id)
        return onec_start.plan(session, workspace, access, base_id, today=contracts_service.today(), osv=osv)

    return _run(member, action)


@router.post("/onec-start/{base_id}/osv")
def onec_start_osv(base_id: UUID, file: UploadFile = File(...), member: Member = Depends(editor)) -> dict[str, Any]:
    """ОСВ по 1210 файлом: прочитать и запомнить к базе - из неё ляжет входящее сальдо."""
    from app.finance.config import finance_settings
    from app.finance.core import onec_start
    from app.finance.onec import osv as osv_module

    limit = int(finance_settings.import_max_mb * 1024 * 1024)
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"Файл больше {finance_settings.import_max_mb} МБ")
    name = file.filename or "ОСВ.xlsx"
    try:
        parsed = osv_module.read(data, name)
    except osv_module.OsvError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _run(member, lambda s, w, a: onec_start.keep_osv(s, w, a, _actor(member), base_id, parsed))


@router.post("/onec-files")
def onec_files_upload(files: list[UploadFile] = File(...), member: Member = Depends(editor)) -> dict[str, Any]:
    """1С без интернета: журналы реализаций и счетов, контрагенты и ОСВ файлами - в базы организаций."""
    from app.finance import heap
    from app.finance.config import finance_settings
    from app.finance.core import onec_start
    from app.finance.onec import files as onec_files

    limit = int(finance_settings.import_max_mb * 1024 * 1024)
    read: list[tuple[str, bytes]] = []
    for item in files:
        data = item.file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(status_code=413, detail=f"Файл «{item.filename}» больше {finance_settings.import_max_mb} МБ")
        read.append((item.filename or "1С.xlsx", data))
    upload = onec_files.read(read)
    read.clear()
    try:
        return _run(member, lambda s, w, a: onec_start.from_files(s, w, a, _actor(member), upload))
    finally:
        heap.trim()


class OnecBalancesIn(BaseModel):
    day: date


@router.post("/onec-start/{base_id}/balances")
def onec_start_balances(base_id: UUID, body: OnecBalancesIn, member: Member = Depends(editor)) -> dict[str, Any]:
    """Сальдо 1210 на первое число из базы 1С по REST - вместо ОСВ файлом."""
    from app.finance.core import onec_start
    from app.finance.onec import balances
    from app.finance.onec import files as onec_files
    from app.finance.onec import sync as onec_sync
    from app.finance.onec.client import Client, OnecError

    if body.day.day != 1:
        raise HTTPException(status_code=400, detail="Учёт начинается с первого числа месяца - выберите первое число")

    def connection(session, workspace, access):
        onec_start._require(access)
        base = onec_start._base(session, workspace, base_id)
        if onec_files.is_files(base):
            raise onec_start.StartError("У базы из файлов REST-сервиса нет - сальдо загрузите ОСВ по 1210 файлом")
        return onec_sync.connection_of(base), base.organization_ref

    found = _run(member, connection)
    if not isinstance(found, tuple):
        return found
    connect, organization = found
    # Сеть - вне транзакции: 1С отвечает секундами, а база KORT ждать не должна.
    try:
        osv = balances.read(Client(connect), organization_ref=organization, day=body.day)
    except OnecError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _run(member, lambda s, w, a: onec_start.keep_osv(s, w, a, _actor(member), base_id, osv))


class OnecStartIn(BaseModel):
    answers: dict[str, dict[str, Any]] = Field(default_factory=dict)


@router.post("/onec-start/{base_id}/{step}")
def onec_start_step(base_id: UUID, step: str, body: OnecStartIn | None = None, member: Member = Depends(editor)) -> dict[str, Any]:
    """Шаг входа из 1С: юрлицо, участники, договоры (с ответами), начало учёта и сальдо, оплаты."""
    from app.finance.core import onec_start

    today = contracts_service.today()
    answers = body.answers if body else {}

    def action(session, workspace, access):
        actor = _actor(member)
        if step == "entity":
            return onec_start.add_entity(session, workspace, access, actor, base_id)
        if step == "participants":
            return onec_start.add_participants(session, workspace, access, actor, base_id, today=today)
        if step == "contracts":
            return onec_start.add_contracts(session, workspace, access, actor, base_id, today=today, answers=answers)
        if step == "since":
            osv = onec_start.stored_osv(session, workspace, base_id)
            if osv is None:
                raise onec_start.StartError("Сначала загрузите ОСВ по 1210 на первое число - из неё ляжет сальдо")
            return onec_start.start_since(session, workspace, access, actor, base_id, osv)
        if step == "payments":
            return onec_start.use_payments(session, workspace, access, actor, base_id)
        raise HTTPException(status_code=404, detail="Такого шага нет")

    return _run(member, action)
