"""Служебные строки таблицы: не данные, но и не ошибка файла.

Итог, нумерация колонок под шапкой, пустая строка-разделитель нужны человеку
в его книге, и удалять их перед загрузкой никто не станет. Разбор их узнаёт
и пропускает со словами, а не валит файл и не заводит договор «Итого:».
Скрытая строка - особый случай: её спрятал человек, и что она значит, знает
только он (`recognize` спрашивает).
"""
from __future__ import annotations

from typing import Any, Sequence

from app.finance.layout import norm

#: Слова, по которым строка опознаётся как итоговая. Итоговую строку не удаляют
#: перед импортом почти никогда - в книге итог нужен. Finmap на таком файле
#: отказывается целиком («Итого - неверный формат даты»).
TOTAL_WORDS = (
    "итого", "всего", "итог", "баланс", "остаток на конец", "остаток на начало",
    "оборот", "сальдо", "total", "subtotal",
)


def has_value(cell: Any) -> bool:
    if cell is None:
        return False
    if isinstance(cell, str):
        return bool(cell.strip())
    return True


def is_empty(cells: Sequence[Any]) -> bool:
    return not any(has_value(cell) for cell in cells)


def is_total(cells: Sequence[Any], *, dated: bool = False) -> bool:
    """Строка итога, а не данные.

    У строки с датой итогом считается только ячейка, которая начинается со
    слова итога («Итого за август»). Раньше хватало слова где угодно в строке,
    и операция «Пополнение баланса Tele2» пропускалась молча как итоговая -
    деньги уходили из учёта без единого замечания.
    """
    texts = [norm(cell) for cell in cells if isinstance(cell, str) and cell.strip()]
    if any(text.startswith(word) for text in texts for word in ("итого", "всего", "total", "subtotal")):
        return True
    if dated:
        return False
    joined = " ".join(norm(cell) for cell in cells if has_value(cell))
    if not joined:
        return False
    return any(joined.startswith(word) or f" {word}" in f" {joined}" for word in TOTAL_WORDS[:4]) or any(
        joined == word for word in TOTAL_WORDS
    )


def is_numbering(cells: Sequence[Any]) -> bool:
    """Строка «1 2 3 … 9» под шапкой - нумерация колонок, как в бланке.

    Её печатают Kaspi Business и выгрузки по форме банка. Разбор принимал её за
    операцию: дата «2» становилась 1 января 1900 года, «3» и «4» - суммами в
    обеих колонках сразу.
    """
    values = [cell for cell in cells if has_value(cell)]
    if len(values) < 3:
        return False
    numbers: list[int] = []
    for value in values:
        if isinstance(value, bool):
            return False
        if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
            numbers.append(int(value))
        elif isinstance(value, str) and value.strip().isdigit():
            numbers.append(int(value.strip()))
        else:
            return False
    return numbers[0] in (0, 1) and numbers == list(range(numbers[0], numbers[0] + len(numbers)))


__all__ = ["TOTAL_WORDS", "has_value", "is_empty", "is_numbering", "is_total"]
