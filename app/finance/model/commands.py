"""Команды модели - что «предложить» и «применить» умеют делать.

Команда - имя, параметры (схема pydantic - её отдаёт `GET /finance/model`),
право и исполнение. Исполнение - те же функции ядра, что у экранов
(`service.patch`, `changes.status_from`, `views.create`…), поэтому правило
записи одно: журнал действий, права полей, конфликты, «опечатка или с
даты». Каждая команда возвращает, что сделала, какие договоры задела и
обратный шаг для точки восстановления пачки (`apply.py`).

В v1 - только команды, которые пишут в базу и больше никуда: прогон
«предложить» откатывается целиком, а письмо в Google или пост в Telegram
откатить нельзя.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from app.finance.contracts import service
from app.finance.model.base import Command, CommandError, Done, Step, _actor, _label, _Params
from app.finance.model.context import Ctx


# ── Договор: правка, заведение, удаление ─────────────────────────────────────


class ContractUpdate(_Params):
    id: uuid.UUID
    #: Поля по ключам схемы (`describe` → `fields`), как у правки карточки.
    values: dict[str, Any]
    #: `seq` договора, который видел клиент: поле, поменянное другим после него, - конфликт.
    known_seq: int | None = None
    #: Ответ на «опечатка или изменение с даты»: `{"kind": "fix"}` / `{"kind": "from_date", "effective_from": "2026-11-01"}`.
    mode: dict[str, Any] | None = None


def _contract_update(ctx: Ctx, p: ContractUpdate) -> Done:
    actor = _actor(ctx)
    contract = service.get_contract(ctx.session, ctx.workspace, p.id)
    registry = service.Registry(ctx.session, ctx.workspace)
    people = service.people_of(ctx.session, [contract.id]).get(contract.id, [])
    before = [
        {"id": str(contract.id), "key": key, "before": service.value_of(contract, key, people)}
        for key in p.values
        if key in registry.field_by_key and key not in ctx.access.hidden
    ]
    changed = service.patch(
        ctx.session, ctx.workspace, ctx.access, actor, p.id, p.values, known_seq=p.known_seq, mode=service.Mode.parse(p.mode),
    )
    titles = sorted({registry.field_by_key[item["key"]].title.lower() for item in before})
    return Done(
        title=f"{_label(changed)}: {', '.join(titles) or 'поля'}",
        result=service.one(ctx.session, ctx.workspace, ctx.access, changed),
        contracts=[str(changed.id)],
        steps=[Step("values", {"items": before})] if before else [],
    )


class ContractCreate(_Params):
    values: dict[str, Any]
    #: Код договора, выданный клиентом: повтор команды не заводит второй договор.
    id: uuid.UUID | None = None
    #: Лист и блок, куда клиент ставит договор: подстановки блока ставит сервер.
    view: str | None = None
    block: int | None = None


def _contract_create(ctx: Ctx, p: ContractCreate) -> Done:
    actor = _actor(ctx)
    existed = p.id is not None and ctx.session.get(service.Contract, p.id) is not None
    contract = service.create(
        ctx.session, ctx.workspace, ctx.access, actor, p.values, view_key=p.view, block=p.block, source="app", contract_id=p.id,
    )
    return Done(
        title=f"заведён {_label(contract)}" if not existed else f"{_label(contract)} уже заведён",
        result=service.one(ctx.session, ctx.workspace, ctx.access, contract),
        contracts=[str(contract.id)],
        steps=[] if existed else [Step("contracts_create", {"ids": [str(contract.id)]})],
    )


class ContractDelete(_Params):
    id: uuid.UUID


def _contract_delete(ctx: Ctx, p: ContractDelete) -> Done:
    actor = _actor(ctx)
    contract = service.get_contract(ctx.session, ctx.workspace, p.id)
    label = _label(contract)
    service.remove(ctx.session, ctx.workspace, ctx.access, actor, p.id)
    return Done(
        title=f"удалён {label}",
        result={"id": str(p.id), "deleted": True},
        contracts=[str(p.id)],
        steps=[Step("contracts_delete", {"ids": [str(p.id)], "numbers": {str(p.id): contract.number or ""}})],
    )


# ── Условия с даты: статус, поправка месяца ──────────────────────────────────


class ContractStatusFrom(_Params):
    id: uuid.UUID
    #: Значение списка «Статус» - id или название (`describe` → `lists.status`).
    status: str
    #: С какого дня.
    since: date
    summary: str = Field(default="", max_length=500)


def _status_id(ctx: Ctx, raw: str) -> uuid.UUID:
    registry = service.Registry(ctx.session, ctx.workspace)
    try:
        wanted = uuid.UUID(raw)
    except ValueError:
        wanted = None
    for value in registry.values.values():
        if value.field_key != "status" or value.archived_at is not None:
            continue
        if value.id == wanted or value.value.strip().casefold() == raw.strip().casefold():
            return value.id
    raise CommandError(f"Статуса «{raw}» в реестре нет")


def _contract_status_from(ctx: Ctx, p: ContractStatusFrom) -> Done:
    from app.finance.core import changes

    actor = _actor(ctx)
    status_id = _status_id(ctx, p.status)
    contract = service.get_contract(ctx.session, ctx.workspace, p.id)
    before = str(contract.status_id) if contract.status_id else None
    amendment = changes.status_from(ctx.session, ctx.workspace, ctx.access, actor, p.id, status_id, p.since, summary=p.summary)
    applied = amendment.applied_at is not None
    return Done(
        title=f"{_label(contract)}: статус с {p.since:%d.%m.%Y}",
        result={
            "amendment_id": str(amendment.id),
            "applied": applied,
            **service.one(ctx.session, ctx.workspace, ctx.access, contract),
        },
        contracts=[str(contract.id)],
        steps=[Step("core_status", {
            "contract_id": str(contract.id), "amendment_id": str(amendment.id), "status_before": before, "label": _label(contract),
        })],
    )


class ContractAdjust(_Params):
    id: uuid.UUID
    #: Месяц начисления - `2026-07`.
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    #: Сумма вместо начисления; пусто вместе с `skip` - «не начислять».
    amount: str | None = Field(default=None, max_length=40)
    skip: bool = False
    #: Поправка - решение человека: причина обязательна.
    reason: str = Field(min_length=1, max_length=500)


def _contract_adjust(ctx: Ctx, p: ContractAdjust) -> Done:
    import sqlalchemy as sa

    from app.finance.core import changes
    from app.finance.core.accruals import month_start
    from app.finance.core.models import Document

    actor = _actor(ctx)
    contract = service.get_contract(ctx.session, ctx.workspace, p.id)
    period = month_start(p.month)
    was = ctx.session.scalar(
        sa.select(Document).where(
            Document.workspace_id == ctx.workspace.id, Document.contract_id == contract.id, Document.kind == "adjustment",
            Document.period == period, Document.deleted_at.is_(None),
        )
    )
    before = (
        {"amount": format(was.amount, "f") if was.amount is not None else None, "skip": was.skip, "reason": was.reason}
        if was is not None else {}
    )
    doc = changes.set_adjustment(
        ctx.session, ctx.workspace, ctx.access, actor, p.id, p.month, amount=p.amount, skip=p.skip, reason=p.reason,
    )
    return Done(
        title=f"{_label(contract)}: поправка {period:%m.%Y}",
        result={"document_id": str(doc.id), "month": p.month, "amount": format(doc.amount, "f") if doc.amount is not None else None, "skip": doc.skip},
        contracts=[str(contract.id)],
        steps=[Step("core_adjust", {
            "contract_id": str(contract.id), "document_id": str(doc.id), "month": p.month, "before": before, "label": _label(contract),
        })],
    )


# ── Клиенты между нашими юрлицами ────────────────────────────────────────────


class Move(_Params):
    contract_id: uuid.UUID
    #: Наше юрлицо, к которому переходит договор (`describe` → `entities`).
    target: uuid.UUID
    since: date


class ClientsTransfer(_Params):
    moves: list[Move] = Field(min_length=1, max_length=500)


def _clients_transfer(ctx: Ctx, p: ClientsTransfer) -> Done:
    from app.finance.core import changes

    actor = _actor(ctx)
    out = changes.transfer(ctx.session, ctx.workspace, ctx.access, actor, p.moves)
    # Точку `core_transfer` перевод ставит сам - «применить» вольёт её в пачку (`restore.COLLECT`).
    return Done(
        title=f"перевод клиентов: {out['done']} договор(ов)",
        result={"done": out["done"], "skipped": out["skipped"]},
        contracts=[str(move.contract_id) for move in p.moves],
    )


# ── Виды ─────────────────────────────────────────────────────────────────────


class ViewSave(_Params):
    template: str | None = None
    title: str | None = Field(default=None, max_length=120)
    source: str | None = None
    #: `company`, `me` или `{"department": id}`.
    owner: Any = None
    filter: dict[str, Any] | None = None
    measures: list[str] | None = None
    groups: list[str] | None = None
    forms: list[str] | None = None
    params: dict[str, Any] | None = None
    pin: str | None = None
    position: int | None = None


def _view_data(p: BaseModel, *, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    return {key: value for key, value in p.model_dump(exclude_unset=True).items() if key not in skip}


def _view_save(ctx: Ctx, p: ViewSave) -> Done:
    from app.finance.core import views as views_module

    saved = views_module.create(ctx.session, ctx.workspace, ctx.viewer, ctx.login, _view_data(p))
    return Done(
        title=f"сохранён вид «{saved['title']}»",
        result={"view": saved},
        steps=[Step("view_created", {"id": saved["id"]})],
    )


class ViewUpdate(ViewSave):
    id: uuid.UUID


def _view_update(ctx: Ctx, p: ViewUpdate) -> Done:
    from app.finance.core import views as views_module
    from app.finance.core.models import View

    view = ctx.session.get(View, p.id)
    if view is None or view.workspace_id != ctx.workspace.id:
        raise CommandError("Такого вида нет")
    before = views_module.state(view)
    saved = views_module.update(ctx.session, ctx.workspace, ctx.viewer, ctx.login, p.id, _view_data(p, skip=("id", "template")))
    return Done(
        title=f"вид «{saved['title']}» изменён",
        result={"view": saved},
        steps=[Step("view_state", {"id": str(p.id), "before": before})],
    )


class ViewRemove(_Params):
    id: uuid.UUID


def _view_remove(ctx: Ctx, p: ViewRemove) -> Done:
    from app.finance.core import views as views_module
    from app.finance.core.models import View

    view = ctx.session.get(View, p.id)
    if view is None or view.workspace_id != ctx.workspace.id:
        raise CommandError("Такого вида нет")
    sheets = views_module.sheets_of(ctx.session, ctx.workspace.id, view)
    views_module.remove(ctx.session, ctx.workspace, ctx.viewer, ctx.login, p.id)
    return Done(
        title=f"вид «{view.title}» убран",
        result={"id": str(p.id), "removed": True},
        steps=[Step("view_removed", {"id": str(p.id), "sheets": sheets})],
    )


CONTRACT_COMMANDS: tuple[Command, ...] = (
    Command("contract.update", "Правка полей договора", "contracts", ContractUpdate, _contract_update, lambda p: [str(p.id)]),
    Command("contract.create", "Новый договор", "contracts", ContractCreate, _contract_create, lambda p: [str(p.id)] if p.id else []),
    Command("contract.delete", "Удалить договор (в корзину)", "contracts", ContractDelete, _contract_delete, lambda p: [str(p.id)]),
    Command("contract.status_from", "Статус договора с даты", "contracts", ContractStatusFrom, _contract_status_from, lambda p: [str(p.id)]),
    Command("contract.adjust", "Поправка начисления месяца", "contracts", ContractAdjust, _contract_adjust, lambda p: [str(p.id)]),
    Command(
        "clients.transfer", "Перевод клиентов к другому нашему юрлицу с даты", "contracts", ClientsTransfer, _clients_transfer,
        lambda p: [str(move.contract_id) for move in p.moves],
    ),
    Command("view.save", "Сохранить вид", "contracts", ViewSave, _view_save),
    Command("view.update", "Изменить вид", "contracts", ViewUpdate, _view_update),
    Command("view.remove", "Убрать вид", "contracts", ViewRemove, _view_remove),
)


def _catalog() -> dict[str, Command]:
    from app.finance.model.entry import ENTRY_COMMANDS

    return {item.name: item for item in (*CONTRACT_COMMANDS, *ENTRY_COMMANDS)}


#: Все команды: договоры и виды - здесь, ручной ввод, деньги и файлы - `entry.py`.
COMMANDS: dict[str, Command] = _catalog()
#: Виды открыты тому, кому открыт их источник, - право проверяет `core/views.py`.
VIEW_COMMANDS = frozenset({"view.save", "view.update", "view.remove"})


def parse(raw: Any) -> tuple[Command, BaseModel]:
    """`{"command": "contract.update", "params": {…}}` → команда и проверенные параметры."""
    from pydantic import ValidationError

    if not isinstance(raw, dict):
        raise CommandError("Команда - объект {command, params}")
    name = str(raw.get("command") or "")
    command = COMMANDS.get(name)
    if command is None:
        raise CommandError(f"Команды «{name}» нет - список команд в GET /finance/model")
    try:
        params = command.params.model_validate(raw.get("params") or {})
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ()))
        raise CommandError(f"{command.title}: {where or 'параметры'} - {first.get('msg', 'записано неверно')}") from exc
    return command, params


def open_to(ctx: Ctx, command: Command) -> bool:
    """Открыта ли команда спрашивающему - то же правило, что `check_right`, без отказа."""
    try:
        check_right(ctx, command)
    except PermissionError:
        return False
    return True


def check_right(ctx: Ctx, command: Command) -> None:
    """Право команды: её раздел на правку; у договоров - права реестра; у некоторых - владелец."""
    if command.name in VIEW_COMMANDS:
        return
    if command.resource == "contracts":
        if not ctx.access.edit:
            raise PermissionError("Правка реестра вам не открыта - только просмотр")
    elif not ctx.can(command.resource, "edit"):
        from app.finance.access import RESOURCE_BY_KEY

        title = RESOURCE_BY_KEY[command.resource].title if command.resource in RESOURCE_BY_KEY else command.resource
        raise PermissionError(f"«{command.title}» - нужен раздел «{title}» на правку")
    if command.setup and not ctx.access.setup:
        raise PermissionError(f"«{command.title}» делает владелец или администратор")


__all__ = ["COMMANDS", "Command", "CommandError", "Done", "Step", "check_right", "open_to", "parse"]
