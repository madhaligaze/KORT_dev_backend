"""Табличный вид журнала: операции как лист, правка ячейки как правка операции.

Зачем он нужен
──────────────
Урок раздела «Книги» (сентябрь 2026): человек, пришедший из Excel, приходит с
привычками - выделить диапазон и увидеть сумму, потянуть за угол, вставить
столбец из буфера, Ctrl+Z. Своя решётка не отвечала ни одной из них, и её
переписали на Univer. Здесь то же самое сразу: лист рисует Univer, а этот
модуль отвечает за перевод между листом и операциями.

Правило, которое здесь держится
───────────────────────────────
**Поверхностей две, поведение одно.** Ячейка «Сумма» разбирается тем же
`parse_money`, которым разбирается файл импорта, а дата - тем же `parse_date`.
Иначе через месяц «1 500,50» в таблице станет числом 150050, а в импорте
останется 1500.50, и объяснить это будет нечем.

Чего таблица намеренно не умеет
───────────────────────────────
Менять вид операции (доход ↔ расход) правкой ячейки. Вид определяет, какие
поля обязательны и какой знак у денег; смена вида - это переписывание строки, а
не правка ячейки, и делается в форме. В таблице колонка «Вид» показывается
только для чтения.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.layout import norm
from app.finance import service
from app.finance.importing import DateReading, parse_date, parse_money
from app.finance.models import Account, Category, Counterparty, Operation, Project, Workspace


@dataclass(frozen=True)
class GridColumn:
    key: str
    title: str
    kind: str  # date | money | text | enum | readonly
    width: int = 120
    editable: bool = True
    #: Для колонок-справочников: откуда брать список значений.
    source: str = ""


#: Состав колонок листа. Порядок - тот, в котором читают: сначала когда и
#: сколько, потом откуда/куда, потом смысл.
COLUMNS: tuple[GridColumn, ...] = (
    GridColumn("paid_at", "Дата платежа", "date", 110),
    GridColumn("kind_label", "Вид", "readonly", 90, editable=False),
    GridColumn("amount", "Сумма", "money", 130),
    GridColumn("account_from", "Со счёта", "enum", 175, source="accounts"),
    GridColumn("account_to", "На счёт", "enum", 175, source="accounts"),
    GridColumn("category", "Категория", "enum", 180, source="categories"),
    GridColumn("counterparty", "Контрагент", "enum", 210, source="counterparties"),
    GridColumn("project", "Проект", "enum", 180, source="projects"),
    GridColumn("accrued_at", "Дата сделки", "date", 110),
    GridColumn("comment", "Комментарий", "text", 260),
    GridColumn("status_label", "Состояние", "readonly", 110, editable=False),
)

KIND_LABELS = {"income": "Поступление", "expense": "Списание", "transfer": "Перевод"}
STATUS_LABELS = {"fact": "Факт", "plan": "План"}

#: Дата в ячейке разбирается как «день-месяц-год»: таблицу заполняет человек
#: руками, а не выгрузка из чужой системы, и порядок здесь известен заранее.
#: Про файлы - другое дело, там порядок решается по файлу (см. `importing`).
CELL_DATE_READING = DateReading("dmy", "ручной ввод в таблице")


def build_grid(
    session: Session, workspace: Workspace, operations: Sequence[Operation]
) -> dict[str, Any]:
    """Операции → лист: шапка, строки и справочники для выпадающих списков."""
    accounts = {account.id: account.name for account in service.list_accounts(session, workspace.id)}
    categories = {item.id: item.name for item in service.list_categories(session, workspace.id)}
    counterparties = {
        item.id: item.name for item in service.list_counterparties(session, workspace.id)
    }
    projects = {item.id: item.name for item in service.list_projects(session, workspace.id)}
    splits = service.operation_projects(session, [operation.id for operation in operations])

    rows: list[dict[str, Any]] = []
    for operation in operations:
        own_splits = splits.get(operation.id, [])
        rows.append(
            {
                "id": str(operation.id),
                "version": operation.version,
                "cells": {
                    "paid_at": operation.paid_at.isoformat(),
                    "kind_label": KIND_LABELS.get(operation.kind, operation.kind),
                    "amount": str(operation.amount),
                    "account_from": accounts.get(operation.account_from_id, ""),
                    "account_to": accounts.get(operation.account_to_id, ""),
                    "category": categories.get(operation.category_id, ""),
                    "counterparty": counterparties.get(operation.counterparty_id, ""),
                    # Разнесение на несколько проектов в одну ячейку не влезает:
                    # показываем первый и помечаем, что их больше. Правка такой
                    # ячейки затрагивает только разнесение целиком - см. ниже.
                    "project": (
                        projects.get(own_splits[0].project_id, "")
                        + (f" +{len(own_splits) - 1}" if len(own_splits) > 1 else "")
                        if own_splits
                        else ""
                    ),
                    "accrued_at": operation.accrued_at.isoformat() if operation.accrued_at else "",
                    "comment": operation.comment or "",
                    "status_label": STATUS_LABELS.get(operation.status, operation.status),
                },
                "kind": operation.kind,
                "status": operation.status,
                "split_count": len(own_splits),
            }
        )

    return {
        "columns": [
            {
                "key": column.key,
                "title": column.title,
                "kind": column.kind,
                "width": column.width,
                "editable": column.editable,
                "source": column.source,
            }
            for column in COLUMNS
        ],
        "rows": rows,
        "options": {
            "accounts": sorted(accounts.values()),
            "categories": sorted(categories.values()),
            "counterparties": sorted(counterparties.values()),
            "projects": sorted(projects.values()),
        },
    }


def row_of(session: Session, workspace: Workspace, operation: Operation) -> dict[str, Any]:
    """Одна строка листа - ровно в том виде, в каком её отдаёт `build_grid`.

    Лист после правки записывает эту строку обратно в свои ячейки. Без неё
    ему оставалось перечитывать журнал целиком, а перечитанный журнал
    отсортирован заново: операция с исправленной датой уезжала на другое
    место, строки на экране оставались прежними, и следующая правка уходила в
    соседнюю операцию.
    """
    return build_grid(session, workspace, [operation])["rows"][0]


def _resolve(session: Session, workspace: Workspace, model, name: str, **extra):
    """Запись справочника по тексту ячейки: точно, иначе единственная по началу.

    Выпадающий список Univer не дополняет набранное: «Кас» + Enter оставлял в
    ячейке «Кас», и сервер отвечал «Счёта «Кас» нет», хотя счёт «Касса» один
    и перепутать его не с чем. А при быстром наборе список перехватывает фокус
    на второй-третьей букве, и в ячейке остаётся «Ба» от «Банковский счёт».
    Человек из Excel печатает, а не щёлкает мышью по списку.

    Правило то же, что у заголовков колонок книг: нечёткое совпадение
    принимается, только если кандидат ровно один. «Ба» при двух счетах на
    «Ба…» - не угадывание, а отказ. И это не тихо: лист записывает ответ
    сервера обратно, и в ячейке появляется полное название.
    """
    clean = (name or "").strip()
    if not clean:
        return None
    found = service.find_entry(session, model, workspace.id, clean, **extra)
    if found is not None:
        return found
    key = norm(clean)
    if len(key) < 2:
        return None
    conditions = [
        model.workspace_id == workspace.id,
        model.normalized_name.startswith(key, autoescape=True),
        model.archived_at.is_(None),
    ]
    for field, value in extra.items():
        conditions.append(getattr(model, field) == value)
    candidates = session.scalars(sa.select(model).where(*conditions).limit(2)).all()
    return candidates[0] if len(candidates) == 1 else None


def _account(session: Session, workspace: Workspace, text: str) -> Account | None:
    """Счёт по тексту ячейки - или отказ со списком того, что есть.

    Счёт из таблицы не создаётся: место, где лежат деньги, не должно
    появляться из опечатки. Но и отказ «такого нет» без списка заставлял идти
    в справочник, чтобы узнать, как счёт называется.
    """
    account = _resolve(session, workspace, Account, text)
    if text.strip() and account is None:
        names = [item.name for item in service.list_accounts(session, workspace.id)]
        raise service.FinanceError(
            f"Счёта «{text.strip()}» нет. Есть: {', '.join(names)}. "
            "Новый счёт заводится в «Справочниках»"
        )
    return account


def apply_cell(
    session: Session,
    workspace: Workspace,
    operation_id: uuid.UUID,
    column_key: str,
    raw_value: Any,
    *,
    version: int | None = None,
    actor: str = "",
) -> Operation:
    """Правка одной ячейки листа.

    Значение разбирается теми же функциями, что и файл импорта. Нераспознанное
    значение - это отказ с объяснением, а не запись нуля: ноль в денежной
    колонке выглядит как «столько и было».
    """
    column = next((item for item in COLUMNS if item.key == column_key), None)
    if column is None:
        raise service.FinanceError(f"В таблице нет колонки «{column_key}»")
    if not column.editable:
        raise service.FinanceError(
            f"«{column.title}» правится в карточке операции, а не в ячейке: "
            "от этого поля зависит, какие другие поля обязательны"
        )

    operation = session.get(Operation, operation_id)
    if operation is None or operation.workspace_id != workspace.id:
        raise service.FinanceError("Операция не найдена")

    changes: dict[str, Any] = {}
    text = "" if raw_value is None else str(raw_value).strip()

    if column.kind == "date":
        if not text:
            if column_key == "paid_at":
                raise service.FinanceError("Дата платежа не может быть пустой")
            changes[column_key] = None
        else:
            parsed = parse_date(raw_value, CELL_DATE_READING)
            if parsed is None:
                raise service.FinanceError(f"Не понял дату «{text}». Пример: 03.09.2026")
            changes[column_key] = parsed
    elif column.kind == "money":
        money = parse_money(raw_value)
        if money is None:
            raise service.FinanceError(f"Не понял сумму «{text}»")
        if money.negative:
            raise service.FinanceError(
                "Минус в сумме не задаёт расход: вид операции меняется в карточке"
            )
        changes["amount"] = money.value
    elif column.kind == "text":
        changes[column_key] = text
    elif column.kind == "enum":
        if column_key in ("account_from", "account_to"):
            account = _account(session, workspace, text)
            changes[f"{column_key}_id"] = account.id if account else None
        elif column_key == "category":
            side = "income" if operation.kind == "income" else "expense"
            category = _resolve(session, workspace, Category, text, side=side)
            if text and category is None:
                category = service.ensure_category(session, workspace.id, side, text)
            changes["category_id"] = category.id if category else None
        elif column_key == "counterparty":
            role = "client" if operation.kind == "income" else "supplier"
            counterparty = _resolve(session, workspace, Counterparty, text, role=role)
            if text and counterparty is None:
                counterparty = service.ensure_counterparty(session, workspace.id, text, role=role)
            changes["counterparty_id"] = counterparty.id if counterparty else None
        elif column_key == "project":
            if not text:
                changes["projects"] = []
            else:
                project = _resolve(session, workspace, Project, text)
                if project is None:
                    project = service.ensure_project(session, workspace.id, text)
                changes["projects"] = [(project.id, Decimal(str(operation.amount)))]

    return service.update_operation(
        session, workspace, operation_id, changes, version=version, actor=actor
    )


def append_row(
    session: Session, workspace: Workspace, cells: dict[str, Any], *, actor: str = ""
) -> Operation:
    """Новая операция из строки, набранной внизу листа.

    Вид операции определяется по заполненным счетам - так же, как в импорте:
    оба счёта → перевод, только «На счёт» → поступление, только «Со счёта» →
    списание. Это избавляет от лишней колонки и повторяет привычку кассовой
    книги, где приход и расход различаются тем, в какую графу поставили сумму.
    """
    account_from = _account(session, workspace, str(cells.get("account_from") or ""))
    account_to = _account(session, workspace, str(cells.get("account_to") or ""))

    if account_from and account_to:
        kind = "transfer"
    elif account_to:
        kind = "income"
    elif account_from:
        kind = "expense"
    else:
        raise service.FinanceError(
            "Не понял, приход это или расход: заполните «Со счёта» или «На счёт»"
        )

    paid_at = parse_date(cells.get("paid_at"), CELL_DATE_READING)
    if paid_at is None:
        raise service.FinanceError("Без даты платежа строка не станет операцией")
    money = parse_money(cells.get("amount"))
    if money is None or not money.value:
        raise service.FinanceError("Без суммы строка не станет операцией")
    # Минус в сумме не выбрасываем молча. Человек из кассовой книги пишет
    # расход с минусом - и если счёт стоит в «Со счёта», минус с ним согласен.
    # Но минус при счёте в «На счёт» означал бы «пришло минус пять тысяч»:
    # взять модуль и завести поступление - ровно та тихая смена знака, за
    # которую разбор импорта критикует соседей по рынку.
    if money.negative and kind != "expense":
        raise service.FinanceError(
            "Сумма с минусом, а счёт указан в «На счёт» - это было бы поступление. "
            "Для расхода поставьте счёт в «Со счёта»"
            if kind == "income"
            else "У перевода сумма без минуса: направление задают «Со счёта» и «На счёт»"
        )

    side = "income" if kind == "income" else "expense"
    # Справочники кроме счетов по-прежнему пополняются из таблицы, но сначала
    # ищется существующая запись - в том числе по началу названия, иначе
    # «Арен» заводило бы вторую статью рядом с «Арендой» и делило отчёт.
    category = None
    if str(cells.get("category") or "").strip() and kind != "transfer":
        text = str(cells["category"])
        category = _resolve(session, workspace, Category, text, side=side) or service.ensure_category(
            session, workspace.id, side, text
        )
    counterparty = None
    if str(cells.get("counterparty") or "").strip():
        role = "client" if kind == "income" else "supplier"
        text = str(cells["counterparty"])
        counterparty = _resolve(session, workspace, Counterparty, text, role=role) or service.ensure_counterparty(
            session, workspace.id, text, role=role
        )
    projects: list[tuple[uuid.UUID, Decimal]] = []
    if str(cells.get("project") or "").strip():
        text = str(cells["project"])
        project = _resolve(session, workspace, Project, text) or service.ensure_project(session, workspace.id, text)
        if project is not None:
            projects.append((project.id, money.value))

    accrued = parse_date(cells.get("accrued_at"), CELL_DATE_READING)
    data = service.OperationInput(
        kind=kind,
        status="plan" if paid_at > date.today() else "fact",
        paid_at=paid_at,
        accrued_at=accrued,
        amount=money.value,
        account_from_id=account_from.id if account_from else None,
        account_to_id=account_to.id if account_to else None,
        category_id=category.id if category else None,
        counterparty_id=counterparty.id if counterparty else None,
        comment=str(cells.get("comment") or ""),
        projects=projects,
        source="grid",
    )
    return service.create_operation(session, workspace, data, actor=actor)


__all__ = ["COLUMNS", "GridColumn", "KIND_LABELS", "append_row", "apply_cell", "build_grid", "row_of"]
