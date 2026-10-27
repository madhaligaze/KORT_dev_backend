"""Шапка таблицы: где она, из одной строки или из двух, что в заголовке значимо.

Строка шапки - та, где узнано больше всего колонок формы: выгрузки начинаются
с названия отчёта, периода и реквизитов, и шапка бывает пятой строкой, а у
выписок юрлиц - двадцатой.

Шапка в две строки - обычное дело: банк ставит «Сумма» над «Дебет | Кредит»,
реестр офиса - «Договор» над «№ | дата», ОСВ 1С - «Сальдо на начало» над
«Дебет | Кредит». Верхняя ячейка объединена над несколькими колонками, и одна
верхняя строка не называет ни суммы, ни даты договора. Подписи складываются
с группой; что из сложенного понятнее форме, решает её же `strength`.

Где объединения известны (xlsx, прочитанный с подробностями), группа колонки
берётся из объединения. Где нет (CSV, HTML, быстрое чтение), группа тянется
вправо от заполненной верхней ячейки, пока снизу есть подписи.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Sequence

from app.finance.intake.rows import has_value
from app.finance.intake.tables import Table
from app.finance.layout import norm, squash

_PAREN = re.compile(r"\([^)]*\)", re.S)
#: Единица в хвосте заголовка: «Сумма в месяц, тг», «Стоимость (тенге)», «Сумма, KZT».
_UNIT = re.compile(r"[\s,;:]*(?:тг|тенге|kzt|₸|руб|usd|\$)\.?\s*$", re.IGNORECASE)
_DATE_LIKE = re.compile(r"^\s*(\d{1,4})[.\-/\\](\d{1,2})[.\-/\\](\d{1,4})")


def text(cell: Any) -> str:
    return "" if cell is None else str(cell)


def core(value: Any) -> str:
    """Заголовок без оформления, пояснений в скобках и единицы.

    «Отдел (ОБО, НО…)» → «отдел»; «Сумма в месяц, тг» → «сумма в месяц».
    Единица - тоже пояснение: тенге в казахстанском реестре и так подразумеваются.
    """
    raw = str(value or "")
    stripped = _PAREN.sub(" ", raw) if "(" in raw else raw
    return norm(_UNIT.sub("", norm(stripped)))


def looks_like_subheader(row: Sequence[Any]) -> bool:
    """Строка из одних подписей: ни дат, ни чисел, ни текста, похожего на них."""
    texts = [cell for cell in row if has_value(cell)]
    if not texts:
        return False
    for cell in texts:
        if not isinstance(cell, str):
            return False
        if _DATE_LIKE.match(cell) or re.fullmatch(r"[\d\s.,\-+()₸$€]+", cell.strip()):
            return False
    return True


def best_row(rows: Sequence[Sequence[Any]], score: Callable[[Sequence[Any]], int], *, look: int = 40) -> tuple[int, int]:
    """Строка с наибольшим числом узнанных заголовков и её счёт; (-1, 0) - не нашлось."""
    best_index, best_score = -1, 0
    for index, row in enumerate(rows[:look]):
        value = score(row)
        if value > best_score:
            best_index, best_score = index, value
    return best_index, best_score


def _groups(header: Sequence[str], sub: Sequence[str], table: Table | None, index: int) -> list[str]:
    """Группа каждой колонки: подпись объединённой ячейки над ней."""
    width = max(len(header), len(sub))
    out = [""] * width
    if table is not None and table.merged:
        for left, right in table.spans(index):
            label = header[left] if left < len(header) else ""
            for position in range(left, min(right, width - 1) + 1):
                out[position] = label
        return out
    group = ""
    for position in range(width):
        top = header[position] if position < len(header) else ""
        low = sub[position] if position < len(sub) else ""
        if squash(top):
            group = top
        elif not squash(low):
            group = ""
        out[position] = group
    return out


def compose(
    rows: Sequence[Sequence[Any]],
    index: int,
    strength: Callable[[str], int],
    score: Callable[[Sequence[Any]], int],
    *,
    table: Table | None = None,
    swapped: bool = True,
) -> tuple[list[str], int]:
    """Шапка с учётом второй строки и номер первой строки тела.

    Для каждой колонки пробуем по порядку: «группа подпись» («Корреспондент
    Счёт» - счёт контрагента, а не наш), одну подпись («Дебет»), одну группу,
    «подпись группа» («дата Договор» - дата договора; `swapped=False` - без
    неё, как читали выписки до приёмника). Берётся самое понятное форме; при
    равных - раньше в списке. Нижняя строка признаётся продолжением шапки,
    только если с ней узнаётся больше колонок, чем без неё: иначе это первая
    строка данных.
    """
    header = [text(cell) for cell in rows[index]]
    if index + 1 >= len(rows) or not looks_like_subheader(rows[index + 1]):
        return header, index + 1
    sub = [text(cell) for cell in rows[index + 1]]
    groups = _groups(header, sub, table, index)
    merged: list[str] = []
    for position in range(max(len(header), len(sub))):
        top = header[position] if position < len(header) else ""
        low = sub[position] if position < len(sub) else ""
        if not squash(low):
            merged.append(top)
            continue
        base = top if squash(top) else groups[position]
        candidates = ([f"{base} {low}"] if squash(base) else []) + [low] + ([top] if squash(top) else [])
        if swapped and squash(base):
            candidates.append(f"{low} {base}")
        best = max(candidates, key=strength)
        merged.append(best if strength(best) else (top if squash(top) else low))
    if score(merged) > score(header):
        return merged, index + 2
    return header, index + 1


__all__ = ["best_row", "compose", "core", "looks_like_subheader", "text"]
