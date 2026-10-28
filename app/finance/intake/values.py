"""Значения ячеек: суммы и даты - одни правила для всех входов приёмника.

Перенесено из `importing` как есть (шаг 3 этапа 2): разбор выписок, реестр и
лист договоров читали деньги и даты одними функциями и раньше - теперь они
живут там, где их найдёт любой вход. `importing` отдаёт те же имена.

Два правила отсюда не меняются:
* знак суммы - отдельно от величины: дальше он становится видом операции, и
  «минус приход» невыразим;
* порядок частей даты решается один раз на файл (`decide_date_order`), а не
  в каждой строке, и если данные его не говорят - спрашиваем, а не угадываем.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

_SPACES = re.compile(r"[\s   ]+")
_MONEY_JUNK = re.compile(r"[^0-9,.\-()]")
_DATE_PARTS = re.compile(r"^\s*(\d{1,4})[.\-/\\](\d{1,2})[.\-/\\](\d{1,4})")
#: День недели перед датой. В книгах, которые ведут руками, дата часто записана
#: форматом «ддд ДД.ММ.ГГ» - в журнале денег копии прода это «пн 01.06.26». Для человека
#: это ячейка с датой, для разбора - текст, не начинающийся с цифры: без снятия
#: приставки книга целиком уходила в отложенные строки с «не понял дату».
_WEEKDAY = re.compile(
    r"^\s*(?:пн|вт|ср|чт|пт|сб|вс|понедельник|вторник|среда|четверг|пятница|суббота|"
    r"воскресенье|дүйсенбі|сейсенбі|сәрсенбі|бейсенбі|жұма|сенбі|жексенбі|"
    r"mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)\.?,?\s+",
    re.IGNORECASE,
)
_MONTH_NAMES = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6, "июл": 7,
    "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ── Числа ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Money:
    """Разобранная сумма: величина по модулю и знак отдельно.

    Знак вынесен из величины намеренно: дальше он превращается в вид операции,
    а не в отрицательное число в базе. Тогда запись «минус приход» становится
    невыразимой, а не тихо возможной.
    """

    value: Decimal
    negative: bool


def parse_money(raw: Any) -> Money | None:
    """Сумма из ячейки. Понимает пробелы, запятую, скобки, минус, апостроф.

    Возвращает `None`, если в ячейке нет числа вообще. Пустая ячейка - не
    ошибка: в двухколоночной выписке («Приход»/«Расход») одна из двух всегда
    пуста.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float, Decimal)):
        value = Decimal(str(raw))
        return Money(abs(value), value < 0)

    text = str(raw).strip()
    if not text:
        return None
    text = text.lstrip("'")  # наследие выгрузок 1С: числа как текст
    negative = False
    if text.startswith("(") and text.endswith(")"):
        # Бухгалтерские скобки. Finmap их теряет: «(15 000)» приезжает
        # доходом 15 000, то есть ошибка вдвое больше суммы.
        negative = True
        text = text[1:-1]
    text = _SPACES.sub("", text)
    if text.endswith("-"):  # «1 500-» - так пишет часть банковских выгрузок
        negative = True
        text = text[:-1]
    text = _MONEY_JUNK.sub("", text)
    if text.startswith("-"):
        negative = True
        text = text[1:]
    if not text:
        return None

    # Разделители: последний из «,» и «.» считается десятичным, если после него
    # не три цифры. «1,234.56» → 1234.56; «1.234,56» → 1234.56; «1,234» → 1234.
    comma, dot = text.rfind(","), text.rfind(".")
    cut = max(comma, dot)
    if cut >= 0:
        tail = text[cut + 1 :]
        if len(tail) == 3 and tail.isdigit():
            text = text.replace(",", "").replace(".", "")
        else:
            text = text[:cut].replace(",", "").replace(".", "") + "." + tail
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return Money(abs(value), negative or value < 0)


# ── Даты ─────────────────────────────────────────────────────────────────────


@dataclass
class DateReading:
    """Как читать даты этого файла.

    `order` - «dmy» или «mdy». `evidence` - чем это доказано; попадает в
    протокол импорта, чтобы решение было видно человеку, а не считалось
    магией.
    """

    order: str = "dmy"
    evidence: str = ""
    ambiguous: bool = False
    #: Примеры дат, из-за которых порядок остался неизвестным.
    samples: tuple[str, ...] = ()


def _date_parts(text: str) -> tuple[int, int, int] | None:
    match = _DATE_PARTS.match(_WEEKDAY.sub("", text, count=1))
    if not match:
        return None
    a, b, c = (int(part) for part in match.groups())
    return a, b, c


def decide_date_order(values: Iterable[Any]) -> DateReading:
    """Решить порядок частей даты один раз на файл.

    Правило: если хоть где-то первая часть больше 12 - порядок «день-месяц»;
    если где-то вторая больше 12 - «месяц-день». Нашлось и то и другое - файл
    внутренне противоречив, и это отдельный разговор с человеком, а не выбор
    большинством. Не нашлось ничего - все даты подходят под оба чтения, и тогда
    мы обязаны спросить, а не взять привычное.

    Именно здесь Finmap ошибается тихо: он решает по каждой строке отдельно, и
    в одном файле «12/25/2026» становится 25 декабря, а «08/03/2026» - 8 марта.
    """
    first_big = second_big = 0
    year_first = 0
    samples: list[str] = []
    for value in values:
        if isinstance(value, (datetime, date)):
            continue
        text = str(value or "").strip()
        if not text:
            continue
        parts = _date_parts(text)
        if not parts:
            continue
        a, b, _c = parts
        if a > 31:  # «2026-08-03» - год впереди, порядок однозначен
            year_first += 1
            continue
        if a > 12:
            first_big += 1
        elif b > 12:
            second_big += 1
        elif len(samples) < 5:
            samples.append(text)

    if first_big and second_big:
        return DateReading(
            order="dmy",
            ambiguous=True,
            evidence=(
                f"в файле есть и даты, где первое число больше 12 ({first_big} шт.), "
                f"и даты, где больше 12 второе ({second_big} шт.) - "
                "один и тот же файл записан двумя разными способами"
            ),
            samples=tuple(samples),
        )
    if first_big:
        return DateReading("dmy", f"нашлись даты с числом больше 12 на первом месте ({first_big} шт.)")
    if second_big:
        return DateReading("mdy", f"нашлись даты с числом больше 12 на втором месте ({second_big} шт.)")
    if year_first and not samples:
        return DateReading("dmy", "все даты записаны с года - порядок однозначен")
    if not samples:
        return DateReading("dmy", "даты пришли из Excel датами - порядок не требуется")
    return DateReading(
        order="dmy",
        ambiguous=True,
        evidence="все даты подходят и под «день-месяц», и под «месяц-день»",
        samples=tuple(samples),
    )


def parse_date(raw: Any, reading: DateReading) -> date | None:
    """Дата из ячейки по решённому для файла порядку частей."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        # Серийный номер Excel. Эпоха 1899-12-30 - та же, что в «Книгах»;
        # полдень не добавляем: он округляет дату вверх и уводит книгу на день.
        try:
            from datetime import timedelta

            return (datetime(1899, 12, 30) + timedelta(days=float(raw))).date()
        except (OverflowError, ValueError):
            return None

    text = str(raw).strip()
    if not text:
        return None

    parts = _date_parts(text)
    if parts:
        a, b, c = parts
        if a > 31:
            year, month, day = a, b, c
        elif reading.order == "mdy":
            month, day, year = a, b, c
        else:
            day, month, year = a, b, c
        if year < 100:
            year += 2000 if year < 70 else 1900
        try:
            return date(year, month, day)
        except ValueError:
            return None

    # «3 сентября 2026», «03 сен 2026», «Sep 3, 2026», «пн 3 сентября 2026»
    lowered = _WEEKDAY.sub("", text, count=1).lower().replace(",", " ")
    chunks = _SPACES.sub(" ", lowered).split(" ")
    day = month = year = 0
    for chunk in chunks:
        if chunk.isdigit():
            number = int(chunk)
            if number > 31:
                year = number
            elif not day:
                day = number
        else:
            key = chunk[:3]
            if key in _MONTH_NAMES:
                month = _MONTH_NAMES[key]
    if day and month:
        try:
            return date(year or date.today().year, month, day)
        except ValueError:
            return None
    return None


__all__ = ["DateReading", "Money", "decide_date_order", "parse_date", "parse_money"]
