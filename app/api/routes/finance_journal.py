"""Журнал финансистов (`/finance/journal/*`): записи, загрузка выписки с предпросмотром, шаблон.

Право - «Журнал» (`journal`): смотреть - `view`, править и загружать -
`edit`. Табличный вид того же журнала - `/finance/grid` (право «Таблица»).
Договоры у записей видны тому, кому открыт реестр (`contracts`): номер,
клиент и «Оплачено» - сведения реестра, а не журнала.

Загрузка - три шага, как окно предпросмотра на экране: разобрать файл
(`POST /journal/preview` - в учёт ничего не пишет), поправить строки
(`PATCH …/rows/{line}`), «Принять» или «Отклонить». Принятая загрузка - одно
событие истории на файл, правки записей - по событию на запись с откатом.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from app.api.routes.finance import _actor, _fail, _guard, _parse_date, _workspace, require_access
from app.finance import history, journal
from app.finance.auth import Member
from app.finance.config import finance_settings
from app.finance.db import finance_session
from app.finance.importing import ImportError_
from app.finance.models import ImportBatch, Operation
from app.finance.service import FinanceError, save_preview

router = APIRouter(prefix="/finance/journal", tags=["finance-journal"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _contracts_visible(member: Member) -> bool:
    return bool(member.rights.can("contracts", "view"))


def _uuids(value: str | None) -> list[UUID]:
    out = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(UUID(part))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Не понял идентификатор «{part}»") from exc
    return out


def _filter(
    month: str | None,
    date_from: str | None,
    date_to: str | None,
    firm: UUID | None,
    accounts: str | None,
    categories: str | None,
    kinds: str | None,
    search: str | None,
    attention: str | None,
    contract_ref: str | None,
) -> journal.JournalFilter:
    if attention and attention not in journal.ATTENTION:
        raise HTTPException(status_code=422, detail="Неизвестный отбор «требует внимания»")
    return journal.JournalFilter(
        month=month or "",
        date_from=_parse_date(date_from, field="date_from"),
        date_to=_parse_date(date_to, field="date_to"),
        firm=firm,
        account_ids=_uuids(accounts),
        category_ids=_uuids(categories),
        kinds=[item for item in (kinds or "").split(",") if item in ("income", "expense", "transfer")],
        search=search or "",
        attention=attention or "",
        contract_ref=contract_ref or "",
    )


def _file(data: bytes, name: str) -> Response:
    return Response(
        content=data,
        media_type=XLSX,
        headers={"Content-Disposition": f"attachment; filename=\"journal.xlsx\"; filename*=UTF-8''{quote(name)}"},
    )


@router.get("")
def list_journal(
    member: Member = Depends(require_access("journal")),
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    date_from: str | None = None,
    date_to: str | None = None,
    firm: UUID | None = None,
    accounts: str | None = None,
    categories: str | None = None,
    kinds: str | None = None,
    search: str | None = None,
    attention: str | None = None,
    contract_ref: str | None = None,
    limit: int = Query(default=400, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Записи журнала по отбору, итоги, «требуют внимания», остатки на счетах, месяцы."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        flt = _filter(month, date_from, date_to, firm, accounts, categories, kinds, search, attention, contract_ref)
        return journal.list_records(
            session, workspace, flt, limit=limit, offset=offset, contracts_visible=_contracts_visible(member)
        )


@router.get("/dictionaries")
def journal_dictionaries(member: Member = Depends(require_access("journal"))) -> dict[str, Any]:
    """Счета, статьи, юрлица, договоры и месяцы ОПиУ - для выбора в записи и в окне загрузки."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return journal.dictionaries(session, workspace, contracts_visible=_contracts_visible(member))


class CreateIn(BaseModel):
    fields: dict[str, Any]


@router.post("", status_code=201)
def create_record(body: CreateIn, member: Member = Depends(require_access("journal", "edit"))) -> dict[str, Any]:
    """Новая запись журнала из карточки: приход, расход или перевод полями журнала."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            operation = journal.create_record(session, workspace, body.fields, actor=_actor(member))
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session, workspace, kind="operation.create", entity="operation", entity_id=operation.id,
            title="запись заведена в журнале", after=history.snapshot(operation),
        )
        return {"record": _record(session, workspace, operation, member)}


class EditIn(BaseModel):
    version: int | None = None
    #: Сторона перевода, которую правят («in» / «out»), - из табличного вида.
    side: str | None = Field(default=None, pattern="^(in|out)$")
    fields: dict[str, Any]


def _record(session: Any, workspace: Any, operation: Operation, member: Member) -> dict[str, Any]:
    book = journal.Book.load(session, workspace.id)
    contracts = journal.Contracts(session, workspace) if _contracts_visible(member) else None
    return journal.record_of(
        operation,
        book,
        splits=journal._split_marks(session, [operation]),
        projects=journal._projects_of(session, [operation.id], book),
        contracts=contracts,
    )


@router.patch("/{operation_id}")
def edit_record(operation_id: UUID, body: EditIn, member: Member = Depends(require_access("journal", "edit"))) -> dict[str, Any]:
    """Правка записи по полям журнала: сумма в «Приходе» или «Расходе», счёт, статья, №Дог., ОПиУ…"""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        existing = session.get(Operation, operation_id)
        before = history.snapshot(existing) if existing is not None and existing.workspace_id == workspace.id else {}
        try:
            operation = journal.edit(
                session, workspace, operation_id, body.fields, version=body.version, side=body.side, actor=_actor(member)
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        after = history.snapshot(operation)
        if after != before:
            history.write(
                session, workspace, kind="operation.update", entity="operation", entity_id=operation.id,
                title="правка в журнале: " + ", ".join(sorted(body.fields)), before=before, after=after,
            )
        return {"record": _record(session, workspace, operation, member), "lines": journal.grid_lines(session, workspace, [operation])}


class PartIn(BaseModel):
    amount: str
    pnl: str | None = None
    contract_ref: str | None = None
    category: str | None = None
    details: str | None = None
    note: str | None = None


class SplitIn(BaseModel):
    version: int | None = None
    parts: list[PartIn] = Field(min_length=2, max_length=60)


@router.post("/{operation_id}/split")
def split_record(operation_id: UUID, body: SplitIn, member: Member = Depends(require_access("journal", "edit"))) -> dict[str, Any]:
    """«Дробление 1 суммы»: запись на части, которые дают сумму выписки до копейки."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        existing = session.get(Operation, operation_id)
        before = history.snapshot(existing) if existing is not None and existing.workspace_id == workspace.id else {}
        try:
            parts = journal.split(
                session,
                workspace,
                operation_id,
                [part.model_dump(exclude_none=True) for part in body.parts],
                version=body.version,
                actor=_actor(member),
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session, workspace, kind="operation.update", entity="operation", entity_id=parts[0].id,
            title=f"запись разбита на {len(parts)} части", before=before, after=history.snapshot(parts[0]),
        )
        for part in parts[1:]:
            history.write(
                session, workspace, kind="operation.create", entity="operation", entity_id=part.id,
                title="часть дробления", after=history.snapshot(part),
            )
        return {"records": [_record(session, workspace, part, member) for part in parts]}


@router.post("/split/{group}/join")
def join_parts(group: UUID, member: Member = Depends(require_access("journal", "edit"))) -> dict[str, Any]:
    """Собрать части дробления обратно в одну запись на сумму выписки."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            head = journal.unsplit(session, workspace, group, actor=_actor(member))
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session, workspace, kind="operation.update", entity="operation", entity_id=head.id,
            title="части дробления собраны в одну запись", after=history.snapshot(head),
        )
        return {"record": _record(session, workspace, head, member)}


# ── Загрузка выписки или шаблона ─────────────────────────────────────────────


@router.post("/preview")
def preview_upload(
    member: Member = Depends(require_access("journal", "edit")),
    file: UploadFile = File(...),
    date_order: str | None = Query(default=None, pattern="^(dmy|mdy)$"),
    default_account: str | None = None,
) -> dict[str, Any]:
    """Разобрать выписку или заполненный шаблон и показать, как журнал будет выглядеть. Ничего не заводит."""
    _guard()
    data = file.file.read(finance_settings.import_max_bytes + 1)
    if len(data) > finance_settings.import_max_bytes:
        raise HTTPException(status_code=413, detail=f"Файл больше {finance_settings.import_max_mb} МБ - разделите его на части")
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            preview, extra = journal.analyze_file(
                session, workspace, data, file.filename or "файл", date_order=date_order, default_account=default_account
            )
        except ImportError_ as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if len(preview.rows) > finance_settings.import_max_rows:
            raise HTTPException(
                status_code=413, detail=f"В файле {len(preview.rows)} строк - потолок {finance_settings.import_max_rows}"
            )
        batch = save_preview(session, workspace, preview, actor=_actor(member))
        batch.decisions = {
            **(batch.decisions or {}),
            "accounts_new": extra["accounts_new"],
            "journal_source": extra["source"],
        }
        history.write(
            session, workspace, kind="import.preview", entity="import_batch", entity_id=batch.id,
            title=f"разбор «{preview.file_name}» для журнала: {len(preview.rows)} строк",
            after={"file_name": preview.file_name, "rows": len(preview.rows)},
        )
        session.flush()
        payload = journal.preview_payload(session, workspace, batch)
        payload["question"] = preview.question
        payload["bank"] = preview.bank
        return payload


def _batch(session: Any, workspace: Any, batch_id: UUID) -> ImportBatch:
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Загрузка не найдена")
    return batch


@router.get("/preview/{batch_id}")
def read_preview(batch_id: UUID, member: Member = Depends(require_access("journal"))) -> dict[str, Any]:
    """Окно загрузки заново - после перезагрузки страницы или из «Прошлых загрузок»."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return journal.preview_payload(session, workspace, _batch(session, workspace, batch_id))


class RowIn(BaseModel):
    fields: dict[str, Any]


@router.patch("/preview/{batch_id}/rows/{line}")
def fix_preview_row(batch_id: UUID, line: int, body: RowIn, member: Member = Depends(require_access("journal", "edit"))) -> dict[str, Any]:
    """Правка строки загрузки до «Принять»: ячейки журнала, «Завести всё равно»."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            row = journal.fix_row(session, workspace, batch_id, line, body.fields)
        except FinanceError as exc:
            raise _fail(exc) from exc
        values = row.parsed or {}
        return {
            "line": row.line,
            "state": row.state,
            "problems": row.problems or [],
            "cells": journal.preview_line(values),
            "kind": values.get("kind"),
            "contract_auto": values.get("contract_auto") or "",
            "paired_with": values.get("paired_with"),
            "maybe_duplicate": bool(values.get("maybe_duplicate")),
            "operation_id": None,
        }


class AcceptIn(BaseModel):
    create_accounts: bool = True


@router.post("/preview/{batch_id}/accept")
def accept_upload(batch_id: UUID, body: AcceptIn, member: Member = Depends(require_access("journal", "edit"))) -> dict[str, Any]:
    """«Принять»: готовые строки ложатся в журнал одной пачкой, новые счета заводятся."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        batch = _batch(session, workspace, batch_id)
        try:
            done = journal.accept(session, workspace, batch_id, actor=_actor(member), create_accounts=body.create_accounts)
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session, workspace, kind="import.apply", entity="import_batch", entity_id=batch_id,
            title=f"загрузка «{batch.file_name}» принята в журнал · заведено {done['imported']}",
            after={key: done.get(key) for key in ("imported", "completed", "failed", "skipped", "duplicate", "total")},
        )
        return done


@router.post("/preview/{batch_id}/reject")
def reject_upload(batch_id: UUID, member: Member = Depends(require_access("journal", "edit"))) -> dict[str, Any]:
    """«Отклонить»: в журнал ничего не ложится."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        batch = _batch(session, workspace, batch_id)
        try:
            journal.reject(session, workspace, batch_id)
        except FinanceError as exc:
            raise _fail(exc) from exc
        history.write(
            session, workspace, kind="import.reject", entity="import_batch", entity_id=batch_id,
            title=f"загрузка «{batch.file_name}» отклонена",
        )
        return {"status": "cancelled"}


# ── Шаблон и выгрузка ────────────────────────────────────────────────────────


@router.get("/template.xlsx")
def journal_template(member: Member = Depends(require_access("journal"))) -> Response:
    """Шаблон журнала со справочником компании: заполнить у себя и загрузить."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        data = journal.template_xlsx(session, workspace)
        title = workspace.title
    return _file(data, f"Шаблон журнала - {title}.xlsx")


@router.get("/export.xlsx")
def journal_export(
    member: Member = Depends(require_access("journal")),
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    date_from: str | None = None,
    date_to: str | None = None,
    firm: UUID | None = None,
    accounts: str | None = None,
    categories: str | None = None,
    kinds: str | None = None,
    search: str | None = None,
    attention: str | None = None,
    contract_ref: str | None = None,
) -> Response:
    """Журнал по отбору файлом - в колонках шаблона: его можно поправить и загрузить обратно."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        flt = _filter(month, date_from, date_to, firm, accounts, categories, kinds, search, attention, contract_ref)
        data = journal.export_xlsx(session, workspace, flt)
        history.write(
            session, workspace, kind="journal.export", entity="operations",
            title="выгрузка журнала" + (f" · {month}" if month else ""),
            after={"month": month, "date_from": date_from, "date_to": date_to},
        )
        title = workspace.title
    return _file(data, f"Журнал - {title} - {month or date.today().strftime('%d.%m.%Y')}.xlsx")
