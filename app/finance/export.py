"""Журнал в файл Excel — дорога из раздела обратно в таблицы.

Зачем
─────
Финансист, который годами ведёт учёт в Excel, первым делом спрашивает не «что
вы умеете», а «как мне это забрать к себе». Раздел умел только принимать:
выписки, книги, CSV. Отдать журнал было нельзя ничем — ни из журнала, ни из
табличного вида. У Finmap выгрузка есть, у нас не было; в этом месте мы
отставали, а не превосходили.

Как устроен файл
────────────────
Ровно то, что открывают в Excel и начинают считать, не переделывая:

* дата — настоящая дата, сумма — настоящее число с разрядами, а не текст;
* приход, расход и перевод — три колонки, как в кассовой книге. Перевод
  отдельно, потому что для компании он ничего не меняет: `СУММ(Приход) −
  СУММ(Расход)` сходится с отчётом «Деньги» за тот же период;
* шапка закреплена, на ней фильтр; под таблицей — `ПРОМЕЖУТОЧНЫЕ.ИТОГИ`,
  которые пересчитываются по видимым строкам. Отфильтровал «Факт» — видишь
  факт, отфильтровал статью — видишь статью.

Фильтры те же, что у журнала на экране: что видно, то и выгружено.
"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import Sequence
from uuid import UUID

import xlsxwriter
from sqlalchemy.orm import Session

from app.finance import service
from app.finance.models import Operation, Workspace

#: Потолок строк одной выгрузки. Выше — это уже не «забрать к себе», а
#: резервная копия, и делается она не через браузер.
EXPORT_MAX_ROWS = 100_000

KIND_LABELS = {"income": "Поступление", "expense": "Списание", "transfer": "Перевод"}
STATUS_LABELS = {"fact": "Факт", "plan": "План"}

HEADER = (
    ("Дата платежа", 13),
    ("Вид", 13),
    ("Приход", 15),
    ("Расход", 15),
    ("Перевод", 15),
    ("Со счёта", 20),
    ("На счёт", 20),
    ("Категория", 22),
    ("Контрагент", 24),
    ("Проект", 18),
    ("Дата сделки", 13),
    ("Комментарий", 48),
    ("Состояние", 11),
)


def journal_xlsx(
    session: Session,
    workspace: Workspace,
    flt: service.OperationFilter,
) -> tuple[bytes, int]:
    """Собрать файл. Возвращает байты и сколько операций в нём."""
    operations, total = service.list_operations(
        session, workspace.id, flt, limit=EXPORT_MAX_ROWS, offset=0
    )
    if total > EXPORT_MAX_ROWS:
        raise service.FinanceError(
            f"Под фильтр попало {total} операций — потолок выгрузки {EXPORT_MAX_ROWS}. "
            "Сузьте период"
        )
    names = {
        "accounts": {a.id: a.name for a in service.list_accounts(session, workspace.id, with_archived=True)},
        "categories": {c.id: c.name for c in service.list_categories(session, workspace.id)},
        "counterparties": {c.id: c.name for c in service.list_counterparties(session, workspace.id)},
        "projects": {p.id: p.name for p in service.list_projects(session, workspace.id)},
    }
    splits = service.operation_projects(session, [operation.id for operation in operations])
    return _write(operations, names, splits), len(operations)


def _write(
    operations: Sequence[Operation],
    names: dict[str, dict[UUID, str]],
    splits: dict[UUID, list],
) -> bytes:
    buffer = io.BytesIO()
    book = xlsxwriter.Workbook(buffer, {"in_memory": True, "strings_to_formulas": False})
    sheet = book.add_worksheet("Журнал")

    head = book.add_format({"bold": True, "bg_color": "#F1F3F5", "bottom": 1, "valign": "vcenter"})
    day = book.add_format({"num_format": "dd.mm.yyyy"})
    money = book.add_format({"num_format": "# ##0.00"})
    total_label = book.add_format({"bold": True, "top": 1})
    total_money = book.add_format({"bold": True, "top": 1, "num_format": "# ##0.00"})

    for column, (title, width) in enumerate(HEADER):
        sheet.write(0, column, title, head)
        sheet.set_column(column, column, width)

    for index, operation in enumerate(operations, start=1):
        amount = float(Decimal(str(operation.amount_base)))
        own = splits.get(operation.id, [])
        project = ", ".join(names["projects"].get(item.project_id, "") for item in own if item.project_id)
        sheet.write_datetime(index, 0, _as_datetime(operation.paid_at), day)
        sheet.write_string(index, 1, KIND_LABELS.get(operation.kind, operation.kind))
        column = {"income": 2, "expense": 3, "transfer": 4}.get(operation.kind)
        if column is not None:
            sheet.write_number(index, column, amount, money)
        sheet.write_string(index, 5, names["accounts"].get(operation.account_from_id, ""))
        sheet.write_string(index, 6, names["accounts"].get(operation.account_to_id, ""))
        sheet.write_string(index, 7, names["categories"].get(operation.category_id, ""))
        sheet.write_string(index, 8, names["counterparties"].get(operation.counterparty_id, ""))
        sheet.write_string(index, 9, project)
        if operation.accrued_at:
            sheet.write_datetime(index, 10, _as_datetime(operation.accrued_at), day)
        # Комментарий — строкой, даже если начинается с «=»: иначе банковское
        # назначение платежа вида «=Оплата» стало бы формулой в чужом Excel.
        sheet.write_string(index, 11, operation.comment or "")
        sheet.write_string(index, 12, STATUS_LABELS.get(operation.status, operation.status))

    last = len(operations)
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, max(last, 1), len(HEADER) - 1)

    # Итоги под таблицей — по видимым строкам (функция 109 не считает скрытые
    # фильтром). Строка через одну пустую, чтобы автофильтр её не захватил.
    summary = last + 2
    sheet.write_string(summary, 1, "Итого по видимым", total_label)
    # Значение считаем и сами: просмотрщики без пересчёта (превью почты,
    # телефон) показали бы на месте формулы ноль.
    totals = {2: 0.0, 3: 0.0, 4: 0.0}
    for operation in operations:
        column = {"income": 2, "expense": 3, "transfer": 4}.get(operation.kind)
        if column is not None:
            totals[column] += float(Decimal(str(operation.amount_base)))
    for column in (2, 3, 4):
        letter = chr(ord("A") + column)
        sheet.write_formula(
            summary, column, f"=SUBTOTAL(109,{letter}2:{letter}{last + 1})", total_money,
            round(totals[column], 2),
        )
    book.close()
    return buffer.getvalue()


def _as_datetime(value: date):
    from datetime import datetime

    return datetime(value.year, value.month, value.day)


__all__ = ["EXPORT_MAX_ROWS", "journal_xlsx"]
