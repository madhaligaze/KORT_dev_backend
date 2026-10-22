"""Книги юрлиц в памяти - для `contracts/bill_book.py` без Google.

Ведёт себя как Sheets API в том, на что опирается запись:
* «Счет» (X) - формула-массив из 3-й строки: TRUE, где заполнен «№ Счета» (Z);
  ячейки X ниже - разлив без введённого значения;
* запись в разлив ломает массив: в X3 появляется #REF!;
* даты - числом от 30.12.1899, показ - по формату ячейки;
* `before_update` - крючок: «человек вставил строку» между чтением и записью.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Callable

EPOCH = date(1899, 12, 30)

#: Колонки помесячной вкладки (A=0) - как в книгах BBC на 22.10.2026.
HEADER = {
    0: "Техн. 2",
    1: "Мес",
    2: "Техн. 1",
    4: "Наша \nФирма",
    5: "Вид \nУслуги",
    7: "Заказчик\n(Название Фирмы)",
    11: "Сумма\nДоговора",
    15: "№\nДоговора",
    17: "Период\nс (нач.)",
    18: "Период\nпо (зав.)",
    19: "Доп. \nКоммент.",
    23: "Счет",
    24: "Счет за\nПериод (Коммент)",
    25: "№ \nСчета",
    26: "Дата выстав.\nСчета",
    27: "Факт Оплата",
}
WIDTH = 58  # A..BF
X, Y, Z, AA, T = 23, 24, 25, 26, 19


def col_index(letters: str) -> int:
    value = 0
    for char in letters.upper():
        value = value * 26 + (ord(char) - 64)
    return value - 1


def parse(a1: str) -> tuple[str, int, int, int | None, int | None]:
    """«'Окт'!T4:AA» → (вкладка, строка0, колонка0, строка1 или None, колонка1)."""
    tab, cells = a1.rsplit("!", 1)
    tab = tab[1:-1].replace("''", "'") if tab.startswith("'") else tab
    start, _, end = cells.partition(":")
    match = re.fullmatch(r"\$?([A-Z]+)\$?(\d+)", start.upper())
    assert match, a1
    c0, r0 = col_index(match.group(1)), int(match.group(2)) - 1
    if not end:
        return tab, r0, c0, r0, c0
    match = re.fullmatch(r"\$?([A-Z]+)\$?(\d*)", end.upper())
    assert match, a1
    c1 = col_index(match.group(1))
    r1 = int(match.group(2)) - 1 if match.group(2) else None
    return tab, r0, c0, r1, c1


class Sheet:
    """Вкладка: ячейки {entered, value, fmt}; X считается формулой из Z."""

    def __init__(self, title: str, sheet_id: int, *, month_tab: bool = True) -> None:
        self.title = title
        self.sheet_id = sheet_id
        self.month_tab = month_tab
        self.rows: list[list[dict[str, Any]]] = []
        #: Ячейки в разливе массива X3, которого по ним не видно: запись туда ломает массив.
        self.hidden_spill: set[tuple[int, int]] = set()

    def cell(self, row: int, column: int) -> dict[str, Any]:
        while len(self.rows) <= row:
            self.rows.append([])
        line = self.rows[row]
        while len(line) <= column:
            line.append({"entered": {}, "value": None, "fmt": None})
        return line[column]

    def put(self, row: int, column: int, value: Any, *, fmt: str | None = None, formula: str = "") -> None:
        cell = self.cell(row, column)
        if formula:
            cell["entered"] = {"formulaValue": formula}
        elif value is None or value == "":
            cell["entered"] = {}
        elif isinstance(value, bool):
            cell["entered"] = {"boolValue": value}
        elif isinstance(value, (int, float)):
            cell["entered"] = {"numberValue": value}
        else:
            cell["entered"] = {"stringValue": str(value)}
        cell["value"] = None if value == "" else value
        if fmt:
            cell["fmt"] = fmt

    def recompute(self) -> None:
        """«Счет» = TRUE, где есть «№ Счета»; запись в разлив ломает массив в X3."""
        if not self.month_tab:
            return
        spill_taken = any(self.cell(row, X)["entered"] for row in range(3, len(self.rows))) or any(
            self.cell(row, column)["entered"] for row, column in self.hidden_spill
        )
        origin = self.cell(2, X)
        origin["entered"] = {"formulaValue": '=ARRAYFORMULA(IF(Z3:Z<>""; TRUE; "Еще рано"))'}
        if spill_taken:
            origin["value"] = {"error": "#REF!"}
            return
        origin["value"] = None
        for row in range(3, len(self.rows)):
            customer = self.cell(row, 7)["value"]
            if not customer:
                continue
            target = self.cell(row, X)
            if not target["entered"]:
                number = self.cell(row, Z)["value"]
                target["value"] = True if number not in (None, "") else "Еще рано"

    def shown(self, cell: dict[str, Any]) -> str:
        value = cell["value"]
        if value is None:
            return ""
        if isinstance(value, dict):
            return value["error"]
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            if cell["fmt"] == "DATE":
                return (EPOCH + timedelta(days=int(value))).strftime("%d.%m.%Y")
            return str(int(value)) if float(value).is_integer() else str(value)
        return str(value)

    def api_cell(self, cell: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if cell["entered"]:
            out["userEnteredValue"] = dict(cell["entered"])
        value = cell["value"]
        if isinstance(value, dict):
            out["effectiveValue"] = {"errorValue": {"type": "REF", "message": value["error"]}}
        elif isinstance(value, bool):
            out["effectiveValue"] = {"boolValue": value}
        elif isinstance(value, (int, float)):
            out["effectiveValue"] = {"numberValue": value}
        elif value not in (None, ""):
            out["effectiveValue"] = {"stringValue": str(value)}
        shown = self.shown(cell)
        if shown:
            out["formattedValue"] = shown
        if cell["fmt"]:
            fmt = {"type": cell["fmt"], "pattern": "dd.MM.yyyy"}
            out["effectiveFormat"] = {"numberFormat": fmt}
            out["userEnteredFormat"] = {"numberFormat": fmt}
        return out

    def insert_row(self, before: int) -> None:
        self.rows.insert(before, [])
        self.recompute()


class Book:
    def __init__(self, spreadsheet_id: str, title: str, summary_tab: str, tabs: list[Sheet]) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.title = title
        self.summary = Sheet(summary_tab, 1, month_tab=False)
        self.tabs = {tab.title: tab for tab in tabs}
        listed = ";".join(f"'{tab.title.replace(chr(39), chr(39) * 2)}'!$A4:$BF" for tab in tabs)
        self.summary.put(0, 0, None, formula=f"={{'{tabs[0].title}'!$A$2:$BF$2;LET(data; {{{listed}}}; SORT(data))}}")

    def sheet(self, title: str) -> Sheet:
        if title == self.summary.title:
            return self.summary
        return self.tabs[title]


def month_tab(title: str, sheet_id: int, rows: list[dict[str, Any]], *, start: int = 3) -> Sheet:
    """Вкладка с шапкой во 2-й строке, массивом «Счет» в 3-й и строками счетов с 4-й."""
    sheet = Sheet(title, sheet_id)
    sheet.put(0, 2, "ОКТЯБРЬ 2026")
    for column, name in HEADER.items():
        sheet.put(1, column, name)
    for offset, data in enumerate(rows):
        row = start + offset
        sheet.put(row, 2, data.get("month", "ОКТЯБРЬ 2026"))
        sheet.put(row, 4, data["own"])
        sheet.put(row, 5, data.get("kind", "Абон.П."))
        sheet.put(row, 7, data["customer"])
        sheet.put(row, 15, data["number"])
        sheet.put(row, 17, data.get("period", "22.10.2026"))
        sheet.put(row, T, data.get("note", ""))
        sheet.put(row, Y, data.get("bill_period", ""))
        sheet.put(row, Z, data.get("bill_no", ""))
        sheet.put(row, AA, data.get("bill_date", ""), fmt=data.get("date_fmt", "DATE"))
        # «Сумма Факт Поступ.» - формула в каждой строке: её KORT не трогает.
        sheet.put(row, 28, 0, formula=f"=SUMIFS('Журнал'!E:E;'Журнал'!B:B;C{row + 1})")
    sheet.cell(len(sheet.rows), WIDTH - 1)  # вкладка шириной до BF
    sheet.recompute()
    return sheet


class FakeApi:
    """`bill_book.Api` над книгами в памяти; `calls` - что спрашивали, по порядку."""

    def __init__(self, summary_id: str, summary_tab: str, books: list[Book]) -> None:
        self.summary_id = summary_id
        self.summary_tab = summary_tab
        self.books = {book.spreadsheet_id: book for book in books}
        self.calls: list[tuple[str, str]] = []
        self.updates: list[list[dict[str, Any]]] = []
        self.before_update: Callable[[], None] | None = None
        self.corrupt: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None
        self.fail_update: Exception | None = None

    # Сводка: формула в B1 со всеми IMPORTRANGE.
    def summary_formula(self) -> str:
        parts = [
            f'data{index}; IMPORTRANGE("{book.spreadsheet_id}"; "{book.summary.title}!$A$2:$Bf")'
            for index, book in enumerate(self.books.values(), start=1)
        ]
        return "=LET(\n  " + ";\n  ".join(parts) + ";\n  VSTACK(data1))"

    def _sheet(self, spreadsheet_id: str, title: str) -> Sheet:
        return self.books[spreadsheet_id].sheet(title)

    def formulas(self, spreadsheet_id: str, a1: str) -> list[list[str]]:
        self.calls.append(("formulas", a1))
        tab, r0, c0, r1, c1 = parse(a1)
        if spreadsheet_id == self.summary_id:
            return [["", self.summary_formula()]]
        sheet = self._sheet(spreadsheet_id, tab)
        out = []
        last = r1 if r1 is not None else len(sheet.rows) - 1
        for row in range(r0, last + 1):
            line = []
            for column in range(c0, (c1 if c1 is not None else c0) + 1):
                cell = sheet.cell(row, column)
                line.append(cell["entered"].get("formulaValue") or sheet.shown(cell))
            out.append(line)
        return out

    def meta(self, spreadsheet_id: str) -> dict[str, Any]:
        self.calls.append(("meta", spreadsheet_id))
        book = self.books[spreadsheet_id]
        tabs = {book.summary.title: book.summary.sheet_id}
        tabs.update({tab.title: tab.sheet_id for tab in book.tabs.values()})
        return {"title": book.title, "locale": "ru_RU", "tabs": tabs}

    def values(self, spreadsheet_id: str, ranges: list[str]) -> list[list[list[str]]]:
        self.calls.append(("values", ";".join(ranges)))
        out = []
        for a1 in ranges:
            tab, r0, c0, r1, c1 = parse(a1)
            sheet = self._sheet(spreadsheet_id, tab)
            last = r1 if r1 is not None else len(sheet.rows) - 1
            rows = []
            for row in range(r0, last + 1):
                line = [sheet.shown(sheet.cell(row, column)) for column in range(c0, (c1 if c1 is not None else c0) + 1)]
                while line and not line[-1]:
                    line.pop()
                rows.append(line)
            while rows and not rows[-1]:
                rows.pop()
            out.append(rows)
        return out

    def grid(self, spreadsheet_id: str, ranges: list[str]) -> list[list[list[dict[str, Any]]]]:
        self.calls.append(("grid", ";".join(ranges)))
        out = []
        for a1 in ranges:
            tab, r0, c0, r1, c1 = parse(a1)
            sheet = self._sheet(spreadsheet_id, tab)
            out.append(
                [
                    [sheet.api_cell(sheet.cell(row, column)) for column in range(c0, (c1 if c1 is not None else c0) + 1)]
                    for row in range(r0, (r1 if r1 is not None else len(sheet.rows) - 1) + 1)
                ]
            )
        return out

    def update(self, spreadsheet_id: str, requests: list[dict[str, Any]]) -> None:
        self.calls.append(("update", str(len(requests))))
        if self.fail_update is not None:
            raise self.fail_update
        if self.before_update is not None:
            hook, self.before_update = self.before_update, None
            hook()
        if self.corrupt is not None:
            requests = self.corrupt(requests)
        self.updates.append(requests)
        book = self.books[spreadsheet_id]
        by_id = {tab.sheet_id: tab for tab in book.tabs.values()}
        for request in requests:
            body = request["updateCells"]
            area = body["range"]
            sheet = by_id[area["sheetId"]]
            cell = sheet.cell(area["startRowIndex"], area["startColumnIndex"])
            data = body["rows"][0]["values"][0]
            fields = body["fields"].split(",")
            if "userEnteredValue" in fields:
                entered = data.get("userEnteredValue") or {}
                cell["entered"] = dict(entered)
                cell["value"] = next(iter(entered.values()), None)
            if "userEnteredFormat.numberFormat" in fields:
                number_format = (data.get("userEnteredFormat") or {}).get("numberFormat") or {}
                cell["fmt"] = number_format.get("type")
        for sheet in book.tabs.values():
            sheet.recompute()


def legal_book() -> Book:
    """BBC Legal: KENESARY - в «Октябре», строка 6; соседи - чтобы было что сдвигать."""
    october = month_tab(
        "Октябрь",
        2034009495,
        [
            {"own": "BBCL", "customer": "ТОО АЗИЯ ПРОМ", "number": "№ЮО-6", "bill_no": 248, "bill_date": 46296},
            {"own": "BBCL", "customer": "ТОО Инвольт", "number": "№ЮО-4"},
            {"own": "BBCL", "customer": "ТОО KENESARY AGRO", "number": "№ЮО/44", "note": "звонить Асель"},
        ],
    )
    september = month_tab(
        "Сентябрь",
        777,
        [{"own": "BBCL", "customer": "ТОО KENESARY AGRO", "number": "№ЮО/44", "month": "СЕНТЯБРЬ 2026",
          "period": "22.09.2026", "bill_no": 201, "bill_date": 46288}],
    )
    return Book("legal-id", "BBC Legal Реестр", "BBC Legal Сводка все", [september, october])


def rent_book() -> Book:
    october = month_tab(
        "Октябрь",
        570776893,
        [{"own": "BBC Omar", "customer": "ТОО Custom Trade", "number": "№RENT/84", "kind": "нет", "period": "21.10.2026"}],
    )
    return Book("rent-id", "BBC Реестр Аренд", "BBC Аренда Сводка все", [october])


def fake_api() -> FakeApi:
    return FakeApi("summary-id", "Сводка все ЮР лица", [legal_book(), rent_book()])
