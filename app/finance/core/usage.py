"""Какие разделы компании пусты (фаза 8, «виды»): пустое не занимает колонку.

Колонка «Финансов» выросла из Finmap: проекты, повторения, план/факт,
календарь - разделы для тех, кто ими пользуется. На копии прода половина из них пуста,
а занимает место, и нужное ищут глазами среди ненужного. Пустой раздел уходит
в «Ещё» и остаётся в поиске (Ctrl+K); закрепить его в колонке может каждый
для себя (личное хранилище `/finance/looks/nav`).

Пусто - значит данных нет вовсе, а не «мало»: раздел с одной операцией уже
чей-то рабочий. Считается одним запросом на раздел, только «есть ли хоть одна
строка» - без счёта всех строк.
"""
from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts.models import SummarySource
from app.finance.models import Invoice, Operation, OperationProject, Plan, Project, Recurrence, Rule, Workspace


def _any(session: Session, query: Any) -> bool:
    return session.scalar(sa.select(sa.exists(query))) is True


def empty_sections(session: Session, workspace: Workspace) -> list[str]:
    """Ключи разделов колонки, в которых у компании нет ни одной строки."""
    live = (Operation.workspace_id == workspace.id) & Operation.deleted_at.is_(None)
    operations = _any(session, sa.select(Operation.id).where(live))
    out: list[str] = []
    if not operations:
        out += ["journal", "table", "calendar", "cash", "balance", "indicators", "statement"]
    projects = _any(session, sa.select(Project.id).where(Project.workspace_id == workspace.id)) or _any(
        session,
        sa.select(OperationProject.id).join(Operation, Operation.id == OperationProject.operation_id).where(live),
    )
    if not projects:
        out.append("projects")
    plans = _any(session, sa.select(Plan.id).where(Plan.workspace_id == workspace.id)) or _any(
        session, sa.select(Operation.id).where(live, Operation.status == "plan")
    )
    if not plans:
        out.append("plan")
    if not _any(session, sa.select(Recurrence.id).where(Recurrence.workspace_id == workspace.id)):
        out.append("recurrences")
    if not _any(session, sa.select(Rule.id).where(Rule.workspace_id == workspace.id)):
        out.append("rules")
    from app.finance.contracts.models import Contract, GroupEntity
    from app.finance.core.settings import payments as payment_settings
    from app.finance.onec.models import OnecBase

    contracts = _any(session, sa.select(Contract.id).where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None)))
    summary = _any(session, sa.select(SummarySource.workspace_id).where(SummarySource.workspace_id == workspace.id))
    # Счета KORT идут от договоров: без сводной таблицы и журнала они есть, как только есть договор.
    if not contracts and not summary and not _any(session, sa.select(Invoice.id).where(Invoice.workspace_id == workspace.id)):
        out.append("invoices")
    # Вкладки появляются по ходу дела: пока договоров и денег нет, считать им нечего.
    if not contracts and not operations and not summary:
        out += ["cash-calendar", "debts", "vat", "profit"]
    # Сверять есть с чем, только если подключена таблица или 1С.
    if not summary and not _any(session, sa.select(OnecBase.id).where(OnecBase.workspace_id == workspace.id)):
        out.append("shadow")
    # Карта группы - отношения между нашими юрлицами: одному не с кем.
    own = session.scalar(
        sa.select(sa.func.count()).select_from(GroupEntity).where(GroupEntity.workspace_id == workspace.id, GroupEntity.archived_at.is_(None))
    ) or 0
    if own < 2:
        out.append("group")
    # «Первые шаги» нужны, пока компания не заведена: договоры есть и учёт оплат начат - в «Ещё».
    started = payment_settings(session, workspace).since is not None or operations
    if started and contracts:
        out.append("start")
    return out


__all__ = ["empty_sections"]
