"""Настройка реестра: наши юрлица, поля, списки со смыслом, листы с блоками.

Каждое изменение здесь двигает счётчик `schema`: клиент видит новый
`schema_rev` в очередном опросе и перечитывает схему - лист перестраивается,
если поменялись колонки или отборы.
"""
from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.layout import norm
from app.finance.contracts import summary as summary_module
from app.finance.contracts import views as views_module
from app.finance.contracts.fields import (
    BOOKS,
    CHOICES,
    ENTITY,
    FIELD_BY_KEY,
    FILL_OPTIONS,
    MODE_FIELDS,
    SYSTEM_KEYS,
    FieldView,
    bump,
    current,
    ensure_registry,
    fields_of,
    fill_of,
    party_key,
    similar_values,
    slug_for,
)
from app.finance.contracts.models import (
    BILLING_KINDS,
    ECONOMIC_ROLES,
    FIELD_TYPES,
    STATUS_PHASES,
    CounterpartyName,
    Department,
    EntityField,
    EntityView,
    GroupEntity,
    ListValue,
)
from app.finance.contracts.service import Access, Registry, today
from app.finance.models import POSITION_STEP, Account, Counterparty, Workspace
from app.finance.service import FinanceError

#: Типы, которые можно дать своему полю. `choice` и `party` - системные.
CUSTOM_TYPES = ("text", "number", "money", "date", "bool", "list", "multi_list", "url", "person", "department")


def _schema_changed(session: Session, workspace: Workspace) -> None:
    bump(session, workspace.id, "schema")


# ── Схема для клиента ────────────────────────────────────────────────────────


def schema(session: Session, workspace: Workspace, access: Access) -> dict[str, Any]:
    """Поля (уже урезанные по правам), списки, отборы, наши юрлица, смыслы."""
    registry = Registry(session, workspace)
    fields: list[dict[str, Any]] = []
    for item in registry.fields:
        if item.key in access.hidden:
            continue
        # «Как было в файле» и «по выписке» - только чтение у всех.
        readonly = item.system and item.key in FIELD_BY_KEY and FIELD_BY_KEY[item.key].readonly
        fields.append(
            FieldView(
                key=item.key,
                type=item.type,
                title=item.title,
                system=item.system,
                editable=access.can_edit_field(item.key) and not readonly,
                required=item.required,
                hidden=item.hidden,
                position=item.position,
                fill=fill_of(item),
            ).to_dict()
        )
    lists: dict[str, list[dict[str, Any]]] = {}
    # Архивные значения выборы не предлагают, но подписать ими старый договор
    # нужно: иначе карточка показала бы вместо «Аренда» голый идентификатор.
    archived_values: dict[str, list[dict[str, Any]]] = {}
    # Двойники в списке («Абонентское обслуживаниее» рядом с
    # «Абонентское обслуживание») - подсказка «похоже на …» во вкладке
    # «Списки»; сводит человек.
    by_field: dict[str, list[ListValue]] = {}
    for value in registry.values.values():
        if value.archived_at is None:
            by_field.setdefault(value.field_key, []).append(value)
    similar = {later: earlier for items in by_field.values() for later, earlier in similar_values(items).items()}
    for value in sorted(registry.values.values(), key=lambda row: (row.field_key, row.position)):
        target = lists if value.archived_at is None else archived_values
        item = {"id": str(value.id), "value": value.value, "meaning": value.meaning or {}, "position": value.position}
        if value.id in similar:
            item["similar"] = str(similar[value.id])
        target.setdefault(value.field_key, []).append(item)
    parties = {pid: party for pid, party in registry.parties.items()}
    own = []
    for entity in sorted(registry.own.values(), key=lambda row: (row.position, row.code)):
        if entity.archived_at is not None:
            continue
        party = parties.get(entity.counterparty_id)
        own.append(
            {
                "id": str(entity.counterparty_id),
                "code": entity.code,
                "name": party.name if party else entity.full_name,
                "full_name": entity.full_name,
                "bin": entity.bin,
                "vat_payer": entity.vat_payer,
                "accounts": [
                    {"id": str(account.id), "name": account.name, "number": account.number}
                    for account in session.scalars(
                        sa.select(Account).where(
                            Account.workspace_id == workspace.id,
                            Account.group_entity_id == entity.counterparty_id,
                        )
                    )
                ],
            }
        )
    return {
        "schema_rev": current(session, workspace.id, "schema"),
        "fields": fields,
        "lists": lists,
        "archived_values": archived_values,
        "departments": [
            {"id": str(item.id), "code": item.code, "title": item.title, "position": item.position}
            for item in sorted(registry.departments.values(), key=lambda row: row.position)
            if item.archived_at is None
        ],
        "views": [view_out(view) for view in registry.views],
        "books": list(BOOKS),
        # Откуда «Оплачено (сводка)»: книга, лист, когда прочитана. Без
        # похода в Google - схему перечитывают на каждую настройку.
        "summary": summary_module.status(session, workspace.id),
        "own_entities": own,
        "mode_fields": list(MODE_FIELDS),
        "virtual_fields": views_module.VIRTUAL_FIELDS,
        "billing_kinds": list(BILLING_KINDS),
        "economic_roles": list(ECONOMIC_ROLES),
        "status_phases": list(STATUS_PHASES),
        "today": today().isoformat(),
        # `payments` - открыты ли «Оплачено/Остаток по выписке» (нужен журнал):
        # без флага лист спрашивал их у каждого юриста и получал 403 в консоль.
        # `admin` - владелец или администратор: убрать отдел из договора может только он.
        "access": {
            "edit": access.edit,
            "setup": access.setup,
            "admin": access.admin,
            "payments": access.view and "paid" not in access.hidden,
        },
    }


def view_out(view: EntityView) -> dict[str, Any]:
    return {
        "id": str(view.id),
        "key": view.key,
        "title": view.title,
        "main": view.main,
        "book": view.book or "",
        "position": view.position,
        "blocks": view.blocks or [],
        "sort": view.sort or [],
    }


# ── Наши юрлица ──────────────────────────────────────────────────────────────


def add_entity(
    session: Session,
    workspace: Workspace,
    *,
    name: str,
    code: str = "",
    full_name: str = "",
    bin: str = "",
    vat_payer: bool = False,
) -> GroupEntity:
    """Отметить контрагента нашим юрлицом (или завести его).

    Если контрагент с таким именем уже есть - он и становится нашим: иначе
    договоры, уже записанные на него, остались бы на двойнике.
    """
    ensure_registry(session, workspace)
    registry = Registry(session, workspace)
    clean = (name or code or "").strip()
    if not clean:
        raise FinanceError("У юрлица должно быть имя")
    resolved = registry.resolve_party(clean, slot="executor")
    if resolved.ambiguous:
        raise FinanceError(f"Под «{clean}» подходит несколько контрагентов - уточните имя")
    party = resolved.party
    assert party is not None
    existing = session.get(GroupEntity, party.id)
    if existing is not None:
        if existing.archived_at is not None:
            # Юрлицо из архива заводят снова - возвращаем его, а не двойника.
            existing.archived_at = None
            session.flush()
            _schema_changed(session, workspace)
        return existing
    entity = GroupEntity(
        counterparty_id=party.id,
        workspace_id=workspace.id,
        code=(code or "").strip(),
        full_name=(full_name or "").strip(),
        bin=(bin or "").strip(),
        vat_payer=vat_payer,
        position=(len(registry.own) + 1) * POSITION_STEP,
    )
    session.add(entity)
    for text in (code, full_name):
        remember_alias(session, workspace.id, party.id, text, source="manual")
    session.flush()
    _schema_changed(session, workspace)
    return entity


def update_entity(session: Session, workspace: Workspace, party_id: uuid.UUID, data: dict[str, Any]) -> GroupEntity:
    entity = session.get(GroupEntity, party_id)
    if entity is None or entity.workspace_id != workspace.id:
        raise FinanceError("Такого юрлица нет")
    for key in ("code", "full_name", "bin"):
        if key in data:
            setattr(entity, key, str(data[key] or "").strip())
            if key in ("code", "full_name"):
                remember_alias(session, workspace.id, party_id, data[key], source="manual")
    if "vat_payer" in data:
        entity.vat_payer = bool(data["vat_payer"])
    if "archived" in data:
        from datetime import datetime, timezone

        # Юрлицо с договорами тоже удаляется - в корзину (27.09.2026: ошибочно
        # заведённое «ИП WE make» с одним договором не удалялось никак). Что
        # его договоры перестанут считаться «нашими» (уйдут из «Исполнитель
        # ГК», получат «ни одна сторона не наша»), говорит вопрос «Удалить?»
        # до нажатия, а из корзины юрлицо возвращается вместе с этим.
        entity.archived_at = datetime.now(timezone.utc) if data["archived"] else None
    if "accounts" in data:
        wanted = {uuid.UUID(str(item)) for item in data["accounts"] or []}
        for account in session.scalars(sa.select(Account).where(Account.workspace_id == workspace.id)):
            if account.id in wanted:
                account.group_entity_id = party_id
            elif account.group_entity_id == party_id:
                account.group_entity_id = None
    session.flush()
    _schema_changed(session, workspace)
    return entity


def remember_alias(
    session: Session, workspace_id: uuid.UUID, party_id: uuid.UUID, name: Any, *, source: str
) -> None:
    """Запомнить написание контрагента. Повтор ничего не делает."""
    text = str(name or "").strip()
    key = party_key(text)
    if not key:
        return
    exists = session.scalar(
        sa.select(CounterpartyName.id).where(
            CounterpartyName.counterparty_id == party_id, CounterpartyName.normalized == key
        )
    )
    if exists is None:
        session.add(
            CounterpartyName(
                workspace_id=workspace_id, counterparty_id=party_id, name=text, normalized=key, source=source
            )
        )
        session.flush()


def merge_parties(
    session: Session, workspace: Workspace, *, keep: uuid.UUID, drop: uuid.UUID
) -> None:
    """Свести двух контрагентов в одного: договоры и написания переезжают.

    Операции журнала тоже переезжают - иначе один клиент остался бы в отчёте
    двумя строками. Сводит только человек: похожесть - повод спросить, а не
    решить.
    """
    from app.finance.contracts.models import Contract
    from app.finance.models import Invoice, Operation, Recurrence

    if keep == drop:
        return
    kept, dropped = session.get(Counterparty, keep), session.get(Counterparty, drop)
    if kept is None or dropped is None or kept.workspace_id != workspace.id or dropped.workspace_id != workspace.id:
        raise FinanceError("Контрагент не найден")
    for column in (Contract.executor_id, Contract.customer_id):
        session.execute(sa.update(Contract).where(column == drop).values({column.key: keep}))
    for model in (Operation, Invoice, Recurrence):
        session.execute(sa.update(model).where(model.counterparty_id == drop).values(counterparty_id=keep))
    session.execute(
        sa.update(CounterpartyName)
        .where(CounterpartyName.counterparty_id == drop)
        .values(counterparty_id=keep)
    )
    remember_alias(session, workspace.id, keep, dropped.name, source="manual")
    if session.get(GroupEntity, drop) is not None and session.get(GroupEntity, keep) is None:
        entity = session.get(GroupEntity, drop)
        session.add(
            GroupEntity(
                counterparty_id=keep,
                workspace_id=workspace.id,
                code=entity.code,
                full_name=entity.full_name,
                bin=entity.bin,
                vat_payer=entity.vat_payer,
                position=entity.position,
            )
        )
        session.flush()
    session.execute(sa.delete(GroupEntity).where(GroupEntity.counterparty_id == drop))
    from datetime import datetime, timezone

    dropped.archived_at = datetime.now(timezone.utc)
    session.flush()
    _schema_changed(session, workspace)


# ── Поля ─────────────────────────────────────────────────────────────────────


def add_field(
    session: Session, workspace: Workspace, *, title: str, type: str, after: str | None = None
) -> EntityField:
    ensure_registry(session, workspace)
    clean = (title or "").strip()
    if not clean:
        raise FinanceError("У поля должна быть подпись")
    if type not in CUSTOM_TYPES:
        raise FinanceError("Такого типа поля нет")
    existing = fields_of(session, workspace.id)
    for item in existing:
        if norm(item.title) == norm(clean):
            raise FinanceError(f"Поле «{clean}» уже есть")
    taken = {
        key
        for (key,) in session.execute(
            sa.select(EntityField.key).where(EntityField.workspace_id == workspace.id)
        )
    }
    position = _position_after(existing, after)
    item = EntityField(
        workspace_id=workspace.id,
        entity=ENTITY,
        key=slug_for(clean, taken),
        system=False,
        type=type,
        title=clean,
        names=[clean.lower()],
        position=position,
    )
    session.add(item)
    session.flush()
    _schema_changed(session, workspace)
    return item


def _position_after(fields: list[EntityField], after: str | None) -> int:
    if not fields:
        return POSITION_STEP
    if after is None:
        return fields[-1].position + POSITION_STEP
    for index, item in enumerate(fields):
        if item.key == after:
            nxt = fields[index + 1].position if index + 1 < len(fields) else item.position + 2 * POSITION_STEP
            middle = (item.position + nxt) // 2
            if middle in (item.position, nxt):
                return item.position + 1
            return middle
    return fields[-1].position + POSITION_STEP


def update_field(session: Session, workspace: Workspace, key: str, data: dict[str, Any]) -> EntityField:
    item = session.scalar(
        sa.select(EntityField).where(
            EntityField.workspace_id == workspace.id, EntityField.entity == ENTITY, EntityField.key == key
        )
    )
    if item is None:
        raise FinanceError("Такого поля нет")
    if "title" in data:
        clean = str(data["title"] or "").strip()
        if not clean:
            raise FinanceError("У поля должна быть подпись")
        names = list(item.names or [])
        if clean.lower() not in names:
            names.append(clean.lower())
        item.title, item.names = clean, names
    if "hidden" in data:
        item.hidden = bool(data["hidden"])
    if "required" in data and bool(data["required"]) != bool(item.required):
        item.required = bool(data["required"])
        # «Не заполнено» - замечание договора, а договоры клиент берёт опросом по
        # номеру изменения: без сдвига номера новое правило проявилось бы у
        # каждого договора только после его следующей правки.
        _touch_all_contracts(session, workspace)
    if "type" in data and data["type"] != item.type:
        if item.system:
            raise FinanceError("Тип системного поля не меняется: на нём держатся начисления и долги")
        if data["type"] not in CUSTOM_TYPES:
            raise FinanceError("Такого типа поля нет")
        item.type = data["type"]
        # Способ заполнения принадлежит типу: у текста списка нет вовсе.
        if (item.fill or "") not in FILL_OPTIONS.get(item.type, ()):
            item.fill = ""
    if "fill" in data:
        wanted = str(data["fill"] or "")
        options = FILL_OPTIONS.get(item.type, ())
        if not options:
            raise FinanceError(f"«{item.title}» заполняется как есть - выбора из списка у этого типа нет")
        if wanted not in options:
            raise FinanceError("Такого способа заполнения у этого поля нет")
        item.fill = wanted
    if "after" in data:
        others = [row for row in fields_of(session, workspace.id) if row.key != key]
        item.position = _position_after(others, data["after"]) if data["after"] else (
            (others[0].position // 2) if others else POSITION_STEP
        )
    if "archived" in data:
        if item.system and data["archived"]:
            raise FinanceError("Системное поле можно спрятать, но не удалить")
        from datetime import datetime, timezone

        item.archived_at = datetime.now(timezone.utc) if data["archived"] else None
    session.flush()
    _schema_changed(session, workspace)
    return item


#: Тип поля словами - для журнала («тип: текст → список»). Те же слова, что
#: у настройки на фронте (`setup/words.ts`).
TYPE_WORDS = {
    "text": "текст", "number": "число", "money": "деньги", "date": "дата", "bool": "да или нет",
    "list": "список", "multi_list": "несколько из списка", "url": "ссылка", "person": "сотрудник",
    "party": "сторона", "department": "отдел", "choice": "выбор",
}
_FIELD_STATE = ("title", "hidden", "required", "type", "fill", "position", "archived")


def field_state(session: Session, workspace: Workspace, key: str) -> dict[str, Any]:
    """Что у поля можно поменять - для журнала «было → стало». Нет поля - пусто."""
    item = session.scalar(
        sa.select(EntityField).where(
            EntityField.workspace_id == workspace.id, EntityField.entity == ENTITY, EntityField.key == key
        )
    )
    if item is None:
        return {}
    return {
        "key": item.key,
        "title": item.title,
        "hidden": bool(item.hidden),
        "required": bool(item.required),
        "type": item.type,
        "fill": item.fill or "",
        "position": item.position,
        "archived": item.archived_at is not None,
    }


def field_change(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Запись журнала о правке поля: что сделано словами, что было и что стало.

    В `before`/`after` - только поменявшееся и ключ поля: по ним журнал
    отвечает «кто спрятал поле и каким оно было», а не одно «поле изменено».
    """
    changed = [name for name in _FIELD_STATE if before.get(name) != after.get(name)]
    title = before.get("title") or after.get("title") or ""
    words: list[str] = []
    for name in changed:
        if name == "hidden":
            words.append("спрятано" if after["hidden"] else "снова в листе и карточке")
        elif name == "required":
            words.append("обязательное" if after["required"] else "необязательное")
        elif name == "title":
            words.append(f"переименовано в «{after['title']}»")
        elif name == "type":
            words.append(f"тип: {TYPE_WORDS.get(before['type'], before['type'])} → {TYPE_WORDS.get(after['type'], after['type'])}")
        elif name == "fill":
            words.append("заполнение: " + ("только из списка" if after["fill"] in ("list", "own") else "список или своё"))
        elif name == "position":
            words.append("передвинуто")
        elif name == "archived":
            words.append("удалено в корзину" if after["archived"] else "возвращено из корзины")
    key = after.get("key") or before.get("key")
    keyed = {"key": key} if key else {}
    return {
        "title": f"поле «{title}» - {', '.join(words)}" if words else f"поле «{title}» сохранено без изменений",
        "before": {**keyed, **{name: before.get(name) for name in changed}},
        "after": {**keyed, **{name: after.get(name) for name in changed}},
    }


def value_state(session: Session, workspace: Workspace, value_id: uuid.UUID) -> dict[str, Any]:
    """Значение списка для журнала «было → стало». Нет значения - пусто."""
    item = session.get(ListValue, value_id)
    if item is None or item.workspace_id != workspace.id:
        return {}
    return {
        "id": str(item.id),
        "field": item.field_key,
        "value": item.value,
        "meaning": dict(item.meaning or {}),
        "archived": item.archived_at is not None,
    }


def value_change(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    changed = [name for name in ("value", "meaning", "archived") if before.get(name) != after.get(name)]
    words: list[str] = []
    for name in changed:
        if name == "value":
            words.append(f"переименовано в «{after['value']}»")
        elif name == "meaning":
            words.append("смысл изменён")
        elif name == "archived":
            words.append("удалено в корзину" if after["archived"] else "возвращено из корзины")
    title = before.get("value") or after.get("value") or ""
    ids = {"id": after.get("id") or before.get("id"), "field": after.get("field") or before.get("field")}
    return {
        "title": f"значение «{title}» - {', '.join(words)}" if words else f"значение «{title}» сохранено без изменений",
        "before": {**ids, **{name: before.get(name) for name in changed}},
        "after": {**ids, **{name: after.get(name) for name in changed}},
    }


def view_state(session: Session, workspace: Workspace, view_id: uuid.UUID) -> dict[str, Any]:
    """Лист для журнала: целиком, чтобы по записи его можно было вернуть."""
    view = session.get(EntityView, view_id)
    if view is None or view.workspace_id != workspace.id:
        return {}
    return {
        "id": str(view.id),
        "key": view.key,
        "title": view.title,
        "blocks": view.blocks or [],
        "style": view.style or {},
        "position": view.position,
        "archived": view.archived_at is not None,
    }


def view_change(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    changed = [name for name in ("title", "blocks", "style", "position", "archived") if before.get(name) != after.get(name)]
    words: list[str] = []
    for name in changed:
        if name == "title":
            words.append(f"переименован в «{after['title']}»")
        elif name == "blocks":
            words.append("блоки изменены")
        elif name == "style":
            words.append("подсветка изменена")
        elif name == "position":
            words.append("передвинут")
        elif name == "archived":
            words.append("удалён в корзину" if after["archived"] else "возвращён из корзины")
    title = before.get("title") or after.get("title") or ""
    ids = {"id": after.get("id") or before.get("id"), "key": after.get("key") or before.get("key")}
    return {
        "title": f"лист «{title}» - {', '.join(words)}" if words else f"лист «{title}» сохранён без изменений",
        "before": {**ids, **{name: before.get(name) for name in changed}},
        "after": {**ids, **{name: after.get(name) for name in changed}},
    }


# ── Значения списков ─────────────────────────────────────────────────────────


def _check_meaning(field_key: str, meaning: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (meaning or {}).items():
        if value in (None, ""):
            continue
        if key == "phase" and value not in STATUS_PHASES:
            raise FinanceError("Такой фазы статуса нет")
        if key == "billing" and value not in BILLING_KINDS:
            raise FinanceError("Начисление: в месяц, вся сумма или условие")
        if key in ("economic_role", "system") and value not in ECONOMIC_ROLES:
            raise FinanceError("Такого хозяйственного смысла нет")
        if key == "handover" and value not in ("accounting",):
            raise FinanceError("Передача - только бухгалтеру")
        if key == "distinct":
            if not isinstance(value, list):
                raise FinanceError("«Это разные» - список значений")
            clean[key] = [str(item) for item in value]
            continue
        if key == "roles":
            if not isinstance(value, dict):
                raise FinanceError("Подписи сторон: ждём исполнителя и заказчика")
            value = {k: str(v).strip() for k, v in value.items() if k in ("executor", "customer") and str(v).strip()}
        clean[key] = value
    return clean


def add_value(
    session: Session, workspace: Workspace, field_key: str, value: str, meaning: dict[str, Any] | None = None
) -> ListValue:
    registry = Registry(session, workspace)
    if field_key not in registry.field_by_key:
        raise FinanceError("Такого поля нет")
    created = registry.resolve_value(field_key, value)
    if created is None:
        raise FinanceError("Пустое значение")
    if meaning is not None:
        created.meaning = _check_meaning(field_key, meaning)
    session.flush()
    _schema_changed(session, workspace)
    return created


def update_value(session: Session, workspace: Workspace, value_id: uuid.UUID, data: dict[str, Any]) -> ListValue:
    item = session.get(ListValue, value_id)
    if item is None or item.workspace_id != workspace.id:
        raise FinanceError("Такого значения нет")
    if "value" in data:
        clean = str(data["value"] or "").strip()
        if not clean:
            raise FinanceError("Пустое значение")
        clash = session.scalar(
            sa.select(ListValue.id).where(
                ListValue.workspace_id == workspace.id,
                ListValue.field_key == item.field_key,
                ListValue.normalized == norm(clean),
                ListValue.id != item.id,
            )
        )
        if clash is not None:
            raise FinanceError(f"«{clean}» в этом списке уже есть - сведите значения")
        item.value, item.normalized = clean, norm(clean)
    base = _base_economic(session, workspace, item)
    if "meaning" in data:
        meaning = _check_meaning(item.field_key, data["meaning"] or {})
        if base and meaning.get("system") != (item.meaning or {}).get("system"):
            raise FinanceError(f"«{item.value}» - системный смысл: по нему считаются выручка и порог НДС")
        # «Это разные» - не смысл значения, а решение о паре: правка фазы или
        # начисления его не стирает.
        kept = (item.meaning or {}).get("distinct")
        if kept and "distinct" not in meaning:
            meaning["distinct"] = kept
        item.meaning = meaning
    if data.get("distinct"):
        other = session.get(ListValue, uuid.UUID(str(data["distinct"])))
        if other is None or other.workspace_id != workspace.id or other.field_key != item.field_key:
            raise FinanceError("Сравнивать можно значения одного списка")
        apart = sorted({*((item.meaning or {}).get("distinct") or []), str(other.id)})
        item.meaning = {**(item.meaning or {}), "distinct": apart}
    if "position" in data:
        item.position = int(data["position"])
    if "archived" in data:
        from datetime import datetime, timezone

        if data["archived"]:
            if base:
                raise FinanceError(f"«{item.value}» - системный смысл: по нему считаются выручка и порог НДС")
            used = _contracts_with_value(session, workspace.id, item)
            if used:
                raise FinanceError(
                    f"«{item.value}» стоит в {used} {_contracts_word(used, case='loc')} - сведите его с другим значением"
                )
        item.archived_at = datetime.now(timezone.utc) if data["archived"] else None
    session.flush()
    _schema_changed(session, workspace)
    # Смысл значения меняет подстановки и принадлежность к листам у договоров,
    # где оно стоит: двигаем их номер, чтобы клиенты перечитали.
    _touch_contracts_with_value(session, workspace, item)
    return item


def _contracts_word(count: int, case: str = "nom") -> str:
    tail = count % 100
    if case == "loc":
        return "договоре" if tail % 10 == 1 and tail != 11 else "договорах"
    if 11 <= tail <= 14:
        return "договоров"
    return {1: "договор", 2: "договора", 3: "договора", 4: "договора"}.get(tail % 10, "договоров")


def _contracts_with_party(session: Session, workspace_id: uuid.UUID, party_id: uuid.UUID) -> int:
    from app.finance.contracts.models import Contract

    return int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(Contract)
            .where(
                Contract.workspace_id == workspace_id,
                Contract.deleted_at.is_(None),
                sa.or_(Contract.executor_id == party_id, Contract.customer_id == party_id),
            )
        )
        or 0
    )


def _contracts_with_value(session: Session, workspace_id: uuid.UUID, value: ListValue) -> int:
    """Сколько живых договоров стоит на значении системного списка.

    Свои списочные поля лежат в `attrs` - их значения в архив уходят
    свободно: подпись остаётся в `archived_values` схемы.
    """
    from app.finance.contracts.models import Contract
    from app.finance.contracts.service import COLUMN_OF

    column = COLUMN_OF.get(value.field_key)
    if column is None:
        return 0
    return int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(Contract)
            .where(
                Contract.workspace_id == workspace_id,
                Contract.deleted_at.is_(None),
                getattr(Contract, column) == value.id,
            )
        )
        or 0
    )


def _base_economic(session: Session, workspace: Workspace, value: ListValue) -> bool:
    """Одно из четырёх системных значений хозяйственного смысла.

    По ним подстановки ставят смысл и будет считаться порог НДС: «Выручка»
    в архиве или со сменённым смыслом молча вывела бы продажи из порога.
    Своё значение поверх системного («Агентский внутри ГК») - не базовое.
    """
    if value.field_key != "economic_role":
        return False
    from app.finance.contracts.fields import economic_role_values

    return any(item.id == value.id for item in economic_role_values(session, workspace.id).values())


def _touch_all_contracts(session: Session, workspace: Workspace) -> None:
    from app.finance.contracts.models import Contract

    seq = bump(session, workspace.id, "contracts")
    session.execute(
        sa.update(Contract)
        .where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
        .values(seq=seq)
    )


def _touch_contracts_with_value(session: Session, workspace: Workspace, value: ListValue) -> None:
    from app.finance.contracts.models import Contract
    from app.finance.contracts.service import COLUMN_OF

    column = COLUMN_OF.get(value.field_key)
    if column is None:
        return
    seq = bump(session, workspace.id, "contracts")
    session.execute(
        sa.update(Contract)
        .where(Contract.workspace_id == workspace.id, getattr(Contract, column) == value.id)
        .values(seq=seq)
    )


def merge_values(session: Session, workspace: Workspace, *, keep: uuid.UUID, drop: uuid.UUID) -> None:
    """Свести два значения списка: «КазМ» и «КазМетал» → одно."""
    from app.finance.contracts.models import Contract
    from app.finance.contracts.service import COLUMN_OF

    kept, dropped = session.get(ListValue, keep), session.get(ListValue, drop)
    if kept is None or dropped is None or kept.field_key != dropped.field_key or kept.workspace_id != workspace.id:
        raise FinanceError("Сводить можно значения одного списка")
    if _base_economic(session, workspace, dropped):
        raise FinanceError(f"«{dropped.value}» - системный смысл: сведите другое значение в него, а не его в другое")
    column = COLUMN_OF.get(kept.field_key)
    seq = bump(session, workspace.id, "contracts")
    if column:
        session.execute(
            sa.update(Contract)
            .where(Contract.workspace_id == workspace.id, getattr(Contract, column) == drop)
            .values({column: keep, "seq": seq})
        )
    session.delete(dropped)
    session.flush()
    _schema_changed(session, workspace)


# ── Отделы ───────────────────────────────────────────────────────────────────


def upsert_department(session: Session, workspace: Workspace, data: dict[str, Any], department_id: uuid.UUID | None = None) -> Department:
    if department_id is None:
        registry = Registry(session, workspace)
        department = registry.resolve_department(data.get("code") or data.get("title"))
        if department is None:
            raise FinanceError("У отдела должен быть код")
    else:
        department = session.get(Department, department_id)
        if department is None or department.workspace_id != workspace.id:
            raise FinanceError("Такого отдела нет")
    if data.get("code"):
        department.code = str(data["code"]).strip()
        department.normalized_name = norm(department.code)
    if "title" in data:
        department.title = str(data["title"] or "").strip()
    session.flush()
    _schema_changed(session, workspace)
    return department


# ── Листы ────────────────────────────────────────────────────────────────────


def _clean_blocks(blocks: Any, registry: Registry) -> list[dict[str, Any]]:
    if not isinstance(blocks, list) or not blocks:
        raise FinanceError("У листа должен быть хотя бы один блок")
    keys = set(registry.field_by_key) | set(views_module.VIRTUAL_FIELDS)
    out = []
    for block in blocks:
        if not isinstance(block, dict):
            raise FinanceError("Блок листа записан неверно")
        rule = views_module.validate(block.get("filter"))
        for used in views_module.fields_used(rule):
            if used not in keys:
                raise FinanceError(f"В правиле поле «{used}», которого нет в реестре")
        roles = block.get("roles") or {}
        columns = [
            {"key": str(column.get("key")), "label": str(column.get("label") or ""), "width": column.get("width")}
            for column in (block.get("columns") or [])
            if isinstance(column, dict) and column.get("key") in keys | {"row_number"}
        ]
        defaults = {
            key: value
            for key, value in (block.get("defaults") or {}).items()
            if key in ("type", "subject", "status", "billing", "economic_role", "department", "own_side")
        }
        paint = views_module.validate_paint(block.get("paint"))
        for item in paint:
            for used in views_module.fields_used(item["filter"]):
                if used not in keys:
                    raise FinanceError(f"В подсветке поле «{used}», которого нет в реестре")
        clean = {
            "title": str(block.get("title") or ""),
            "filter": rule,
            "roles": roles if isinstance(roles, dict) else {},
            "columns": columns,
            "defaults": defaults,
        }
        choices = _clean_choices(block.get("choices"), registry)
        if choices:
            clean["choices"] = choices
        if paint:
            clean["paint"] = paint
        out.append(clean)
    return out


def _clean_choices(raw: Any, registry: Registry) -> dict[str, list[str]]:
    """Что предлагает выбор поля в этом блоке: `{поле: [значения]}`.

    В «Разовых» статус - только «на исполнении» и «исполнен», как в книге
    юротдела; в реестре у того же поля шесть значений. Значение договора это
    не ограничивает - только то, что предлагают лист и карточка из этого блока.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise FinanceError("Выбор значений блока записан неверно")
    out: dict[str, list[str]] = {}
    for key, ids in raw.items():
        field_def = registry.field_by_key.get(str(key))
        if field_def is None or field_def.type not in ("list", "multi_list"):
            raise FinanceError(f"Ограничить выбор можно только у списка, а «{key}» - не список")
        if not isinstance(ids, list):
            raise FinanceError("Выбор значений блока записан неверно")
        clean: list[str] = []
        for raw_id in ids:
            try:
                value = registry.values.get(uuid.UUID(str(raw_id)))
            except ValueError:
                value = None
            if value is None or value.field_key != field_def.key:
                raise FinanceError(f"В списке «{field_def.title}» такого значения нет")
            if str(value.id) not in clean:
                clean.append(str(value.id))
        if clean:
            out[field_def.key] = clean
    return out


def upsert_view(
    session: Session, workspace: Workspace, data: dict[str, Any], view_id: uuid.UUID | None = None
) -> EntityView:
    registry = Registry(session, workspace)
    if view_id is None:
        title = str(data.get("title") or "").strip()
        if not title:
            raise FinanceError("У листа должно быть название")
        taken = {view.key for view in session.scalars(sa.select(EntityView).where(EntityView.workspace_id == workspace.id))}
        key = str(data.get("key") or "").strip() or slug_for(title, taken)
        if key in taken:
            key = slug_for(key, taken)
        book = str(data.get("book") or "")
        if book not in BOOKS:
            raise FinanceError("Такой книги листов нет")
        view = EntityView(
            workspace_id=workspace.id,
            entity=ENTITY,
            key=key,
            title=title,
            main=False,
            book=book,
            blocks=_clean_blocks(data.get("blocks") or [{"title": "", "filter": {"any": []}}], registry),
            style=data.get("style") or {},
            position=(max((v.position for v in registry.views), default=0) + POSITION_STEP),
        )
        session.add(view)
    else:
        view = session.get(EntityView, view_id)
        if view is None or view.workspace_id != workspace.id:
            raise FinanceError("Такого листа нет")
        if "title" in data:
            view.title = str(data["title"] or "").strip() or view.title
        if "blocks" in data:
            view.blocks = _clean_blocks(data["blocks"], registry)
        if "style" in data:
            view.style = data["style"] or {}
        if "position" in data:
            view.position = int(data["position"])
        if "archived" in data:
            if view.main and data["archived"]:
                raise FinanceError("Главный лист не удаляется")
            from datetime import datetime, timezone

            view.archived_at = datetime.now(timezone.utc) if data["archived"] else None
    session.flush()
    _schema_changed(session, workspace)
    return view


def preview_filter(session: Session, workspace: Workspace, access: Access, rule: Any) -> dict[str, Any]:
    """Сколько договоров подходит под правило - для живого счётчика."""
    from app.finance.contracts.models import Contract
    from app.finance.contracts.service import facts_of, people_of, visible_to

    registry = Registry(session, workspace)
    clean = views_module.validate(rule)
    contracts = list(
        session.scalars(
            sa.select(Contract).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
        )
    )
    people = people_of(session, [item.id for item in contracts])
    # Счётчик правила пересчитывается на каждую правку условия - стороны
    # одним запросом, а не по одному на договор внутри facts_of.
    registry.parties_for({pid for item in contracts for pid in (item.executor_id, item.customer_id)})
    count, sample = 0, []
    for item in contracts:
        mine = people.get(item.id, [])
        if not visible_to(item, registry, access, mine):
            continue
        if views_module.matches(clean, facts_of(item, registry, mine)):
            count += 1
            if len(sample) < 5:
                party = registry.parties.get(item.customer_id) if item.customer_id else None
                sample.append(f"{item.number or '-'} · {party.name if party else '-'}")
    return {"count": count, "sample": sample}


__all__ = [
    "CUSTOM_TYPES",
    "add_entity",
    "add_field",
    "add_value",
    "merge_parties",
    "merge_values",
    "preview_filter",
    "remember_alias",
    "schema",
    "update_entity",
    "update_field",
    "update_value",
    "upsert_department",
    "upsert_view",
    "view_out",
]
