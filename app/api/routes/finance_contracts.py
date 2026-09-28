"""HTTP-маршруты реестра договоров «Финансов» (`/finance/contracts/…`).

Вход и компания — те же, что у раздела (`current_member`, `_workspace` из
`routes/finance.py`); сюда не импортируется ничего из `app.bbc`.

Что фронт получает на отказ
───────────────────────────
* 422 `{"code": "mode_required", "fields": [...]}` — правка стороны или суммы
  без ответа «опечатка или с даты»; по коду фронт открывает вопрос у поля.
* 409 `{"code": "conflict", "conflicts": [...], "contract": {...}}` — поле
  успели поменять; конфликт считается по полю, а не по записи.
* 404 — договора нет или он не открыт: ответ одинаковый, иначе перебор
  идентификаторов выдавал бы, какие договоры существуют.
* 403 — действие не открыто; 400 — значение нельзя принять, с текстом.

Обработчики — обычный `def`: внутри синхронные SQLAlchemy и openpyxl.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.routes.finance import _workspace, require_access
from app.finance import heap, history
from app.finance.auth import Member
from app.finance.config import finance_settings
from app.finance.contracts import amendments as amendments_module
from app.finance.contracts import export as export_module
from app.finance.contracts import importer, payments, service, setup
from app.finance.contracts import summary as summary_module
from app.finance.contracts.views import FilterError
from app.finance.db import finance_session
from app.finance.service import FinanceError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/finance/contracts", tags=["finance-contracts"])

#: Одна проверка на все двери реестра — раздел «Договоры» открыт на чтение
#: или на правку. Временный пароль (`must_change_password`), продиктованный
#: по телефону и оставшийся в переписке, не открывает договоры: его
#: отсекает `require_access` в `routes/finance.py` — одна дверь для всего
#: раздела, а не проверка по месту (урок дашборда BBC, где её забыли в одном
#: маршруте). Строки и поля договоров режутся дальше, в сервисе, по `Access`.
contract_member = require_access("contracts", "view")
contract_editor = require_access("contracts", "edit")


def _access(member: Member) -> service.Access:
    access = service.access_of(member)
    if not access.view:
        raise HTTPException(status_code=403, detail="Реестр договоров вам не открыт")
    return access


def _actor(member: Member) -> service.Actor:
    # Логин, а не почта: у сотрудника, входящего по номеру, почты нет.
    return service.Actor(member.user_id, member.login)


def _fail(exc: Exception, session=None, workspace=None, access=None) -> HTTPException | JSONResponse:
    if isinstance(exc, service.ModeRequired):
        return JSONResponse(
            status_code=422,
            content={"code": "mode_required", "fields": exc.fields, "detail": str(exc)},
        )
    if isinstance(exc, service.FieldConflict):
        payload: dict[str, Any] = {"code": "conflict", "conflicts": exc.conflicts, "detail": str(exc)}
        if session is not None and workspace is not None and access is not None:
            try:
                payload.update(service.one(session, workspace, access, exc.contract))
            except FinanceError:
                pass
        return JSONResponse(status_code=409, content=payload)
    if isinstance(exc, service.NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (FinanceError, FilterError)):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


def _raise(exc: Exception, **context: Any):
    outcome = _fail(exc, **context)
    if isinstance(outcome, HTTPException):
        raise outcome from exc
    return outcome


# ── Схема, список, изменения ─────────────────────────────────────────────────


@router.get("/schema")
def get_schema(member: Member = Depends(contract_member)) -> dict[str, Any]:
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        return setup.schema(session, workspace, access)


@router.get("")
def list_contracts(member: Member = Depends(contract_member)) -> Response:
    """Весь реестр готовыми байтами: одна сборка на одинаковые запросы, без
    обхода 13 МБ словарей `jsonable_encoder` (см. `service.list_all_bytes`)."""
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        body = service.list_all_bytes(session, workspace, access)
    return Response(content=body, media_type="application/json")


@router.get("/changes")
def get_changes(since: int = Query(0, ge=0), member: Member = Depends(contract_member)) -> Response:
    """Опрос раз в 2 с из каждой вкладки: одна сборка на курсор (см.
    `service.changes_bytes`), готовые байты без `jsonable_encoder`."""
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        body = service.changes_bytes(session, workspace, access, since)
    return Response(content=body, media_type="application/json")


@router.get("/payments")
def list_payments(member: Member = Depends(contract_member)) -> dict[str, Any]:
    """«Оплачено/Остаток по выписке» видимых договоров. Объявлен до
    `/{contract_id}`: иначе «payments» разбирался бы как идентификатор."""
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return payments.summaries(session, workspace, access)
        except Exception as exc:  # noqa: BLE001
            return _raise(exc)


@router.get("/summary")
def list_summary(force: bool = Query(False), member: Member = Depends(contract_member)) -> dict[str, Any]:
    """«Оплачено/Остаток (сводка)» видимых договоров — из книги-сводки компании.
    `force` — «Обновить»: перечитать книгу, не дожидаясь срока кэша."""
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return summary_module.contracts_out(session, workspace, access, force=force)
        except Exception as exc:  # noqa: BLE001
            return _raise(exc)


@router.get("/export.xlsx")
def export_xlsx(views: str = Query(""), member: Member = Depends(contract_member)) -> Response:
    access = _access(member)
    keys = [key for key in views.split(",") if key] or None
    try:
        with finance_session() as session:
            workspace = _workspace(session, member)
            data = export_module.build(session, workspace, access, _actor(member), keys)
            title = workspace.title
    finally:
        heap.trim()
    name = f"Реестр договоров — {title} — {datetime.now():%Y-%m-%d}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


# ── Стороны и люди ───────────────────────────────────────────────────────────


@router.get("/parties")
def search_parties(q: str = Query(""), limit: int = Query(20, ge=1, le=100), member: Member = Depends(contract_member)) -> dict[str, Any]:
    _access(member)
    from app.finance.layout import norm

    from app.finance.contracts.fields import party_key

    with finance_session() as session:
        workspace = _workspace(session, member)
        registry = service.Registry(session, workspace)
        needle, key = norm(q), party_key(q)
        scored = []
        for party in registry.parties.values():
            if party.archived_at is not None:
                continue
            own = registry.is_own(party.id)
            if not q:
                score = 0 if own else 1
            elif norm(party.name).startswith(needle) or party_key(party.name).startswith(key):
                score = 0
            elif needle in norm(party.name) or (key and key in party_key(party.name)):
                score = 1
            else:
                bin_value = (party.details or {}).get("bin", "")
                if q.strip() and bin_value and q.strip() in bin_value:
                    score = 1
                else:
                    continue
            scored.append((score, 0 if own else 1, party.name.lower(), party))
        scored.sort(key=lambda item: item[:3])
        output = service.Output(session, registry, service.Access(view=True))
        return {"parties": list(output.parties([item[3].id for item in scored[:limit]]).values())}


@router.get("/parties/similar")
def similar_parties(name: str = Query(""), bin: str = Query(""), member: Member = Depends(contract_member)) -> dict[str, Any]:
    """Кандидаты на «Это ТОО «Атриум плюс»?» — похожие, но не равные по ключу."""
    _access(member)
    import re

    from app.finance.contracts.fields import party_key

    with finance_session() as session:
        workspace = _workspace(session, member)
        registry = service.Registry(session, workspace)
        key = party_key(name)
        bare = re.sub(r"^(тоо|ип|ао)", "", key)
        found = []
        for party in registry.parties.values():
            if party.archived_at is not None:
                continue
            other = party_key(party.name)
            other_bare = re.sub(r"^(тоо|ип|ао)", "", other)
            same_bin = bool(bin.strip()) and (party.details or {}).get("bin") == bin.strip()
            if other == key and not same_bin:
                continue
            if same_bin or (len(bare) >= 4 and (other_bare == bare or other_bare.startswith(bare) or bare.startswith(other_bare) and len(other_bare) >= 4)):
                found.append(party.id)
        output = service.Output(session, registry, service.Access(view=True))
        return {"parties": list(output.parties(found[:10]).values())}


@router.get("/people")
def list_people(member: Member = Depends(contract_member)) -> dict[str, Any]:
    """Справочник ответственных — действующие сотрудники из личного кабинета.

    Ушедшие в архив в выбор не попадают; подпись у старых договоров, где они
    стоят, приходит вместе с договорами.
    """
    _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        registry = service.Registry(session, workspace)
        output = service.Output(session, registry, service.Access(view=True))
        active = sorted(
            (item for item in registry.employees.values() if item.archived_at is None),
            key=lambda item: (item.position, item.full_name),
        )
        found = output.people([item.id for item in active])
        return {"people": [found[str(item.id)] for item in active if str(item.id) in found]}


# ── Загрузка Excel ───────────────────────────────────────────────────────────


class DecisionsIn(BaseModel):
    decisions: dict[str, Any] = Field(default_factory=dict)


def _batch_out(batch) -> dict[str, Any]:
    return {
        "id": str(batch.id),
        "status": batch.status,
        "file_name": batch.file_name,
        "decisions": batch.decisions or {},
        "report": batch.report or {},
    }


def _require_setup(access: service.Access) -> None:
    if not access.setup:
        raise HTTPException(status_code=403, detail="Это может владелец или администратор")


@router.post("/imports", status_code=201)
def upload_registry(file: UploadFile = File(...), member: Member = Depends(contract_editor)) -> dict[str, Any]:
    access = _access(member)
    _require_setup(access)
    limit = int(finance_settings.import_max_mb * 1024 * 1024)
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"Файл больше {finance_settings.import_max_mb} МБ")
    name = file.filename or "реестр.xlsx"
    if not name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Реестр загружается из .xlsx")
    try:
        with finance_session() as session:
            workspace = _workspace(session, member)
            try:
                batch = importer.start(session, workspace, _actor(member), data, name)
            except Exception as exc:  # noqa: BLE001
                _raise(exc)
            history.write(
                session, workspace, kind="contract.import.upload", entity="contract_import", entity_id=batch.id,
                title=f"разбор реестра «{name}»", after={"file_name": name, "bytes": len(data)},
            )
            return _batch_out(batch)
    finally:
        del data
        heap.trim()


@router.get("/imports/{batch_id}")
def get_import(batch_id: UUID, member: Member = Depends(contract_member)) -> dict[str, Any]:
    access = _access(member)
    _require_setup(access)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return _batch_out(importer.get_batch(session, workspace, batch_id))
        except Exception as exc:  # noqa: BLE001
            _raise(exc)


@router.post("/imports/{batch_id}/decide")
def decide_import(batch_id: UUID, body: DecisionsIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    access = _access(member)
    _require_setup(access)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return _batch_out(importer.decide(session, workspace, batch_id, body.decisions))
        except Exception as exc:  # noqa: BLE001
            _raise(exc)


@router.post("/imports/{batch_id}/apply")
def apply_import(batch_id: UUID, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    access = _access(member)
    _require_setup(access)
    try:
        with finance_session() as session:
            workspace = _workspace(session, member)
            try:
                result = importer.apply(session, workspace, access, _actor(member), batch_id)
            except Exception as exc:  # noqa: BLE001
                _raise(exc)
            return {"result": result}
    finally:
        heap.trim()


@router.post("/imports/{batch_id}/cancel")
def cancel_import(batch_id: UUID, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    access = _access(member)
    _require_setup(access)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            importer.cancel(session, workspace, batch_id)
        except Exception as exc:  # noqa: BLE001
            _raise(exc)
        history.write(
            session, workspace, kind="contract.import.cancel", entity="contract_import", entity_id=batch_id,
            title="разбор реестра отменён",
        )
        return {"ok": True}


# ── Настройка ────────────────────────────────────────────────────────────────


class FieldIn(BaseModel):
    title: str | None = None
    type: str | None = None
    after: str | None = None
    hidden: bool | None = None
    required: bool | None = None
    archived: bool | None = None
    fill: str | None = None


class ValueIn(BaseModel):
    value: str | None = None
    meaning: dict[str, Any] | None = None
    position: int | None = None
    archived: bool | None = None
    #: «Это разные»: значение того же списка, которое не двойник этого.
    distinct: UUID | None = None


class MergeIn(BaseModel):
    keep: UUID
    drop: UUID


class EntityIn(BaseModel):
    name: str | None = None
    code: str | None = None
    full_name: str | None = None
    bin: str | None = None
    vat_payer: bool | None = None
    archived: bool | None = None
    accounts: list[UUID] | None = None


class ViewIn(BaseModel):
    title: str | None = None
    book: str | None = None
    blocks: list[dict[str, Any]] | None = None
    style: dict[str, Any] | None = None
    position: int | None = None
    archived: bool | None = None


class DepartmentIn(BaseModel):
    code: str | None = None
    title: str | None = None


class FilterIn(BaseModel):
    filter: dict[str, Any] = Field(default_factory=dict)


def _setup_call(member: Member, action, what: str, kind: str):
    """Настройка реестра — только владелец и администратор; каждая — событие журнала.

    Поле, список, лист, юрлицо меняют то, как читается весь реестр, и
    «кто добавил этот лист» должно быть видно так же, как правка договора.
    """
    access = _access(member)
    _require_setup(access)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            result = action(session, workspace, access)
        except Exception as exc:  # noqa: BLE001
            _raise(exc)
        history.write(
            session, workspace, kind=f"contracts.setup.{kind}", entity="contract_setup",
            title=f"настройка реестра: {what}",
            after=result if isinstance(result, dict) and len(str(result)) < 4000 else {},
        )
        return result


def _data(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(exclude_unset=True)


@router.post("/setup/fields", status_code=201)
def add_field(body: FieldIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    def action(session, workspace, access):
        item = setup.add_field(session, workspace, title=body.title or "", type=body.type or "text", after=body.after)
        return {"key": item.key}

    return _setup_call(member, action, "поле добавлено", "field_add")


@router.patch("/setup/fields/{key}")
def update_field(key: str, body: FieldIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    return _setup_call(member, lambda s, w, a: {"key": setup.update_field(s, w, key, _data(body)).key}, "поле изменено", "field_update")


@router.post("/setup/lists/{field_key}", status_code=201)
def add_value(field_key: str, body: ValueIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    return _setup_call(
        member, lambda s, w, a: {"id": str(setup.add_value(s, w, field_key, body.value or "", body.meaning).id)}, "значение списка добавлено", "value_add")


@router.patch("/setup/values/{value_id}")
def update_value(value_id: UUID, body: ValueIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    return _setup_call(member, lambda s, w, a: {"id": str(setup.update_value(s, w, value_id, _data(body)).id)}, "значение списка изменено", "value_update")


@router.post("/setup/values/merge")
def merge_values(body: MergeIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    def action(session, workspace, access):
        setup.merge_values(session, workspace, keep=body.keep, drop=body.drop)
        return {"ok": True}

    return _setup_call(member, action, "значения списка объединены", "value_merge")


@router.post("/setup/entities", status_code=201)
def add_entity(body: EntityIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    def action(session, workspace, access):
        entity = setup.add_entity(
            session, workspace, name=body.name or body.code or "", code=body.code or "",
            full_name=body.full_name or "", bin=body.bin or "", vat_payer=bool(body.vat_payer),
        )
        return {"id": str(entity.counterparty_id)}

    return _setup_call(member, action, "наше юрлицо добавлено", "entity_add")


@router.patch("/setup/entities/{party_id}")
def update_entity(party_id: UUID, body: EntityIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    return _setup_call(
        member, lambda s, w, a: {"id": str(setup.update_entity(s, w, party_id, _data(body)).counterparty_id)}, "наше юрлицо изменено", "entity_update")


@router.post("/setup/parties/merge")
def merge_parties(body: MergeIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    def action(session, workspace, access):
        setup.merge_parties(session, workspace, keep=body.keep, drop=body.drop)
        return {"ok": True}

    return _setup_call(member, action, "контрагенты объединены", "party_merge")


@router.post("/setup/departments", status_code=201)
def add_department(body: DepartmentIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    return _setup_call(member, lambda s, w, a: {"id": str(setup.upsert_department(s, w, _data(body)).id)}, "отдел добавлен", "department_add")


@router.patch("/setup/departments/{department_id}")
def update_department(department_id: UUID, body: DepartmentIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    return _setup_call(
        member, lambda s, w, a: {"id": str(setup.upsert_department(s, w, _data(body), department_id).id)}, "отдел изменён", "department_update")


@router.post("/setup/views", status_code=201)
def add_view(body: ViewIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    return _setup_call(member, lambda s, w, a: setup.view_out(setup.upsert_view(s, w, _data(body))), "лист добавлен", "view_add")


@router.patch("/setup/views/{view_id}")
def update_view(view_id: UUID, body: ViewIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    return _setup_call(member, lambda s, w, a: setup.view_out(setup.upsert_view(s, w, _data(body), view_id)), "лист изменён", "view_update")


class SummaryIn(BaseModel):
    link: str
    worksheet: str


@router.post("/setup/summary")
def connect_summary(body: SummaryIn, member: Member = Depends(contract_editor)) -> dict[str, Any]:
    """Подключить книгу-сводку: читается сразу, сохраняется, только если читается."""

    def action(session, workspace, access):
        try:
            source, index = summary_module.connect(
                session, workspace.id, link=body.link, worksheet=body.worksheet, user_id=member.user_id
            )
        except summary_module.SummaryError as exc:
            raise FinanceError(str(exc)) from exc
        setup._schema_changed(session, workspace)
        return {"title": source.title, "worksheet": source.worksheet, "rows": index.rows, "drift": index.drift}

    return _setup_call(member, action, "подключена сводка оплат", "summary_connect")


@router.delete("/setup/summary")
def disconnect_summary(member: Member = Depends(contract_editor)) -> dict[str, Any]:
    def action(session, workspace, access):
        summary_module.disconnect(session, workspace.id)
        setup._schema_changed(session, workspace)
        return {"ok": True}

    return _setup_call(member, action, "сводка оплат отключена", "summary_disconnect")


@router.post("/setup/views/preview")
def preview_view(body: FilterIn, member: Member = Depends(contract_member)) -> dict[str, Any]:
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return setup.preview_filter(session, workspace, access, body.filter)
        except Exception as exc:  # noqa: BLE001
            _raise(exc)


# ── Договор ──────────────────────────────────────────────────────────────────


class CreateIn(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    view: str | None = None
    block: int | None = None
    source: str = "app"


class PatchIn(BaseModel):
    values: dict[str, Any]
    known_seq: int | None = None
    mode: dict[str, Any] | None = None


class AcknowledgeIn(BaseModel):
    code: str
    on: bool = True


class PieceIn(BaseModel):
    piece: dict[str, Any]


@router.post("", status_code=201)
def create_contract(body: CreateIn, member: Member = Depends(contract_editor)):
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            contract = service.create(
                session, workspace, access, _actor(member), body.values,
                view_key=body.view, block=body.block,
                source="grid" if body.source == "grid" else "app",
            )
            return service.one(session, workspace, access, contract)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            return _raise(exc)


@router.get("/{contract_id}")
def get_contract(contract_id: UUID, member: Member = Depends(contract_member)):
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            contract = service.get_contract(session, workspace, contract_id)
            return service.one(session, workspace, access, contract)
        except Exception as exc:  # noqa: BLE001
            return _raise(exc)


@router.patch("/{contract_id}")
def patch_contract(contract_id: UUID, body: PatchIn, member: Member = Depends(contract_editor)):
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            mode = service.Mode.parse(body.mode)
            contract = service.patch(
                session, workspace, access, _actor(member), contract_id, body.values,
                known_seq=body.known_seq, mode=mode,
            )
            return service.one(session, workspace, access, contract)
        except Exception as exc:  # noqa: BLE001
            # Правка не состоялась целиком: ни одно поле, ни соглашение, ни
            # заведённый по пути контрагент не остаются в базе.
            session.rollback()
            return _raise(exc, session=session, workspace=_workspace(session, member), access=access)


@router.delete("/{contract_id}")
def delete_contract(contract_id: UUID, member: Member = Depends(contract_editor)):
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            service.remove(session, workspace, access, _actor(member), contract_id)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            return _raise(exc)


@router.post("/{contract_id}/acknowledge")
def acknowledge(contract_id: UUID, body: AcknowledgeIn, member: Member = Depends(contract_editor)):
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            contract = service.acknowledge(session, workspace, access, _actor(member), contract_id, body.code, on=body.on)
            return service.one(session, workspace, access, contract)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            return _raise(exc)


class PaymentIn(BaseModel):
    operation_id: UUID
    action: str


@router.get("/{contract_id}/payments")
def contract_payments(contract_id: UUID, member: Member = Depends(contract_member)):
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return payments.of_contract(session, workspace, access, contract_id)
        except Exception as exc:  # noqa: BLE001
            return _raise(exc)


@router.post("/{contract_id}/payments")
def decide_payment(contract_id: UUID, body: PaymentIn, member: Member = Depends(contract_editor)):
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return payments.decide(
                session, workspace, access, _actor(member), contract_id, body.operation_id, body.action
            )
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            return _raise(exc)


@router.get("/{contract_id}/history")
def contract_history(contract_id: UUID, before: str | None = Query(None), member: Member = Depends(contract_member)):
    _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            cursor = datetime.fromisoformat(before) if before else None
            return {"items": service.history_of(session, workspace, contract_id, before=cursor)}
        except Exception as exc:  # noqa: BLE001
            return _raise(exc)


@router.get("/{contract_id}/amendments")
def list_amendments(contract_id: UUID, member: Member = Depends(contract_member)):
    _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return {"items": amendments_module.listing(session, workspace, contract_id)}
        except Exception as exc:  # noqa: BLE001
            return _raise(exc)


@router.post("/{contract_id}/amendments/parse")
def parse_amendments(contract_id: UUID, member: Member = Depends(contract_member)):
    _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return {"pieces": amendments_module.parse(session, workspace, contract_id)}
        except Exception as exc:  # noqa: BLE001
            return _raise(exc)


@router.post("/{contract_id}/amendments/confirm")
def confirm_amendment(contract_id: UUID, body: PieceIn, member: Member = Depends(contract_editor)):
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            contract = amendments_module.confirm(session, workspace, access, _actor(member), contract_id, body.piece)
            return service.one(session, workspace, access, contract)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            return _raise(exc)


@router.delete("/{contract_id}/amendments/{amendment_id}")
def remove_amendment(contract_id: UUID, amendment_id: UUID, member: Member = Depends(contract_editor)):
    access = _access(member)
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            amendments_module.remove_amendment(session, workspace, access, _actor(member), contract_id, amendment_id)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            return _raise(exc)
