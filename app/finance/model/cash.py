"""Платёжный календарь в API модели: вопрос `calendar` и команды ожиданий.

Числа - `core/cash_calendar.py` (то же, что `GET /core/calendar`), запись -
`core/cash_plans.py`: права, журнал действий и «не угадывать» - одни. Любой
фронт заводит календарь так же, как экран: руками (плановый платёж,
обещание по долгу), из таблицы (файл или лист Google - формы «платёж по плану»
и «обещание по долгу»), из журнала (принять повторяющийся расход). Каждая
команда - с обратным шагом: «Отменить» возвращает как было.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from pydantic import Field

from app.finance.model.base import Command, CommandError, Done, Step, _actor, _Params
from app.finance.model.context import Ctx
from app.finance.service import FinanceError

#: Раздел, без которого календаря не видно, и раздел, открытый на правку, - кто его ведёт.
VIEW_RESOURCE = "reports.cash"
EDIT_RESOURCE = "reports.plan"


def _day(raw: Any) -> date | None:
    if raw in (None, ""):
        return None
    try:
        return raw if isinstance(raw, date) else date.fromisoformat(str(raw))
    except ValueError as exc:
        raise FinanceError("День - ГГГГ-ММ-ДД") from exc


def ask(ctx: Ctx, raw: dict[str, Any]) -> dict[str, Any]:
    """Вопрос `{"source": "calendar", "params": {from, to, days, scenario, exclude}}` - календарь на день `as_of`."""
    from app.finance.contracts.service import today
    from app.finance.core import book as book_module
    from app.finance.core import cash_calendar
    from app.finance.model import VERSION
    from app.finance.model.ask import debts_access

    if not ctx.viewer.sees(VIEW_RESOURCE):
        raise PermissionError("Платёжный календарь видит тот, кому открыт раздел «Деньги»")
    debts_access(ctx)
    params = dict(raw.get("params") or {})
    day = _day(raw.get("as_of")) or today()
    scenario = str(params.get("scenario") or "terms")
    if scenario not in cash_calendar.SCENARIOS:
        raise FinanceError("Сценарий - terms, habit или cash")
    days = params.get("days")
    if days is not None and not (1 <= int(days) <= cash_calendar.MAX_DAYS):
        raise FinanceError(f"Дней в окне - от 1 до {cash_calendar.MAX_DAYS}")
    exclude = [str(item) for item in params.get("exclude") or [] if str(item).strip()]
    book, error = book_module.current(ctx.session, ctx.workspace.id, wait=book_module.FIRST_READ_WAIT)
    try:
        body = cash_calendar.build(
            ctx.session, ctx.workspace, ctx.access, today=day, start=_day(params.get("from")), end=_day(params.get("to")),
            days=int(days) if days is not None else None, scenario=scenario, exclude=exclude, book=book,
        )
    except ValueError as exc:
        raise FinanceError(str(exc)) from exc
    body["book_error"] = error
    question = {"source": "calendar", "params": {key: value for key, value in params.items() if value not in (None, "", [])}}
    return {"version": VERSION, "question": question, "as_of": day.isoformat(), "source": "calendar", **body}


# ── Плановый платёж и обещание ───────────────────────────────────────────────


class CashPlanParams(_Params):
    #: Есть - правка этого ожидания, нет - новое.
    id: uuid.UUID | None = None
    direction: Literal["in", "out"] | None = None
    #: Наше юрлицо (`describe` → `entities`).
    entity: uuid.UUID | None = None
    day: date | None = None
    amount: str | None = Field(default=None, max_length=32)
    #: Строка календаря словами компании («Аренда офисов»).
    line: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=300)
    party: str | None = Field(default=None, max_length=300)
    counterparty: uuid.UUID | None = None
    category: uuid.UUID | None = None
    contract: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=2000)
    #: Оплачено этой операцией - решение человека, когда сведение с фактом ошиблось.
    paid_by: uuid.UUID | None = None
    cancelled: bool | None = None


_RENAMES = {"entity": "entity_id", "counterparty": "counterparty_id", "category": "category_id", "contract": "contract_id"}


def _values(p: _Params) -> dict[str, Any]:
    given = p.model_dump(exclude_unset=True)
    given.pop("id", None)
    return {_RENAMES.get(key, key): value for key, value in given.items()}


def _save(ctx: Ctx, p: CashPlanParams | "PromiseParams", kind: str) -> Done:
    from app.finance.core import cash_plans

    actor = _actor(ctx)
    values = _values(p)
    if p.id is None:
        if kind == "payment" and values.get("amount") in (None, ""):
            raise CommandError("У планового платежа нужна сумма")
        plan = cash_plans.add(ctx.session, ctx.workspace, ctx.access, actor, kind=kind, **values)
        return Done(title=cash_plans._title(plan), result=cash_plans.out(plan, {}), steps=[Step("cash_created", {"id": str(plan.id)})],
                    contracts=[str(plan.contract_id)] if plan.contract_id else [])
    plan = cash_plans.get(ctx.session, ctx.workspace, p.id)
    if plan.kind != kind:
        raise CommandError("Это ожидание другого вида: плановый платёж правит cash.plan, обещание - cash.promise")
    plan, before = cash_plans.update(ctx.session, ctx.workspace, ctx.access, actor, p.id, values)
    return Done(title=cash_plans._title(plan), result=cash_plans.out(plan, {}),
                steps=[Step("cash_state", {"id": str(plan.id), "before": before})],
                contracts=[str(plan.contract_id)] if plan.contract_id else [])


def _plan(ctx: Ctx, p: CashPlanParams) -> Done:
    return _save(ctx, p, "payment")


class PromiseParams(_Params):
    id: uuid.UUID | None = None
    contract: uuid.UUID | None = None
    day: date | None = None
    #: Пусто - весь долг договора.
    amount: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=2000)
    cancelled: bool | None = None


def _promise(ctx: Ctx, p: PromiseParams) -> Done:
    if p.id is None and (p.contract is None or p.day is None):
        raise CommandError("Обещание - по договору (contract) и к дате (day)")
    return _save(ctx, p, "promise")


class PlanRemove(_Params):
    id: uuid.UUID


def _remove(ctx: Ctx, p: PlanRemove) -> Done:
    from app.finance.core import cash_plans

    before = cash_plans.remove(ctx.session, ctx.workspace, ctx.access, _actor(ctx), p.id)
    return Done(title="ожидание снято", result={"id": str(p.id)}, steps=[Step("cash_state", {"id": str(p.id), "before": before})])


# ── Принять повтор журнала ───────────────────────────────────────────────────


class Accept(_Params):
    #: Ключи предложений (`calendar` → `proposals[].key`).
    keys: list[str] = Field(min_length=1, max_length=200)
    #: До какого дня искать предложения (по умолчанию - окно календаря).
    to: date | None = None


def _accept(ctx: Ctx, p: Accept) -> Done:
    from app.finance.contracts.service import today
    from app.finance.core import cash_calendar, cash_plans

    actor = _actor(ctx)
    now = today()
    calendar = cash_calendar.Calendar(
        ctx.session, ctx.workspace, ctx.access, start=now, end=p.to or cash_calendar.window(now)[1], today=now,
    )
    calendar._repeats(cash_plans.active(ctx.session, ctx.workspace.id))
    found = {item["key"]: item for item in calendar.proposals}
    created: list[str] = []
    for key in dict.fromkeys(p.keys):
        item = found.get(key)
        if item is None:
            raise CommandError(f"Предложения {key} нет - спросите календарь заново")
        plan = cash_plans.add(
            ctx.session, ctx.workspace, ctx.access, actor, kind="payment", direction="out", entity_id=item["entity_id"],
            day=item["day"], amount=item["amount"], line=item["line"], party=item["party"],
            counterparty_id=item["counterparty_id"], category_id=item["category_id"], source="history", source_key=key,
            source_name=f"журнал: {item['words']}",
        )
        created.append(str(plan.id))
    return Done(title=f"из журнала принято плановых платежей: {len(created)}", result={"created": created},
                steps=[Step("cash_import", {"created": created, "name": "повторы журнала"})])


# ── Загрузка из таблицы ──────────────────────────────────────────────────────


class Import(_Params):
    """Лист файлом (`name` + `data` base64) или таблица Google (`link` + `tabs`)."""

    name: str = Field(default="", max_length=255)
    data: str | None = Field(default=None, max_length=22 * 1024 * 1024)
    link: str | None = Field(default=None, max_length=500)
    tabs: list[str] = Field(default_factory=list, max_length=40)


def _import(ctx: Ctx, p: Import) -> Done:
    from app.finance.core import cash_plans
    from app.finance.model.entry import _file

    if p.data:
        if not p.name:
            raise CommandError("У файла нужно имя (name)")
        tables = cash_plans.tables_of_file(_file(p.name, p.data), p.name)
        name = p.name
    elif p.link:
        if not p.tabs:
            raise CommandError("Какие листы таблицы читать - tabs")
        tables = cash_plans.tables_of_google(p.link, p.tabs)
        name = p.name or "таблица Google"
    else:
        raise CommandError("Файл (name + data) или таблица Google (link + tabs)")
    from app.finance.core import settings as core_settings

    sheets, skipped = cash_plans.read_tables(tables, core_settings.intake_words(ctx.session, ctx.workspace.id))
    if not sheets:
        raise CommandError("; ".join(skipped) or "В таблице нет листов с плановыми платежами или долгами с ожидаемой датой")
    out = cash_plans.import_sheets(ctx.session, ctx.workspace, ctx.access, _actor(ctx), sheets, name=name, apply=True)
    out["skipped"] = skipped
    step = Step("cash_import", {"created": out.pop("created"), "before": out.pop("before"), "name": name})
    return Done(
        title=f"«{name}»: новых {out['new']}, изменено {out['changed']}, снято {out['removed']}", result=out, steps=[step],
    )


# ── Настройки ────────────────────────────────────────────────────────────────


class Settings(_Params):
    #: Счета, которые не деньги календаря (виртуальные, «сумма из 1С»).
    skip_accounts: list[uuid.UUID] | None = None
    #: {"days": срок компании, "contracts": {id: {"day": N} | {"days": N} | null}}.
    terms: dict[str, Any] | None = None
    #: С какого возраста долг - старый, дней.
    old_after: int | None = Field(default=None, ge=1, le=3650)
    #: Неснижаемый остаток юрлица: {id юрлица: сумма | null}.
    reserve: dict[str, Any] | None = None
    #: Свои нерабочие и рабочие дни: {"off": [...], "working": [...]}.
    holidays: dict[str, list[str]] | None = None


def _settings(ctx: Ctx, p: Settings) -> Done:
    from app.finance.core import cash_plans

    changes = {key: ([str(item) for item in value] if key == "skip_accounts" else value)
               for key, value in p.model_dump(exclude_unset=True).items()}
    if not changes:
        raise CommandError("Нечего менять: skip_accounts, terms, old_after, reserve или holidays")
    settings, before = cash_plans.set_calendar(ctx.session, ctx.workspace, ctx.access, _actor(ctx), changes)
    return Done(title="платёжный календарь: настройки", result=settings.out(), steps=[Step("cash_settings", {"before": before})])


CASH_COMMANDS: tuple[Command, ...] = (
    Command("cash.plan", "Плановый платёж (приход или расход) в платёжном календаре", EDIT_RESOURCE, CashPlanParams, _plan,
            lambda p: [str(p.contract)] if p.contract else []),
    Command("cash.promise", "Обещание клиента заплатить долг договора к дате", EDIT_RESOURCE, PromiseParams, _promise,
            lambda p: [str(p.contract)] if p.contract else []),
    Command("cash.plan.remove", "Снять плановый платёж или обещание", EDIT_RESOURCE, PlanRemove, _remove),
    Command("cash.accept", "Принять повторяющиеся расходы журнала плановыми платежами", EDIT_RESOURCE, Accept, _accept),
    Command("cash.import", "Плановые платежи и обещания из таблицы (файл или Google)", EDIT_RESOURCE, Import, _import),
    Command("cash.settings", "Настройки платёжного календаря", EDIT_RESOURCE, Settings, _settings, setup=True),
)


__all__ = ["CASH_COMMANDS", "EDIT_RESOURCE", "VIEW_RESOURCE", "ask"]
