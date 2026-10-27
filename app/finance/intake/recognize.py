"""Что значит строка листа: одна из шести форм, или вопрос, или отказ.

Для каждого листа и каждой формы ищется строка шапки, где форма узнаёт
больше всего колонок (шапка в две строки складывается - `header.compose`).
Форма выигрывает, если её колонок больше, чем у остальных, и узнано то, без
чего строка не эта форма (`Form.needs`). Таблица может назвать себя сама:
«Оборотно-сальдовая ведомость», «Выписка», «Реализация товаров и услуг» в
имени файла, листа или над шапкой - довод в пользу формы.

Не угадываем: две формы почти поровну - вопрос «что значит строка» с
обеими; ни одной - «не понимаю лист», а не самая похожая. Две колонки на
одни деньги («Сумма» и «Сумма») - отказ читать лист: какая из них что, по
названию не понять. Скрытые строки - вопрос: их спрятал человек, и черновик
договора, который не заключили, заводить молча нельзя.

Ничего не пишет: это «предложить» для файла - что KORT видит и о чём
спросит, до того как разбор формы что-то заведёт.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.finance.intake import forms as forms_module
from app.finance.intake.forms import Field, Form
from app.finance.intake.header import best_row, compose, core, text
from app.finance.intake.rows import has_value, is_empty, is_numbering, is_total
from app.finance.intake.tables import Table
from app.finance.layout import norm, squash

#: Мягкое совпадение: одно начало другого, и короче - не меньше этой доли длинного.
LOOSE_MIN_RATIO = 0.6
#: Вторая форма считается «почти поровну», если её счёт не меньше этой доли первой.
CLOSE_RATIO = 0.8
#: Довод «таблица назвала себя сама» - в очках совпадения колонок.
WORD_BONUS = 3
_ROW_NUMBER = frozenset({"№", "#", "n", "no", "№ п/п", "п/п"})


@dataclass(frozen=True)
class Match:
    keys: tuple[str, ...]
    #: exact | core | squashed | loose | none
    how: str

    @property
    def strength(self) -> int:
        return {"exact": 2, "core": 2, "squashed": 2, "loose": 1}.get(self.how, 0)


class Matcher:
    """Колонка формы по заголовку: ярусы от строгого к мягкому, спор - список кандидатов."""

    def __init__(self, fields: Sequence[Field]):
        self.fields = list(fields)
        self.by_name: dict[str, list[str]] = {}
        self.by_squashed: dict[str, list[str]] = {}
        self.squashed: dict[str, set[str]] = {}
        for item in self.fields:
            for name in item.names:
                self.by_name.setdefault(name, []).append(item.key)
                squashed = squash(name)
                if squashed:
                    self.by_squashed.setdefault(squashed, []).append(item.key)
                    self.squashed.setdefault(item.key, set()).add(squashed)
        self._cache: dict[str, Match] = {}

    def match(self, value: Any) -> Match:
        raw = text(value)
        cached = self._cache.get(raw)
        if cached is not None:
            return cached
        found = self._match(raw)
        self._cache[raw] = found
        return found

    def _match(self, raw: str) -> Match:
        full = norm(raw)
        if not full or full in _ROW_NUMBER:
            return Match((), "none")
        bare = core(raw)
        squashed = squash(bare)
        for how, keys in (
            ("exact", self.by_name.get(full, [])),
            ("core", self.by_name.get(bare, []) if bare else []),
            ("squashed", self.by_squashed.get(squashed, []) if squashed else []),
        ):
            if keys:
                return Match(tuple(dict.fromkeys(keys)), how)
        if len(squashed) >= 4:
            keys = [key for key, names in self.squashed.items() if any(_loose(squashed, name) for name in names)]
            if keys:
                return Match(tuple(keys), "loose")
        return Match((), "none")

    def strength(self, value: Any) -> int:
        return self.match(value).strength

    def score(self, row: Sequence[Any]) -> int:
        """Сколько колонок формы узнано в строке: ключ считается один раз, по лучшей ячейке."""
        best: dict[str, int] = {}
        for cell in row:
            if not isinstance(cell, str) or not cell.strip():
                continue
            found = self.match(cell)
            if len(found.keys) == 1:
                key = found.keys[0]
                best[key] = max(best.get(key, 0), found.strength)
        return sum(best.values())


def _loose(actual: str, wanted: str) -> bool:
    if len(actual) < 4 or len(wanted) < 4:
        return False
    shorter, longer = sorted((actual, wanted), key=len)
    return longer.startswith(shorter) and len(shorter) / len(longer) >= LOOSE_MIN_RATIO


@dataclass
class Placed:
    index: int
    header: str
    key: str | None
    how: str
    candidates: tuple[str, ...] = ()


@dataclass
class Reading:
    """Как лист читается одной формой: шапка, колонки, счёт."""

    form: Form
    header_index: int
    body_start: int
    header: list[str]
    columns: list[Placed]
    score: int
    needs_ok: bool
    word: int = 0
    refusals: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.score + self.word

    def at(self, key: str) -> int | None:
        return next((column.index for column in self.columns if column.key == key), None)


@dataclass
class Sheet:
    """Что KORT видит в листе."""

    name: str
    form: str | None
    title: str = ""
    reading: Reading | None = None
    alternatives: list[Reading] = field(default_factory=list)
    rows: int = 0
    service: dict[str, int] = field(default_factory=dict)
    hidden: list[int] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        reading = self.reading
        return {
            "sheet": self.name,
            "form": self.form,
            "form_title": reading.form.title if reading else None,
            "title": self.title,
            "header_line": reading.header_index + 1 if reading else None,
            "body_line": reading.body_start + 1 if reading else None,
            "columns": [
                {
                    "index": column.index,
                    "header": column.header.strip(),
                    "key": column.key,
                    "title": (reading.form.field(column.key).title if column.key and reading.form.field(column.key) else None),
                    "how": column.how,
                    "candidates": list(column.candidates),
                }
                for column in (reading.columns if reading else [])
            ],
            "rows": self.rows,
            "service": self.service,
            "hidden": self.hidden,
            "alternatives": [
                {"form": item.form.key, "title": item.form.title, "score": item.total} for item in self.alternatives
            ],
            "score": reading.total if reading else 0,
            "questions": self.questions,
            "refusals": self.refusals,
        }


def _haystack(*parts: str) -> str:
    return " ".join(norm(part) for part in parts if part)


def _words_hit(words: Sequence[str], haystack: str) -> bool:
    return any(word in haystack for word in words)


def _title(rows: Sequence[Sequence[Any]], header_index: int) -> str:
    """Название над шапкой: первая строка из одной текстовой ячейки, не дата и не «на 20.10.2026»."""
    for row in rows[:max(header_index, 0)]:
        cells = [cell for cell in row if has_value(cell)]
        if len(cells) != 1 or not isinstance(cells[0], str):
            continue
        value = re.sub(r"\s+", " ", cells[0]).strip()
        if re.fullmatch(r"(на|по|с|от|за)?\s*[\d.\-/ ]+(г\.?)?", value, re.IGNORECASE):
            continue
        return value
    return ""


def read_as(table: Table, form: Form, *, matcher: Matcher | None = None) -> Reading | None:
    """Лист глазами одной формы; `None` - шапки этой формы в листе нет."""
    matcher = matcher or Matcher(form.fields)
    rows = table.rows
    index, _score = best_row(rows, matcher.score)
    if index < 0:
        return None
    header, body_start = compose(rows, index, matcher.strength, matcher.score, table=table)
    columns: list[Placed] = []
    by_key: dict[str, list[Placed]] = {}
    for position, cell in enumerate(header):
        if not squash(cell):
            continue
        found = matcher.match(cell)
        placed = Placed(position, cell, found.keys[0] if len(found.keys) == 1 else None, found.how, found.keys if len(found.keys) > 1 else ())
        columns.append(placed)
        if placed.key:
            by_key.setdefault(placed.key, []).append(placed)
    reading = Reading(form, index, body_start, header, columns, 0, False)
    score = 0
    for key, placed in by_key.items():
        best = max(Match((key,), item.how).strength for item in placed)
        top = [item for item in placed if Match((key,), item.how).strength == best]
        score += best
        if len(top) > 1:
            item = form.field(key)
            names = ", ".join(f"«{entry.header.strip()}»" for entry in top)
            if item is not None and item.money:
                reading.refusals.append({
                    "kind": "duplicate",
                    "key": key,
                    "columns": [entry.index for entry in top],
                    "text": f"Две колонки на одни деньги - {names}: какая из них что, по названию не понять. Лист не читается, пока одну не переименуют.",
                })
            else:
                reading.questions.append({
                    "kind": "column",
                    "key": key,
                    "columns": [entry.index for entry in top],
                    "text": f"«{item.title if item else key}» - в колонках {names}. Какая из них?",
                })
            # Спорные колонки ключа не получают: брать первую - угадывать.
            for entry in top:
                entry.key = None
                entry.candidates = (key,)
    for placed in columns:
        if placed.key is None and placed.candidates and not any(placed.index in q.get("columns", []) for q in reading.questions + reading.refusals):
            titles = [form.field(key).title for key in placed.candidates if form.field(key)]
            reading.questions.append({
                "kind": "column",
                "columns": [placed.index],
                "candidates": list(placed.candidates),
                "text": f"Колонка «{placed.header.strip()}» подходит сразу к нескольким: {', '.join(titles)}. Какая?",
            })
    reading.score = score
    placed_keys = {column.key for column in columns if column.key}
    reading.needs_ok = all(any(key in placed_keys for key in group) for group in form.needs)
    return reading


def recognize(table: Table, file_name: str = "", *, catalog: Sequence[Form] | None = None) -> Sheet:
    """Лист → форма, колонки, вопросы и отказы. Ничего не пишет."""
    catalog = list(catalog or forms_module.forms())
    readings: list[Reading] = []
    for form in catalog:
        reading = read_as(table, form)
        if reading is None:
            continue
        haystack = _haystack(file_name, table.name, *(text(cell) for row in table.rows[: reading.header_index] for cell in row))
        if _words_hit(form.words, haystack):
            reading.word += WORD_BONUS
        if _words_hit(form.not_words, haystack):
            reading.word -= WORD_BONUS
        readings.append(reading)
    fit = sorted((item for item in readings if item.needs_ok and item.score > 0), key=lambda item: -item.total)
    sheet = Sheet(table.name, None)
    if not fit:
        sheet.title = _title(table.rows, 40)
        sheet.questions.append({
            "kind": "form",
            "text": f"Лист «{table.name}»: не понимаю, что значит строка - ни одна форма не узнала своих колонок.",
            "options": [{"value": form.key, "label": form.title, "example": form.examples} for form in catalog],
        })
        return sheet
    best = fit[0]
    sheet.reading = best
    sheet.form = best.form.key
    sheet.alternatives = fit[1:3]
    sheet.title = _title(table.rows, best.header_index)
    if len(fit) > 1 and fit[1].total >= best.total * CLOSE_RATIO:
        sheet.form = None
        sheet.questions.append({
            "kind": "form",
            "text": f"Лист «{table.name}»: строка похожа и на «{best.form.title}», и на «{fit[1].form.title}». Что значит строка?",
            "options": [{"value": item.form.key, "label": item.form.title, "example": item.form.examples} for item in fit[:2]],
        })
    sheet.questions.extend(best.questions)
    sheet.refusals.extend(best.refusals)
    _count_rows(table, best, sheet)
    return sheet


def _count_rows(table: Table, reading: Reading, sheet: Sheet) -> None:
    """Строки данных, служебные и скрытые - под шапкой."""
    placed = [column.index for column in reading.columns if column.key]
    service = {"total": 0, "numbering": 0, "empty": 0}
    hidden: list[int] = []
    rows = 0
    for offset, cells in enumerate(table.rows[reading.body_start:]):
        line = reading.body_start + offset
        if is_empty(cells):
            service["empty"] += 1
            continue
        if offset < 2 and is_numbering(cells):
            service["numbering"] += 1
            continue
        if is_total(cells):
            service["total"] += 1
            continue
        filled = sum(1 for index in placed if index < len(cells) and has_value(cells[index]))
        if filled < min(2, len(placed)):
            continue
        if line in table.hidden:
            hidden.append(line + 1)
            continue
        rows += 1
    sheet.rows = rows
    sheet.service = {key: value for key, value in service.items() if value}
    sheet.hidden = hidden
    if hidden:
        count = len(hidden)
        sheet.questions.append({
            "kind": "hidden",
            "lines": hidden,
            "text": (
                f"Лист «{table.name}»: {count} скрыт{'ая строка' if count == 1 else 'ые строки'} "
                f"({', '.join(map(str, hidden[:5]))}{'…' if count > 5 else ''}). Их спрятал человек - "
                "завести их или пропустить?"
            ),
            "options": [{"value": "skip", "label": "Пропустить"}, {"value": "take", "label": "Завести"}],
        })


def recognize_file(tables: Sequence[Table], file_name: str = "", *, catalog: Sequence[Form] | None = None) -> list[Sheet]:
    catalog = list(catalog or forms_module.forms())
    return [recognize(table, file_name, catalog=catalog) for table in tables if any(not is_empty(row) for row in table.rows)]


def describe(data: bytes, file_name: str, *, contract_fields: Sequence[Any] = ()) -> dict[str, Any]:
    """«Предложить» для файла: что KORT видит в каждом листе и о чём спросит. Ничего не пишет.

    `contract_fields` - поля реестра компании: свои названия колонок договора
    узнаются так же, как системные. PDF KORT читает только как выписку банка -
    так и отвечает, не разбирая.
    """
    from app.finance import formats
    from app.finance.intake import tables as tables_module

    if formats.sniff(data, file_name) == "pdf":
        payment = next(form for form in forms_module.forms(contract_fields) if form.key == forms_module.PAYMENT)
        return {
            "file": file_name,
            "pdf": True,
            "sheets": [{"sheet": file_name, "form": payment.key, "form_title": payment.title, "questions": [], "refusals": [],
                        "columns": [], "rows": None, "note": "PDF читается разбором выписок банка"}],
        }
    try:
        found = tables_module.read(data, file_name, detail=True)
    except tables_module.TableError as exc:
        return {"file": file_name, "error": str(exc), "sheets": []}
    catalog = forms_module.forms(contract_fields)
    return {"file": file_name, "sheets": [sheet.to_dict() for sheet in recognize_file(found, file_name, catalog=catalog)]}


__all__ = ["Matcher", "Reading", "Sheet", "describe", "read_as", "recognize", "recognize_file"]
