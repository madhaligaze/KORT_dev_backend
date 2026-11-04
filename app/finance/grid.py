"""Табличный вид журнала: строки журнала финансистов как лист, правка ячейки - правка записи.

Зачем он нужен
──────────────
Человек, пришедший из Excel, приходит с привычками - выделить диапазон и
увидеть сумму, потянуть за угол, вставить столбец из буфера, Ctrl+Z. Своя
решётка не отвечает ни одной из них, поэтому лист рисует Univer, а этот
модуль отвечает за перевод между листом и записями журнала.

Колонки - колонки журнала BBC (`journal.COLUMNS`): ОПиУ период, Дата, Счёт,
Приход, Расход, Контрагент, Дробление, №Дог., Подкатегория, ФОТ/Детали,
Проект, Категория, Комментарии, Вопросы и производные (ДДС месяц, Фирма, Код
договора). Перевод между своими счетами - две строки, по строке на каждый
счёт, как в выписках (`journal.lines_of`): правка любой из двух - правка
одного перевода.

Правило, которое здесь держится
───────────────────────────────
**Поверхностей две, поведение одно.** Ячейка разбирается тем же
`parse_money` / `parse_date`, что и файл загрузки, а правка идёт через ту же
`journal.edit`, что и карточка записи. Иначе «1 500,50» в таблице однажды
станет числом 150050, а в карточке - 1500.50.

Сторону записи менять можно: сумма, вписанная в «Приход» строки расхода,
делает её приходом (счёт остаётся), статья-перемещение - половинкой перевода.
Так лист позволяет передумать, как в своей таблице, не удаляя строку.
"""
from __future__ import annotations

import uuid
from typing import Any, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import journal, service
from app.finance.layout import norm
from app.finance.models import Category, Operation, Workspace

#: Колонки листа - колонки журнала.
COLUMNS = journal.COLUMNS


def split_line_id(line_id: str) -> tuple[uuid.UUID, str | None]:
    """«<id>:in» → (id, «in»): строка перевода знает свою сторону."""
    text = str(line_id or "").strip()
    head, _, side = text.partition(":")
    try:
        operation_id = uuid.UUID(head)
    except ValueError as exc:
        raise service.FinanceError("Строка листа не найдена") from exc
    if side not in ("", "in", "out"):
        raise service.FinanceError("Строка листа не найдена")
    return operation_id, side or None


def build_grid(session: Session, workspace: Workspace, operations: Sequence[Operation]) -> dict[str, Any]:
    """Записи → лист: шапка, строки и справочники для выпадающих списков."""
    book = journal.Book.load(session, workspace.id)
    return {
        "columns": journal.grid_columns(),
        "rows": journal.grid_lines(session, workspace, operations, book),
        "options": journal.grid_options(session, workspace, book),
    }


def rows_of(session: Session, workspace: Workspace, operation: Operation) -> list[dict[str, Any]]:
    """Строки одной записи - ровно в том виде, в каком их отдаёт `build_grid`."""
    return journal.grid_lines(session, workspace, [operation])


def row_of(session: Session, workspace: Workspace, operation: Operation, side: str | None = None) -> dict[str, Any] | None:
    """Строка листа после правки - та сторона записи, которую правили.

    Лист после правки записывает её обратно в свои ячейки. Без неё ему
    оставалось перечитывать журнал целиком, а перечитанный журнал
    отсортирован заново: запись с исправленной датой уезжала на другое место,
    строки на экране оставались прежними, и следующая правка уходила в
    соседнюю запись.
    """
    rows = rows_of(session, workspace, operation)
    if not rows:
        return None
    if side:
        for row in rows:
            if row["side"] == side:
                return row
    return rows[0]


def _resolve(session: Session, workspace: Workspace, model, name: str, **extra):
    """Запись справочника по тексту ячейки: точно, иначе единственная по началу.

    Выпадающий список Univer не дополняет набранное: «Кас» + Enter оставлял в
    ячейке «Кас», и сервер отвечал «Счёта «Кас» нет», хотя счёт «Касса» один
    и перепутать его не с чем. А при быстром наборе список перехватывает фокус
    на второй-третьей букве, и в ячейке остаётся «Ба» от «Банковский счёт».
    Человек из Excel печатает, а не щёлкает мышью по списку.

    Правило то же, что у заголовков колонок книг: нечёткое совпадение
    принимается, только если кандидат ровно один. «Ба» при двух счетах на
    «Ба…» - не угадывание, а отказ. И это не тихо: лист записывает ответ
    сервера обратно, и в ячейке появляется полное название.
    """
    clean = (name or "").strip()
    if not clean:
        return None
    found = service.find_entry(session, model, workspace.id, clean, **extra)
    if found is not None:
        return found
    key = norm(clean)
    if len(key) < 2:
        return None
    conditions = [
        model.workspace_id == workspace.id,
        model.normalized_name.startswith(key, autoescape=True),
        model.archived_at.is_(None),
    ]
    for field, value in extra.items():
        conditions.append(getattr(model, field) == value)
    candidates = session.scalars(sa.select(model).where(*conditions).limit(2)).all()
    return candidates[0] if len(candidates) == 1 else None


def apply_cell(
    session: Session,
    workspace: Workspace,
    line_id: str | uuid.UUID,
    column_key: str,
    raw_value: Any,
    *,
    version: int | None = None,
    actor: str = "",
) -> Operation:
    """Правка одной ячейки листа - правка записи журнала по смыслу колонки."""
    column = journal.COLUMN_BY_KEY.get(column_key)
    if column is None:
        raise service.FinanceError(f"В журнале нет колонки «{column_key}»")
    if not column.editable:
        raise service.FinanceError(
            f"«{column.title}» считается сам: "
            + {
                "split": "дробление правится в записи - «Разделить» и «Собрать»",
                "group": "категория - группа подкатегории",
            }.get(column_key, "его не печатают")
        )
    operation_id, side = split_line_id(str(line_id))
    text = "" if raw_value is None else str(raw_value).strip()
    if column_key in ("income", "expense"):
        operation = session.get(Operation, operation_id)
        if operation is None or operation.workspace_id != workspace.id:
            raise service.FinanceError("Запись не найдена")
        if not text:
            # Сумму стёрли в своей колонке: если она стоит в соседней - это
            # не правка, а очистка соседней ячейки при переносе суммы.
            raise service.FinanceError("Сумма не может быть пустой: запись без суммы удаляется в журнале, а не обнуляется")
        patch = {column_key: raw_value}
    else:
        patch = {column_key: raw_value}
    return journal.edit(session, workspace, operation_id, patch, version=version, side=side, actor=actor)


def append_row(session: Session, workspace: Workspace, cells: dict[str, Any], *, actor: str = "") -> Operation:
    """Новая запись из строки, напечатанной внизу листа."""
    return journal.append_line(session, workspace, cells, actor=actor)


__all__ = ["COLUMNS", "Category", "append_row", "apply_cell", "build_grid", "row_of", "rows_of", "split_line_id"]
