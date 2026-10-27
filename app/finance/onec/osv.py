"""ОСВ по счёту 1210 файлом: долги покупателей на первое число по договорам.

Бухгалтер с 1С на своём компьютере выгружает «Оборотно-сальдовую ведомость
по счёту» с группировкой «Контрагенты → Договоры». Форма отчёта во всех
компаниях одна: название и период над шапкой, шапка в две строки («Сальдо на
начало периода» над «Дебет | Кредит»), строка счёта, потом контрагенты и под
каждым его договоры, внизу «Итого». Уровень строки 1С ставит отступом первой
ячейки или группировкой строк (`Table.levels`); где их нет (CSV), договор
узнаётся по слову «договор» в подписи.

Сальдо на начало = дебет - кредит: кредитовое сальдо - аванс клиента, со
знаком минус (полигон, С3). Модуль ничего не пишет: он читает отчёт, а куда
сальдо ляжет, решает `core/onec_start.py`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.finance.intake import forms, recognize, tables
from app.finance.intake.rows import has_value, is_total
from app.finance.intake.values import parse_money
from app.finance.layout import norm

_ZERO = Decimal("0")
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_PERIOD = re.compile(r"с\s+(\d{1,2})\.(\d{1,2})\.(\d{4})\s*(?:г\.?)?\s+по\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", re.IGNORECASE)
_MONTH = re.compile(r"\bза\s+([а-яё]+)\s+(\d{4})", re.IGNORECASE)
_ACCOUNT = re.compile(r"^\d{4}(?:\.\d+)?$")
#: «Договор № ТС-03/2025 от 15.01.2025» - номер и дата договора из подписи 1С.
_CONTRACT = re.compile(r"№\s*(.+?)\s+от\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", re.IGNORECASE)
_FILTER = re.compile(r"организация\s+(?:в списке|равно)\s+\"(.+)\"\s*$", re.IGNORECASE)


class OsvError(RuntimeError):
    """Файл не читается как ОСВ по счёту - с текстом для человека."""


@dataclass(frozen=True)
class Line:
    """Договор в ОСВ: чей, как подписан и сальдо."""

    party: str
    contract: str
    number: str
    signed: date | None
    opening: Decimal
    debit: Decimal
    credit: Decimal
    closing: Decimal
    line: int

    @property
    def main(self) -> bool:
        """«Основной договор» - договор 1С по умолчанию, без номера."""
        return not self.number


@dataclass
class Osv:
    file: str
    sheet: str
    start: date | None
    end: date | None
    account: str
    organizations: list[str] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    opening_total: Decimal = _ZERO
    closing_total: Decimal = _ZERO
    problems: list[str] = field(default_factory=list)

    def to_store(self) -> dict[str, Any]:
        """Разобранная ведомость целиком - чтобы шаги «из 1С» не читали файл заново."""
        return {
            **self.to_dict(),
            "opening_total": format(self.opening_total, "f"),
            "closing_total": format(self.closing_total, "f"),
            "lines": [
                {
                    "party": line.party, "contract": line.contract, "number": line.number,
                    "signed": line.signed.isoformat() if line.signed else None, "opening": format(line.opening, "f"),
                    "debit": format(line.debit, "f"), "credit": format(line.credit, "f"), "closing": format(line.closing, "f"),
                    "line": line.line,
                }
                for line in self.lines
            ],
        }

    @classmethod
    def from_store(cls, data: dict[str, Any]) -> "Osv":
        day = lambda value: date.fromisoformat(value) if value else None  # noqa: E731
        return cls(
            file=data.get("file") or "",
            sheet=data.get("sheet") or "",
            start=day(data.get("start")),
            end=day(data.get("end")),
            account=data.get("account") or "",
            organizations=list(data.get("organizations") or []),
            lines=[
                Line(item["party"], item["contract"], item["number"], day(item.get("signed")), Decimal(item["opening"]),
                     Decimal(item["debit"]), Decimal(item["credit"]), Decimal(item["closing"]), int(item["line"]))
                for item in data.get("lines") or []
            ],
            opening_total=Decimal(data.get("opening_total") or "0"),
            closing_total=Decimal(data.get("closing_total") or "0"),
            problems=list(data.get("problems") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "sheet": self.sheet,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "account": self.account,
            "organizations": self.organizations,
            "contracts": len(self.lines),
            "opening": format(self.opening_total, "f"),
            "closing": format(self.closing_total, "f"),
            "problems": self.problems,
        }


def _money(cells: list[Any], index: int | None) -> Decimal:
    if index is None or index >= len(cells):
        return _ZERO
    money = parse_money(cells[index])
    if money is None:
        return _ZERO
    return -money.value if money.negative else money.value


def period_of(texts: list[str]) -> tuple[date | None, date | None]:
    """Период отчёта из строк над шапкой: «с 01.06.2026 по 30.06.2026» или «за Июнь 2026 г.»."""
    for text in texts:
        found = _PERIOD.search(text)
        if found:
            day, month, year, day2, month2, year2 = (int(part) for part in found.groups())
            try:
                return date(year, month, day), date(year2, month2, day2)
            except ValueError:
                return None, None
    for text in texts:
        found = _MONTH.search(text)
        if not found:
            continue
        word = norm(found.group(1))
        month = next((number for stem, number in _MONTHS.items() if word.startswith(stem)), None)
        if month is None:
            continue
        year = int(found.group(2))
        start = date(year, month, 1)
        end = date(year + (month == 12), month % 12 + 1, 1)
        return start, date.fromordinal(end.toordinal() - 1)
    return None, None


def _organizations(texts: list[str]) -> list[str]:
    for text in texts:
        found = _FILTER.search(text.strip())
        if found:
            return [part.strip() for part in found.group(1).split(";") if part.strip()]
    return []


def contract_of(label: str) -> tuple[str, date | None]:
    """Номер и дата договора из подписи; «Основной договор» - без номера."""
    found = _CONTRACT.search(label or "")
    if not found:
        return "", None
    number = found.group(1).strip()
    try:
        signed = date(int(found.group(4)), int(found.group(3)), int(found.group(2)))
    except ValueError:
        signed = None
    return number, signed


def parse(table: tables.Table, file_name: str = "") -> Osv:
    """Лист ОСВ → договоры с сальдо. Не ОСВ - `OsvError` словами."""
    balance = next(form for form in forms.forms() if form.key == forms.BALANCE)
    reading = recognize.read_as(table, balance)
    if reading is None or not reading.needs_ok:
        raise OsvError(f"Лист «{table.name}» не похож на оборотно-сальдовую ведомость: нет колонок сальдо")
    above = [str(cell) for row in table.rows[: reading.header_index] for cell in row if isinstance(cell, str) and cell.strip()]
    start, end = period_of(above)
    out = Osv(file_name, table.name, start, end, "", _organizations(above))
    if start is None:
        out.problems.append("Период отчёта не назван над шапкой - на какой день сальдо, скажет человек")
    at = reading.at
    first = 0
    party = ""
    has_levels = bool(table.levels)
    for offset, cells in enumerate(table.rows[reading.body_start:]):
        line = reading.body_start + offset
        if not cells or not has_value(cells[0]):
            continue
        label = re.sub(r"\s+", " ", str(cells[first])).strip()
        numbers = [index for index in (at("opening_debit"), at("opening_credit"), at("turnover_debit"),
                                       at("turnover_credit"), at("closing_debit"), at("closing_credit")) if index is not None]
        filled = any(index < len(cells) and has_value(cells[index]) for index in numbers)
        if is_total(cells):
            out.opening_total = _money(cells, at("opening_debit")) - _money(cells, at("opening_credit"))
            out.closing_total = _money(cells, at("closing_debit")) - _money(cells, at("closing_credit"))
            continue
        if _ACCOUNT.match(label):
            out.account = out.account or label
            continue
        if not filled:
            # «Контрагенты», «Договоры» - подписи уровней группировки, не данные.
            continue
        level = table.levels.get(line, 0)
        is_contract = level >= 2 if has_levels else "договор" in norm(label)
        if not is_contract:
            party = label
            continue
        number, signed = contract_of(label)
        out.lines.append(
            Line(
                party=party,
                contract=label,
                number=number,
                signed=signed,
                opening=_money(cells, at("opening_debit")) - _money(cells, at("opening_credit")),
                debit=_money(cells, at("turnover_debit")),
                credit=_money(cells, at("turnover_credit")),
                closing=_money(cells, at("closing_debit")) - _money(cells, at("closing_credit")),
                line=line + 1,
            )
        )
    if not out.lines:
        out.problems.append("В ведомости нет ни одного договора - нужна группировка «Контрагенты → Договоры»")
    summed = sum((item.opening for item in out.lines), _ZERO)
    if out.opening_total and summed != out.opening_total:
        out.problems.append(
            f"Сальдо договоров ({format(summed, 'f')}) не сходится с итогом ведомости ({format(out.opening_total, 'f')})"
        )
    return out


def read(data: bytes, file_name: str) -> Osv:
    try:
        found = tables.read(data, file_name, detail=True)
    except tables.TableError as exc:
        raise OsvError(str(exc)) from exc
    errors: list[str] = []
    for table in found:
        try:
            return parse(table, file_name)
        except OsvError as exc:
            errors.append(str(exc))
    raise OsvError(errors[0] if errors else "В файле нет листов")


__all__ = ["Line", "Osv", "OsvError", "contract_of", "parse", "period_of", "read"]
