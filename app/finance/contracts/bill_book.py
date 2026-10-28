"""Карточка счёта: строка таблицы юрлица, где счёт отмечают, - чтение и запись.

Где живёт отметка
─────────────────
Сводную таблицу компании собирает формула из таблиц её юрлиц (`IMPORTRANGE`).
В каждой таблице юрлица своя вкладка-сводка - тоже формула: она складывает
помесячные вкладки, данные с 4-й строки, шапка - во 2-й. Руками вводят только
в помесячные вкладки - туда KORT и пишет. Какие таблицы и вкладки, KORT берёт
из самих формул (`IMPORTRANGE` сводной и `{'Июнь'!$A4:$BF; …}` таблицы
юрлица), а не из списка в коде: добавят «Ноябрь» в формулу - KORT увидит его
сам. Колонки - ключи ядра (`core/columns.py`) под названиями из словаря
колонок компании: шапку её таблиц знает компания, а не код.

Что пишется
───────────
Отметку «счёт выставлен» таблица считает сама: `ARRAYFORMULA` в 3-й строке
ставит TRUE каждой строке, где заполнен номер счёта (так во всех таблицах на
22.10.2026). Записать TRUE в отметку - значит сломать эту формулу на всю
колонку. Поэтому KORT пишет туда же, куда человек: номер счёта, дата счёта,
«счёт за период», комментарий. Снять отметку - стереть номер счёта.

Почему так осторожно
────────────────────
Таблицы ведут руками, и это деньги. Правила записи:
* Строка - по ключу (юрлицо, клиент, № договора, месяц), а не по номеру:
  строки вставляют и сортируют. Совпадение должно быть ровно одно, иначе отказ.
* Колонки - по точным названиям шапки. Над ними в шапке не должно быть формулы
  (`ARRAYFORMULA` залила бы колонку), а в самой ячейке - ни формулы, ни
  разлива чужого массива.
* Пишется только изменённое поле и только если в таблице оно всё ещё такое,
  каким его показала карточка: иначе таблицу правили параллельно, и KORT не
  затирает чужую правку, а отказывает.
* Запись - одним `batchUpdate`: в таблице либо всё, либо ничего.
* После записи строка читается заново: ключ на месте, значения записались,
  формулы 3-й строки целы. Не так - KORT возвращает прежние значения и говорит,
  что случилось.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, Sequence

from app.finance import google
from app.finance.contracts.bills import bill_key
from app.finance.core.columns import column, with_company_names
from app.finance.layout import Column, LayoutError, norm, resolve_layout

log = logging.getLogger(__name__)

#: Сколько держатся найденные книги, вкладки и указатель «ключ → книга»:
#: формулы меняют раз в месяц, а строку перед записью KORT ищет заново.
SOURCES_TTL = 1800.0
#: Шапка помесячной вкладки - 2-я строка, данные - с 4-й.
HEADER_ROW = 2
FIRST_ROW = 4
#: Строки над данными, где не должно быть формулы над нашими колонками.
TOP_ROWS = 3
#: Ширина вкладки, которую собирает «Сводка все» (`$A4:$BF`).
LAST_COL = "BF"
#: Отсчёт дат Google Таблиц.
EPOCH = date(1899, 12, 30)

#: Поля карточки, которые KORT пишет в книгу.
FIELDS = ("bill_no", "bill_date", "bill_period", "note")

#: Колонки помесячной вкладки - ключи ядра, только точные названия: сюда пишут.
#: «bill» - отметка «счёт выставлен» (в ядре `bill_state`).
TAB_COLUMNS: tuple[Column, ...] = (
    column("own", required=True),
    column("customer", required=True),
    column("number", required=True),
    column("month", required=True),
    column("bill", core="bill_state", required=True),
    column("bill_period", required=True),
    column("bill_no", required=True),
    column("bill_date", required=True),
    column("note", required=True),
)
#: Ключ колонки здесь → ключ ядра в словаре колонок компании, где они разные.
CORE_KEYS = {"bill": "bill_state"}
KEY_FIELDS = ("own", "customer", "number", "month")
#: Подписи полей карточки общими словами; у компании - её названия (`Book.titles`).
TITLES = {column.key: column.title for column in TAB_COLUMNS if column.key in ("bill_no", "bill_date", "bill_period", "note")}


def words_key(extra: dict[str, list[str]] | None) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Словарь колонок компании - частью ключа сводной: другой словарь - таблицы читаются заново."""
    return tuple(sorted((key, tuple(names)) for key, names in (extra or {}).items()))


def tab_columns(words: Sequence[tuple[str, Sequence[str]]] = ()) -> tuple[Column, ...]:
    return with_company_names(TAB_COLUMNS, {key: list(names) for key, names in words}, CORE_KEYS)


#: Сколько знаков принимает поле: номер - короткий, комментарии - строкой.
LIMITS = {"bill_no": 40, "bill_period": 300, "note": 300}


class BookError(Exception):
    """Отказ записи или чтения книги юрлица - с текстом для человека.

    `status` - код ответа API: 404 - строки нет, 409 - книгу правили
    параллельно, 422 - неверное значение, 424 - Google не ответил или книга
    устроена не так, как KORT умеет писать, 429 - Google и после повторов
    просит подождать (лимит обращений в минуту): ничего не записано, повтор
    через полминуты безопасен. Не 5xx: посредник перед сервером мог бы
    заменить ответ своим «Сервер не отвечает», и человек не узнал бы, что
    запись отменена и книга возвращена.
    """

    def __init__(self, message: str, *, status: int = 424, place: Any = None, changed: list[str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        #: Запись в книгу уже была, но пошла не так: куда писали и какие поля -
        #: для журнала KORT.
        self.place = place
        self.changed = changed or []


# ── Google ───────────────────────────────────────────────────────────────────


class Api(Protocol):
    """Пять вызовов Sheets API, которыми живёт карточка. В тестах - книга в памяти."""

    def formulas(self, spreadsheet_id: str, a1: str) -> list[list[str]]: ...

    def meta(self, spreadsheet_id: str) -> dict[str, Any]: ...

    def values(self, spreadsheet_id: str, ranges: list[str]) -> list[list[list[str]]]: ...

    def grid(self, spreadsheet_id: str, ranges: list[str]) -> list[list[list[dict[str, Any]]]]: ...

    def update(self, spreadsheet_id: str, requests: list[dict[str, Any]]) -> None: ...


#: Паузы перед повтором, когда Google ответил «лимит» (429): квота - 60 чтений
#: в минуту на аккаунт, и её делят сводка, «Оплачено (сводка)» и карточка.
RATE_PAUSES = (2.0, 4.0, 8.0, 16.0)


def _limited(exc: Exception) -> bool:
    code = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "code", None)
    return code == 429 or "RATE_LIMIT" in str(exc) or "Quota exceeded" in str(exc)


def _google(call: Any) -> Any:
    """Вызов Google с текстом для человека. «Лимит» (429) - повтор с паузой:
    Google такой запрос отклонил, а не выполнил, так что повтор безопасен и для записи."""
    for pause in (*RATE_PAUSES, None):
        try:
            return call()
        except BookError:
            raise
        except google.GoogleError as exc:
            raise BookError(str(exc), status=424) from exc
        except Exception as exc:  # noqa: BLE001 - gspread бросает много типов
            if pause is not None and _limited(exc):
                log.info("счета: Google ответил «лимит» - повтор через %s с", pause)
                time.sleep(pause)
                continue
            raise BookError(google.humanize(exc), status=429 if _limited(exc) else 424) from exc
    raise BookError("Google не ответил", status=424)  # недостижимо: последний проход либо вернул, либо бросил


class GoogleApi:
    """Sheets API от сервисного аккаунта - токеном записи (`google.writer`).

    Мимо `Spreadsheet` gspread: тот на каждое открытие книги делает лишний
    запрос метаданных. Клиент - свой на поток: книги читаются параллельно.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def _http(self) -> Any:
        http = getattr(self._local, "http", None)
        if http is None:

            def make() -> Any:
                client = google.writer().http_client
                client.set_timeout(30)
                return client

            http = self._local.http = _google(make)
        return http

    def formulas(self, spreadsheet_id: str, a1: str) -> list[list[str]]:
        http = self._http()
        raw = _google(lambda: http.values_get(spreadsheet_id, a1, params={"valueRenderOption": "FORMULA"}))
        return [[str(cell) for cell in row] for row in raw.get("values", [])]

    def meta(self, spreadsheet_id: str) -> dict[str, Any]:
        http = self._http()
        raw = _google(
            lambda: http.fetch_sheet_metadata(
                spreadsheet_id, params={"fields": "properties(title,locale),sheets.properties(title,sheetId)"}
            )
        )
        return {
            "title": raw.get("properties", {}).get("title", ""),
            "locale": raw.get("properties", {}).get("locale", ""),
            "tabs": {
                sheet["properties"]["title"]: int(sheet["properties"]["sheetId"]) for sheet in raw.get("sheets", [])
            },
        }

    def values(self, spreadsheet_id: str, ranges: list[str]) -> list[list[list[str]]]:
        http = self._http()
        raw = _google(
            lambda: http.values_batch_get(
                spreadsheet_id,
                ranges,
                params={"valueRenderOption": "FORMATTED_VALUE", "dateTimeRenderOption": "FORMATTED_STRING"},
            )
        )
        return [[[str(cell) for cell in row] for row in item.get("values", [])] for item in raw.get("valueRanges", [])]

    def grid(self, spreadsheet_id: str, ranges: list[str]) -> list[list[list[dict[str, Any]]]]:
        """Ячейки диапазонов - с введённым, вычисленным и показанным значением.

        Один диапазон - одна вкладка: Google отдаёт данные листа по порядку
        запрошенных диапазонов, и порядок здесь сверяется по их началу.
        """
        http = self._http()
        raw = _google(
            lambda: http.fetch_sheet_metadata(
                spreadsheet_id,
                params={
                    "ranges": ranges,
                    "fields": (
                        "sheets(properties.title,data(startRow,startColumn,rowData.values("
                        "userEnteredValue,effectiveValue,formattedValue,"
                        "userEnteredFormat.numberFormat,effectiveFormat.numberFormat)))"
                    ),
                }
            )
        )
        datas = [data for sheet in raw.get("sheets", []) for data in sheet.get("data", [])]
        if len(datas) != len(ranges):
            raise BookError(f"Google вернул {len(datas)} диапазонов из {len(ranges)}", status=424)
        out = []
        for wanted, data in zip(ranges, datas):
            row0, col0 = _start_of(wanted)
            if int(data.get("startRow", 0)) != row0 or int(data.get("startColumn", 0)) != col0:
                raise BookError("Google вернул диапазоны не в том порядке - запись отменена", status=424)
            out.append([list(row.get("values", [])) for row in data.get("rowData", [])])
        return out

    def update(self, spreadsheet_id: str, requests: list[dict[str, Any]]) -> None:
        http = self._http()
        _google(lambda: http.batch_update(spreadsheet_id, {"requests": requests}))


# ── Адреса ───────────────────────────────────────────────────────────────────


def col(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    letters = ""
    n = index + 1
    while n > 0:
        n, rest = divmod(n - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def col_index(letters: str) -> int:
    value = 0
    for char in letters.upper():
        value = value * 26 + (ord(char) - 64)
    return value - 1


def quote(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def _start_of(a1: str) -> tuple[int, int]:
    """«'Октябрь'!T1:AA3» → (0, 19): начало диапазона от нуля, как у GridData."""
    cell = a1.rsplit("!", 1)[-1].split(":", 1)[0]
    match = re.fullmatch(r"\$?([A-Z]+)\$?(\d+)", cell.upper())
    if not match:
        raise ValueError(f"не адрес ячейки: {a1}")
    return int(match.group(2)) - 1, col_index(match.group(1))


IMPORTRANGE = re.compile(r'IMPORTRANGE\(\s*"([^"]+)"\s*[;,]\s*"([^"!]+)!\$?A\$?2:', re.IGNORECASE)
MONTH_TABS = re.compile(r"'((?:[^']|'')+)'!\$?A\$?4:\$?BF", re.IGNORECASE)


# ── Книги и вкладки ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Tab:
    title: str
    sheet_id: int
    #: Колонки по шапке: ключ - индекс от нуля. Пусто - шапку не прочитать (`problem`).
    columns: dict[str, int]
    problem: str = ""


@dataclass(frozen=True)
class Book:
    spreadsheet_id: str
    title: str
    tabs: tuple[Tab, ...]
    #: Колонки вкладок с названиями компании.
    columns: tuple[Column, ...] = TAB_COLUMNS

    def title_of(self, name: str) -> str:
        return next((item.title for item in self.columns if item.key == name), TITLES.get(name, name))


@dataclass
class _Sources:
    books: list[Book]
    at: float
    index: dict[str, list[tuple[int, str, int]]] = field(default_factory=dict)
    index_at: float = 0.0
    #: Ключи, которых нет ни в одной книге даже после полного чтения.
    missing: set[str] = field(default_factory=set)


_sources: dict[tuple[Any, ...], _Sources] = {}
_sources_guard = threading.Lock()
_row_locks: dict[str, threading.Lock] = {}


def _layout(tab: str, header: Sequence[str], columns: Sequence[Column] = TAB_COLUMNS) -> tuple[dict[str, int], str]:
    """Колонки вкладки по шапке - только точные названия: здесь пишут, а не читают."""
    try:
        layout = resolve_layout(tab, columns, list(header))
    except LayoutError as exc:
        return {}, str(exc)
    loose = [placement.title for placement in layout.placements.values() if placement.how not in ("exact", "squashed")]
    if loose:
        return {}, f"Вкладка «{tab}»: колонки {', '.join(loose)} нашлись только по похожему названию"
    return {key: placement.index for key, placement in layout.placements.items() if placement.index is not None}, ""


def _discover_book(api: Api, spreadsheet_id: str, summary_of_book: str, columns: tuple[Column, ...] = TAB_COLUMNS) -> Book:
    cells = api.formulas(spreadsheet_id, f"{quote(summary_of_book)}!A1")
    formula = cells[0][0] if cells and cells[0] else ""
    titles = [name.replace("''", "'") for name in dict.fromkeys(MONTH_TABS.findall(formula))]
    meta = api.meta(spreadsheet_id)
    if not titles:
        raise BookError(f"Таблица «{meta['title']}»: вкладка «{summary_of_book}» собрана не так, как KORT умеет читать")
    missing = [title for title in titles if title not in meta["tabs"]]
    if missing:
        raise BookError(f"Таблица «{meta['title']}»: нет вкладок {', '.join(missing)}, хотя формула их собирает")
    headers = api.values(spreadsheet_id, [f"{quote(title)}!A{HEADER_ROW}:{LAST_COL}{HEADER_ROW}" for title in titles])
    tabs = []
    for title, rows in zip(titles, headers):
        found, problem = _layout(title, rows[0] if rows else [], columns)
        tabs.append(Tab(title=title, sheet_id=meta["tabs"][title], columns=found, problem=problem))
    return Book(spreadsheet_id=spreadsheet_id, title=meta["title"], tabs=tuple(tabs), columns=columns)


def discover(
    api: Api, summary_id: str, summary_tab: str, words: Sequence[tuple[str, Sequence[str]]] = ()
) -> list[Book]:
    """Таблицы юрлиц и их помесячные вкладки - по формулам сводной и таблиц; таблицы - параллельно.

    `words` - словарь колонок компании (`words_key`): шапку вкладок знает компания.
    """
    columns = tab_columns(words)
    row = api.formulas(summary_id, f"{quote(summary_tab)}!A1:{LAST_COL}1")
    text = "\n".join(cell for line in row for cell in line)
    pairs = list(dict.fromkeys(IMPORTRANGE.findall(text)))
    if not pairs:
        raise BookError(f"В первой строке «{summary_tab}» нет IMPORTRANGE - откуда собрана сводка, не видно")
    with ThreadPoolExecutor(max_workers=min(len(pairs), 9)) as pool:
        return list(pool.map(lambda pair: _discover_book(api, *pair, columns), pairs))


def _key_ranges(tab: Tab) -> list[str]:
    return [f"{quote(tab.title)}!{col(tab.columns[name])}{FIRST_ROW}:{col(tab.columns[name])}" for name in KEY_FIELDS]


def _rows_of(book: Book, values: list[list[list[str]]]) -> list[tuple[str, int, str]]:
    """(вкладка, строка, ключ) всех строк книги по колонкам ключа."""
    out: list[tuple[str, int, str]] = []
    position = 0
    for tab in book.tabs:
        if not tab.columns:
            continue
        parts = values[position : position + len(KEY_FIELDS)]
        position += len(KEY_FIELDS)
        height = max((len(part) for part in parts), default=0)
        for offset in range(height):
            cells = [part[offset][0] if offset < len(part) and part[offset] else "" for part in parts]
            own, customer, number, month = cells
            if not (customer.strip() or number.strip()):
                continue
            out.append((tab.title, FIRST_ROW + offset, bill_key(own, customer, number, month)))
    return out


def _read_keys(api: Api, book: Book) -> list[tuple[str, int, str]]:
    ranges = [part for tab in book.tabs if tab.columns for part in _key_ranges(tab)]
    if not ranges:
        return []
    return _rows_of(book, api.values(book.spreadsheet_id, ranges))


#: Чтение всех книг - одно на процесс: прогрев и карточка, пришедшие разом,
#: не читают девять книг дважды (на стенде 22.10.2026 так кончилась квота Google).
_build_lock = threading.Lock()


def _sources_of(api: Api, summary: tuple[Any, ...], *, fresh: bool = False) -> _Sources:
    asked = time.monotonic()
    with _sources_guard:
        known = _sources.get(summary)
    if known is not None and not fresh and asked - known.at < SOURCES_TTL:
        return known
    with _build_lock:
        with _sources_guard:
            known = _sources.get(summary)
        # Пока ждали, книги прочитал соседний поток - его чтение и берём.
        if known is not None and (known.at >= asked or (not fresh and time.monotonic() - known.at < SOURCES_TTL)):
            return known
        made = _Sources(books=discover(api, *summary), at=time.monotonic())
        with _sources_guard:
            _sources[summary] = made
        return made


def _build_index(api: Api, sources: _Sources, *, asked: float | None = None) -> None:
    """Указатель «ключ → (книга, вкладка, строка)» по всем книгам - параллельно.

    `asked` - когда указатель понадобился: построенный позже соседним потоком
    не строится заново.
    """
    with _build_lock:
        if asked is not None and sources.index_at >= asked:
            return
        index: dict[str, list[tuple[int, str, int]]] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(len(sources.books), 9))) as pool:
            rows_of_books = list(pool.map(lambda book: _read_keys(api, book), sources.books))
        for number, rows in enumerate(rows_of_books):
            for tab, row, row_key in rows:
                index.setdefault(row_key, []).append((number, tab, row))
        sources.index, sources.index_at = index, time.monotonic()


def _book_of(api: Api, summary: tuple[Any, ...], key: str, *, fresh: bool = False) -> Book:
    """Книга, где живёт строка: по указателю, а нет в нём - перечитав книги.

    Указатель только подсказывает книгу: саму строку `locate` ищет в книге
    заново. Поэтому он живёт долго, и раздел «Счета» не дёргает девять книг
    каждые пять минут.
    """
    asked = time.monotonic()
    sources = _sources_of(api, summary, fresh=fresh)
    if key in sources.missing and not fresh:
        raise BookError("Строку счёта не нашёл ни в одной таблице юрлица - её могли переименовать или удалить", status=404)
    if fresh or key not in sources.index or time.monotonic() - sources.index_at >= SOURCES_TTL:
        _build_index(api, sources, asked=asked)
    books = {number for number, _, _ in sources.index.get(key, [])}
    if len(books) == 1:
        return sources.books[books.pop()]
    if len(books) > 1:
        names = ", ".join(f"«{sources.books[number].title}»" for number in sorted(books))
        raise BookError(f"Этот счёт есть сразу в нескольких таблицах ({names}) - какую править, KORT не угадывает")
    if not fresh:
        return _book_of(api, summary, key, fresh=True)
    # Не нашлась и после полного чтения: до следующего чтения книг - сразу
    # отказ, а не девять книг заново на каждое открытие карточки.
    sources.missing.add(key)
    raise BookError("Строку счёта не нашёл ни в одной таблице юрлица - её могли переименовать или удалить", status=404)


# ── Строка ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Place:
    book: Book
    tab: Tab
    row: int

    @property
    def url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.book.spreadsheet_id}/edit#gid={self.tab.sheet_id}&range=A{self.row}"

    def out(self) -> dict[str, Any]:
        return {"book": self.book.title, "tab": self.tab.title, "row": self.row, "url": self.url}


def locate(api: Api, summary: tuple[Any, ...], key: str, *, fresh: bool = False) -> Place:
    """Где строка счёта сейчас - читая книгу заново: строки вставляют и сортируют."""
    book = _book_of(api, summary, key, fresh=fresh)
    found = [(tab, row) for tab, row, row_key in _read_keys(api, book) if row_key == key]
    if not found:
        if not fresh:
            return locate(api, summary, key, fresh=True)
        raise BookError(f"В таблице «{book.title}» строки этого счёта уже нет - её переименовали или удалили", status=409)
    if len(found) > 1:
        where = ", ".join(f"«{tab}» строка {row}" for tab, row in found)
        raise BookError(f"В таблице «{book.title}» этот счёт стоит несколько раз ({where}) - какую править, KORT не угадывает")
    title, row = found[0]
    tab = next(item for item in book.tabs if item.title == title)
    return Place(book=book, tab=tab, row=row)


@dataclass
class Snapshot:
    """Строка и шапка вкладки в момент чтения."""

    top: list[list[dict[str, Any]]]
    row: list[dict[str, Any]]

    def cell(self, index: int) -> dict[str, Any]:
        return self.row[index] if index < len(self.row) else {}

    def shown(self, index: int) -> str:
        return str(self.cell(index).get("formattedValue", "")).strip()

    def header(self) -> list[str]:
        line = self.top[HEADER_ROW - 1] if len(self.top) >= HEADER_ROW else []
        return [str(cell.get("formattedValue", "")) for cell in line]

    def errors(self) -> set[int]:
        """Колонки 3-й строки, где формула сейчас отдаёт ошибку (#REF! и прочие)."""
        line = self.top[TOP_ROWS - 1] if len(self.top) >= TOP_ROWS else []
        return {index for index, cell in enumerate(line) if "errorValue" in (cell.get("effectiveValue") or {})}


def _snapshot(api: Api, place: Place) -> Snapshot:
    title = quote(place.tab.title)
    top, row = api.grid(
        place.book.spreadsheet_id,
        [f"{title}!A1:{LAST_COL}{TOP_ROWS}", f"{title}!A{place.row}:{LAST_COL}{place.row}"],
    )
    return Snapshot(top=top, row=row[0] if row else [])


def _check_place(place: Place, snap: Snapshot, key: str) -> None:
    """Шапка та же, строка - та самая. Иначе книгу правят прямо сейчас."""
    columns, problem = _layout(place.tab.title, snap.header(), place.book.columns)
    if problem:
        raise BookError(problem)
    if columns != place.tab.columns:
        raise BookError(
            f"В таблице «{place.book.title}» на вкладке «{place.tab.title}» переставили колонки - откройте карточку заново",
            status=409,
        )
    own, customer, number, month = (snap.shown(place.tab.columns[name]) for name in KEY_FIELDS)
    if bill_key(own, customer, number, month) != key:
        raise BookError(
            f"Строка {place.row} на вкладке «{place.tab.title}» сейчас другая - таблицу правят, откройте карточку заново",
            status=409,
        )


def _blocked(place: Place, snap: Snapshot, name: str) -> str:
    """Почему в поле нельзя писать; `""` - можно."""
    index = place.tab.columns.get(name)
    if index is None:
        return f"На вкладке «{place.tab.title}» нет колонки «{place.book.title_of(name)}»"
    for line in snap.top:
        cell = line[index] if index < len(line) else {}
        entered = cell.get("userEnteredValue") or {}
        if "formulaValue" in entered or (not entered and cell.get("effectiveValue")):
            return f"Над колонкой «{place.book.title_of(name)}» стоит формула - её значения считает таблица"
    cell = snap.cell(index)
    entered = cell.get("userEnteredValue") or {}
    if "formulaValue" in entered:
        return f"В «{place.book.title_of(name)}» стоит формула - KORT её не затирает"
    if not entered and cell.get("effectiveValue"):
        return f"«{place.book.title_of(name)}» заполняет формула соседней ячейки - KORT туда не пишет"
    return ""


#: Формулы, которые разливают результат на соседние ячейки: запись в их
#: разлив ломает формулу целиком (#REF!).
SPILLING = re.compile(
    r"ARRAYFORMULA|\{|(?:FILTER|QUERY|SORTN?|UNIQUE|IMPORTRANGE|SEQUENCE|FLATTEN|SPLIT|TRANSPOSE|BYROW|BYCOL|"
    r"MAP|MAKEARRAY|HSTACK|VSTACK|CHOOSECOLS|CHOOSEROWS)\s*\(",
    re.IGNORECASE,
)


def _spilling_above(api: Api, place: Place) -> dict[str, str]:
    """Поля, над которыми в той же колонке ниже шапки стоит формула-массив."""
    indices = {name: place.tab.columns[name] for name in FIELDS if name in place.tab.columns}
    if not indices or place.row <= FIRST_ROW:
        return {}
    lo, hi = min(indices.values()), max(indices.values())
    rows = api.formulas(
        place.book.spreadsheet_id, f"{quote(place.tab.title)}!{col(lo)}{FIRST_ROW}:{col(hi)}{place.row - 1}"
    )
    out: dict[str, str] = {}
    for offset, line in enumerate(rows):
        for name, index in indices.items():
            cell = line[index - lo] if index - lo < len(line) else ""
            if name not in out and cell.startswith("=") and SPILLING.search(cell):
                out[name] = (
                    f"В «{place.book.title_of(name)}» выше, в строке {FIRST_ROW + offset}, стоит формула-массив - "
                    "запись сюда её сломала бы"
                )
    return out


def fields_of(place: Place, snap: Snapshot) -> dict[str, str]:
    return {name: snap.shown(place.tab.columns[name]) if name in place.tab.columns else "" for name in FIELDS}


# ── Карточка ─────────────────────────────────────────────────────────────────


def card(api: Api, summary: tuple[Any, ...], key: str) -> dict[str, Any]:
    """Строка счёта в книге юрлица: где она, что в полях и куда можно писать."""
    place = locate(api, summary, key)
    snap = _snapshot(api, place)
    _check_place(place, snap, key)
    above = _spilling_above(api, place)
    return {
        "place": place.out(),
        "fields": fields_of(place, snap),
        "state": snap.shown(place.tab.columns["bill"]),
        "blocked": {name: reason for name in FIELDS if (reason := _blocked(place, snap, name) or above.get(name, ""))},
    }


# ── Запись ───────────────────────────────────────────────────────────────────


def _clean_text(name: str, value: Any) -> str:
    text = " ".join(str(value or "").split())
    limit = LIMITS[name]
    if len(text) > limit:
        raise BookError(f"«{TITLES[name]}» длиннее {limit} знаков", status=422)
    return text


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise BookError("Дата выставления - не дата", status=422) from exc
    if not 2020 <= parsed.year <= 2100:
        raise BookError("Дата выставления - не из этого века", status=422)
    return parsed


@dataclass(frozen=True)
class Value:
    """Что положить в ячейку: число, строку или ничего (стереть)."""

    number: float | int | None = None
    text: str | None = None
    date_format: bool = False

    @property
    def empty(self) -> bool:
        return self.number is None and self.text is None

    def entered(self) -> dict[str, Any]:
        if self.number is not None:
            return {"numberValue": self.number}
        if self.text is not None:
            return {"stringValue": self.text}
        return {}


def wanted_values(values: dict[str, Any]) -> dict[str, Value]:
    """Значения карточки - в ячейки таблицы. Только присланные поля."""
    out: dict[str, Value] = {}
    if "bill_no" in values:
        text = _clean_text("bill_no", values["bill_no"])
        if text[:1] in ("=", "+", "@"):
            raise BookError(f"«{TITLES['bill_no']}» не может начинаться с =, + или @", status=422)
        # Целое без ведущих нулей - числом, как его вводит человек; «007»,
        # «248/1» - строкой: иначе Google съест нули или прочтёт дату.
        out["bill_no"] = Value(number=int(text)) if re.fullmatch(r"[1-9]\d{0,14}", text) else Value(text=text or None)
    if "bill_date" in values:
        parsed = _parse_date(values["bill_date"])
        out["bill_date"] = Value(number=(parsed - EPOCH).days, date_format=True) if parsed else Value()
    for name in ("bill_period", "note"):
        if name in values:
            text = _clean_text(name, values[name])
            out[name] = Value(text=text or None)
    return out


def _holds(cell: dict[str, Any], value: Value) -> bool:
    """В ячейке уже лежит это значение - писать незачем."""
    effective = cell.get("effectiveValue") or {}
    if value.empty:
        return not effective and not str(cell.get("formattedValue", "")).strip()
    if value.number is not None:
        return effective.get("numberValue") == value.number
    return effective.get("stringValue") == value.text


def _is_date_format(cell: dict[str, Any]) -> bool:
    kind = ((cell.get("effectiveFormat") or {}).get("numberFormat") or {}).get("type", "")
    return kind in ("DATE", "DATE_TIME")


def _cell_request(place: Place, index: int, entered: dict[str, Any], number_format: dict[str, Any] | None) -> dict[str, Any]:
    cell: dict[str, Any] = {"userEnteredValue": entered} if entered else {}
    fields = "userEnteredValue"
    if number_format is not None:
        cell["userEnteredFormat"] = {"numberFormat": number_format}
        fields += ",userEnteredFormat.numberFormat"
    return {
        "updateCells": {
            "range": {
                "sheetId": place.tab.sheet_id,
                "startRowIndex": place.row - 1,
                "endRowIndex": place.row,
                "startColumnIndex": index,
                "endColumnIndex": index + 1,
            },
            "rows": [{"values": [cell]}],
            "fields": fields,
        }
    }


@dataclass
class Saved:
    place: Place
    before: dict[str, str]
    after: dict[str, str]
    state: str
    changed: list[str]
    warning: str = ""


def _lock_of(key: str) -> threading.Lock:
    with _sources_guard:
        return _row_locks.setdefault(key, threading.Lock())


def save(
    api: Api,
    summary: tuple[Any, ...],
    key: str,
    expect: dict[str, Any],
    values: dict[str, Any],
    defaults: dict[str, Any] | None = None,
) -> Saved:
    """Записать поля карточки в строку книги юрлица - по правилам из шапки модуля.

    `expect` - поля такими, какими их показала карточка: если в книге они уже
    другие, запись отменяется (409), чтобы не затереть чужую правку. Если же в
    книге уже ровно то, что пишем, - это не чужая правка, а та же: отказа нет
    (повтор записи, ответ на которую потерялся по дороге).

    `defaults` - поля, которые пишутся, только если в книге они пусты и туда
    можно писать: «Счет за Период» при выставлении из шаблона Excel (карточка
    подставляет его так же). Заполненное в книге они не трогают и записи не
    отменяют.
    """
    wanted = wanted_values(values)
    extra = wanted_values(defaults or {})
    if not wanted:
        raise BookError("Нечего записывать", status=422)
    final_no = wanted["bill_no"] if "bill_no" in wanted else None
    final_date = wanted["bill_date"] if "bill_date" in wanted else None
    if final_no is not None and not final_no.empty and final_date is not None and final_date.empty:
        raise BookError("У выставленного счёта нужна дата выставления", status=422)
    with _lock_of(key):
        place = locate(api, summary, key)
        before = _snapshot(api, place)
        _check_place(place, before, key)
        shown = fields_of(place, before)
        if final_no is not None and not final_no.empty and "bill_date" not in wanted and not shown["bill_date"]:
            raise BookError("У выставленного счёта нужна дата выставления", status=422)
        for name in wanted:
            if name in expect and str(expect[name] or "").strip() != shown[name]:
                if _holds(before.cell(place.tab.columns[name]), wanted[name]):
                    continue
                raise BookError(
                    f"«{place.book.title_of(name)}» в таблице за это время поменяли: было «{expect[name] or ''}», "
                    f"стало «{shown[name]}». Откройте карточку заново",
                    status=409,
                )
        requests: list[dict[str, Any]] = []
        undo: list[dict[str, Any]] = []
        changed: list[str] = []
        above: dict[str, str] | None = None
        for name, value in extra.items():
            if name in wanted or name not in place.tab.columns or shown[name] or _blocked(place, before, name):
                continue
            if above is None:
                above = _spilling_above(api, place)
            if not above.get(name):
                wanted[name] = value
        for name, value in wanted.items():
            index = place.tab.columns[name]
            cell = before.cell(index)
            if _holds(cell, value):
                continue
            if above is None:
                above = _spilling_above(api, place)
            reason = _blocked(place, before, name) or above.get(name, "")
            if reason:
                raise BookError(reason)
            number_format = None
            if value.date_format and not value.empty and not _is_date_format(cell):
                number_format = {"type": "DATE", "pattern": "dd.MM.yyyy"}
            requests.append(_cell_request(place, index, value.entered(), number_format))
            undo.append(
                _cell_request(
                    place,
                    index,
                    cell.get("userEnteredValue") or {},
                    ((cell.get("userEnteredFormat") or {}).get("numberFormat") or {}) if number_format else None,
                )
            )
            changed.append(name)
        if not requests:
            return Saved(place, shown, shown, before.shown(place.tab.columns["bill"]), [])

        broken_before = before.errors()
        try:
            api.update(place.book.spreadsheet_id, requests)
        except BookError as exc:
            if exc.status == 429:
                # «Лимит» Google отклоняет запрос целиком, не выполнив: в книге ничего не поменялось.
                raise BookError(f"{exc}. Ничего не записано", status=429, place=place, changed=changed) from exc
            # Запрос мог дойти, а ответ - потеряться: записано или нет, видно
            # только по книге. Карточка, открытая заново, покажет.
            raise BookError(
                f"Google не подтвердил запись ({exc}). Откройте карточку заново: если значения уже в таблице, запись прошла",
                status=424,
                place=place,
                changed=changed,
            ) from exc
        try:
            after = _snapshot(api, place)
        except BookError as exc:
            raise BookError(
                f"В таблицу записано, но перечитать строку не удалось ({exc}). Откройте карточку заново и проверьте",
                status=424,
                place=place,
                changed=changed,
            ) from exc

        try:
            _check_place(place, after, key)
        except BookError as exc:
            # Строку сдвинули между чтением и записью - то, что записано, легло
            # не туда. Чужую строку вслепую не «возвращаем»: говорим, где смотреть.
            log.error("счета: запись в %s «%s» строка %s - строка сдвинулась: %s",
                      place.book.title, place.tab.title, place.row, exc)
            raise BookError(
                f"Пока KORT писал, строки на вкладке «{place.tab.title}» таблицы «{place.book.title}» сдвинули. "
                f"Проверьте строку {place.row}: в «{', '.join(place.book.title_of(name) for name in changed)}» могло лечь значение "
                f"этого счёта. Строка счёта: {place.url}",
                status=409,
                place=place,
                changed=changed,
            ) from exc

        problems = [
            place.book.title_of(name) for name in changed if not _holds(after.cell(place.tab.columns[name]), wanted[name])
        ]
        broken = sorted(after.errors() - broken_before)
        if problems or broken:
            what = (
                f"в «{', '.join(problems)}» легло не то" if problems
                else f"сломалась формула в колонке {', '.join(col(index) for index in broken)}"
            )
            try:
                api.update(place.book.spreadsheet_id, undo)
            except BookError as exc:
                log.error("счета: запись в %s «%s» строка %s - %s, и вернуть не удалось: %s",
                          place.book.title, place.tab.title, place.row, what, exc)
                raise BookError(
                    f"Запись пошла не так ({what}), и вернуть таблицу KORT не смог: {exc}. "
                    f"Проверьте строку {place.row} на вкладке «{place.tab.title}»: {place.url}",
                    status=424,
                    place=place,
                    changed=changed,
                ) from exc
            log.error("счета: запись в %s «%s» строка %s отменена - %s", place.book.title, place.tab.title, place.row, what)
            raise BookError(
                f"Запись отменена: {what}. Таблица возвращена как была", status=424, place=place, changed=changed
            )

        result = fields_of(place, after)
        state = after.shown(place.tab.columns["bill"])
        warning = ""
        issued = norm(state) in ("true", "истина")
        if result["bill_no"] and not issued:
            warning = f"«№ Счета» записан, но «Счет» в таблице не стал TRUE (сейчас «{state}») - проверьте формулу таблицы"
        elif not result["bill_no"] and issued:
            warning = "«№ Счета» стёрт, но «Счет» в таблице всё ещё TRUE - проверьте формулу таблицы"
        return Saved(place, shown, result, state, changed, warning)


_api: Api | None = None


def api() -> Api:
    """Sheets API карточки: один на процесс (клиенты внутри - свои на поток). Тесты подменяют."""
    global _api
    if _api is None:
        _api = GoogleApi()
    return _api


_warming: set[tuple[str, str]] = set()


def warm(api: Api, summary: tuple[Any, ...]) -> None:
    """Найти книги и строки заранее, в фоне: иначе первая карточка ждёт девять книг."""
    if isinstance(api, GoogleApi) and not google.is_configured():
        return
    with _sources_guard:
        known = _sources.get(summary)
        fresh = known is not None and time.monotonic() - known.index_at < SOURCES_TTL
        if fresh or summary in _warming:
            return
        _warming.add(summary)

    def run() -> None:
        try:
            _build_index(api, _sources_of(api, summary))
        except BookError as exc:
            log.warning("счета: таблицы юрлиц не прочитались заранее (%s)", exc)
        except Exception:  # noqa: BLE001 - прогрев не роняет раздел
            log.exception("счета: таблицы юрлиц не прочитались заранее")
        finally:
            with _sources_guard:
                _warming.discard(summary)

    threading.Thread(target=run, daemon=True).start()


def forget() -> None:
    with _sources_guard:
        _sources.clear()


__all__ = [
    "Api",
    "api",
    "BookError",
    "FIELDS",
    "GoogleApi",
    "Place",
    "Saved",
    "card",
    "discover",
    "forget",
    "locate",
    "save",
    "wanted_values",
    "warm",
]
