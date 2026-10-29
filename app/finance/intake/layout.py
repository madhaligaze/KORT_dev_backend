"""Разметка листа: что значит строка и каждая колонка - и строки по этому ответу.

Лист, который KORT узнал сам, читается по каталогу формы. Лист, который он
не узнал («не понимаю лист» у таблицы слушателей), человек размечает один
раз: форма строки, ключ каждой нужной колонки, порядок частей даты, чьи это
договоры, как они начисляются. Ответ ложится в словарь компании
(`core/settings.remember_layout`), и в следующий раз такой лист узнаётся без
вопроса.

Колонки - по названиям, не по номерам (правило проекта): разметка хранит
шапку колонки, а не её место, и лист «Июль» с переставленными колонками
читается той же разметкой (`signature` - набор названий, без порядка).

Ничего не пишет: отдаёт строки ключами формы, сырыми значениями ячеек.
Деньги и даты разбирает тот, кто кладёт строку в модель (`core/excel.py`),
а порядок частей даты решается здесь, один раз на лист
(`values.decide_date_order`): все даты слушателей «07/09/2026» с днём не
больше 12 - вопрос человеку, а не привычный порядок (полигон, Ф3).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from app.finance.intake import forms as forms_module
from app.finance.intake.forms import Form
from app.finance.intake.header import core, text
from app.finance.intake.recognize import Matcher, Reading, read_as
from app.finance.intake.rows import has_value, is_empty, is_label_total, is_numbering, is_total
from app.finance.intake.tables import Table
from app.finance.intake.values import DateReading, decide_date_order
from app.finance.layout import norm, squash

#: Ключи, чьи ячейки - даты: по ним решается порядок частей даты листа.
DATE_KEYS = frozenset({
    "signed_at", "billing_from", "end_date", "planned_end_at", "pay_date", "date", "paid_at", "accrued_at",
    "period_from", "period_to",
})
_REPEAT_NO = re.compile(r"(\d{1,2})\s*$")
#: Ключ «колонка не нужна» в ответе человека.
SKIP = "-"


@dataclass
class Layout:
    """Ответ человека о листе. Пустое поле - KORT решает сам (по каталогу, по данным)."""

    form: str
    #: Шапка колонки как в листе → ключ формы; `SKIP` - колонку не читать.
    columns: dict[str, str] = field(default_factory=dict)
    #: «dmy» / «mdy»; пусто - по данным, а неясно - вопрос.
    date_order: str = ""
    #: Наше юрлицо договоров листа, если колонки исполнителя нет (id участника).
    executor: str = ""
    department: str = ""
    #: Как начисляются договоры листа без своей колонки: `total` / `month`.
    billing: str = ""
    #: Остаток на дату - ISO; пусто - из шапки («Долг на 01.06») или названия листа.
    as_of: str = ""
    #: Месяц строк «договор за месяц» - ISO первого числа; пусто - из названия листа.
    month: str = ""
    #: Скрытые строки: `skip` / `take`; пусто - вопрос.
    hidden: str = ""
    skip: bool = False
    sheet: str = ""

    @classmethod
    def of(cls, data: dict[str, Any] | None) -> "Layout | None":
        """Разметка из ответа; «лист не нужен» - разметка без формы."""
        if not data or not (data.get("form") or data.get("skip")):
            return None
        known = {name for name in cls.__dataclass_fields__}
        values = {key: value for key, value in data.items() if key in known}
        values["columns"] = {str(header): str(key or "") for header, key in (data.get("columns") or {}).items()}
        values["form"] = str(values.get("form") or "")
        values["skip"] = bool(values.get("skip"))
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value not in ("", False, {}, None) or key == "form"}


def signature(header: Sequence[Any]) -> str:
    """Подпись шапки: набор названий колонок без оформления и порядка."""
    names = sorted({squash(core(cell)) for cell in header if squash(core(cell))})
    return "|".join(names)


@dataclass
class Column:
    index: int
    header: str
    key: str | None
    #: catalog - узнана каталогом или словарём компании; answer - назначил человек; none - не нужна.
    how: str
    #: Номер повтора у колонок-повторов («Оплата 2» - 2).
    repeat: int = 0


@dataclass
class Row:
    #: Номер строки листа, с 1 - как видит человек.
    line: int
    values: dict[str, Any]
    #: Ключ повтора → значения по номерам повтора (1, 2, 3…).
    repeats: dict[str, dict[int, Any]] = field(default_factory=dict)


@dataclass
class Read:
    """Лист по разметке: колонки, строки, порядок даты, вопросы и отказы."""

    sheet: str
    form: Form
    reading: Reading | None
    columns: list[Column]
    rows: list[Row]
    dates: DateReading
    hidden: list[int] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)

    def has(self, key: str) -> bool:
        return any(column.key == key for column in self.columns)


def total_rule(form: str) -> Any:
    """Как узнать строку итога листа этой формы.

    Платёж - строка выписки: итогом бывает и «Остаток на конец» без даты, слово
    итога ищется во всей строке (`is_total`). Остальные формы - записи (договор,
    участник, документ, сальдо): итог - только подпись «Итого»/«Всего» единственным
    текстом строки (`is_label_total`), иначе договор с примечанием «в итоге» пропадал.
    """
    from app.finance.intake.forms import PAYMENT

    return is_total if form == PAYMENT else is_label_total


def read(table: Table, form: Form, layout: Layout | None = None) -> Read:
    """Строки листа ключами формы: каталог (со словарём компании) и поверх - ответ человека."""
    matcher = Matcher(form.fields)
    answered = {norm(header): key for header, key in ((layout.columns if layout else {}) or {}).items()}
    if answered:
        # Ответ человека - тоже написание: шапка, которую он назначил, находит строку шапки.
        extra: dict[str, list[str]] = {}
        for header, key in answered.items():
            if key and key != SKIP:
                extra.setdefault(key, []).append(header)
        form = forms_module.with_words(form, extra)
        matcher = Matcher(form.fields)
    reading = read_as(table, form, matcher=matcher)
    if reading is None:
        return Read(table.name, form, None, [], [], DateReading())
    columns: list[Column] = []
    repeats = {item.key for item in form.fields if item.repeat}
    counters: dict[str, int] = {}
    for placed in reading.columns:
        header = placed.header.strip()
        key, how = placed.key, "catalog" if placed.key else "none"
        chosen = answered.get(norm(header))
        if chosen is not None:
            key, how = (None, "none") if chosen in ("", SKIP) else (chosen, "answer")
        number = 0
        if key in repeats:
            found = _REPEAT_NO.search(norm(header))
            counters[key] = counters.get(key, 0) + 1
            number = int(found.group(1)) if found else counters[key]
        columns.append(Column(placed.index, header, key, how, number))
    out = Read(table.name, form, reading, columns, [], DateReading())
    answered_keys = {column.key for column in columns if column.how == "answer"}
    # Спор каталога, который человек решил ответом, - больше не вопрос и не отказ.
    out.questions = [item for item in reading.questions if item.get("key") not in answered_keys]
    out.refusals = [item for item in reading.refusals if item.get("key") not in answered_keys]
    placed = [column for column in columns if column.key]
    total = total_rule(form.key)
    dates: list[Any] = []
    for offset, cells in enumerate(table.rows[reading.body_start:]):
        line = reading.body_start + offset
        if is_empty(cells) or (offset < 2 and is_numbering(cells)) or total(cells):
            continue
        if sum(1 for column in placed if column.index < len(cells) and has_value(cells[column.index])) < min(2, len(placed)):
            continue
        if line in table.hidden and (layout is None or layout.hidden != "take"):
            out.hidden.append(line + 1)
            continue
        row = Row(line + 1, {})
        for column in placed:
            value = cells[column.index] if column.index < len(cells) else None
            if not has_value(value):
                continue
            if column.key in repeats:
                row.repeats.setdefault(column.key, {})[column.repeat] = value
            else:
                row.values.setdefault(column.key, value)
            if column.key in DATE_KEYS:
                dates.append(value)
        out.rows.append(row)
    out.dates = decide_date_order(dates)
    if layout is not None and layout.date_order in ("dmy", "mdy"):
        out.dates = DateReading(layout.date_order, "так ответил человек")
    elif out.dates.ambiguous:
        samples = ", ".join(f"«{text(item)}»" for item in out.dates.samples[:3])
        out.questions.append({
            "kind": "date_order",
            "text": f"Лист «{table.name}»: даты {samples} читаются и как «день.месяц», и как «месяц.день». Как записаны даты?",
            "options": [{"value": "dmy", "label": "день, месяц, год"}, {"value": "mdy", "label": "месяц, день, год"}],
        })
    if out.hidden and (layout is None or layout.hidden not in ("skip", "take")):
        count = len(out.hidden)
        out.questions.append({
            "kind": "hidden",
            "lines": out.hidden,
            "text": f"Лист «{table.name}»: скрытых строк - {count}. Их спрятал человек - завести их или пропустить?",
            "options": [{"value": "skip", "label": "Пропустить"}, {"value": "take", "label": "Завести"}],
        })
    return out


__all__ = ["DATE_KEYS", "SKIP", "Column", "Layout", "Read", "Row", "read", "signature"]
