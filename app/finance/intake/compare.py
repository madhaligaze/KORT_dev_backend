"""Сравнить - режим приёмника: таблица «договор за месяц» против KORT по всем звеньям.

До этого модуля сверка и переход (`core/shadow.py`, `core/switch.py`) умели одну
таблицу - сводную Google, подключённую компанией: её читал свой читатель
(`core/book.py`), и сверить с KORT таблицу, присланную файлом, - помесячные
листы отдела, выгрузку другой системы, - было нечем. Теперь сверка - режим
приёмника: лист, который приёмник узнал как «договор за месяц» (колонки по
названиям и словарю компании, запомненная разметка - `core/excel.read_file`),
становится той же книгой строк (`core/book.Book`), и сравнивают её те же
правила:

* начисления - с предложениями правок условий договора (`core/shadow`);
* оплаты - KORT против «поступило за месяц» таблицы (`core/payments.reconcile_paid`);
* АВР - 1С против таблицы (`core/acts`);
* счета к выставлению - KORT против списка таблицы (`core/billing.reconcile_bills`).

Сводная Google - один из источников этой сверки (`company_book`), а не её
устройство. Ничего не пишет: предложения применяются обычными правками договора
(`POST /core/shadow/apply`), с историей.
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.finance.contracts.service import Access
from app.finance.models import Workspace
from app.finance.service import FinanceError

#: Звенья сверки - как у «Перехода» (`core/switch.LINKS`), без сальдо: его сверяет перенос сальдо.
LINKS = ("accruals", "payments", "acts", "bills")
#: Колонки формы «договор за месяц», которых нет у книги строк, → её колонки.
_AS_BOOK = {"accrued": "amount", "sum": "amount", "opening": "opening_accruals", "debt": "closing_accruals"}
#: Части суммы месяца: аренда и коммунальные складываются в сумму строки.
_PARTS = ("fixed", "utilities")
_MONTHS = (
    "ЯНВАРЬ", "ФЕВРАЛЬ", "МАРТ", "АПРЕЛЬ", "МАЙ", "ИЮНЬ", "ИЮЛЬ", "АВГУСТ", "СЕНТЯБРЬ", "ОКТЯБРЬ", "НОЯБРЬ", "ДЕКАБРЬ",
)


class CompareError(FinanceError):
    """Таблицу не сравнить - словами: листа «договор за месяц» нет или в нём не хватает колонок."""


def _cell(value: Any) -> str:
    """Значение ячейки - строкой, как его читает книга строк (`book.money_of`, `book.date_of`)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return f"{value:%d.%m.%Y}"
    if isinstance(value, date):
        return f"{value:%d.%m.%Y}"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _money(value: Any) -> Decimal | None:
    from app.finance.core.book import money_of

    return money_of(_cell(value))


def table_book(
    session: Session, workspace: Workspace, data: bytes, file_name: str, *, today: date,
    entity: uuid.UUID | None = None, answers: dict[str, Any] | None = None,
) -> Any:
    """Листы файла формы «договор за месяц» → книга строк, как у сводной таблицы.

    Строки читает приёмник (колонки - по названиям, словарю и разметке
    компании); книга получает их под названиями своих колонок. Месяц строки -
    колонка месяца, иначе название листа («Июнь», «06.2026»). Нашего юрлица в
    листе нет - `entity` (у компании с одним юрлицом - оно само).
    """
    import sqlalchemy as sa

    from app.finance.contracts.models import GroupEntity
    from app.finance.core import book as book_module
    from app.finance.core import excel
    from app.finance.core import settings as core_settings

    # Книга находит колонки по написаниям; у колонки без написаний (их даёт только
    # словарь компании) написанием становится её подпись - так её зовёт и шапка ниже.
    columns = tuple(
        dataclasses.replace(column, names=column.names or (column.title,))
        for column in book_module.columns_for(core_settings.book_columns(session, workspace.id))
    )
    title = {column.key: column.names[0] for column in columns}
    sheets = [item for item in excel.read_file(session, workspace, data, file_name, answers) if item.form == "contract_month"]
    usable = [item for item in sheets if item.read is not None and item.read.reading is not None]
    if not usable:
        raise CompareError(f"В «{file_name}» нет листа, где строка - договор за месяц: сравнивать нечего")
    codes = dict(
        session.execute(
            sa.select(GroupEntity.counterparty_id, GroupEntity.code).where(
                GroupEntity.workspace_id == workspace.id, GroupEntity.archived_at.is_(None)
            )
        ).all()
    )
    if entity is None and len(codes) == 1:
        entity = next(iter(codes))
    own_code = codes.get(entity, "") if entity is not None else ""
    keys: list[str] = [key for key in title]
    header = [title[key] for key in keys]
    values: list[list[str]] = [header]
    for item in usable:
        month = excel._month_of(item.table.name, today)
        for row in item.read.rows:
            got: dict[str, Any] = {}
            for key, raw in row.values.items():
                target = _AS_BOOK.get(key, key)
                if target in title and raw not in (None, "") and target not in got:
                    got[target] = raw
            if "amount" not in got:
                parts = [_money(row.values.get(key)) for key in _PARTS if row.values.get(key) not in (None, "")]
                parts = [part for part in parts if part is not None]
                if parts:
                    got["amount"] = sum(parts, Decimal("0"))
            if "month" not in got and month is not None:
                got["month"] = f"{_MONTHS[month.month - 1]} {month.year}"
            if "own" not in got and own_code:
                got["own"] = own_code
            values.append([_cell(got.get(key)) for key in keys])
    try:
        return book_module.build_book(values, title=file_name, worksheet=", ".join(item.table.name for item in usable), columns=columns)
    except book_module.LayoutBroken as exc:
        raise CompareError(f"«{file_name}»: {exc}") from exc
    except book_module.BookError as exc:
        raise CompareError(str(exc)) from exc


def company_book(session: Session, workspace_id: uuid.UUID) -> tuple[Any, str]:
    """Сводная таблица компании (Google) - книга строк и отказ чтения; `(None, "")` - не подключена."""
    from app.finance.core import book as book_module

    return book_module.current(session, workspace_id, wait=book_module.FIRST_READ_WAIT)


def compare(
    session: Session, workspace: Workspace, access: Access, book: Any, *, today: date,
    links: tuple[str, ...] = LINKS, entity: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Сверка книги строк с KORT по звеньям - те же правила, что у экранов сверки и «Перехода»."""
    from app.finance.core import acts as acts_module
    from app.finance.core import billing, shadow
    from app.finance.core import payments as payments_module
    from app.finance.core.accruals import month_key

    if not access.setup:
        raise PermissionError("Сверку с таблицей ведёт владелец или администратор: в ней деньги всех договоров")
    wanted = [link for link in LINKS if link in links]
    out: dict[str, Any] = {
        "book": {"title": book.title, "worksheet": book.worksheet, "rows": len(book.rows), "read_at": book.read_at.isoformat()},
        "links": wanted,
    }
    if "accruals" in wanted:
        reconciler = shadow.Reconciler(session, workspace, access, book)
        found = reconciler.run()
        if entity is not None:
            found = found.of_entity(entity, reconciler.codes.get(entity, ""))
        out["accruals"] = reconciler.out(found, money=True)
    if "payments" in wanted:
        out["payments"] = payments_module.reconcile_paid(session, workspace, access, book, today=today, entity=entity)
    if "acts" in wanted:
        acts = acts_module.Acts(session, workspace, access, book, today=today)
        contracts = {item.id: item for item in shadow._visible(session, workspace, access)}
        current = month_key(today)
        window = [month for month in (acts.reconciler.window if acts.reconciler else []) if month < current]
        out["acts"] = acts.reconcile(contracts, window=window, entity=entity)
    if "bills" in wanted:
        out["bills"] = billing.reconcile_bills(session, workspace, access, book, today=today, entity=entity)
    return out


__all__ = ["LINKS", "CompareError", "company_book", "compare", "table_book"]
