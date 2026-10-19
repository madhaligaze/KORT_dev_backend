"""Корзина «Финансов»: всё удалённое - одним списком, восстановить или удалить насовсем.

Зачем
─────
До 27.09.2026 удаление в разделе называлось по-разному и жило по-разному:
операция «убиралась», договор «убирался», юрлицо и значение списка уходили «в
архив», сотрудник - «в архив» с доступом. Где этот архив - не было видно
нигде: «кнопка „Архив“ есть, а где архив - фиг пойми». Юрлицо с договором не
удалялось вовсе (ошибочно заведённое «ИП WE make» так и висело в «Наших
юрлицах»).

Теперь везде «Удалить», а удалённое лежит здесь. Само удаление осталось
мягким, как и было (`deleted_at` / `archived_at`): на удалённое ссылаются
операции, договоры, журнал действий, и молча оборвать эти ссылки нельзя.

«Удалить насовсем»
──────────────────
Стирает запись из базы. Своё содержимое уходит вместе с ней (у договора -
ответственные и соглашения, у операции - разбивки, у своего поля - значения в
договорах и колонки листов). А справочник, на который ещё ссылается живая
запись (статус у договоров, счёт у операций, отдел у сотрудников), насовсем
не удаляется: база либо отказала бы, либо молча стёрла бы значение у сотен
договоров. Отказ - словами: «используется: договоров 12».
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.finance.accounts_model import FinanceUser
from app.finance.contracts.fields import bump
from app.finance.contracts.models import (
    Contract,
    ContractDepartment,
    ContractPerson,
    Department,
    Employee,
    EntityField,
    EntityView,
    GroupEntity,
    ListValue,
)
from app.finance.models import (
    ActionLog,
    Account,
    Category,
    Counterparty,
    Operation,
    Project,
    Tag,
    Workspace,
)
from app.finance.service import FinanceError

#: Сколько записей одного вида показывать. Корзину не листают тысячами.
LIMIT = 300


class TrashError(FinanceError):
    """Восстановить или удалить насовсем нельзя - с причиной для человека."""


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".00", "")
    except (TypeError, ValueError):
        return str(value or "")


def _day(value: Any) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def _aware(value: datetime) -> datetime:
    """SQLite отдаёт время без пояса, Postgres - с поясом: сравниваем в UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _count(session: Session, query: Any) -> int:
    return int(session.scalar(query) or 0)


# ── Виды удалённого ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Kind:
    key: str
    #: Как называется вид в корзине: «Договор», «Операция»…
    title: str
    #: Где это жило - чтобы найти восстановленное: «Реестр», «Журнал»…
    where: str
    model: Any
    #: Колонка удаления: `deleted_at` или `archived_at`.
    column: str
    label: Callable[[Session, Any], str]
    #: Почему нельзя удалить насовсем; пусто - можно.
    blocked: Callable[[Session, Any], str] = lambda session, item: ""
    #: Что убрать вместе с записью (своё содержимое, не чужие ссылки).
    before_purge: Callable[[Session, Any], None] = lambda session, item: None
    #: Лишнее условие выборки (системные поля в корзину не попадают).
    where_clause: Callable[[], Any] | None = None
    #: Меняет ли вид схему реестра: лист, карточка и списки должны перечитать её.
    schema: bool = False


def _contract_label(session: Session, item: Contract) -> str:
    party_id = item.customer_id or item.executor_id
    party = session.get(Counterparty, party_id) if party_id else None
    return " · ".join(part for part in (item.number or "без номера", party.name if party else "") if part)


def _operation_label(session: Session, item: Operation) -> str:
    word = {"income": "поступление", "expense": "списание", "transfer": "перевод"}.get(item.kind, item.kind)
    parts = [_day(item.paid_at), f"{word} {_money(item.amount)}", (item.comment or "")[:60]]
    return " · ".join(part for part in parts if part)


def _entity_label(session: Session, item: GroupEntity) -> str:
    party = session.get(Counterparty, item.counterparty_id)
    return (party.name if party else "") or item.full_name or item.code or "юрлицо"


def _value_label(session: Session, item: ListValue) -> str:
    field = session.scalar(
        sa.select(EntityField.title).where(EntityField.workspace_id == item.workspace_id, EntityField.key == item.field_key)
    )
    return f"{field or item.field_key}: {item.value}"


def _view_label(session: Session, item: EntityView) -> str:
    return f"{item.title}{' (Разовые)' if item.book == 'oneoff' else ''}"


def _used_in_contracts(session: Session, workspace_id: uuid.UUID, *conditions: Any) -> int:
    return _count(
        session,
        sa.select(sa.func.count())
        .select_from(Contract)
        .where(Contract.workspace_id == workspace_id, Contract.deleted_at.is_(None), sa.or_(*conditions)),
    )


def _value_blocked(session: Session, item: ListValue) -> str:
    used = _used_in_contracts(
        session,
        item.workspace_id,
        Contract.type_id == item.id,
        Contract.subject_id == item.id,
        Contract.status_id == item.id,
        Contract.economic_role_id == item.id,
        sa.cast(Contract.attrs, sa.Text).contains(str(item.id)),
    )
    return f"значение стоит в договорах: {used} - сначала замените его там или сведите с другим" if used else ""


def _department_blocked(session: Session, item: Department) -> str:
    contracts = _used_in_contracts(
        session,
        item.workspace_id,
        Contract.id.in_(sa.select(ContractDepartment.contract_id).where(ContractDepartment.department_id == item.id)),
    )
    staff = _count(
        session,
        sa.select(sa.func.count())
        .select_from(Employee)
        .where(Employee.department_id == item.id, Employee.archived_at.is_(None)),
    )
    parts = [f"договоров {contracts}" if contracts else "", f"сотрудников {staff}" if staff else ""]
    used = ", ".join(part for part in parts if part)
    return f"отдел используется: {used}" if used else ""


def _employee_blocked(session: Session, item: Employee) -> str:
    contracts = _count(
        session,
        sa.select(sa.func.count())
        .select_from(ContractPerson)
        .join(Contract, Contract.id == ContractPerson.contract_id)
        .where(ContractPerson.employee_id == item.id, Contract.deleted_at.is_(None)),
    )
    if contracts:
        return f"сотрудник - ответственный в договорах: {contracts}; удалить насовсем значит стереть его из них"
    return ""


def _operations_with(session: Session, workspace_id: uuid.UUID, *conditions: Any) -> int:
    return _count(
        session,
        sa.select(sa.func.count())
        .select_from(Operation)
        .where(Operation.workspace_id == workspace_id, sa.or_(*conditions)),
    )


def _account_blocked(session: Session, item: Account) -> str:
    used = _operations_with(session, item.workspace_id, Operation.account_from_id == item.id, Operation.account_to_id == item.id)
    return f"по счёту операций: {used} (с удалёнными)" if used else ""


def _category_blocked(session: Session, item: Category) -> str:
    used = _operations_with(session, item.workspace_id, Operation.category_id == item.id)
    return f"статья стоит в операциях: {used}" if used else ""


def _counterparty_blocked(session: Session, item: Counterparty) -> str:
    operations = _operations_with(session, item.workspace_id, Operation.counterparty_id == item.id)
    contracts = _used_in_contracts(session, item.workspace_id, Contract.executor_id == item.id, Contract.customer_id == item.id)
    own = session.get(GroupEntity, item.id) is not None
    parts = [
        f"операций {operations}" if operations else "",
        f"договоров {contracts}" if contracts else "",
        "это наше юрлицо" if own else "",
    ]
    used = ", ".join(part for part in parts if part)
    return f"контрагент используется: {used}" if used else ""


def _project_blocked(session: Session, item: Project) -> str:
    from app.finance.models import OperationProject

    used = _count(
        session, sa.select(sa.func.count()).select_from(OperationProject).where(OperationProject.project_id == item.id)
    )
    return f"проект стоит в операциях: {used}" if used else ""


def _tag_blocked(session: Session, item: Tag) -> str:
    from app.finance.models import OperationTag

    used = _count(session, sa.select(sa.func.count()).select_from(OperationTag).where(OperationTag.tag_id == item.id))
    return f"тег стоит в операциях: {used}" if used else ""


def _field_purge(session: Session, item: EntityField) -> None:
    """Своё поле насовсем: его значения в договорах и колонки листов - тоже."""
    for contract in session.scalars(
        sa.select(Contract).where(Contract.workspace_id == item.workspace_id, sa.cast(Contract.attrs, sa.Text).contains(f'"{item.key}"'))
    ):
        attrs = dict(contract.attrs or {})
        if attrs.pop(item.key, None) is not None or item.key in (contract.attrs or {}):
            contract.attrs = attrs
    for view in session.scalars(sa.select(EntityView).where(EntityView.workspace_id == item.workspace_id)):
        blocks = [
            {**block, "columns": [column for column in (block.get("columns") or []) if column.get("key") != item.key]}
            for block in (view.blocks or [])
        ]
        if blocks != (view.blocks or []):
            view.blocks = blocks


KINDS: tuple[Kind, ...] = (
    Kind("contract", "Договор", "Реестр", Contract, "deleted_at", _contract_label),
    Kind("operation", "Операция", "Журнал", Operation, "deleted_at", _operation_label),
    Kind("entity", "Наше юрлицо", "Настройка реестра", GroupEntity, "archived_at", _entity_label, schema=True),
    Kind(
        "field", "Поле реестра", "Настройка реестра", EntityField, "archived_at",
        lambda session, item: item.title, before_purge=_field_purge,
        where_clause=lambda: EntityField.system.is_(False), schema=True,
    ),
    Kind("value", "Значение списка", "Настройка реестра", ListValue, "archived_at", _value_label,
         blocked=_value_blocked, schema=True),
    Kind("view", "Лист реестра", "Настройка реестра", EntityView, "archived_at", _view_label,
         where_clause=lambda: EntityView.main.is_(False), schema=True),
    Kind("department", "Отдел", "Кабинет", Department, "archived_at",
         lambda session, item: f"{item.code}{f' · {item.title}' if item.title and item.title != item.code else ''}",
         blocked=_department_blocked, schema=True),
    Kind("employee", "Сотрудник", "Кабинет", Employee, "archived_at",
         lambda session, item: item.full_name, blocked=_employee_blocked, schema=True),
    Kind("account", "Счёт", "Справочники", Account, "archived_at", lambda session, item: item.name,
         blocked=_account_blocked),
    Kind("category", "Статья", "Справочники", Category, "archived_at", lambda session, item: item.name,
         blocked=_category_blocked),
    Kind("counterparty", "Контрагент", "Справочники", Counterparty, "archived_at", lambda session, item: item.name,
         blocked=_counterparty_blocked),
    Kind("project", "Проект", "Справочники", Project, "archived_at", lambda session, item: item.name,
         blocked=_project_blocked),
    Kind("tag", "Тег", "Справочники", Tag, "archived_at", lambda session, item: item.name, blocked=_tag_blocked),
)
KIND_BY_KEY = {kind.key: kind for kind in KINDS}


# ── Список ───────────────────────────────────────────────────────────────────


def _item_id(kind: Kind, item: Any) -> uuid.UUID:
    return item.counterparty_id if kind.key == "entity" else item.id


def _who(session: Session, workspace_id: uuid.UUID, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, tuple[datetime, str]]:
    """Кто удалил: последняя запись журнала действий о каждой записи."""
    wanted = list(set(ids))
    if not wanted:
        return {}
    rows = session.execute(
        sa.select(ActionLog.entity_id, ActionLog.at, ActionLog.user_id, ActionLog.actor)
        .where(ActionLog.workspace_id == workspace_id, ActionLog.entity_id.in_(wanted))
        .order_by(ActionLog.at)
    ).all()
    users = {
        user.id: user
        for user in session.scalars(
            sa.select(FinanceUser).where(FinanceUser.id.in_({row.user_id for row in rows if row.user_id}))
        )
    }
    out: dict[uuid.UUID, tuple[datetime, str]] = {}
    for entity_id, at, user_id, actor in rows:
        user = users.get(user_id) if user_id else None
        name = (getattr(user, "full_name", "") or getattr(user, "email", "") or actor or "") if user else (actor or "")
        out[entity_id] = (at, name)
    return out


def listing(session: Session, workspace: Workspace) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for kind in KINDS:
        column = getattr(kind.model, kind.column)
        query = sa.select(kind.model).where(kind.model.workspace_id == workspace.id, column.is_not(None))
        if kind.where_clause is not None:
            query = query.where(kind.where_clause())
        rows = list(session.scalars(query.order_by(column.desc()).limit(LIMIT)))
        who = _who(session, workspace.id, [_item_id(kind, row) for row in rows])
        for row in rows:
            item_id = _item_id(kind, row)
            deleted_at = getattr(row, kind.column)
            actor = ""
            hit = who.get(item_id)
            if hit and deleted_at and abs((_aware(hit[0]) - _aware(deleted_at)).total_seconds()) < 120:
                actor = hit[1]
            items.append(
                {
                    "kind": kind.key,
                    "kind_title": kind.title,
                    "where": kind.where,
                    "id": str(item_id),
                    "title": kind.label(session, row) or "без названия",
                    "deleted_at": deleted_at.isoformat() if deleted_at else None,
                    "by": actor,
                }
            )
    items.sort(key=lambda item: item["deleted_at"] or "", reverse=True)
    return {"items": items}


# ── Восстановить и удалить насовсем ─────────────────────────────────────────


def _get(session: Session, workspace: Workspace, kind_key: str, item_id: uuid.UUID) -> tuple[Kind, Any]:
    kind = KIND_BY_KEY.get(kind_key)
    if kind is None:
        raise TrashError("Такого в корзине не бывает")
    item = session.get(kind.model, item_id)
    if item is None or item.workspace_id != workspace.id or getattr(item, kind.column) is None:
        raise TrashError("В корзине этого уже нет")
    # Системное поле и главный лист не удаляются вовсе - в корзину они
    # попасть не могли, но адрес можно набрать руками.
    if (kind.key == "field" and item.system) or (kind.key == "view" and item.main):
        raise TrashError("Это не удаляется")
    return kind, item


def _touch(session: Session, workspace: Workspace, kind: Kind, item: Any) -> None:
    if kind.schema:
        bump(session, workspace.id, "schema")
    if kind.key == "contract":
        # Договор вернулся в опрос: номер изменений реестра, как у правки.
        item.seq = bump(session, workspace.id, "contracts")
        item.updated_at = datetime.now(timezone.utc)
    if kind.key == "operation":
        item.version += 1


def restore(session: Session, workspace: Workspace, kind_key: str, item_id: uuid.UUID) -> dict[str, Any]:
    kind, item = _get(session, workspace, kind_key, item_id)
    title = kind.label(session, item)
    setattr(item, kind.column, None)
    _touch(session, workspace, kind, item)
    session.flush()
    return {"kind": kind.key, "id": str(item_id), "title": title, "where": kind.where}


def purge(session: Session, workspace: Workspace, kind_key: str, item_id: uuid.UUID) -> dict[str, Any]:
    kind, item = _get(session, workspace, kind_key, item_id)
    reason = kind.blocked(session, item)
    if reason:
        raise TrashError(f"Насовсем не удалить: {reason}")
    title = kind.label(session, item)
    kind.before_purge(session, item)
    try:
        with session.begin_nested():
            session.delete(item)
            session.flush()
    except IntegrityError as exc:
        raise TrashError("Насовсем не удалить: на запись ещё ссылаются другие данные") from exc
    if kind.schema:
        bump(session, workspace.id, "schema")
    return {"kind": kind.key, "id": str(item_id), "title": title}


__all__ = ["KINDS", "LIMIT", "TrashError", "listing", "purge", "restore"]
