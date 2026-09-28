"""Клиент Google для переноса книг в учёт — только значения, без оформления.

Жил в разделе «Таблицы» (`app.webexcel.google`) и переехал сюда, когда «Таблицы»
отказались от сервисного аккаунта: раздел открыт без входа, и читать чужие
книги от имени программы он больше не должен. Здесь остались ровно три вызова,
которыми пользуются «Финансы»: список книг, вкладки книги и значения вкладки.

Права — только чтение (`spreadsheets.readonly`): учёт книгу не правит, правка
живёт в учёте, а книга остаётся источником.
"""
from __future__ import annotations

import json
import threading
import time
from functools import lru_cache
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from app.finance.config import finance_settings

#: `drive.readonly` нужен, чтобы ПЕРЕЧИСЛИТЬ книги: человек выбирает книгу по
#: названию, а не вбивает id. Запись закрыта на уровне токена.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

#: Потолок колонок вкладки. Строки ограничивает `FINANCE_IMPORT_MAX_ROWS` — тот
#: же, что у загруженного файла: одна книга не должна читаться по-разному в
#: зависимости от того, скачали её или открыли.
MAX_COLS = 60

_CACHE_TTL_SECONDS = 600.0
_MAX_ENTRIES = 32


class GoogleError(Exception):
    """Отказ конфигурации / сети / Google с текстом для человека."""


def humanize(exc: Exception) -> str:
    text = str(exc)
    if "429" in text or "Quota exceeded" in text or "RATE_LIMIT" in text:
        return (
            "Google временно ограничил чтение — слишком много обращений подряд. "
            "Это проходит само за минуту"
        )
    if "403" in text and "PERMISSION" in text.upper():
        return "У сервисного аккаунта нет доступа к этой книге — её нужно открыть ему"
    if "404" in text:
        return "Книга не найдена — проверьте ссылку или id"
    return text


def is_configured() -> bool:
    return bool(finance_settings.credentials_available)


def _load_credentials() -> Credentials:
    raw = (finance_settings.service_account_json or "").strip()
    if not raw:
        raise GoogleError("Не заданы креды Google (FINANCE_SERVICE_ACCOUNT_JSON)")
    try:
        if raw.startswith("{"):
            return Credentials.from_service_account_info(json.loads(raw), scopes=GOOGLE_SCOPES)
        path = finance_settings.credentials_path
        if path is None or not path.is_file():
            raise GoogleError(f"Файл кредов Google не найден: {path or raw}")
        return Credentials.from_service_account_file(str(path), scopes=GOOGLE_SCOPES)
    except (ValueError, OSError) as exc:
        raise GoogleError(f"Некорректные креды Google: {exc}") from exc


@lru_cache(maxsize=1)
def _client() -> gspread.Client:
    return gspread.authorize(_load_credentials())


# ── Кэш ─────────────────────────────────────────────────────────────────────
#
# Квота Google — 60 чтений в минуту на сервисный аккаунт. Повторное открытие той
# же вкладки в мастере переноса не должно идти в Google заново. Кэш ограничен и
# по сроку, и по числу записей: просроченное выбрасывается при каждой записи, а
# не лежит до перезапуска.

_files_cache: tuple[float, list[dict[str, Any]]] | None = None
_meta_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_values_cache: dict[tuple[str, str], tuple[float, list[list[str]]]] = {}
_lock = threading.Lock()


def _fresh(stamp: float) -> bool:
    return (time.monotonic() - stamp) < _CACHE_TTL_SECONDS


def _remember(cache: dict[Any, tuple[float, Any]], key: Any, value: Any) -> None:
    """Положить в кэш, выбросив просроченное и самое старое сверх потолка. Под `_lock`."""
    now = time.monotonic()
    for stale in [k for k, (stamp, _) in cache.items() if now - stamp >= _CACHE_TTL_SECONDS]:
        del cache[stale]
    cache.pop(key, None)  # переложить в конец: порядок словаря — порядок записи
    cache[key] = (now, value)
    while len(cache) > _MAX_ENTRIES:
        cache.pop(next(iter(cache)))


def invalidate_cache() -> None:
    global _files_cache
    with _lock:
        _files_cache = None
        _meta_cache.clear()
        _values_cache.clear()


def list_spreadsheets() -> list[dict[str, Any]]:
    """Все таблицы, открытые сервисному аккаунту."""
    global _files_cache
    with _lock:
        if _files_cache is not None and _fresh(_files_cache[0]):
            return _files_cache[1]

    try:
        raw = _client().list_spreadsheet_files()
    except GoogleError:
        raise
    except Exception as exc:  # noqa: BLE001 — gspread бросает много типов
        raise GoogleError(humanize(exc)) from exc

    files = [
        {
            "id": item.get("id", ""),
            "name": item.get("name", "") or "Без названия",
            "modified": item.get("modifiedTime", ""),
        }
        for item in raw
        if item.get("id")
    ]
    files.sort(key=lambda item: item["name"].lower())

    with _lock:
        _files_cache = (time.monotonic(), files)
    return files


def _open(spreadsheet_id: str) -> gspread.Spreadsheet:
    try:
        return _client().open_by_key(spreadsheet_id)
    except GoogleError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GoogleError(humanize(exc)) from exc


def spreadsheet_meta(spreadsheet_id: str) -> dict[str, Any]:
    """Название книги и её вкладки — без грида, один дешёвый запрос."""
    with _lock:
        hit = _meta_cache.get(spreadsheet_id)
        if hit and _fresh(hit[0]):
            return hit[1]

    spreadsheet = _open(spreadsheet_id)
    try:
        raw = spreadsheet.fetch_sheet_metadata(
            params={"fields": "properties.title,sheets.properties"}
        )
    except Exception as exc:  # noqa: BLE001
        raise GoogleError(humanize(exc)) from exc

    tabs = []
    for sheet in raw.get("sheets", []):
        props = sheet.get("properties", {})
        grid = props.get("gridProperties", {})
        tabs.append(
            {
                "sheet_id": props.get("sheetId", 0),
                "title": props.get("title", ""),
                "index": props.get("index", 0),
                "hidden": bool(props.get("hidden")),
                "rows": grid.get("rowCount", 0),
                "cols": grid.get("columnCount", 0),
            }
        )

    meta = {
        "id": spreadsheet_id,
        "title": raw.get("properties", {}).get("title", ""),
        "tabs": tabs,
    }
    with _lock:
        _remember(_meta_cache, spreadsheet_id, meta)
    return meta


def _a1_col(index_zero_based: int) -> str:
    """0→A, 25→Z, 26→AA."""
    letters = ""
    n = index_zero_based + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _quote_tab(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def fetch_tab_values(spreadsheet_id: str, tab_title: str, *, fresh: bool = False) -> list[list[str]]:
    """Значения вкладки строками — как их видит человек.

    Запрашиваются **форматированные** значения: «18.09.2026» и «95 323,00», а не
    46 271 и 95323. Разбор в «Финансах» читает человеческий текст — он для того и
    написан, чтобы понимать выписки. Сырые значения пришлось бы переводить
    обратно через эпоху дат, и на этом переводе теряется день.

    `fresh` — мимо кэша: у сводки оплат свой срок (`contracts/summary.py`), и
    десятиминутный кэш мастера переноса держал бы её оплаты дольше него.
    """
    key = (spreadsheet_id, tab_title)
    with _lock:
        hit = _values_cache.get(key)
        if hit and _fresh(hit[0]) and not fresh:
            return hit[1]

    meta = spreadsheet_meta(spreadsheet_id)
    tab = next((t for t in meta["tabs"] if t["title"] == tab_title), None)
    if tab is None:
        known = ", ".join(f"«{t['title']}»" for t in meta["tabs"]) or "ни одной"
        raise GoogleError(f"Вкладка «{tab_title}» не найдена. Есть: {known}")

    rows = max(1, min(int(tab["rows"] or 1), finance_settings.import_max_rows))
    cols = max(1, min(int(tab["cols"] or 1), MAX_COLS))
    a1 = f"{_quote_tab(tab_title)}!A1:{_a1_col(cols - 1)}{rows}"

    spreadsheet = _open(spreadsheet_id)
    try:
        raw = spreadsheet.values_get(
            a1,
            params={
                "valueRenderOption": "FORMATTED_VALUE",
                "dateTimeRenderOption": "FORMATTED_STRING",
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise GoogleError(humanize(exc)) from exc

    values = [[str(cell) for cell in row] for row in raw.get("values", [])]
    with _lock:
        _remember(_values_cache, key, values)
    return values


__all__ = [
    "GoogleError",
    "fetch_tab_values",
    "humanize",
    "invalidate_cache",
    "is_configured",
    "list_spreadsheets",
    "spreadsheet_meta",
]
