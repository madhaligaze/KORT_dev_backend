"""Схема вызовов модели - что принимает и что отдаёт каждый (`GET /finance/model`).

Запросы описаны строго (лишнее поле - отказ), ответы - главными полями: в
ответах бывают поля сверх описанных, это младшая версия, и старый клиент их
не замечает. Схемы - JSON Schema из pydantic, по ним клиент можно
сгенерировать, а не писать руками.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.finance.model import HEADER, VERSION
from app.finance.model.commands import COMMANDS


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


# ── Запросы ──────────────────────────────────────────────────────────────────


class AskIn(_In):
    """Сохранённый вид (`view`) или вопрос без сохранения (`source` и остальное)."""

    view: str | None = Field(default=None, max_length=64, description="Ключ вида; реестр - пустая строка")
    source: Literal["contracts", "debts", "unbilled", "calendar", "questions", "attention", "arrived", "documents"] | None = Field(
        default=None,
        description=(
            "calendar - платёжный календарь: params from, to, days, scenario, exclude (describe → calendar); "
            "questions - что ждёт решения (params.kind); attention - что под угрозой (params.item); "
            "arrived - что пришло (params.since); documents - дела по АВР и ЭСФ (params.view) - describe → home"
        ),
    )
    title: str | None = Field(default=None, max_length=120)
    filter: dict[str, Any] | None = Field(default=None, description="Отбор - правило листа: {any: [{all: [{field, op, value}]}]}")
    measures: list[str] | None = None
    groups: list[str] | None = None
    params: dict[str, Any] | None = Field(default=None, description="Параметры источника - describe → sources.<ключ>.params")
    as_of: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="День; пусто - сегодня")


class CommandIn(_In):
    command: str = Field(description="Имя команды - GET /finance/model → commands")
    params: dict[str, Any] = Field(default_factory=dict)


class ProposeIn(_In):
    commands: list[CommandIn] = Field(min_length=1, max_length=200)


class ApplyIn(_In):
    commands: list[CommandIn] = Field(min_length=1, max_length=200)
    expect: str | None = Field(default=None, max_length=64, description="`base` предложения: поменялось - 409 с новым предложением")


class UndoIn(_In):
    point: str = Field(description="id точки восстановления из ответа «применить» или GET /finance/model/points")


class CompareIn(_In):
    """Таблица «договор за месяц» против KORT: файл (`name`, `data` base64) или, без файла, сводная Google компании."""

    name: str | None = Field(default=None, max_length=255)
    data: str | None = Field(default=None, max_length=22 * 1024 * 1024)
    links: list[Literal["accruals", "payments", "acts", "bills"]] | None = None
    entity: str | None = Field(default=None, description="Наше юрлицо: строк без «Нашего юрлица» и сверка только его месяцев")
    answers: dict[str, Any] | None = Field(default=None, description="Разметка листов - как у команды table.apply")


# ── Ответы ───────────────────────────────────────────────────────────────────


class Column(_Out):
    key: str
    title: str
    type: str


class Row(_Out):
    values: dict[str, Any] | None = None
    refs: dict[str, str] = Field(default_factory=dict, description="Ссылки на числа строки - для «объяснить»")


class AskOut(_Out):
    version: str
    question: dict[str, Any]
    as_of: str
    source: str
    columns: list[Column]
    rows: list[dict[str, Any]]
    totals: dict[str, Any]


class Part(_Out):
    title: str
    value: str | None
    sign: str = Field(description="`+` или `-` - слагаемое; иначе пояснение")
    ref: str | None = Field(default=None, description="Раскрыть дальше")
    fact: dict[str, Any] | None = Field(default=None, description="Факт журнала или условие договора - дальше не раскрывается")
    parts: list[dict[str, Any]] | None = None


class ExplainOut(_Out):
    version: str
    ref: str
    title: str
    value: str | None
    formula: str
    parts: list[Part]
    check: str | None
    exact: bool


class ProposeOut(_Out):
    version: str
    commands: list[dict[str, Any]]
    problems: list[dict[str, Any]]
    changes: list[dict[str, Any]]
    base: str
    ready: bool


class ApplyOut(_Out):
    version: str
    commands: list[dict[str, Any]]
    point: dict[str, Any] | None
    cursor: str


class UndoOut(_Out):
    version: str
    title: str
    done: list[str]
    skipped: list[str]
    cursor: str


class ChangesOut(_Out):
    version: str
    cursor: str
    start: bool
    more: bool
    moved: dict[str, bool] | None = None


class DescribeOut(_Out):
    version: str
    company: dict[str, Any]
    #: Пакеты компании: страна (код, номер участника, валюта) и отрасль - с 1.1.
    packs: dict[str, Any]
    me: dict[str, Any]
    views: list[dict[str, Any]]
    sources: dict[str, Any]
    measures: list[dict[str, Any]]
    commands: list[dict[str, Any]]
    cursor: str


CALLS = (
    ("describe", "GET", "/finance/model/describe", "Описать: схема компании и что можно спрашивающему", None, DescribeOut),
    ("ask", "POST", "/finance/model/ask", "Спросить: вид или вопрос на день", AskIn, AskOut),
    ("ask", "GET", "/finance/model/ask?view=&as_of=", "Спросить сохранённый вид (ответ кэширует десктоп)", None, AskOut),
    ("explain", "GET", "/finance/model/explain?ref=", "Объяснить: число по ссылке до слагаемых и фактов", None, ExplainOut),
    ("propose", "POST", "/finance/model/propose", "Предложить: что сделают команды, без записи", ProposeIn, ProposeOut),
    ("apply", "POST", "/finance/model/apply", "Применить: пачка команд одной транзакцией и одной точкой", ApplyIn, ApplyOut),
    ("undo", "POST", "/finance/model/undo", "Отменить: по точке восстановления", UndoIn, UndoOut),
    ("points", "GET", "/finance/model/points", "Точки восстановления", None, None),
    ("subscribe", "GET", "/finance/model/changes?cursor=&wait=&include=", "Подписаться: что изменилось после курсора", None, ChangesOut),
    ("compare", "POST", "/finance/model/compare", "Сравнить: таблица «договор за месяц» против KORT по звеньям, без записи", CompareIn, None),
    ("keys", "GET|POST|DELETE", "/finance/model/keys", "Ключи для чужого сервиса: выдать, список, отозвать (из сеанса)", None, None),
)


def catalog() -> dict[str, Any]:
    return {
        "version": VERSION,
        "header": HEADER,
        "compatibility": "младшая цифра - поля и команды добавлены; старшая - смысл сменился, клиент с прежней старшей получает 409 model_version",
        "calls": [
            {
                "call": name,
                "method": method,
                "path": path,
                "title": title,
                "request": request.model_json_schema() if request is not None else None,
                "response": response.model_json_schema() if response is not None else None,
            }
            for name, method, path, title, request, response in CALLS
        ],
        "commands": [
            {
                "name": command.name, "title": command.title, "resource": command.resource, "setup": command.setup,
                "params": command.params.model_json_schema(),
            }
            for command in COMMANDS.values()
        ],
    }


__all__ = ["AskIn", "ApplyIn", "CALLS", "CommandIn", "CompareIn", "ProposeIn", "UndoIn", "catalog"]
