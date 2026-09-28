"""История действий и отмена.

Учёт ведут несколько человек, и вопрос «кто убрал эту операцию на два
миллиона» возникает раньше вопроса «сколько денег». Поэтому запись хранит не
описание («изменил операцию»), а **состояние до и после**.

Почему отмена — это возврат состояния, а не обратное действие
─────────────────────────────────────────────────────────────
Обратное действие («было списание — сделаем поступление») кажется проще и
врёт: в журнале появляется вторая операция, которой в жизни не было, отчёт за
период меняется дважды, а сверка с выпиской перестаёт сходиться. Возврат
состояния возвращает ровно то, что было, и в истории остаётся отметка, что
это была отмена.

`undone_at` не даёт отменить дважды: вторая отмена вернула бы уже отменённое и
выглядела бы как новая правка.

Кто, из какого сеанса, откуда
─────────────────────────────
Запись журнала несёт автора (`user_id`), сеанс, IP и браузер. Передавать их
в каждый вызов `write` по цепочке маршрут → сервис → запись — значит однажды
забыть в одном месте, и журнал «пишется всегда» перестанет быть правдой
тихо. Поэтому их кладёт в `CONTEXT` зависимость доступа маршрута
(`require_access` в `routes/finance.py`) — одна на все двери, — а `write`
берёт оттуда сам. Явные аргументы важнее контекста: вход и регистрация
пишут событие до того, как сеанс появился.

`ContextVar`, а не глобальная переменная: запросы идут параллельно в пуле
потоков, и у каждого свой автор. Значение ставится в асинхронной части
зависимости — в задаче запроса, — поэтому доезжает до обычного `def`
обработчика в потоке и не протекает в соседний запрос. Фоновые задачи
контекста не имеют и подписываются «система».
"""
from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.models import ACTION_CATEGORIES, Account, ActionLog, Operation, Workspace
from app.finance.service import FinanceError, check_money


@dataclass(frozen=True)
class AuditContext:
    """Кто действует в этом запросе."""

    user_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    ip: str = ""
    user_agent: str = ""
    #: Подпись для колонки `actor` — почта или телефон.
    actor: str = ""


CONTEXT: contextvars.ContextVar[AuditContext | None] = contextvars.ContextVar(
    "finance_audit_context", default=None
)


def set_context(context: AuditContext) -> None:
    CONTEXT.set(context)


def current_context() -> AuditContext | None:
    return CONTEXT.get()


#: Вид записи по её коду, если вызывающий не назвал его сам. Префиксы, а не
#: список кодов: новый код `people.*` сам попадает в администрирование.
_CATEGORY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("auth.", "auth"),
    ("people.", "admin"),
    ("access.", "admin"),
    ("company.", "admin"),
    ("notification.", "admin"),
    ("contracts.setup.", "admin"),
    ("view.", "view"),
    ("import.", "import"),
    ("sheets.", "import"),
    ("contract.import", "import"),
)
_CATEGORY_EXACT = {
    "contract.export": "export",
    "journal.export": "export",
}


def category_of(kind: str) -> str:
    if kind in _CATEGORY_EXACT:
        return _CATEGORY_EXACT[kind]
    for prefix, category in _CATEGORY_PREFIXES:
        if kind.startswith(prefix):
            return category
    return "data"

#: Поля операции, которые история хранит и умеет вернуть. Список закрытый: то,
#: что в него не входит, отменой не восстановится, и лучше знать это заранее,
#: чем обнаружить при отмене.
OPERATION_FIELDS = (
    "kind",
    "status",
    "paid_at",
    "accrued_at",
    "amount",
    "currency",
    "rate",
    "account_from_id",
    "account_to_id",
    "category_id",
    "counterparty_id",
    "comment",
    "deleted_at",
)

TITLES = {
    "operation.create": "операция заведена",
    "operation.update": "операция изменена",
    "operation.delete": "операция убрана",
    "operation.settle": "ожидание закрыто оплатой",
    "invoice.create": "счёт выставлен",
    "invoice.void": "счёт отменён",
    "import.apply": "загрузка заведена",
    "rules.apply": "правила применены",
    "recurrence.create": "повторение создано",
    "recurrence.remove": "повторение удалено",
    "integration.create": "подключение создано",
    "integration.receive": "операции пришли из подключения",
    "account.balance": "начальный остаток счёта изменён",
    "account.number": "номер счёта изменён",
    "account.create": "счёт заведён",
    "autotag.apply": "авторазметка статей",
    "dictionary.create": "запись справочника заведена",
    "dictionary.archive": "запись справочника удалена в корзину",
    "category.nature": "природа статьи изменена",
    "plan.set": "план поставлен",
    "rule.create": "правило заведено",
    "rule.delete": "правило удалено",
    "rule.toggle": "правило включено или выключено",
    "recurrence.toggle": "повторение включено или выключено",
    "recurrence.materialize": "ожидания повторений продлены",
    "integration.token": "токен подключения заменён",
    "integration.state": "подключение включено или выключено",
    "integration.delete": "подключение удалено",
}


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def snapshot(operation: Operation) -> dict[str, Any]:
    """Состояние операции для истории."""
    return {field: _plain(getattr(operation, field, None)) for field in OPERATION_FIELDS}


def write(
    session: Session,
    workspace: Workspace,
    *,
    kind: str,
    entity: str,
    entity_id: uuid.UUID | None = None,
    title: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    actor: str = "",
    category: str | None = None,
    user_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    workspace_id: uuid.UUID | None = None,
) -> ActionLog:
    """Записать событие. Автора, сеанс и адрес берёт из `CONTEXT`, если их не дали.

    `workspace_id` — для событий, у которых объекта компании под рукой нет
    (вход пишет в компанию учётки по её идентификатору).
    """
    context = CONTEXT.get()
    chosen = category or category_of(kind)
    if chosen not in ACTION_CATEGORIES:
        raise ValueError(f"неизвестный вид записи журнала: {chosen}")
    entry = ActionLog(
        workspace_id=workspace_id or workspace.id,
        # Время ставит код, а не `now()` базы: у SQLite оно с точностью до
        # секунды, и курсор ленты `(at, id)` путал бы порядок событий одной
        # секунды.
        at=datetime.now(timezone.utc),
        actor=actor or (context.actor if context else ""),
        kind=kind,
        entity=entity,
        entity_id=entity_id,
        title=title or TITLES.get(kind, kind),
        before=before or {},
        after=after or {},
        category=chosen,
        user_id=user_id if user_id is not None else (context.user_id if context else None),
        session_id=session_id if session_id is not None else (context.session_id if context else None),
        ip=(ip if ip is not None else (context.ip if context else ""))[:64],
        user_agent=(user_agent if user_agent is not None else (context.user_agent if context else ""))[:400],
    )
    session.add(entry)
    session.flush()
    return entry


def listing(
    session: Session, workspace_id: uuid.UUID, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Старая лента «История»: только правки данных, загрузки и выгрузки.

    Входы, просмотры и администрирование — в журнале действий (`audit.py`),
    за правом `audit`: в ленте «Истории», открытой всем, кто видит журнал,
    чужие IP и время входов были бы лишними.
    """
    rows = session.scalars(
        sa.select(ActionLog)
        .where(
            ActionLog.workspace_id == workspace_id,
            ActionLog.category.in_(("data", "import", "export")),
        )
        .order_by(ActionLog.at.desc())
        .limit(limit)
    )
    out: list[dict[str, Any]] = []
    for entry in rows:
        before, after = entry.before or {}, entry.after or {}
        if entry.kind == "autotag.apply":
            # Разметка хранит пары «операция → статья» на все тысячи строк; в
            # список истории они не нужны — только сколько.
            before = {"operations": len(before.get("items", []))} if "items" in before else before
            after = {"operations": len(after.get("items", []))} if "items" in after else after
        out.append(
            {
                "id": str(entry.id),
                "at": entry.at.isoformat() if entry.at else None,
                "actor": entry.actor,
                "kind": entry.kind,
                "entity": entry.entity,
                "entity_id": str(entry.entity_id) if entry.entity_id else None,
                "title": entry.title,
                "before": before,
                "after": after,
                "undone_at": entry.undone_at.isoformat() if entry.undone_at else None,
                "can_undo": entry.undone_at is None and _undoable(entry),
            }
        )
    return out


def _undoable(entry: ActionLog) -> bool:
    """Отменить можно только то, что `undo` умеет вернуть.

    Раньше здесь стояли одни операции, и отмена начального остатка счёта,
    которую `undo` умеет, с экрана была недоступна: кнопки не было.

    Откат есть только у правок данных: вход, выгрузку или просмотр вернуть
    нечем, и кнопка у них соврала бы.
    """
    if (entry.category or "data") != "data":
        return False
    if entry.entity == "operation" and entry.entity_id:
        return True
    if entry.kind in ("account.balance", "account.number") and entry.entity == "account" and entry.entity_id:
        return not str(entry.title).startswith("отменено")
    if entry.kind == "autotag.apply":
        return "items" in (entry.after or {})
    return False


def _restore(operation: Operation, state: dict[str, Any]) -> None:
    for field in OPERATION_FIELDS:
        if field not in state:
            continue
        value = state[field]
        if field in ("paid_at", "accrued_at") and isinstance(value, str) and value:
            value = date.fromisoformat(value[:10])
        elif field == "deleted_at" and isinstance(value, str) and value:
            value = datetime.fromisoformat(value)
        elif field in ("amount", "rate") and value is not None:
            value = Decimal(str(value))
        elif field.endswith("_id") and isinstance(value, str) and value:
            value = uuid.UUID(value)
        setattr(operation, field, value)


def undo(
    session: Session, workspace: Workspace, entry_id: uuid.UUID, *, actor: str = ""
) -> dict[str, Any]:
    """Отменить действие, вернув состояние «до».

    Создание отменяется удалением (мягким — `deleted_at`), изменение и удаление
    — возвратом прежних полей. Пересчёт `amount_base` обязателен: без него
    отменённая правка суммы вернула бы сумму, но не её оценку в валюте
    компании, и отчёты показали бы третью цифру, которой не было никогда.
    """
    entry = session.get(ActionLog, entry_id)
    if entry is None or entry.workspace_id != workspace.id:
        raise FinanceError("Запись истории не найдена")
    if entry.undone_at is not None:
        raise FinanceError("Это действие уже отменено")
    if (entry.category or "data") != "data":
        raise FinanceError("Откатить можно только правку данных")
    if entry.entity == "account" and entry.kind == "account.balance" and entry.entity_id:
        # Начальный остаток меняет остаток счёта во всех отчётах задним числом —
        # ошибку в нём обязаны уметь вернуть так же, как правку операции.
        account = session.get(Account, entry.entity_id)
        if account is None or account.workspace_id != workspace.id:
            raise FinanceError("Счёта больше нет — отменять нечего")
        previous = (entry.before or {}).get("starting_balance")
        if previous is None:
            raise FinanceError("В записи нет состояния «до»")
        now = str(account.starting_balance)
        account.starting_balance = Decimal(str(previous))
        entry.undone_at = datetime.now(timezone.utc)
        session.flush()
        write(
            session, workspace, kind="account.balance", entity="account", entity_id=account.id,
            title=f"отменено: {entry.title}", before={"starting_balance": now},
            after={"starting_balance": str(previous)}, actor=actor,
        )
        return {"ok": True, "account_id": str(account.id)}
    if entry.entity == "account" and entry.kind == "account.number" and entry.entity_id:
        # Номер счёта решает, куда ляжет следующая выписка. Ошибочно
        # записанный номер обязан сниматься одной кнопкой, как и остаток.
        account = session.get(Account, entry.entity_id)
        if account is None or account.workspace_id != workspace.id:
            raise FinanceError("Счёта больше нет — отменять нечего")
        previous = str((entry.before or {}).get("number") or "")
        if previous and any(
            other.number == previous and other.id != account.id
            for other in session.scalars(
                sa.select(Account).where(Account.workspace_id == workspace.id, Account.archived_at.is_(None))
            )
        ):
            raise FinanceError(f"Номер {previous} теперь записан у другого счёта — вернуть его нельзя")
        now = account.number or ""
        account.number = previous
        entry.undone_at = datetime.now(timezone.utc)
        session.flush()
        write(
            session, workspace, kind="account.number", entity="account", entity_id=account.id,
            title=f"отменено: {entry.title}", before={"number": now},
            after={"number": previous}, actor=actor,
        )
        return {"ok": True, "account_id": str(account.id)}
    if entry.kind == "autotag.apply":
        # Авторазметка отменяется целиком: снимаем статью только там, где она
        # всё ещё та, что поставила разметка. Статью, которую человек потом
        # поправил руками, отмена не трогает — иначе она стёрла бы его работу.
        cleared = 0
        for operation_id, category_id in (entry.after or {}).get("items", []):
            operation = session.get(Operation, uuid.UUID(operation_id))
            if (
                operation is None
                or operation.workspace_id != workspace.id
                or str(operation.category_id) != category_id
            ):
                continue
            operation.category_id = None
            operation.version += 1
            cleared += 1
        entry.undone_at = datetime.now(timezone.utc)
        session.flush()
        write(
            session, workspace, kind="autotag.apply", entity="operations",
            title=f"отменено: {entry.title}",
            before={"operations": len((entry.after or {}).get("items", []))},
            after={"cleared": cleared},
            actor=actor,
        )
        return {"ok": True, "cleared": cleared}
    if entry.entity != "operation" or not entry.entity_id:
        raise FinanceError("Отмена пока умеет только операции и начальный остаток счёта")

    operation = session.get(Operation, entry.entity_id)
    if operation is None:
        raise FinanceError("Операции больше нет — отменять нечего")

    if entry.kind == "operation.create":
        operation.deleted_at = datetime.now(timezone.utc)
    else:
        if not entry.before:
            raise FinanceError("В записи нет состояния «до»")
        _restore(operation, entry.before)

    # Тем же правилом округления, что при записи (`check_money`): иначе
    # отменённая правка валютной операции возвращала бы сумму в валюте
    # компании на копейку другой.
    operation.amount_base = check_money(
        Decimal(str(operation.amount)) * Decimal(str(operation.rate)), field="Сумма в валюте компании"
    )
    operation.version += 1
    entry.undone_at = datetime.now(timezone.utc)
    session.flush()

    write(
        session,
        workspace,
        kind="operation.undo",
        entity="operation",
        entity_id=operation.id,
        title=f"отменено: {entry.title}",
        before=entry.after or {},
        after=snapshot(operation),
        actor=actor,
    )
    return {"ok": True, "operation_id": str(operation.id)}


__all__ = [
    "CONTEXT",
    "OPERATION_FIELDS",
    "TITLES",
    "AuditContext",
    "category_of",
    "current_context",
    "listing",
    "set_context",
    "snapshot",
    "undo",
    "write",
]
