"""Слой компании для тестов: шапка таблиц первой компании - её словарём колонок.

Тесты сводной, «Счетов», таблиц юрлиц и шаблона читают таблицы той формы,
что у компании на копии прода. С п.9 «Пакеты» эту шапку знает не код, а
словарь компании (`core_settings.book_columns`); названия - те же, что
миграция 0043 записала компаниям со сводной таблицей (берутся из неё, чтобы
второй копии не было).
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Any, Sequence

from app.finance.core.columns import with_company_names

_MIGRATION = Path(__file__).resolve().parents[1] / "app" / "migrations" / "versions" / "0043_finance_packs.py"
_spec = importlib.util.spec_from_file_location("migration_0043", _MIGRATION)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

#: Ключ колонки ядра → названия у компании (первое - подпись колонки).
COLUMNS: dict[str, list[str]] = {key: list(names) for key, names in _module.COMPANY_COLUMNS.items()}


def columns(reader: Sequence[Any], keys: dict[str, str] | None = None) -> tuple[Any, ...]:
    """Колонки читателя с названиями компании."""
    return with_company_names(reader, COLUMNS, keys)


def apply(session: Any, workspace_id: uuid.UUID) -> None:
    """Записать компании её шапку - как миграция 0043."""
    from app.finance.core.models import CoreSettings

    row = session.get(CoreSettings, workspace_id)
    if row is None:
        row = CoreSettings(workspace_id=workspace_id, data={})
        session.add(row)
    data = dict(row.data or {})
    data["book_columns"] = {key: list(names) for key, names in COLUMNS.items()}
    row.data = data
    session.flush()


def everywhere(monkeypatch: Any) -> None:
    """Компания теста - со шапкой копии прода: словарь колонок и читатели таблиц её знают.

    Для тестов, которые моделируют компанию на копии прода (таблицы её формы).
    Тест новой компании этого не делает - у неё словаря нет.
    """
    from functools import partial

    from app.finance.contracts import bill_book, bill_template, bills, summary
    from app.finance.core import book
    from app.finance.core import settings as core_settings

    stored = core_settings.book_columns

    def book_columns(session: Any, workspace_id: uuid.UUID) -> dict[str, list[str]]:
        own = stored(session, workspace_id)
        return {key: [*own.get(key, []), *(name for name in names if name not in own.get(key, []))]
                for key, names in {**COLUMNS, **own}.items()}

    monkeypatch.setattr(core_settings, "book_columns", book_columns)
    monkeypatch.setattr(book, "build_book", partial(book.build_book, columns=book.columns_for(COLUMNS)))
    monkeypatch.setattr(summary, "build_index", partial(summary.build_index, columns=summary.columns_for(COLUMNS)))
    monkeypatch.setattr(bills, "build_board", partial(bills.build_board, columns=bills.columns_for(COLUMNS)))
    discover, build, parse = bill_book.discover, bill_template.build, bill_template.parse
    monkeypatch.setattr(
        bill_book, "discover", lambda api, sid, tab, words=(): discover(api, sid, tab, words or bill_book.words_key(COLUMNS))
    )
    monkeypatch.setattr(
        bill_template, "build", lambda items, kind_of=lambda bill: None, words=None: build(items, kind_of, words or COLUMNS)
    )
    monkeypatch.setattr(bill_template, "parse", lambda data, board, words=None: parse(data, board, words or COLUMNS))
