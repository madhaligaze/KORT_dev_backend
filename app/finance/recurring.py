"""Повторяющиеся операции: аренда, зарплата, подписки.

Почему порождаются настоящие операции, а не рисуются на экране
──────────────────────────────────────────────────────────────
Соблазн был показывать повторения «виртуально»: правило есть, а операций нет,
экран их дорисовывает. Так короче кода и так нельзя. Повторение — это
обязательство: аренду можно заплатить раньше, можно пропустить месяц, можно
заплатить не ту сумму. Виртуальная строка ничего из этого не умеет: её не
поправить, не оплатить и не удалить, а в календаре она живёт по особым
правилам, отличным от остальных.

Поэтому правило создаёт **ожидания** (`status='plan'`) на горизонт вперёд. С
ними работают как с любыми другими: правят, оплачивают, удаляют.

Горизонт и `next_at`
────────────────────
`next_at` — дата следующего ещё не созданного повторения. Продление идёт от
неё, поэтому повторный вызов не создаёт дубликатов и не пропускает месяц. Это
важнее, чем кажется: продление вызывается и по кнопке, и при открытии раздела,
и из фоновой задачи — все три пути обязаны сходиться к одному результату.
"""
from __future__ import annotations

import calendar
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.models import (
    Account,
    Category,
    Counterparty,
    Operation,
    Project,
    Recurrence,
    Workspace,
)
from app.finance.service import FinanceError, OperationInput, create_operation

#: На сколько вперёд держим ожидания. Три месяца — это то, что видно в
#: календаре и в кассовом разрыве; дальше горизонт превращается в гадание.
HORIZON_DAYS = 100


def _shift(when: date, period: str, day: int) -> date:
    """Следующая дата повторения.

    Тридцать первое число в феврале — это последний день февраля, а не первое
    марта: «платим тридцать первого» означает «в конце месяца». Прыжок в
    следующий месяц сдвинул бы платёж и сломал бы месячные отчёты.
    """
    if period == "week":
        return when + timedelta(days=7)
    months = {"month": 1, "quarter": 3, "year": 12}[period]
    year = when.year + (when.month - 1 + months) // 12
    month = (when.month - 1 + months) % 12 + 1
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def clamp_day(day: Any) -> int:
    """Число месяца в пределах 1–31.

    Приведение живёт отдельной функцией, потому что раньше оно стояло только
    в одном из двух мест: в базу день писался приведённым, а первая дата
    считалась из сырого. День `-5` доходил до `date(год, месяц, -5)` и падал
    пятисотой. Одно значение — одно приведение.
    """
    try:
        return max(1, min(31, int(day)))
    except (TypeError, ValueError) as exc:
        raise FinanceError("Число месяца должно быть числом от 1 до 31") from exc


def first_date(start_at: date, period: str, day: int) -> date:
    if period == "week":
        return start_at
    safe = clamp_day(day)
    last = calendar.monthrange(start_at.year, start_at.month)[1]
    candidate = date(start_at.year, start_at.month, min(safe, last))
    return candidate if candidate >= start_at else _shift(candidate, period, safe)


def create(
    session: Session,
    workspace: Workspace,
    *,
    title: str,
    kind: str,
    amount: Decimal,
    period: str,
    day: int,
    start_at: date,
    until: date | None = None,
    account_from_id: uuid.UUID | None = None,
    account_to_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    counterparty_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    comment: str = "",
    actor: str = "",
) -> Recurrence:
    if period not in ("week", "month", "quarter", "year"):
        raise FinanceError("Период бывает недельным, месячным, квартальным или годовым")
    if kind not in ("income", "expense", "transfer"):
        raise FinanceError("Вид операции неизвестен")
    if Decimal(str(amount)) <= 0:
        raise FinanceError("Сумма повторения должна быть больше нуля")
    if until and until < start_at:
        raise FinanceError("Дата окончания раньше даты начала")

    recurrence = Recurrence(
        workspace_id=workspace.id,
        title=title.strip() or "Без названия",
        kind=kind,
        period=period,
        day=clamp_day(day),
        start_at=start_at,
        until=until,
        next_at=first_date(start_at, period, day),
        amount=Decimal(str(amount)),
        currency=workspace.base_currency,
        account_from_id=account_from_id,
        account_to_id=account_to_id,
        category_id=category_id,
        counterparty_id=counterparty_id,
        project_id=project_id,
        comment=comment.strip(),
        created_by=actor,
    )
    session.add(recurrence)
    session.flush()
    return recurrence


def materialize(
    session: Session, workspace: Workspace, *, horizon_days: int = HORIZON_DAYS, actor: str = ""
) -> int:
    """Создать ожидания по всем правилам до горизонта. Возвращает сколько создано.

    Идемпотентно: сколько раз ни вызови, столько же операций и будет. Это
    держится на `next_at`, а не на проверке «нет ли уже такой операции»:
    сравнение «такая же» на деньгах ненадёжно — две одинаковые аренды в один
    месяц бывают законно.
    """
    limit = date.today() + timedelta(days=horizon_days)
    created = 0
    rules = session.scalars(
        sa.select(Recurrence).where(
            Recurrence.workspace_id == workspace.id, Recurrence.active.is_(True)
        )
    ).all()
    for rule in rules:
        when = rule.next_at
        while when <= limit and (rule.until is None or when <= rule.until):
            create_operation(
                session,
                workspace,
                OperationInput(
                    kind=rule.kind,
                    status="plan",
                    paid_at=when,
                    amount=Decimal(str(rule.amount)),
                    account_from_id=rule.account_from_id,
                    account_to_id=rule.account_to_id,
                    category_id=rule.category_id,
                    counterparty_id=rule.counterparty_id,
                    comment=rule.comment or rule.title,
                    projects=((rule.project_id, Decimal(str(rule.amount))),) if rule.project_id else (),
                    source="recurrence",
                    recurrence_id=rule.id,
                ),
                actor=actor or rule.created_by,
            )
            created += 1
            when = _shift(when, rule.period, rule.day)
        rule.next_at = when
        if rule.until is not None and when > rule.until:
            rule.active = False
    session.flush()
    return created


def set_active(
    session: Session, workspace_id: uuid.UUID, recurrence_id: uuid.UUID, active: bool
) -> Recurrence:
    rule = session.get(Recurrence, recurrence_id)
    if rule is None or rule.workspace_id != workspace_id:
        raise FinanceError("Повторение не найдено")
    rule.active = active
    session.flush()
    return rule


def remove(
    session: Session, workspace_id: uuid.UUID, recurrence_id: uuid.UUID, *, with_future: bool = True
) -> int:
    """Удалить правило. По умолчанию — вместе с будущими неоплаченными ожиданиями.

    Уже оплаченное не трогаем никогда: это факт движения денег, и он не
    перестаёт быть фактом от того, что правило отменили.
    """
    rule = session.get(Recurrence, recurrence_id)
    if rule is None or rule.workspace_id != workspace_id:
        raise FinanceError("Повторение не найдено")
    removed = 0
    if with_future:
        rows = session.scalars(
            sa.select(Operation).where(
                Operation.recurrence_id == rule.id,
                Operation.status == "plan",
                Operation.paid_at >= date.today(),
                Operation.deleted_at.is_(None),
            )
        ).all()
        for operation in rows:
            session.delete(operation)
            removed += 1
    session.delete(rule)
    session.flush()
    return removed


def list_recurrences(session: Session, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        sa.select(Recurrence, Category.name, Counterparty.name, Project.name)
        .outerjoin(Category, Recurrence.category_id == Category.id)
        .outerjoin(Counterparty, Recurrence.counterparty_id == Counterparty.id)
        .outerjoin(Project, Recurrence.project_id == Project.id)
        .where(Recurrence.workspace_id == workspace_id)
        .order_by(Recurrence.active.desc(), Recurrence.next_at)
    )
    titles = {
        "week": "каждую неделю",
        "month": "каждый месяц",
        "quarter": "раз в квартал",
        "year": "раз в год",
    }
    out: list[dict[str, Any]] = []
    for rule, category, counterparty, project in rows:
        waiting = session.scalar(
            sa.select(sa.func.count())
            .select_from(Operation)
            .where(
                Operation.recurrence_id == rule.id,
                Operation.status == "plan",
                Operation.deleted_at.is_(None),
            )
        )
        out.append(
            {
                "id": str(rule.id),
                "title": rule.title,
                "active": rule.active,
                "kind": rule.kind,
                "period": rule.period,
                "period_title": titles.get(rule.period, rule.period),
                "day": rule.day,
                "amount": str(rule.amount),
                "next_at": rule.next_at.isoformat(),
                "until": rule.until.isoformat() if rule.until else None,
                "category": category or "",
                "counterparty": counterparty or "",
                "project": project or "",
                "comment": rule.comment,
                "waiting": int(waiting or 0),
            }
        )
    return out


__all__ = [
    "HORIZON_DAYS",
    "create",
    "first_date",
    "list_recurrences",
    "materialize",
    "remove",
    "set_active",
]
