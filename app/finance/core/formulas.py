"""Формулы компании - меры, которые компания заводит сама (этап 3, решение владельца 31.10.2026).

В таблице «оплачено / сумма, %» - формула в клетке: её пишут в одной книге, она
читает соседнюю колонку, когда вставили новую, и в карточке её нет. Здесь
формула - мера компании: одна запись на компанию, считает ядро, видна в
каждой форме, где есть меры вида, и раскрывается «объяснить» до своих
слагаемых.

Язык - арифметика над мерами договора:

* `amount` - сумма (у вида с долей отдела - доля), `paid` - оплачено,
  `remaining` - остаток, `age` - срок, мес;
* числа (дробь - через точку: запятая разделяет аргументы `round(x, 1)`),
  `+ - * /`, скобки, `min`, `max`, `abs`, `round`.

Других имён и вызовов нет: формулу пишет человек, исполняет сервер, и
`ast` разбирается с белым списком, а не `eval`. Деление на ноль и пустая
мера (сумма скрыта правами, договор не нашёлся в сводной) - пусто, а не 0:
«оплачено 0% от неизвестной суммы» было бы неправдой.
"""
from __future__ import annotations

import ast
import functools
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts.service import Access, Actor
from app.finance.core.models import CoreSettings
from app.finance.models import Workspace
from app.finance.service import FinanceError

#: Имя в формуле → мера вида договоров (`core/views.MEASURES`, `amount`) и подпись.
VARIABLES: dict[str, tuple[str, str]] = {
    "amount": ("amount", "сумма"),
    "paid": ("summary_paid", "оплачено"),
    "remaining": ("summary_remaining", "остаток"),
    "age": ("age_months", "срок, мес"),
}
FUNCTIONS = ("min", "max", "abs", "round")
TYPES = {"money": "сумма", "number": "число", "percent": "проценты"}
KEY = re.compile(r"^f_[a-z0-9_]{1,40}$")
#: Формул у компании - не больше: каждая считается у каждого договора вида.
MAX = 20
TEXT_MAX = 200


class FormulaError(FinanceError):
    """Формула записана неверно - словами, с местом ошибки."""


@dataclass(frozen=True)
class Formula:
    key: str
    title: str
    text: str
    type: str
    digits: int

    def out(self) -> dict[str, Any]:
        return {
            "key": self.key, "title": self.title, "text": self.text, "type": self.type, "digits": self.digits,
            "uses": sorted(names(self.text)), "formula": True,
        }


# ── Язык ─────────────────────────────────────────────────────────────────────


def parse(text: str) -> ast.Expression:
    """Разобрать формулу или отказать словами: только имена мер, числа, четыре действия и четыре функции."""
    clean = " ".join(str(text or "").split())
    if not clean:
        raise FormulaError("Формула пуста - например, paid / amount * 100")
    if len(clean) > TEXT_MAX:
        raise FormulaError(f"Формула длиннее {TEXT_MAX} знаков")
    try:
        tree = ast.parse(clean, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Формула не читается около «{clean[max(0, (exc.offset or 1) - 3):(exc.offset or 1) + 2]}»") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Load)):
            continue
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                raise FormulaError("Действия - только + - * /")
            continue
        if isinstance(node, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd)):
            continue
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.USub, ast.UAdd)):
                raise FormulaError("Перед числом - только минус")
            continue
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise FormulaError("В формуле бывают только числа, без текста")
            continue
        if isinstance(node, ast.Name):
            if node.id not in VARIABLES and node.id not in FUNCTIONS:
                raise FormulaError(f"«{node.id}» в формуле нет - меры: " + ", ".join(VARIABLES))
            continue
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                raise FormulaError("Функции - только " + ", ".join(FUNCTIONS))
            if node.keywords or not node.args or len(node.args) > (2 if node.func.id == "round" else 10):
                raise FormulaError(f"{node.func.id}(…) записана неверно")
            continue
        raise FormulaError("В формуле только меры, числа, + - * / и скобки")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FUNCTIONS:
            parent_calls = [item for item in ast.walk(tree) if isinstance(item, ast.Call) and item.func is node]
            if not parent_calls:
                raise FormulaError(f"{node.id} - функция: {node.id}(…)")
    return tree


@functools.lru_cache(maxsize=256)
def _tree(text: str) -> ast.Expression:
    """Разобранная формула - один раз на текст: вид считает её у каждого договора."""
    return parse(text)


def names(text: str) -> set[str]:
    """Какие меры формула читает."""
    return {node.id for node in ast.walk(parse(text)) if isinstance(node, ast.Name) and node.id in VARIABLES}


def evaluate(text: str, values: dict[str, Decimal | None]) -> Decimal | None:
    """Значение формулы по мерам договора: пустая мера или деление на ноль - пусто."""

    def walk(node: ast.AST) -> Decimal | None:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            return Decimal(str(node.value))
        if isinstance(node, ast.Name):
            return values.get(node.id)
        if isinstance(node, ast.UnaryOp):
            value = walk(node.operand)
            return None if value is None else (-value if isinstance(node.op, ast.USub) else value)
        if isinstance(node, ast.BinOp):
            left, right = walk(node.left), walk(node.right)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                return None
            return left / right
        if isinstance(node, ast.Call):
            args = [walk(item) for item in node.args]
            if any(item is None for item in args):
                return None
            name = node.func.id  # type: ignore[attr-defined]
            if name == "min":
                return min(args)  # type: ignore[type-var]
            if name == "max":
                return max(args)  # type: ignore[type-var]
            if name == "abs":
                return abs(args[0])  # type: ignore[arg-type]
            digits = int(args[1]) if len(args) > 1 else 0  # type: ignore[arg-type]
            return args[0].quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)  # type: ignore[union-attr]
        raise FormulaError("В формуле только меры, числа, + - * / и скобки")

    try:
        result = walk(_tree(text))
    except (InvalidOperation, OverflowError):
        return None
    return result


def shown(formula: Formula, value: Decimal | None) -> str | None:
    """Значение формулы строкой ответа: сумма - до копеек, число и проценты - до своих знаков."""
    if value is None:
        return None
    digits = 2 if formula.type == "money" else formula.digits
    return format(value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP), "f")


# ── Хранение: настройки ядра компании ────────────────────────────────────────


def of(session: Session, workspace_id: uuid.UUID) -> list[Formula]:
    row = session.get(CoreSettings, workspace_id)
    raw = ((row.data if row is not None else None) or {}).get("formulas") or []
    out: list[Formula] = []
    for item in raw:
        if not isinstance(item, dict) or not KEY.match(str(item.get("key") or "")):
            continue
        out.append(Formula(
            key=str(item["key"]), title=str(item.get("title") or item["key"]), text=str(item.get("text") or ""),
            type=str(item.get("type") or "number") if item.get("type") in TYPES else "number",
            digits=int(item.get("digits") or 0) if str(item.get("digits") or "0").isdigit() else 0,
        ))
    return out


def _slug(title: str, taken: set[str]) -> str:
    table = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "i",
        "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
        "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
        "ә": "a", "ғ": "g", "қ": "k", "ң": "n", "ө": "o", "ұ": "u", "ү": "u", "һ": "h", "і": "i",
    })
    base = re.sub(r"[^a-z0-9]+", "_", title.lower().translate(table)).strip("_")[:30] or "mera"
    key = f"f_{base}"
    index = 2
    while key in taken:
        key = f"f_{base}_{index}"
        index += 1
    return key


def _write(session: Session, workspace: Workspace, actor: Actor, items: list[Formula]) -> None:
    row = session.get(CoreSettings, workspace.id)
    if row is None:
        row = CoreSettings(workspace_id=workspace.id, data={})
        session.add(row)
    data = dict(row.data or {})
    data["formulas"] = [
        {"key": item.key, "title": item.title, "text": item.text, "type": item.type, "digits": item.digits} for item in items
    ]
    row.data = data
    row.updated_by = actor.user_id
    row.updated_at = datetime.now(timezone.utc)
    session.flush()
    # Формула - колонка листа: схема реестра у открытых вкладок перечитывается.
    from app.finance.contracts.fields import bump

    bump(session, workspace.id, "schema")


def save(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *,
    key: str | None, title: str, text: str, type: str, digits: int,
) -> tuple[Formula, list[dict[str, Any]]]:
    """Завести или поменять формулу компании. Возвращает её и прежний список - для «Вернуть»."""
    if not access.setup:
        raise PermissionError("Формулы компании заводит владелец или администратор")
    clean_title = " ".join(str(title or "").split())
    if not clean_title or len(clean_title) > 80:
        raise FormulaError("Название формулы - до 80 знаков, например «Оплачено, %»")
    if type not in TYPES:
        raise FormulaError("Что считает формула - сумма, число или проценты")
    if not 0 <= int(digits) <= 4:
        raise FormulaError("Знаков после запятой - от 0 до 4")
    parse(text)
    items = of(session, workspace.id)
    before = [item.out() for item in items]
    if any(item.title.casefold() == clean_title.casefold() and item.key != key for item in items):
        raise FormulaError(f"Формула «{clean_title}» уже есть")
    clean_text = " ".join(str(text).split())
    if key:
        found = next((item for item in items if item.key == key), None)
        if found is None:
            raise FormulaError("Такой формулы нет - её успели убрать")
        made = Formula(key, clean_title, clean_text, type, int(digits))
        items = [made if item.key == key else item for item in items]
    else:
        if len(items) >= MAX:
            raise FormulaError(f"Формул у компании - не больше {MAX}")
        made = Formula(_slug(clean_title, {item.key for item in items}), clean_title, clean_text, type, int(digits))
        items.append(made)
    _write(session, workspace, actor, items)
    history.write(
        session, workspace, kind="core.formula", entity="core_settings", entity_id=None,
        title=f"формула «{made.title}»: {made.text}", before={"formulas": before}, after={"formula": made.out()}, actor=actor.email,
    )
    return made, before


def remove(session: Session, workspace: Workspace, access: Access, actor: Actor, key: str) -> tuple[Formula, list[dict[str, Any]]]:
    if not access.setup:
        raise PermissionError("Формулы компании убирает владелец или администратор")
    items = of(session, workspace.id)
    found = next((item for item in items if item.key == key), None)
    if found is None:
        raise FormulaError("Такой формулы нет - её успели убрать")
    before = [item.out() for item in items]
    _write(session, workspace, actor, [item for item in items if item.key != key])
    history.write(
        session, workspace, kind="core.formula", entity="core_settings", entity_id=None,
        title=f"формула «{found.title}» убрана", before={"formulas": before}, after={"removed": key}, actor=actor.email,
    )
    return found, before


def restore(session: Session, workspace: Workspace, actor: Actor, before: list[dict[str, Any]]) -> None:
    """«Вернуть»: список формул, как был до команды."""
    _write(session, workspace, actor, [
        Formula(str(item["key"]), str(item["title"]), str(item["text"]), str(item["type"]), int(item.get("digits") or 0))
        for item in before if KEY.match(str(item.get("key") or ""))
    ])


def values_of(measures: dict[str, Any]) -> dict[str, Decimal | None]:
    """Меры строки вида договоров (`core/views.ask`) - значения имён формулы."""
    out: dict[str, Decimal | None] = {}
    for name, (key, _title) in VARIABLES.items():
        raw = measures.get(key)
        try:
            out[name] = Decimal(str(raw)) if raw not in (None, "") else None
        except InvalidOperation:
            out[name] = None
    return out


__all__ = [
    "FUNCTIONS", "MAX", "TYPES", "VARIABLES", "Formula", "FormulaError",
    "evaluate", "names", "of", "parse", "remove", "restore", "save", "shown", "values_of",
]
