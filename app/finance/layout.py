"""Привязка колонок к листу по названиям, а не по номерам.

Зачем это существует
────────────────────
Книги BBC ведут руками. В них вставляют колонки. Раньше позиции были прибиты
числами (`SALDO_END = 42`), и вставленная колонка сдвигала всё правее себя —
парсер начинал читать соседнюю ячейку. Это ломается двумя способами, и второй
хуже первого:

1. Громко: `verify_layout` замечал несовпадение и валил чтение. Дашборд писал
   «таблица не читается» и показывал числа от прошлого удачного чтения. Так
   в августе 2026 лёг мастер-лист — в блок АВР добавили четыре колонки
   («АВР (клиент принял)», «Дата АВР (клиент принял)», «ЭСФ (отпр.)»,
   «Дата ЭСФ (отпр.)»), и всё от «Сальдо Конец» и правее уехало на +4.

2. Тихо: у «Журнала» проверки не было вовсе. Там колонку «ФОТ/Детали» вставили
   на позицию 10, и «Проект», «Категория», «Комментарии» уехали на +1. Парсер
   этого не заметил и месяцами складывал расходы по «Проекту» в графу
   «Категория». Никакой ошибки, просто неверные цифры с уверенным видом.

Отсюда решение: колонка ищется по своему заголовку. Номер остаётся, но только
как подсказка (`hint`) — чтобы выбрать между одинаковыми названиями и чтобы
было видно, что именно сдвинулось. Вставка, удаление и перестановка колонок
больше не ломают ничего. Ломает только переименование — и тогда ошибка называет
поле и показывает похожие заголовки, а не номер позиции.

Почему не «найти хоть что-то похожее и продолжить»
──────────────────────────────────────────────────
Потому что цена ошибки — деньги на экране начальника. Нечёткое совпадение
принимается, только если кандидат ровно один. Два кандидата на денежную
колонку — это отказ читать, а не подбрасывание монетки: см. `_pick`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

log = logging.getLogger(__name__)


class LayoutError(RuntimeError):
    """Раскладка листа не совпала с той, под которую написан парсер."""


# ── Нормализация заголовков ──────────────────────────────────────────────────────
#
# В шапках живут переносы строк («Сальдо\nКонец»), ведущие и хвостовые пробелы
# (« Приход »), неразрывные пробелы и разнобой регистра. Всё это — оформление,
# а не смысл, и сравнивать надо без него.

_SPACES = re.compile(r"[\s   ]+")
_NOT_WORD = re.compile(r"[^0-9a-zа-я№]+")


def norm(value: Any) -> str:
    """Заголовок без оформления: без переносов, лишних пробелов и регистра."""
    text = _SPACES.sub(" ", str(value or "")).strip().lower()
    return text.replace("ё", "е")


def squash(value: Any) -> str:
    """Ещё жёстче: только буквы, цифры и «№». «№ Договора» → «№договора»."""
    return _NOT_WORD.sub("", norm(value))


#: Заголовки-пустышки: разделители «.», технические колонки без имени. По ним
#: ничего не ищется — иначе первая же «.» стала бы кандидатом на всё подряд.
def _is_blank(header: str) -> bool:
    return not squash(header)


@dataclass(frozen=True)
class Column:
    """Логическая колонка: как называется в коде и как — в книге.

    `names` — все написания заголовка, которые считаются этой колонкой. Список,
    а не одна строка, потому что книгу переименовывают: «АВР (Реал.)» стало
    «АВР (наша)», и оба написания обязаны читаться, чтобы старая копия книги и
    тесты на ней не разъехались с продом.

    `hint` — позиция, на которой колонка стояла раньше. Не требование: если по
    имени нашлось другое место, берётся оно, а сдвиг попадает в `Layout.drift`.

    `required=False` — колонка может отсутствовать. Тогда `Layout.cell` вернёт
    пустую строку, и парсер получит None/0, как для пустой ячейки.
    """

    key: str
    title: str
    names: tuple[str, ...]
    hint: int | None = None
    required: bool = True

    def matches_exact(self, header: str) -> bool:
        return any(norm(header) == norm(name) for name in self.names)

    def matches_squashed(self, header: str) -> bool:
        return any(squash(header) == squash(name) for name in self.names)

    def matches_loose(self, header: str) -> bool:
        """Один заголовок — начало другого. «Сумма Договора» ↔ «Сумма Дог.».

        Порог в четыре символа отсекает мусор: без него «№» совпало бы с
        «№ Договора», «№ Счета» и «№ АВР» разом.
        """
        actual = squash(header)
        if len(actual) < 4:
            return False
        for name in self.names:
            wanted = squash(name)
            if len(wanted) < 4:
                continue
            if actual.startswith(wanted) or wanted.startswith(actual):
                return True
        return False


@dataclass(frozen=True)
class Placement:
    """Куда в итоге села колонка и насколько уверенно."""

    key: str
    title: str
    index: int | None
    header: str = ""
    #: exact | squashed | loose | missing — как именно опознали.
    how: str = "exact"

    @property
    def found(self) -> bool:
        return self.index is not None


@dataclass(frozen=True)
class Drift:
    """Колонка нашлась, но не там, где стояла раньше."""

    key: str
    title: str
    was: int
    now: int
    header: str

    def describe(self) -> str:
        arrow = "→"
        return f"«{self.title}»: {self.was} {arrow} {self.now}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "was": self.was,
            "now": self.now,
            "header": self.header,
        }


@dataclass(frozen=True)
class Layout:
    """Раскладка листа, разобранная по шапке.

    Даёт доступ к ячейке по логическому имени: `layout.cell(row, "saldo_end")`.
    Номера позиций после разбора нигде больше не упоминаются — в этом вся суть.
    """

    sheet: str
    placements: Mapping[str, Placement]
    drift: tuple[Drift, ...] = ()
    width: int = 0
    #: Необязательные колонки, которых в книге не нашлось.
    absent: tuple[str, ...] = ()

    def at(self, key: str) -> int | None:
        placement = self.placements.get(key)
        return placement.index if placement else None

    def cell(self, row: Sequence[str], key: str) -> str:
        """Значение ячейки как строка. Нет колонки или строка короче — «»."""
        index = self.at(key)
        if index is None or index >= len(row):
            return ""
        value = row[index]
        return "" if value is None else str(value)

    def has(self, key: str) -> bool:
        return self.at(key) is not None

    @property
    def shifted(self) -> bool:
        return bool(self.drift)

    def describe_drift(self) -> str:
        return "; ".join(item.describe() for item in self.drift)

    def to_dict(self) -> dict[str, Any]:
        """Для API: что именно уехало. Дашборд показывает это как примечание."""
        return {
            "sheet": self.sheet,
            "width": self.width,
            "shifted": self.shifted,
            "drift": [item.to_dict() for item in self.drift],
            "absent": list(self.absent),
            "columns": {
                key: {"index": placement.index, "header": placement.header, "how": placement.how}
                for key, placement in self.placements.items()
            },
        }


def _candidates(column: Column, headers: Sequence[str]) -> tuple[list[int], str]:
    """Позиции-кандидаты и то, каким способом они нашлись.

    Три ступени, от строгой к мягкой. Мягкая включается, только если строгая не
    дала ничего: точное совпадение всегда бьёт похожее.
    """
    for how, predicate in (
        ("exact", column.matches_exact),
        ("squashed", column.matches_squashed),
        ("loose", column.matches_loose),
    ):
        found = [
            index
            for index, header in enumerate(headers)
            if not _is_blank(header) and predicate(header)
        ]
        if found:
            return found, how
    return [], "missing"


def _pick(column: Column, found: list[int], how: str) -> int:
    """Выбрать одну позицию из нескольких кандидатов.

    Точные совпадения разрешаются подсказкой — в книге бывают одинаковые
    заголовки в разных блоках, и `hint` говорит, какой из них наш. Нечёткое
    совпадение с несколькими кандидатами не разрешается вовсе: угадывать, из
    какой колонки брать деньги, хуже, чем не прочитать лист.
    """
    if len(found) == 1:
        return found[0]
    if how == "loose":
        raise LayoutError(
            f"«{column.title}»: точного заголовка нет, а похожих сразу несколько "
            f"(позиции {', '.join(map(str, found))}). Уточните название в книге "
            "или добавьте его в `names` этой колонки."
        )
    if column.hint is not None:
        if column.hint in found:
            return column.hint
        return min(found, key=lambda index: (abs(index - column.hint), index))
    raise LayoutError(
        f"«{column.title}»: заголовок встречается несколько раз "
        f"(позиции {', '.join(map(str, found))}), и подсказки, какой из них наш, нет."
    )


def _near(headers: Sequence[str], hint: int | None, span: int = 3) -> str:
    """Что стоит рядом с прежней позицией — чтобы было видно, чем заменили."""
    if hint is None:
        return ""
    lo, hi = max(0, hint - span), min(len(headers), hint + span + 1)
    around = [f"[{i}] «{norm(headers[i])}»" for i in range(lo, hi) if not _is_blank(headers[i])]
    return ("рядом с прежней позицией: " + ", ".join(around)) if around else ""


def resolve_layout(sheet: str, columns: Iterable[Column], header: Sequence[str]) -> Layout:
    """Разобрать шапку листа в `Layout` или объяснить, почему не вышло.

    Падает только на том, что действительно нечитаемо: пропала обязательная
    колонка, или её название стало неоднозначным. Сдвиг колонок — не ошибка:
    он попадает в `Layout.drift`, лист читается дальше, а дашборд показывает
    примечание о том, что книга изменилась.
    """
    headers = [str(value or "") for value in header]
    columns = list(columns)

    placements: dict[str, Placement] = {}
    drift: list[Drift] = []
    absent: list[str] = []
    problems: list[str] = []
    taken: dict[int, str] = {}

    for column in columns:
        found, how = _candidates(column, headers)
        if not found:
            if column.required:
                hint_note = _near(headers, column.hint)
                problems.append(
                    f"«{column.title}» — не нашёл колонку с таким заголовком"
                    + (f"; {hint_note}" if hint_note else "")
                )
            else:
                absent.append(column.key)
                placements[column.key] = Placement(column.key, column.title, None, how="missing")
            continue

        try:
            index = _pick(column, found, how)
        except LayoutError as exc:
            problems.append(str(exc))
            continue

        if index in taken:
            problems.append(
                f"«{column.title}» и «{taken[index]}» указывают на одну и ту же "
                f"колонку [{index}] «{norm(headers[index])}»"
            )
            continue
        taken[index] = column.title

        placements[column.key] = Placement(
            key=column.key,
            title=column.title,
            index=index,
            header=norm(headers[index]),
            how=how,
        )
        if column.hint is not None and index != column.hint:
            drift.append(Drift(column.key, column.title, column.hint, index, norm(headers[index])))

    if problems:
        raise LayoutError(
            f"Лист «{sheet}»: не удалось привязать колонки по названиям. "
            "Колонку переименовали или лист заменили другим. "
            + "; ".join(problems)
        )

    layout = Layout(
        sheet=sheet,
        placements=placements,
        drift=tuple(drift),
        width=len(headers),
        absent=tuple(absent),
    )
    if drift:
        # Не ошибка, но обязано быть видно в логах: книга изменилась, и когда
        # цифры на дашборде поедут, это первое, на что надо смотреть.
        log.warning("BBC: лист «%s» — колонки сдвинулись: %s", sheet, layout.describe_drift())
    if absent:
        log.info("BBC: лист «%s» — необязательные колонки отсутствуют: %s", sheet, ", ".join(absent))
    return layout


__all__ = [
    "Column",
    "Drift",
    "Layout",
    "LayoutError",
    "Placement",
    "norm",
    "resolve_layout",
    "squash",
]
