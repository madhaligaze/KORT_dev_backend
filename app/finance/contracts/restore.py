"""Изменения листа, которые лист называет ломающими, и возврат к «как было».

Зачем
─────
До 29.09.2026 лист реестра был защищён механизмом Univer: вставка и удаление
строк и колонок закрыты, колонки «только чтение» - защищённым диапазоном. Цена
- замки на вкладках и отказ «Диапазон защищён, у вас нет разрешения на
установку стилей» на заливке строки, выделенной по «№» (колонка «№»
защищена, выделение строки её захватывает). Пользователь: «убери блокировку, а
если кто-то попробует сломать логику таблицы - предупреди окном, что именно
изменится, и дай вернуть».

Теперь лист ничего не запрещает сам: действие, которое меняет таблицу для
всех (удалить договоры, добавить или убрать колонку, переименовать шапку или
лист, передвинуть строки, вставить значения в много договоров), лист
перехватывает, объясняет последствия и, если человек согласен, делает здесь -
одной транзакцией вместе с точкой восстановления и событием журнала.

Точка восстановления
────────────────────
Хранит только задетое изменением: листы - целиком (их немного), договоры -
идентификаторами, значения - полями «как было». Не снимок таблицы: вернуть
свою вставку не должно значить стереть правки коллег, сделанные после неё.
Поэтому возврат значений пропускает поле, которое после точки поменял кто-то
другой, и говорит об этом словами.

Вернуть точку может её автор и администратор - Ctrl+Z в листе или
«Восстановление» в личном кабинете.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.accounts_model import FinanceUser
from app.finance.contracts import setup
from app.finance.contracts.fields import MODE_FIELDS, bump
from app.finance.contracts.models import (
    RESTORE_KINDS,
    Contract,
    Employee,
    EntityField,
    EntityView,
    RestorePoint,
)
from app.finance.contracts.service import (
    Access,
    Actor,
    Mode,
    NotFound,
    Registry,
    _check_write,
    _finish,
    get_contract,
    people_of,
    remove,
    value_of,
    visible_to,
)
from app.finance.models import POSITION_STEP, ActionLog, Workspace
from app.finance.service import FinanceError

#: Сколько точек держать на компанию. Дальше - старые уходят: вернуть
#: изменение месячной давности значило бы откатить месяц чужой работы.
KEEP = 300
#: Поля, которых нет в листе без явного списка колонок (`sheet-adapter.ts`,
#: `EXPLICIT_ONLY`): лист «Все договоры» новой компании не показывает сводку.
EXPLICIT_ONLY = frozenset({"summary_paid", "summary_remaining", "age_months"})
ORDINAL = "row_number"


class RestoreError(FinanceError):
    """Вернуть или изменить нельзя - с причиной для человека."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plural(count: int, one: str, few: str, many: str) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def _contracts_word(count: int) -> str:
    return f"{count} {_plural(count, 'договор', 'договора', 'договоров')}"


def _contracts_of(count: int) -> str:
    """«у 1 договора», «у 3 договоров» - родительный падеж."""
    return f"{count} {_plural(count, 'договора', 'договоров', 'договоров')}"


def _require_setup(access: Access) -> None:
    if not access.setup:
        raise PermissionError("Колонки и листы реестра меняет владелец или администратор")


def _require_edit(access: Access) -> None:
    if not access.edit:
        raise PermissionError("Правка реестра вам не открыта - только просмотр")


# ── Точки ────────────────────────────────────────────────────────────────────


def _point(
    session: Session,
    workspace: Workspace,
    actor: Actor,
    kind: str,
    title: str,
    payload: dict[str, Any],
    *,
    book: str = "",
    view_key: str = "",
) -> RestorePoint:
    if kind not in RESTORE_KINDS:
        raise RestoreError("Такой точки восстановления не бывает")
    point = RestorePoint(
        workspace_id=workspace.id,
        user_id=actor.user_id,
        kind=kind,
        title=title[:300],
        book=book or "",
        view_key=view_key or "",
        payload=payload,
        created_at=_now(),
    )
    session.add(point)
    session.flush()
    _trim(session, workspace.id)
    return point


def _trim(session: Session, workspace_id: uuid.UUID) -> None:
    old = session.scalars(
        sa.select(RestorePoint.id)
        .where(RestorePoint.workspace_id == workspace_id)
        .order_by(RestorePoint.created_at.desc())
        .offset(KEEP)
    ).all()
    if old:
        session.execute(sa.delete(RestorePoint).where(RestorePoint.id.in_(old)))


def _view(session: Session, workspace: Workspace, key: str) -> EntityView:
    view = session.scalar(
        sa.select(EntityView).where(
            EntityView.workspace_id == workspace.id, EntityView.key == key, EntityView.archived_at.is_(None)
        )
    )
    if view is None:
        raise RestoreError("Такого листа нет - его успели убрать")
    return view


def _view_state(view: EntityView) -> dict[str, Any]:
    return {
        "key": view.key,
        "title": view.title,
        "blocks": view.blocks or [],
        "style": view.style or {},
        "position": view.position,
        "archived": view.archived_at is not None,
    }


# ── Колонки листа ────────────────────────────────────────────────────────────


def _columns(block: dict[str, Any], registry: Registry) -> list[dict[str, Any]]:
    """Колонки блока списком: явные - как есть, «все поля» - развёрнутые.

    Блок без своего списка колонок показывает все поля реестра по порядку (так
    же разворачивает его лист). Правка одной колонки такого блока делает его
    список явным - иначе вставлять и убирать было бы некуда.
    """
    columns = [dict(column) for column in (block.get("columns") or []) if isinstance(column, dict)]
    if columns:
        return columns
    return [{"key": ORDINAL, "label": "№", "width": None}] + [
        {"key": item.key, "label": item.title, "width": None}
        for item in registry.fields
        if not item.hidden and item.key not in EXPLICIT_ONLY
    ]


def _label_of(column: dict[str, Any], registry: Registry) -> str:
    if column.get("label"):
        return str(column["label"])
    field = registry.field_by_key.get(str(column.get("key")))
    return field.title if field is not None else str(column.get("key") or "")


def _insert_after(columns: list[dict[str, Any]], item: dict[str, Any], after: str | None) -> list[dict[str, Any]]:
    out = [column for column in columns if column.get("key") != item["key"]]
    if after:
        for index, column in enumerate(out):
            if column.get("key") == after:
                return [*out[: index + 1], item, *out[index + 1 :]]
    # Без соседа слева - сразу за «№»: адрес строки всегда первый.
    at = 1 if out and out[0].get("key") == ORDINAL else 0
    if after is None:
        return [*out[:at], item, *out[at:]]
    return [*out, item]


def _put_blocks(session: Session, workspace: Workspace, view: EntityView, blocks: list[dict[str, Any]]) -> None:
    setup.upsert_view(session, workspace, {"blocks": blocks}, view.id)


def _log_view(session: Session, workspace: Workspace, before: dict[str, Any], view: EntityView, actor: Actor) -> None:
    change = setup.view_change(
        {**before, "id": str(view.id)}, {**_view_state(view), "id": str(view.id)}
    )
    if len(str(change)) > 12000:
        change = {"title": change["title"], "before": {"id": str(view.id)}, "after": {"id": str(view.id)}}
    history.write(
        session, workspace, kind="contracts.setup.view_update", entity="contract_setup",
        title=f"настройка реестра: {change['title']} (из листа)", before=change["before"], after=change["after"],
        actor=actor.email,
    )


# ── Действия ─────────────────────────────────────────────────────────────────


def delete_contracts(
    session: Session, workspace: Workspace, access: Access, actor: Actor, ids: Sequence[str], *, view: str = "", book: str = ""
) -> dict[str, Any]:
    """Удалить договоры строк листа в корзину - одной точкой восстановления."""
    _require_edit(access)
    done: list[str] = []
    numbers: dict[str, str] = {}
    failed: list[dict[str, str]] = []
    for raw in ids:
        try:
            contract_id = uuid.UUID(str(raw))
        except ValueError:
            continue
        try:
            with session.begin_nested():
                contract = get_contract(session, workspace, contract_id)
                numbers[str(contract_id)] = contract.number or ""
                remove(session, workspace, access, actor, contract_id)
            done.append(str(contract_id))
        except (FinanceError, PermissionError, NotFound) as exc:
            failed.append({"id": str(contract_id), "error": str(exc)})
    point = None
    if done:
        where = _view_title(session, workspace, view)
        title = f"удалено {_contracts_word(len(done))}" + (f" из листа «{where}»" if where else "")
        point = _point(
            session, workspace, actor, "contracts_delete", title,
            {"ids": done, "numbers": {key: numbers.get(key, "") for key in done}}, book=book, view_key=view,
        )
    return {"done": done, "failed": failed, "point": _point_ref(point)}


def _view_title(session: Session, workspace: Workspace, key: str) -> str:
    if not key:
        return ""
    return session.scalar(
        sa.select(EntityView.title).where(EntityView.workspace_id == workspace.id, EntityView.key == key)
    ) or ""


def _point_ref(point: RestorePoint | None) -> dict[str, Any] | None:
    if point is None:
        return None
    return {"id": str(point.id), "title": point.title, "kind": point.kind}


def add_column(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    *,
    view: str,
    title: str,
    type: str = "text",
    after: Sequence[str | None] | None = None,
) -> dict[str, Any]:
    """Новая колонка = новое поле реестра, вставленное в лист там, где её вставили."""
    _require_setup(access)
    target = _view(session, workspace, view)
    before = _view_state(target)
    field = setup.add_field(session, workspace, title=title, type=type)
    history.write(
        session, workspace, kind="contracts.setup.field_add", entity="contract_setup",
        title=f"настройка реестра: поле «{field.title}» добавлено (из листа «{target.title}»)",
        after={"key": field.key, "title": field.title, "type": field.type}, actor=actor.email,
    )
    registry = Registry(session, workspace)
    lefts = list(after or [])
    blocks = []
    for index, block in enumerate(target.blocks or []):
        # Сосед слева - у каждой части листа свой: колонка F в двух частях -
        # разные поля. Нет соседа - сразу за «№».
        left = lefts[index] if index < len(lefts) else None
        columns = _insert_after(_columns(block, registry), {"key": field.key, "label": field.title, "width": None}, left)
        blocks.append({**block, "columns": columns})
    _put_blocks(session, workspace, target, blocks)
    _log_view(session, workspace, before, target, actor)
    point = _point(
        session, workspace, actor, "field_add",
        f"добавлена колонка «{field.title}» в лист «{target.title}»",
        {"field": field.key, "views": [before]}, book=target.book or "", view_key=target.key,
    )
    return {"key": field.key, "point": _point_ref(point)}


def _change_view(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    view: str,
    change: Any,
    title: Any,
) -> dict[str, Any]:
    """Правка листа из самого листа: точка «как было» - лист целиком, до правки.

    `title(старое название листа)` - что сделано словами, для точки.
    """
    _require_setup(access)
    target = _view(session, workspace, view)
    before = _view_state(target)
    registry = Registry(session, workspace)
    data = change(target, registry)
    setup.upsert_view(session, workspace, data, target.id)
    _log_view(session, workspace, before, target, actor)
    point = _point(
        session, workspace, actor, "view", title(before["title"]),
        {"views": [before]}, book=target.book or "", view_key=target.key,
    )
    return {"point": _point_ref(point)}


def remove_column(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *, view: str, keys: Sequence[str | None]
) -> dict[str, Any]:
    """Убрать колонку из листа у всех. Значения поля в договорах остаются."""
    if any(key == ORDINAL for key in keys):
        raise RestoreError("«№» - адрес строки листа: без неё строки не отличить друг от друга")
    names: list[str] = []

    def change(target: EntityView, registry: Registry) -> dict[str, Any]:
        blocks = []
        for index, block in enumerate(target.blocks or []):
            key = keys[index] if index < len(keys) else (keys[0] if keys else None)
            columns = _columns(block, registry)
            kept = [column for column in columns if column.get("key") != key]
            if len(kept) != len(columns):
                names.extend(_label_of(column, registry) for column in columns if column.get("key") == key)
            blocks.append({**block, "columns": kept})
        if not names:
            raise RestoreError("Такой колонки в листе уже нет")
        return {"blocks": blocks}

    out = _change_view(
        session, workspace, access, actor, view, change,
        lambda old: f"убрана колонка «{', '.join(names)}» из листа «{old}»",
    )
    return {**out, "removed": names}


def rename_column(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *, view: str, block: int, key: str, label: str
) -> dict[str, Any]:
    clean = (label or "").strip()
    if not clean:
        raise RestoreError("У колонки должно быть название")
    if key == ORDINAL:
        raise RestoreError("«№» - адрес строки листа, его название не меняется")

    def change(target: EntityView, registry: Registry) -> dict[str, Any]:
        blocks = list(target.blocks or [])
        if not (0 <= block < len(blocks)):
            raise RestoreError("Такой части листа нет")
        columns = _columns(blocks[block], registry)
        hit = False
        for column in columns:
            if column.get("key") == key:
                column["label"] = clean
                hit = True
        if not hit:
            raise RestoreError("Такой колонки в листе уже нет")
        blocks[block] = {**blocks[block], "columns": columns}
        return {"blocks": blocks}

    return _change_view(
        session, workspace, access, actor, view, change,
        lambda old: f"колонка переименована в «{clean}» в листе «{old}»",
    )


def rename_block(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *, view: str, block: int, title: str
) -> dict[str, Any]:
    def change(target: EntityView, registry: Registry) -> dict[str, Any]:
        blocks = list(target.blocks or [])
        if not (0 <= block < len(blocks)):
            raise RestoreError("Такой части листа нет")
        blocks[block] = {**blocks[block], "title": (title or "").strip()}
        return {"blocks": blocks}

    return _change_view(
        session, workspace, access, actor, view, change, lambda old: f"переименована часть листа «{old}»"
    )


def move_column(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    *,
    view: str,
    keys: Sequence[str | None],
    after: Sequence[str | None],
) -> dict[str, Any]:
    if any(key == ORDINAL for key in keys):
        raise RestoreError("«№» стоит первой всегда - это адрес строки листа")

    def change(target: EntityView, registry: Registry) -> dict[str, Any]:
        blocks = []
        for index, block in enumerate(target.blocks or []):
            key = keys[index] if index < len(keys) else None
            columns = _columns(block, registry)
            item = next((column for column in columns if column.get("key") == key), None)
            if item is None:
                blocks.append({**block, "columns": columns} if block.get("columns") else block)
                continue
            left = after[index] if index < len(after) else None
            blocks.append({**block, "columns": _insert_after(columns, item, left or ORDINAL)})
        return {"blocks": blocks}

    return _change_view(
        session, workspace, access, actor, view, change, lambda old: f"передвинута колонка в листе «{old}»"
    )


def rename_view(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *, view: str, title: str
) -> dict[str, Any]:
    clean = (title or "").strip()
    if not clean:
        raise RestoreError("У листа должно быть название")
    return _change_view(
        session, workspace, access, actor, view, lambda target, registry: {"title": clean},
        lambda old: f"лист «{old}» переименован в «{clean}»",
    )


def archive_view(session: Session, workspace: Workspace, access: Access, actor: Actor, *, view: str) -> dict[str, Any]:
    target = _view(session, workspace, view)
    if target.main:
        raise RestoreError("Главный лист не удаляется: на нём стоят все договоры")
    return _change_view(
        session, workspace, access, actor, view, lambda target, registry: {"archived": True},
        lambda old: f"убран лист «{old}»",
    )


def order_views(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *, book: str, keys: Sequence[str]
) -> dict[str, Any]:
    _require_setup(access)
    views = list(
        session.scalars(
            sa.select(EntityView)
            .where(
                EntityView.workspace_id == workspace.id,
                EntityView.book == (book or ""),
                EntityView.archived_at.is_(None),
            )
            .order_by(EntityView.position, EntityView.created_at)
        )
    )
    by_key = {view.key: view for view in views}
    ordered = [by_key[key] for key in keys if key in by_key]
    ordered += [view for view in views if view not in ordered]
    before = [_view_state(view) for view in views]
    for index, view in enumerate(ordered):
        view.position = (index + 1) * POSITION_STEP
    session.flush()
    setup._schema_changed(session, workspace)
    history.write(
        session, workspace, kind="contracts.setup.view_update", entity="contract_setup",
        title="настройка реестра: порядок листов изменён (из листа)",
        before={"order": [view.key for view in views]}, after={"order": [view.key for view in ordered]},
        actor=actor.email,
    )
    point = _point(session, workspace, actor, "view", "изменён порядок листов", {"views": before}, book=book or "")
    return {"point": _point_ref(point)}


def move_rows(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    *,
    ids: Sequence[str],
    before: str | None,
    view: str = "",
    book: str = "",
) -> dict[str, Any]:
    """Передвинуть строки договоров: порядок реестра - общий у всех листов и людей."""
    _require_edit(access)
    registry = Registry(session, workspace)
    wanted: list[Contract] = []
    for raw in ids:
        try:
            contract = get_contract(session, workspace, uuid.UUID(str(raw)), for_update=True)
        except (ValueError, NotFound):
            continue
        people = people_of(session, [contract.id]).get(contract.id, [])
        if not visible_to(contract, registry, access, people):
            continue
        _check_write(contract, registry, access, people)
        wanted.append(contract)
    if not wanted:
        raise RestoreError("Передвигать нечего: таких договоров в листе нет")
    moving = {contract.id for contract in wanted}
    anchor = None
    if before:
        try:
            anchor = get_contract(session, workspace, uuid.UUID(str(before)))
        except (ValueError, NotFound):
            anchor = None
    others = [
        (position, contract_id)
        for contract_id, position in session.execute(
            sa.select(Contract.id, Contract.position)
            .where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
            .order_by(Contract.position, Contract.created_at)
        )
        if contract_id not in moving
    ]
    if anchor is not None and anchor.id not in moving:
        upper = anchor.position
        lower = max((position for position, _cid in others if position < upper), default=upper - POSITION_STEP * (len(wanted) + 1))
    else:
        lower = max((position for position, _cid in others), default=0)
        upper = lower + POSITION_STEP * (len(wanted) + 1)
    old = {str(contract.id): contract.position for contract in wanted}
    step = (upper - lower) // (len(wanted) + 1)
    if step < 1:
        # Места между соседями нет - порядок реестра раскладывается заново
        # гнёздами по 1024, как у новых договоров, и только потом вставка.
        _respace(session, workspace, [cid for _p, cid in others])
        return move_rows(session, workspace, access, actor, ids=ids, before=before, view=view, book=book)
    ordered = sorted(wanted, key=lambda contract: [str(i) for i in ids].index(str(contract.id)))
    for index, contract in enumerate(ordered):
        contract.position = lower + step * (index + 1)
        _finish(session, registry, contract, actor, [])
    numbers = ", ".join(f"№ {contract.number}" if contract.number else "без номера" for contract in ordered[:3])
    more = f" и ещё {len(ordered) - 3}" if len(ordered) > 3 else ""
    title = f"передвинуты строки: {numbers}{more}"
    history.write(
        session, workspace, kind="contract.reorder", entity="contract", entity_id=ordered[0].id,
        title=title, before={"positions": old},
        after={"positions": {str(contract.id): contract.position for contract in ordered}}, actor=actor.email,
    )
    point = _point(session, workspace, actor, "order", title, {"positions": old}, book=book, view_key=view)
    return {"point": _point_ref(point)}


def _respace(session: Session, workspace: Workspace, ordered_ids: Iterable[uuid.UUID]) -> None:
    for index, contract_id in enumerate(ordered_ids):
        session.execute(
            sa.update(Contract)
            .where(Contract.id == contract_id)
            .values(position=(index + 1) * POSITION_STEP, seq=bump(session, workspace.id, "contracts"))
        )
    session.flush()
    session.expire_all()


def values_point(
    session: Session,
    workspace: Workspace,
    access: Access,
    actor: Actor,
    *,
    items: Sequence[dict[str, Any]],
    title: str = "",
    view: str = "",
    book: str = "",
) -> dict[str, Any]:
    """Точка перед массовой правкой: значения полей «как было» - снимает сервер, не лист."""
    _require_edit(access)
    registry = Registry(session, workspace)
    stored: list[dict[str, Any]] = []
    fields: set[str] = set()
    for item in items or []:
        try:
            contract = get_contract(session, workspace, uuid.UUID(str(item.get("id"))))
        except (ValueError, NotFound):
            continue
        people = people_of(session, [contract.id]).get(contract.id, [])
        if not visible_to(contract, registry, access, people):
            continue
        for key in item.get("keys") or []:
            key = str(key)
            if key not in registry.field_by_key or key in access.hidden:
                continue
            stored.append({"id": str(contract.id), "key": key, "before": value_of(contract, key, people)})
            fields.add(registry.field_by_key[key].title.lower())
    if not stored:
        return {"point": None}
    count = len({entry["id"] for entry in stored})
    text = title or f"правка {_contracts_of(count)}: {', '.join(sorted(fields))}"
    point = _point(session, workspace, actor, "values", text, {"items": stored}, book=book, view_key=view)
    return {"point": _point_ref(point)}


def created_point(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *, ids: Sequence[str], view: str = "", book: str = ""
) -> dict[str, Any]:
    """Точка после заведения многих договоров вставкой: вернуть - значит убрать их в корзину."""
    _require_edit(access)
    done = []
    for raw in ids:
        try:
            contract = get_contract(session, workspace, uuid.UUID(str(raw)))
        except (ValueError, NotFound):
            continue
        done.append(str(contract.id))
    if not done:
        return {"point": None}
    where = _view_title(session, workspace, view)
    title = f"заведено вставкой {_contracts_word(len(done))}" + (f" в листе «{where}»" if where else "")
    point = _point(session, workspace, actor, "contracts_create", title, {"ids": done}, book=book, view_key=view)
    return {"point": _point_ref(point)}


# ── Возврат ──────────────────────────────────────────────────────────────────


def _changed_by_others(
    session: Session, workspace: Workspace, point: RestorePoint, contract_id: uuid.UUID, key: str
) -> bool:
    """Поменял ли поле после точки кто-то другой (журнал правок договора)."""
    rows = session.execute(
        sa.select(ActionLog.user_id, ActionLog.after, ActionLog.kind).where(
            ActionLog.workspace_id == workspace.id,
            ActionLog.entity_id == contract_id,
            ActionLog.at > point.created_at,
            ActionLog.kind.in_(("contract.update", "contract.amendment", "contract.amendment.apply")),
        )
    ).all()
    for user_id, after, _kind in rows:
        if key in (after or {}) and user_id != point.user_id:
            return True
    return False


def restore_point(
    session: Session, workspace: Workspace, access: Access, actor: Actor, point_id: uuid.UUID
) -> dict[str, Any]:
    point = session.get(RestorePoint, point_id)
    if point is None or point.workspace_id != workspace.id:
        raise RestoreError("Такой точки восстановления нет - старые уходят сами")
    if not access.admin and point.user_id != actor.user_id:
        raise PermissionError("Вернуть чужое изменение может администратор")
    if point.restored_at is not None:
        raise RestoreError("Это изменение уже возвращено")
    payload = point.payload or {}
    done: list[str] = []
    skipped: list[str] = []
    if point.kind == "contracts_delete":
        _restore_deleted(session, workspace, access, actor, payload, done, skipped)
    elif point.kind == "contracts_create":
        _remove_created(session, workspace, access, actor, payload, done, skipped)
    elif point.kind == "values":
        _restore_values(session, workspace, access, actor, point, payload, done, skipped)
    elif point.kind in ("view", "field_add"):
        _require_setup(access)
        _restore_views(session, workspace, actor, payload, done, skipped)
        if point.kind == "field_add":
            _drop_field(session, workspace, actor, payload, done, skipped)
    elif point.kind == "order":
        _restore_order(session, workspace, access, actor, payload, done, skipped)
    point.restored_at = _now()
    point.restored_by = actor.user_id
    session.flush()
    history.write(
        session, workspace, kind="contracts.restore", entity="restore_point", entity_id=point.id,
        title=f"возвращено как было: {point.title}", before={"point": str(point.id)},
        after={"done": done[:50], "skipped": skipped[:50]}, actor=actor.email,
    )
    return {"title": point.title, "done": done, "skipped": skipped, "kind": point.kind}


def _restore_deleted(session, workspace, access, actor, payload, done, skipped) -> None:
    _require_edit(access)
    registry = Registry(session, workspace)
    numbers = payload.get("numbers") or {}
    for raw in payload.get("ids") or []:
        contract = session.get(Contract, uuid.UUID(raw))
        label = f"№ {numbers.get(raw)}" if numbers.get(raw) else "договор без номера"
        if contract is None or contract.workspace_id != workspace.id:
            skipped.append(f"{label} удалён насовсем - вернуть нечего")
            continue
        if contract.deleted_at is None:
            skipped.append(f"{label} уже в реестре")
            continue
        people = people_of(session, [contract.id]).get(contract.id, [])
        if not visible_to(contract, registry, access, people, write=True):
            skipped.append(f"{label} вам не открыт на правку")
            continue
        contract.deleted_at = None
        _finish(session, registry, contract, actor, [])
        history.write(
            session, workspace, kind="contract.restore", entity="contract", entity_id=contract.id,
            title=f"договор возвращён из корзины {contract.number or ''}".strip(),
            before={"deleted_at": "…"}, after={"deleted_at": None}, actor=actor.email,
        )
        done.append(label)


def _remove_created(session, workspace, access, actor, payload, done, skipped) -> None:
    for raw in payload.get("ids") or []:
        try:
            with session.begin_nested():
                contract = get_contract(session, workspace, uuid.UUID(raw))
                label = f"№ {contract.number}" if contract.number else "договор без номера"
                remove(session, workspace, access, actor, contract.id)
            done.append(label)
        except NotFound:
            skipped.append("договор уже удалён")
        except (FinanceError, PermissionError) as exc:
            skipped.append(str(exc))


def _restore_values(session, workspace, access, actor, point, payload, done, skipped) -> None:
    from app.finance.contracts import service

    _require_edit(access)
    by_contract: dict[str, dict[str, Any]] = {}
    for item in payload.get("items") or []:
        by_contract.setdefault(item["id"], {})[item["key"]] = item.get("before")
    registry = Registry(session, workspace)
    for raw, values in by_contract.items():
        contract_id = uuid.UUID(raw)
        try:
            contract = get_contract(session, workspace, contract_id)
        except NotFound:
            skipped.append("договор удалён - его значения не возвращались")
            continue
        label = f"№ {contract.number}" if contract.number else "договор без номера"
        people = people_of(session, [contract.id]).get(contract.id, [])
        wanted: dict[str, Any] = {}
        for key, before in values.items():
            if _changed_by_others(session, workspace, point, contract_id, key):
                title = registry.field_by_key[key].title if key in registry.field_by_key else key
                skipped.append(f"{label}: «{title}» после этого поменяли - не тронуто")
                continue
            if value_of(contract, key, people) != before:
                wanted[key] = before
        if not wanted:
            continue
        try:
            with session.begin_nested():
                mode = Mode(kind="fix") if set(wanted) & set(MODE_FIELDS) else None
                service.patch(session, workspace, access, actor, contract_id, wanted, known_seq=None, mode=mode)
            done.append(label)
        except (FinanceError, PermissionError) as exc:
            skipped.append(f"{label}: {exc}")


def _restore_views(session, workspace, actor, payload, done, skipped) -> None:
    for state in payload.get("views") or []:
        view = session.scalar(
            sa.select(EntityView).where(EntityView.workspace_id == workspace.id, EntityView.key == state.get("key"))
        )
        if view is None:
            skipped.append(f"лист «{state.get('title')}» удалён насовсем")
            continue
        before = _view_state(view)
        try:
            with session.begin_nested():
                setup.upsert_view(
                    session, workspace,
                    {
                        "title": state.get("title") or view.title,
                        "blocks": state.get("blocks") or view.blocks,
                        "style": state.get("style") or {},
                        "position": state.get("position", view.position),
                        "archived": bool(state.get("archived")),
                    },
                    view.id,
                )
        except FinanceError as exc:
            skipped.append(f"лист «{state.get('title')}»: {exc}")
            continue
        if before != _view_state(view):
            _log_view(session, workspace, before, view, actor)
            done.append(f"лист «{view.title}»")


def _drop_field(session, workspace, actor, payload, done, skipped) -> None:
    key = payload.get("field")
    field = session.scalar(
        sa.select(EntityField).where(EntityField.workspace_id == workspace.id, EntityField.key == key)
    ) if key else None
    if field is None or field.archived_at is not None:
        return
    used = session.scalar(
        sa.select(sa.func.count())
        .select_from(Contract)
        .where(
            Contract.workspace_id == workspace.id,
            Contract.deleted_at.is_(None),
            sa.cast(Contract.attrs, sa.Text).contains(f'"{key}"'),
        )
    )
    if used:
        # Колонку успели заполнить - поле уходит из листа, но не из договоров.
        skipped.append(f"поле «{field.title}» уже заполнено у {_contracts_of(int(used))} - осталось в реестре, из листа убрано")
        return
    setup.update_field(session, workspace, field.key, {"archived": True})
    history.write(
        session, workspace, kind="contracts.setup.field_update", entity="contract_setup",
        title=f"настройка реестра: поле «{field.title}» - удалено в корзину (возврат листа)",
        before={"key": field.key, "archived": False}, after={"key": field.key, "archived": True}, actor=actor.email,
    )
    done.append(f"поле «{field.title}» - в корзину")


def _restore_order(session, workspace, access, actor, payload, done, skipped) -> None:
    _require_edit(access)
    registry = Registry(session, workspace)
    for raw, position in (payload.get("positions") or {}).items():
        contract = session.get(Contract, uuid.UUID(raw))
        if contract is None or contract.deleted_at is not None:
            continue
        contract.position = int(position)
        _finish(session, registry, contract, actor, [])
        done.append(f"№ {contract.number}" if contract.number else "договор без номера")


# ── Список ───────────────────────────────────────────────────────────────────


_KIND_WORDS = {
    "contracts_delete": "договоры вернутся из корзины",
    "contracts_create": "заведённые договоры уйдут в корзину",
    "values": "значения полей станут прежними",
    "view": "лист станет прежним",
    "field_add": "колонка уйдёт, поле - в корзину",
    "order": "строки встанут на прежние места",
}


def listing(session: Session, workspace: Workspace, access: Access, actor: Actor, *, limit: int = 60) -> dict[str, Any]:
    query = sa.select(RestorePoint).where(RestorePoint.workspace_id == workspace.id)
    if not access.admin:
        query = query.where(RestorePoint.user_id == actor.user_id)
    points = list(session.scalars(query.order_by(RestorePoint.created_at.desc()).limit(min(max(limit, 1), 200))))
    users = {point.user_id for point in points if point.user_id} | {point.restored_by for point in points if point.restored_by}
    names: dict[uuid.UUID, str] = {}
    if users:
        for user_id, name in session.execute(
            sa.select(Employee.user_id, Employee.full_name).where(
                Employee.workspace_id == workspace.id, Employee.user_id.in_(users)
            )
        ):
            if name:
                names[user_id] = name
        for user in session.scalars(sa.select(FinanceUser).where(FinanceUser.id.in_(users))):
            names.setdefault(user.id, user.full_name or user.email or user.phone or "")
    views = {
        key: title
        for key, title in session.execute(
            sa.select(EntityView.key, EntityView.title).where(EntityView.workspace_id == workspace.id)
        )
    }
    return {
        "items": [
            {
                "id": str(point.id),
                "kind": point.kind,
                "title": point.title,
                "returns": _KIND_WORDS.get(point.kind, ""),
                "book": point.book,
                "view": views.get(point.view_key, "") if point.view_key else "",
                "created_at": point.created_at.isoformat() if point.created_at else None,
                "by": names.get(point.user_id, "") if point.user_id else "",
                "mine": point.user_id == actor.user_id,
                "restored_at": point.restored_at.isoformat() if point.restored_at else None,
                "restored_by": names.get(point.restored_by, "") if point.restored_by else "",
                "can_restore": point.restored_at is None and (access.admin or point.user_id == actor.user_id),
            }
            for point in points
        ]
    }


__all__ = [
    "KEEP",
    "RestoreError",
    "add_column",
    "archive_view",
    "created_point",
    "delete_contracts",
    "listing",
    "move_column",
    "move_rows",
    "order_views",
    "remove_column",
    "rename_block",
    "rename_column",
    "rename_view",
    "restore_point",
    "values_point",
]
