"""Что за файл пришёл и как прочитать его в строки.

Формат определяется по содержимому, а не по расширению
──────────────────────────────────────────────────────
Банки подписывают файлы как придётся. «Выписка.xls» из интернет-банка
бывает тремя разными вещами: настоящим Excel 97–2003, HTML-таблицей, которой
дали расширение .xls, чтобы Excel её открыл, и XML-таблицей Excel 2003. По
расширению все три — «старый Excel, сохраните как .xlsx», и человеку
предлагалось вручную пересохранять файл, который мы могли прочитать сами.

Поэтому первые байты решают, чем читать: `PK` — zip, то есть xlsx; `D0 CF 11
E0` — старый Excel; `<` — разметка; «1CClientBankExchange» — выгрузка
банк-клиента для 1С. Остальное — текст с разделителями.

Что модуль НЕ делает
────────────────────
Он не ищет шапку и не решает, что где лежит. Его ответ — таблицы как есть,
строками ячеек, в порядке листов. Какую из таблиц читать и что в ней значит
каждая колонка, решает `importing` — один разбор для всех форматов. Второй
разбор «для 1С» или «для HTML» разъехался бы с общим на первой же правке.

Исключение одно — выгрузка 1С. Это не таблица, а записи «ключ=значение», и её
приходится переложить в таблицу. Перекладывается она в тот же вид, в каком
банк печатает выписку в Excel: реквизиты над таблицей, «Дебет»/«Кредит»,
контрагент, его БИН и счёт. Дальше она читается общим разбором, как любая
выписка.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from xml.etree import ElementTree

#: Таблица — строки ячеек. Значения не приводятся к тексту: дата, пришедшая
#: датой, не имеет неоднозначности порядка частей.
Table = list[list[Any]]


class FormatError(RuntimeError):
    """Файл не читается ни одним из известных способов."""


# ── Что за файл ──────────────────────────────────────────────────────────────


def sniff(data: bytes, file_name: str = "") -> str:
    """Определить формат по первым байтам. Расширение — только подсказка.

    Возвращает одно из: pdf, xlsx, xls, html, xml2003, 1c, image, text.
    """
    head = data[:2048]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "xlsx"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "xls"
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff")):
        return "image"

    text = _peek_text(head)
    stripped = text.lstrip("﻿ \t\r\n")
    if stripped.startswith("1CClientBankExchange"):
        return "1c"
    lowered = stripped[:2048].lower()
    if lowered.startswith("<"):
        if "urn:schemas-microsoft-com:office:spreadsheet" in lowered or "<workbook" in lowered:
            return "xml2003"
        return "html"
    return "text"


def _peek_text(head: bytes) -> str:
    for encoding in ("utf-8", "cp1251"):
        try:
            return head.decode(encoding)
        except UnicodeDecodeError:
            continue
    return head.decode("latin-1")


def decode_text(data: bytes) -> str:
    """Текст файла. Кодировку выбираем по тому, что прочиталось без ошибок.

    UTF-16 проверяется по метке порядка байт, а не перебором: cp1251 «читает»
    любой набор байт, и UTF-16 без проверки метки превращался бы в кашу из
    букв с нулями между ними.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise FormatError("Не удалось определить кодировку файла")


# ── Старый Excel (.xls) ──────────────────────────────────────────────────────


def read_xls(data: bytes) -> list[Table]:
    """Excel 97–2003. Даты возвращаются датами, как и из xlsx."""
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover — библиотека в зависимостях
        raise FormatError(
            "На сервере нет чтения старого Excel (.xls). Сохраните файл как .xlsx."
        ) from exc

    try:
        book = xlrd.open_workbook(file_contents=data, on_demand=True)
    except Exception as exc:  # noqa: BLE001 — битый файл даёт любую ошибку
        raise FormatError(f"Файл не открывается как Excel 97–2003: {exc}") from exc

    tables: list[Table] = []
    try:
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            table: Table = []
            for row_index in range(sheet.nrows):
                row: list[Any] = []
                for col_index in range(sheet.ncols):
                    cell = sheet.cell(row_index, col_index)
                    row.append(_xls_value(xlrd, cell, book.datemode))
                table.append(row)
            tables.append(table)
    finally:
        book.release_resources()
    return tables


def _xls_value(xlrd: Any, cell: Any, datemode: int) -> Any:
    kind = cell.ctype
    if kind in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if kind == xlrd.XL_CELL_DATE:
        try:
            moment = xlrd.xldate_as_datetime(cell.value, datemode)
        except Exception:  # noqa: BLE001 — дата за пределами календаря Excel
            return cell.value
        return moment if (moment.hour or moment.minute or moment.second) else moment.date()
    if kind == xlrd.XL_CELL_NUMBER:
        value = cell.value
        return int(value) if float(value).is_integer() else value
    if kind == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if kind == xlrd.XL_CELL_ERROR:
        return None
    return cell.value


# ── HTML-таблица под видом Excel ─────────────────────────────────────────────


class _TableCollector(HTMLParser):
    """Все строки всех таблиц документа — в порядке, в котором они закрылись.

    Вложенные таблицы (реквизиты в одной, операции в другой, обе внутри
    таблицы-обёртки) не редкость. Текст попадает только в самую внутреннюю
    открытую ячейку, иначе строка-обёртка повторила бы всю выписку одной
    ячейкой.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: Table = []
        self._tables: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._tables.append({"row": None, "cell": None})
        elif not self._tables:
            return
        elif tag == "tr":
            self._close_row()
            self._tables[-1]["row"] = []
        elif tag in ("td", "th"):
            table = self._tables[-1]
            if table["row"] is None:
                table["row"] = []
            self._close_cell()
            span = dict(attrs).get("colspan") or "1"
            table["cell"] = {"text": [], "span": int(span) if span.isdigit() else 1}
        elif tag == "br":
            self._text("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self._tables:
            return
        if tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_row()
        elif tag == "table":
            self._close_row()
            self._tables.pop()

    def handle_data(self, data: str) -> None:
        self._text(data)

    def _text(self, data: str) -> None:
        if self._tables and self._tables[-1]["cell"] is not None:
            self._tables[-1]["cell"]["text"].append(data)

    def _close_cell(self) -> None:
        table = self._tables[-1]
        cell = table["cell"]
        if cell is None:
            return
        text = "".join(cell["text"])
        text = "\n".join(re.sub(r"[ \t\r\f\v\xa0]+", " ", part).strip() for part in text.split("\n"))
        text = text.strip("\n")
        table["row"].append(text or None)
        # Объединённая ячейка занимает несколько колонок — пустыми ячейками
        # за ней, как в Excel. Иначе всё правее съехало бы влево.
        table["row"].extend([None] * (cell["span"] - 1))
        table["cell"] = None

    def _close_row(self) -> None:
        table = self._tables[-1]
        self._close_cell()
        if table["row"] is not None:
            self.rows.append(table["row"])
            table["row"] = None


def read_html(data: bytes) -> list[Table]:
    text = decode_text(data)
    # Кодировку объявляют в meta — верим ей, если она расходится с угаданной.
    declared = re.search(r"charset\s*=\s*[\"']?([\w-]+)", text[:4096], re.IGNORECASE)
    if declared:
        try:
            text = data.decode(declared.group(1))
        except (LookupError, UnicodeDecodeError):
            pass
    parser = _TableCollector()
    parser.feed(text)
    parser.close()
    if not parser.rows:
        raise FormatError("В файле-разметке не нашлось ни одной таблицы")
    return [parser.rows]


# ── XML-таблица Excel 2003 ───────────────────────────────────────────────────

_SS = "urn:schemas-microsoft-com:office:spreadsheet"


def read_xml2003(data: bytes) -> list[Table]:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise FormatError(f"Файл не читается как таблица Excel 2003: {exc}") from exc

    tables: list[Table] = []
    for sheet in root.iter(f"{{{_SS}}}Worksheet"):
        table: Table = []
        for row in sheet.iter(f"{{{_SS}}}Row"):
            cells: list[Any] = []
            for cell in row.findall(f"{{{_SS}}}Cell"):
                index = cell.get(f"{{{_SS}}}Index")
                if index and index.isdigit():
                    # Пропущенные пустые ячейки XML не пишет вовсе, а
                    # указывает номер следующей непустой.
                    cells.extend([None] * max(0, int(index) - 1 - len(cells)))
                data_node = cell.find(f"{{{_SS}}}Data")
                cells.append(_xml_value(data_node))
                merge = cell.get(f"{{{_SS}}}MergeAcross")
                if merge and merge.isdigit():
                    cells.extend([None] * int(merge))
            table.append(cells)
        tables.append(table)
    if not tables:
        raise FormatError("В файле нет ни одного листа")
    return tables


def _xml_value(node: ElementTree.Element | None) -> Any:
    if node is None:
        return None
    text = "".join(node.itertext())
    kind = node.get(f"{{{_SS}}}Type") or "String"
    if kind == "Number":
        try:
            number = float(text)
        except ValueError:
            return text
        return int(number) if number.is_integer() else number
    if kind == "DateTime":
        try:
            moment = datetime.fromisoformat(text.split(".")[0])
        except ValueError:
            return text
        return moment if (moment.hour or moment.minute or moment.second) else moment.date()
    return text or None


# ── Текст с разделителями ────────────────────────────────────────────────────


def read_csv(data: bytes) -> list[Table]:
    text = decode_text(data)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if sample.count(";") > sample.count(",") else ","
    return [[list(row) for row in csv.reader(io.StringIO(text), dialect)]]


# ── Выгрузка банк-клиента для 1С ─────────────────────────────────────────────
#
# Формат `1CClientBankExchange` — общий язык банков и бухгалтерии: его отдают
# почти все интернет-банки для юрлиц, и в Казахстане, и в России. Это не
# таблица, а записи: секция счёта с остатками и секции документов.
#
# Направление платежа в записи не написано. Оно следует из счетов: если счёт
# плательщика — наш, деньги ушли; если счёт получателя — пришли. Если не наш
# ни тот, ни другой, строка ляжет с суммой без направления — и общий разбор
# отложит её с вопросом, а не угадает.

_1C_PAYER_ACCOUNT = ("ПлательщикСчет", "ПлательщикРасчСчет", "ПлательщикИИК")
_1C_PAYEE_ACCOUNT = ("ПолучательСчет", "ПолучательРасчСчет", "ПолучательИИК")
_1C_PAYER_NAME = ("Плательщик1", "Плательщик", "ПлательщикНаименование")
_1C_PAYEE_NAME = ("Получатель1", "Получатель", "ПолучательНаименование")
_1C_PAYER_ID = ("ПлательщикБИН", "ПлательщикИИН", "ПлательщикИНН", "ПлательщикБИНИИН")
_1C_PAYEE_ID = ("ПолучательБИН", "ПолучательИИН", "ПолучательИНН", "ПолучательБИНИИН")


def read_1c(data: bytes) -> list[Table]:
    text = decode_text(data)
    if re.search(r"^Кодировка\s*=\s*DOS", text[:2048], re.MULTILINE):
        try:
            text = data.decode("cp866")
        except UnicodeDecodeError:
            pass

    header: dict[str, str] = {}
    accounts: list[dict[str, str]] = []
    documents: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    target = header
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "СекцияРасчСчет":
            current = {}
            accounts.append(current)
            target = current
            continue
        if line.startswith("СекцияДокумент"):
            current = {"ВидДокумента": line.partition("=")[2].strip()}
            documents.append(current)
            target = current
            continue
        if line in ("КонецРасчСчет", "КонецДокумента"):
            current = None
            target = header
            continue
        key, sep, value = line.partition("=")
        if sep:
            target.setdefault(key.strip(), value.strip())

    ours = {
        _account_key(value)
        for value in [header.get("РасчСчет", "")] + [item.get("РасчСчет", "") for item in accounts]
        if value
    }
    if not documents:
        raise FormatError("В выгрузке 1С нет ни одного документа")

    table: Table = []
    main_account = accounts[0].get("РасчСчет") if accounts else header.get("РасчСчет", "")
    period_start = (accounts[0].get("ДатаНачала") if accounts else "") or header.get("ДатаНачала", "")
    period_end = (accounts[-1].get("ДатаКонца") if accounts else "") or header.get("ДатаКонца", "")
    # Реквизиты — теми же словами, какими их печатает банк в Excel-выписке:
    # общий разбор узнаёт их по подписям.
    # Несколько своих счетов в одной выгрузке — тогда у каждой строки своя
    # колонка «Счёт», а общих реквизитов счёта и остатков нет: они разные.
    many = len(ours) > 1
    if not many and main_account:
        table.append(["Текущий счет:", main_account])
    if period_start or period_end:
        table.append(["Период:", f"{period_start} - {period_end}"])
    if accounts and not many:
        if accounts[0].get("НачальныйОстаток"):
            table.append(["Входящий остаток:", accounts[0]["НачальныйОстаток"]])
        if accounts[-1].get("КонечныйОстаток"):
            table.append(["Исходящий остаток:", accounts[-1]["КонечныйОстаток"]])

    owner_name = owner_id = ""
    rows: Table = []
    for doc in documents:
        payer_account = _account_key(_first(doc, _1C_PAYER_ACCOUNT))
        payee_account = _account_key(_first(doc, _1C_PAYEE_ACCOUNT))
        amount = doc.get("Сумма", "")
        debit = credit = plain = None
        if payer_account in ours and payee_account not in ours:
            debit = amount
            side = "payee"
        elif payee_account in ours and payer_account not in ours:
            credit = amount
            side = "payer"
        elif payer_account in ours and payee_account in ours:
            # Перевод между двумя своими счетами одной выгрузки: списание со
            # счёта плательщика, второй счёт — «контрагент».
            debit = amount
            side = "payee"
        else:
            plain = amount
            side = "payee"
        if side == "payee" and payer_account in ours and not owner_name:
            owner_name, owner_id = _first(doc, _1C_PAYER_NAME), _first(doc, _1C_PAYER_ID)
        if side == "payer" and not owner_name:
            owner_name, owner_id = _first(doc, _1C_PAYEE_NAME), _first(doc, _1C_PAYEE_ID)

        names, ids, accts = (
            (_1C_PAYEE_NAME, _1C_PAYEE_ID, _1C_PAYEE_ACCOUNT)
            if side == "payee"
            else (_1C_PAYER_NAME, _1C_PAYER_ID, _1C_PAYER_ACCOUNT)
        )
        when = (
            (doc.get("ДатаСписано") if debit else doc.get("ДатаПоступило"))
            or doc.get("Дата", "")
        )
        our_account = (
            _first(doc, _1C_PAYER_ACCOUNT) if debit else _first(doc, _1C_PAYEE_ACCOUNT) if credit else ""
        )
        row = [doc.get("Номер", ""), _1c_date(when), debit, credit, plain]
        if many:
            row.append(our_account)
        row += [
            _first(doc, names),
            _first(doc, ids),
            _first(doc, accts),
            doc.get("КНП") or doc.get("КодНазначенияПлатежа") or doc.get("КодНазПлатежа") or "",
            doc.get("НазначениеПлатежа", "") or doc.get("НазначениеПлатежа1", ""),
        ]
        rows.append(row)

    if owner_name:
        table.append(["Наименование:", owner_name])
    if owner_id:
        table.append(["ИИН/БИН:", owner_id])
    table.append([])
    head = ["№ документа", "Дата операции", "Дебет", "Кредит", "Сумма"]
    if many:
        head.append("Счёт")
    head += ["Контрагент", "БИН контрагента", "Счёт контрагента", "КНП", "Назначение платежа"]
    table.append(head)
    table.extend(rows)
    return [table]


def _1c_date(text: str) -> Any:
    """Дата 1С — всегда «ДД.ММ.ГГГГ», так записано в самом формате обмена.

    Отдаём её датой, а не текстом: иначе выгрузка за первые двенадцать дней
    месяца упиралась бы в вопрос «как записаны даты», на который формат уже
    ответил.
    """
    try:
        return datetime.strptime(text.strip(), "%d.%m.%Y").date()
    except ValueError:
        return text


def _first(doc: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        if doc.get(key):
            return doc[key]
    return ""


def _account_key(value: str) -> str:
    return re.sub(r"[\s\-]", "", value or "").upper()


# ── Точка входа ──────────────────────────────────────────────────────────────


def read_tables(data: bytes, kind: str) -> list[Table]:
    """Таблицы файла уже определённого формата (кроме xlsx и pdf).

    xlsx читается в `importing` — у него своё чтение с выбором вкладки, и
    переносить его сюда ради симметрии значило бы менять проверенное.
    """
    if kind == "xls":
        return read_xls(data)
    if kind == "html":
        return read_html(data)
    if kind == "xml2003":
        return read_xml2003(data)
    if kind == "1c":
        return read_1c(data)
    if kind == "text":
        return read_csv(data)
    raise FormatError(f"Формат «{kind}» таблицей не читается")


__all__ = [
    "FormatError",
    "Table",
    "decode_text",
    "read_1c",
    "read_csv",
    "read_html",
    "read_tables",
    "read_xls",
    "read_xml2003",
    "sniff",
]
