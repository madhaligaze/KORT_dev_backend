"""Кирпичи команд модели: команда, её итог и обратный шаг.

Отдельным модулем, чтобы наборы команд (`commands.py` - договоры и виды,
`entry.py` - ручной ввод, деньги и файлы) не замыкались импортом друг на друга.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.finance.contracts import service
from app.finance.model.context import Ctx
from app.finance.service import FinanceError


class CommandError(FinanceError):
    """Команда записана неверно - словами."""


@dataclass
class Step:
    """Обратный шаг для точки восстановления: вид точки и что вернуть."""

    kind: str
    payload: dict[str, Any]


@dataclass
class Done:
    title: str
    result: dict[str, Any] = field(default_factory=dict)
    contracts: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)


@dataclass(frozen=True)
class Command:
    name: str
    title: str
    #: Раздел (`app/finance/access.py`), открытый на правку, без которого команды нет.
    resource: str
    params: type[BaseModel]
    run: Callable[[Ctx, Any], Done]
    #: Какие договоры заденет - до исполнения: отпечаток «предложить» и «до» его следствий.
    touches: Callable[[Any], list[str]] = lambda params: []
    #: Только владелец и администратор (наше юрлицо, начало учёта, сальдо, 1С).
    setup: bool = False


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _actor(ctx: Ctx) -> service.Actor:
    if ctx.actor is None:
        raise PermissionError("Записывать может только вошедший")
    return ctx.actor


def _label(contract: Any) -> str:
    number = (contract.number or "").strip() if contract is not None else ""
    if not number:
        return "договор без номера"
    return number if number.startswith("№") else f"№ {number}"


__all__ = ["Command", "CommandError", "Done", "Step", "_Params", "_actor", "_label"]
