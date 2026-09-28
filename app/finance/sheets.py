"""Мост к книгам Google Sheets.

Зачем он нужен
──────────────
Учёт у людей уже ведётся — в книгах Google, руками, годами. Любая программа
учёта предлагает им начать заново: «выгрузите в Excel, загрузите к нам». Дорога
получается в один конец, и первые же полгода в книге остаются, а новые данные
живут отдельно.

Здесь книга открывается как есть: список книг, доступных сервисному аккаунту,
вкладка на выбор, и строки вкладки проходят **тот же** разбор, что и файл
выписки — колонки ищутся по названиям, даты решаются по всему листу сразу,
испорченные строки откладываются с объяснением, остальные заводятся.

Почему чтение, а не правка книги
────────────────────────────────
Права сервисного аккаунта — только чтение (`spreadsheets.readonly` в
`app.finance.google`). Правка живёт в учёте, а книга остаётся источником.
Человеку это сказано прямо, а не спрятано в отказ при записи.

Клиент Google свой (`app.finance.google`). Раньше он был общим с разделом
«Таблицы», но «Таблицы» от сервисного аккаунта отказались, и делить квоту
больше не с кем.
"""
from __future__ import annotations

from typing import Any, Callable, Protocol

from app.finance.importing import Preview, analyze_rows

#: Чем читаются строки вкладки. Подменяется в тестах: разбор книги проверяется
#: без сети и без кредов, иначе тест на разбор зависел бы от чужого доступа.
Reader = Callable[[str, str], list[list[Any]]]


class SheetsError(Exception):
    """Отказ Google или настройки — с текстом для человека."""


class _GoogleModule(Protocol):  # только то, чем пользуемся
    def list_spreadsheets(self) -> list[dict[str, Any]]: ...
    def spreadsheet_meta(self, spreadsheet_id: str) -> dict[str, Any]: ...
    def fetch_tab_values(self, spreadsheet_id: str, tab_title: str) -> list[list[Any]]: ...


def _google() -> Any:
    """Клиент Google подтягивается лениво.

    Без этого модуль нельзя было бы импортировать там, где нет ни gspread, ни
    кредов — а «нет кредов» обязано быть пустым экраном с объяснением, а не
    падением всего раздела на старте.
    """
    import app.finance.google as google

    return google


def is_configured() -> bool:
    """Есть ли у программы доступ к Google вообще."""
    try:
        from app.finance.config import finance_settings

        return bool(finance_settings.credentials_available)
    except Exception:  # noqa: BLE001 — сломанные настройки тоже «не настроено»
        return False


def books(*, google: Any | None = None) -> list[dict[str, Any]]:
    """Книги, открытые сервисному аккаунту."""
    api = google or _google()
    try:
        return api.list_spreadsheets()
    except Exception as exc:  # noqa: BLE001
        raise SheetsError(str(exc)) from exc


def tabs(book_id: str, *, google: Any | None = None) -> dict[str, Any]:
    """Название книги и её вкладки."""
    api = google or _google()
    try:
        meta = api.spreadsheet_meta(book_id)
    except Exception as exc:  # noqa: BLE001
        raise SheetsError(str(exc)) from exc
    return {
        "id": meta.get("id", book_id),
        "title": meta.get("title", ""),
        "url": book_url(book_id),
        "tabs": [tab for tab in meta.get("tabs", []) if not tab.get("hidden")],
    }


def book_url(book_id: str, sheet_id: int | None = None) -> str:
    """Ссылка на саму книгу: работа с ней в Google не запрещается, а дополняется."""
    base = f"https://docs.google.com/spreadsheets/d/{book_id}/edit"
    return f"{base}#gid={sheet_id}" if sheet_id is not None else base


def read_tab(book_id: str, tab_title: str, *, reader: Reader | None = None) -> list[list[Any]]:
    read = reader or (lambda book, tab: _google().fetch_tab_values(book, tab))
    try:
        return read(book_id, tab_title)
    except Exception as exc:  # noqa: BLE001
        raise SheetsError(str(exc)) from exc


def preview_tab(
    book_id: str,
    tab_title: str,
    known_accounts: list[str],
    *,
    date_order: str | None = None,
    default_account: str | None = None,
    reader: Reader | None = None,
    account_numbers: dict[str, str] | None = None,
) -> Preview:
    """Разобрать вкладку книги так же, как разбирается загруженный файл.

    Ключевое слово — «так же». Отдельный разбор для Google звучал бы проще, но
    он разъехался бы с файловым на первой правке, и одна и та же книга читалась
    бы по-разному в зависимости от того, скачали её или открыли.
    """
    rows = read_tab(book_id, tab_title, reader=reader)
    if not any(any(str(cell).strip() for cell in row) for row in rows):
        raise SheetsError(f"Вкладка «{tab_title}» пустая — переносить нечего")
    return analyze_rows(
        rows,
        f"{tab_title} · Google Таблицы",
        known_accounts,
        date_order=date_order,
        default_account=default_account,
        account_numbers=account_numbers,
    )


__all__ = [
    "Reader",
    "SheetsError",
    "book_url",
    "books",
    "is_configured",
    "preview_tab",
    "read_tab",
    "tabs",
]
