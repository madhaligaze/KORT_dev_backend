"""Ожидания платёжного календаря: плановые платежи, обещания по долгам, настройки, загрузка из таблиц.

Календарь (`core/cash_calendar.py`) выводит почти всё сам. Здесь - то, чего в
данных нет и что говорит человек:

* **плановый платёж** (`CashPlan.kind = payment`) - приход или расход нашего
  юрлица на день: аренда офиса, налоги, зарплата, займ от учредителя. Строка
  календаря - словами компании («Аренда офисов»), не из списка ядра: у другой
  компании строки другие;
* **обещание** (`promise`) - клиент сказал, что заплатит долг договора к дате:
  сумма (пусто - весь долг) и итоги переговоров. Долг без обещания в остаток
  не идёт - «без даты»;
* **настройки календаря** (`core_settings.data.calendar`): какие счета - деньги
  календаря (`skip_accounts` - не считать), срок оплаты после начала периода
  (`terms`: дней у компании, день или дней у договора), с какого возраста долг
  старый (`old_after`, дней), неснижаемый остаток юрлица (`reserve`) - ниже
  него разрыв, свои праздничные и рабочие дни (`holidays`).

Загрузка из таблиц - формы приёмника «платёж по плану» и «обещание по долгу»
(`intake.recognize.read_as`): колонки по названиям, а не по номерам. Лист -
правда для своих строк: повторная загрузка того же листа правит дату и сумму
своих ожиданий (ключ строки - юрлицо, строка, статья, получатель и порядковый
номер такой же строки), строки, которых в листе больше нет, снимаются. Чужое
- ожидания руками, другого листа, журнала - не трогается.
"""
from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts.fields import party_key
from app.finance.contracts.models import Contract, CounterpartyName, GroupEntity
from app.finance.contracts.service import Access, Actor
from app.finance.core.models import CASH_DIRECTIONS, CashPlan, CoreSettings
from app.finance.layout import norm
from app.finance.models import Account, Category, Counterparty, Operation, Workspace
from app.finance.service import FinanceError

_ZERO = Decimal("0")
#: Срок оплаты по умолчанию: день начала периода (срок счёта), без отсрочки.
TERMS_DAYS = 0
#: С какого возраста долг - старый: строка «долг» делится на две.
OLD_AFTER = 60
#: Сведение плана с фактом: факт на столько дней раньше плана и позже.
MATCH_BEFORE, MATCH_AFTER = 5, 10
#: Тот же получатель - сумма может отличаться на столько (доля).
MATCH_SHARE = Decimal("0.05")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _uuid(raw: Any) -> uuid.UUID | None:
    if raw in (None, ""):
        return None
    try:
        return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
    except ValueError:
        return None


# ── Настройки календаря ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Settings:
    skip_accounts: frozenset[uuid.UUID] = frozenset()
    terms_days: int = TERMS_DAYS
    #: Договор → {"day": число месяца} или {"days": дней после начала периода}.
    contract_terms: dict[uuid.UUID, dict[str, int]] = field(default_factory=dict)
    old_after: int = OLD_AFTER
    reserve: dict[uuid.UUID, Decimal] = field(default_factory=dict)
    holidays_off: frozenset[date] = frozenset()
    holidays_working: frozenset[date] = frozenset()

    def out(self) -> dict[str, Any]:
        return {
            "skip_accounts": sorted(str(item) for item in self.skip_accounts),
            "terms": {"days": self.terms_days, "contracts": {str(key): value for key, value in self.contract_terms.items()}},
            "old_after": self.old_after,
            "reserve": {str(key): _money(value) for key, value in self.reserve.items()},
            "holidays": {"off": sorted(day.isoformat() for day in self.holidays_off),
                         "working": sorted(day.isoformat() for day in self.holidays_working)},
        }


def _raw(session: Session, workspace: Workspace) -> dict[str, Any]:
    row = session.get(CoreSettings, workspace.id)
    return dict(((row.data if row is not None else None) or {}).get("calendar") or {})


def _days(items: Any) -> frozenset[date]:
    out = set()
    for item in items or []:
        try:
            out.add(date.fromisoformat(str(item)))
        except ValueError:
            continue
    return frozenset(out)


def settings_of(session: Session, workspace: Workspace) -> Settings:
    raw = _raw(session, workspace)
    terms = raw.get("terms") or {}
    contract_terms: dict[uuid.UUID, dict[str, int]] = {}
    for key, value in (terms.get("contracts") or {}).items():
        cid = _uuid(key)
        if cid is None or not isinstance(value, dict):
            continue
        clean = {name: int(value[name]) for name in ("day", "days") if isinstance(value.get(name), int)}
        if clean:
            contract_terms[cid] = clean
    reserve: dict[uuid.UUID, Decimal] = {}
    for key, value in (raw.get("reserve") or {}).items():
        entity = _uuid(key)
        try:
            if entity is not None:
                reserve[entity] = Decimal(str(value))
        except InvalidOperation:
            continue
    holidays = raw.get("holidays") or {}
    return Settings(
        skip_accounts=frozenset(item for item in (_uuid(raw_id) for raw_id in raw.get("skip_accounts") or []) if item),
        terms_days=int(terms.get("days") or 0),
        contract_terms=contract_terms,
        old_after=int(raw.get("old_after") or OLD_AFTER),
        reserve=reserve,
        holidays_off=_days(holidays.get("off")),
        holidays_working=_days(holidays.get("working")),
    )


def _write_raw(session: Session, workspace: Workspace, actor: Actor, raw: dict[str, Any] | None) -> None:
    row = session.get(CoreSettings, workspace.id)
    if row is None:
        row = CoreSettings(workspace_id=workspace.id, data={})
        session.add(row)
    data = dict(row.data or {})
    if raw:
        data["calendar"] = raw
    else:
        data.pop("calendar", None)
    row.data = data
    row.updated_by = actor.user_id
    row.updated_at = _now()
    session.flush()


def set_calendar(
    session: Session, workspace: Workspace, access: Access, actor: Actor, changes: dict[str, Any]
) -> tuple[Settings, dict[str, Any]]:
    """Записать настройки календаря; вернуть новые и прежние (для «Вернуть»).

    `changes` - те же ключи, что в `Settings.out()`; ключ, которого нет, не
    меняется. Срок оплаты договора - `terms.contracts[id] = {"day": 10}` или
    `{"days": 5}`, `null` - снять.
    """
    if not access.setup:
        raise PermissionError("Настройки платёжного календаря меняет владелец или администратор")
    before = _raw(session, workspace)
    raw = dict(before)
    own = _own_entities(session, workspace.id)
    if "skip_accounts" in changes:
        accounts = set(session.scalars(sa.select(Account.id).where(Account.workspace_id == workspace.id)))
        ids = []
        for item in changes.get("skip_accounts") or []:
            account = _uuid(item)
            if account is None or account not in accounts:
                raise FinanceError(f"Счёта {item} нет")
            ids.append(str(account))
        raw["skip_accounts"] = sorted(set(ids))
    if "terms" in changes:
        terms = dict(raw.get("terms") or {})
        given = changes.get("terms") or {}
        if "days" in given:
            days = int(given.get("days") or 0)
            if not 0 <= days <= 365:
                raise FinanceError("Срок оплаты - от 0 до 365 дней после начала периода")
            terms["days"] = days
        contracts = dict(terms.get("contracts") or {})
        for key, value in (given.get("contracts") or {}).items():
            cid = _uuid(key)
            if cid is None or session.get(Contract, cid) is None:
                raise FinanceError(f"Договора {key} нет")
            if value in (None, {}):
                contracts.pop(str(cid), None)
                continue
            if "day" in value and not 1 <= int(value["day"]) <= 31:
                raise FinanceError("Число оплаты - от 1 до 31")
            if "days" in value and not 0 <= int(value["days"]) <= 365:
                raise FinanceError("Срок оплаты - от 0 до 365 дней после начала периода")
            contracts[str(cid)] = {name: int(value[name]) for name in ("day", "days") if name in value}
        terms["contracts"] = contracts
        raw["terms"] = terms
    if "old_after" in changes:
        old_after = int(changes.get("old_after") or OLD_AFTER)
        if not 1 <= old_after <= 3650:
            raise FinanceError("Старый долг - от 1 дня")
        raw["old_after"] = old_after
    if "reserve" in changes:
        reserve = dict(raw.get("reserve") or {})
        for key, value in (changes.get("reserve") or {}).items():
            entity = _uuid(key)
            if entity is None or entity not in own:
                raise FinanceError(f"{key} - не наше юрлицо")
            if value in (None, ""):
                reserve.pop(str(entity), None)
            else:
                reserve[str(entity)] = str(_amount(value))
        raw["reserve"] = reserve
    if "holidays" in changes:
        given = changes.get("holidays") or {}
        raw["holidays"] = {
            name: sorted(day.isoformat() for day in _days(given.get(name)))
            for name in ("off", "working")
        }
    _write_raw(session, workspace, actor, raw)
    history.write(
        session, workspace, kind="core.settings", entity="core_settings", entity_id=workspace.id,
        title="платёжный календарь: настройки", before={"calendar": before}, after={"calendar": raw}, actor=actor.email,
    )
    return settings_of(session, workspace), before


def restore_settings(session: Session, workspace: Workspace, access: Access, actor: Actor, before: dict[str, Any] | None) -> None:
    if not access.setup:
        raise PermissionError("Настройки платёжного календаря меняет владелец или администратор")
    _write_raw(session, workspace, actor, before or None)


# ── Ожидания: заведение, правка, снятие ──────────────────────────────────────


def _own_entities(session: Session, workspace_id: uuid.UUID) -> dict[uuid.UUID, str]:
    rows = session.execute(
        sa.select(GroupEntity.counterparty_id, GroupEntity.code, Counterparty.name)
        .join(Counterparty, Counterparty.id == GroupEntity.counterparty_id)
        .where(GroupEntity.workspace_id == workspace_id, GroupEntity.archived_at.is_(None))
    ).all()
    return {entity_id: (code or name or "") for entity_id, code, name in rows}


def _amount(raw: Any) -> Decimal:
    try:
        value = Decimal(str(raw).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise FinanceError("Сумма - числом") from exc
    if value < 0:
        raise FinanceError("Сумма - без минуса: приход или расход говорит направление")
    return value.quantize(Decimal("0.01"))


def snapshot(plan: CashPlan) -> dict[str, Any]:
    return {
        "kind": plan.kind, "direction": plan.direction, "entity_id": str(plan.entity_id) if plan.entity_id else None,
        "day": plan.day.isoformat() if plan.day else None, "amount": _money(plan.amount), "line": plan.line,
        "category_id": str(plan.category_id) if plan.category_id else None,
        "counterparty_id": str(plan.counterparty_id) if plan.counterparty_id else None, "party": plan.party,
        "title": plan.title, "contract_id": str(plan.contract_id) if plan.contract_id else None, "note": plan.note,
        "operation_id": str(plan.operation_id) if plan.operation_id else None,
        "cancelled": plan.cancelled_at is not None, "deleted": plan.deleted_at is not None,
    }


def _revive(plan: CashPlan, state: dict[str, Any]) -> None:
    plan.direction = state.get("direction") or plan.direction
    plan.entity_id = _uuid(state.get("entity_id"))
    plan.day = date.fromisoformat(state["day"]) if state.get("day") else None
    plan.amount = Decimal(state["amount"]) if state.get("amount") is not None else None
    plan.line = state.get("line") or ""
    plan.category_id = _uuid(state.get("category_id"))
    plan.counterparty_id = _uuid(state.get("counterparty_id"))
    plan.party = state.get("party") or ""
    plan.title = state.get("title") or ""
    plan.contract_id = _uuid(state.get("contract_id"))
    plan.note = state.get("note") or ""
    plan.operation_id = _uuid(state.get("operation_id"))
    plan.cancelled_at = (plan.cancelled_at or _now()) if state.get("cancelled") else None
    plan.deleted_at = (plan.deleted_at or _now()) if state.get("deleted") else None


#: Поля, которые человек правит у ожидания.
FIELDS = ("direction", "entity_id", "day", "amount", "line", "category_id", "counterparty_id", "party", "title", "contract_id", "note")


def visible_ids(session: Session, workspace: Workspace, access: Access, *, want: uuid.UUID | None = None) -> frozenset[uuid.UUID]:
    """Договоры, открытые спрашивающему, - один раз на запрос: загрузка листа проверяет каждое обещание.

    `want` - договор, которого в запомненном нет: его могли завести в той же
    пачке команд, и список перечитывается один раз.
    """
    from app.finance.core.shadow import _visible

    cache: dict[tuple[uuid.UUID, int], frozenset[uuid.UUID]] = session.info.setdefault("cash_visible", {})
    key = (workspace.id, id(access))
    if key not in cache or (want is not None and want not in cache[key]):
        cache[key] = frozenset(item.id for item in _visible(session, workspace, access))
    return cache[key]


def _check(session: Session, workspace: Workspace, access: Access, plan: CashPlan) -> None:
    if plan.direction not in CASH_DIRECTIONS:
        raise FinanceError("Направление - приход (in) или расход (out)")
    own = _own_entities(session, workspace.id)
    if plan.entity_id is not None and plan.entity_id not in own:
        raise FinanceError("Юрлицо ожидания - одно из наших юрлиц")
    if plan.category_id is not None:
        category = session.get(Category, plan.category_id)
        if category is None or category.workspace_id != workspace.id:
            raise FinanceError("Такой статьи нет")
    if plan.counterparty_id is not None:
        party = session.get(Counterparty, plan.counterparty_id)
        if party is None or party.workspace_id != workspace.id:
            raise FinanceError("Такого участника нет")
    if plan.contract_id is not None:
        contract = session.get(Contract, plan.contract_id)
        if contract is None or contract.workspace_id != workspace.id or contract.deleted_at is not None:
            raise FinanceError("Такого договора нет")
        if contract.id not in visible_ids(session, workspace, access, want=contract.id):
            raise PermissionError("Этот договор вам не открыт")
    if plan.kind == "promise":
        if plan.contract_id is None:
            raise FinanceError("Обещание - по договору: чей долг обещали заплатить")
        if plan.day is None:
            raise FinanceError("Обещание - к дате: когда обещали заплатить")
        plan.direction = "in"
    elif plan.amount is None:
        raise FinanceError("У планового платежа нужна сумма")
    if plan.entity_id is None and plan.contract_id is None and len(own) > 1:
        raise FinanceError("Чьи это деньги - укажите наше юрлицо")


def _apply(plan: CashPlan, values: dict[str, Any]) -> None:
    for name in FIELDS:
        if name not in values:
            continue
        value = values[name]
        if name in ("entity_id", "category_id", "counterparty_id", "contract_id"):
            value = _uuid(value)
        elif name == "day":
            value = value if isinstance(value, date) or value is None else date.fromisoformat(str(value))
        elif name == "amount":
            value = None if value in (None, "") else _amount(value)
        elif value is None:
            value = ""
        setattr(plan, name, value)


def add(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *, kind: str = "payment", **values: Any
) -> CashPlan:
    """Завести плановый платёж или обещание (`kind`); поля - `FIELDS`."""
    plan = CashPlan(workspace_id=workspace.id, kind=kind, direction=values.pop("direction", None) or "out",
                    source=values.pop("source", "manual"), source_key=values.pop("source_key", "") or "",
                    source_name=values.pop("source_name", "") or "", created_by=actor.user_id, updated_by=actor.user_id)
    _apply(plan, values)
    _check(session, workspace, access, plan)
    session.add(plan)
    session.flush()
    history.write(session, workspace, kind="cash.plan", entity="cash_plan", entity_id=plan.id,
                  title=_title(plan), after=snapshot(plan), actor=actor.email)
    return plan


def get(session: Session, workspace: Workspace, plan_id: uuid.UUID) -> CashPlan:
    plan = session.get(CashPlan, plan_id)
    if plan is None or plan.workspace_id != workspace.id or plan.deleted_at is not None:
        raise FinanceError("Такого ожидания нет")
    return plan


def update(
    session: Session, workspace: Workspace, access: Access, actor: Actor, plan_id: uuid.UUID, values: dict[str, Any]
) -> tuple[CashPlan, dict[str, Any]]:
    plan = get(session, workspace, plan_id)
    before = snapshot(plan)
    _apply(plan, values)
    if "paid_by" in values:
        operation_id = _uuid(values.get("paid_by"))
        if operation_id is not None:
            operation = session.get(Operation, operation_id)
            if operation is None or operation.workspace_id != workspace.id or operation.deleted_at is not None:
                raise FinanceError("Такой операции нет")
        plan.operation_id = operation_id
    if "cancelled" in values:
        plan.cancelled_at = _now() if values.get("cancelled") else None
    _check(session, workspace, access, plan)
    plan.updated_by = actor.user_id
    plan.updated_at = _now()
    session.flush()
    history.write(session, workspace, kind="cash.plan", entity="cash_plan", entity_id=plan.id,
                  title=_title(plan), before=before, after=snapshot(plan), actor=actor.email)
    return plan, before


def remove(session: Session, workspace: Workspace, access: Access, actor: Actor, plan_id: uuid.UUID) -> dict[str, Any]:
    plan = get(session, workspace, plan_id)
    if plan.contract_id is not None:
        _check(session, workspace, access, plan)
    before = snapshot(plan)
    plan.deleted_at = _now()
    plan.updated_by = actor.user_id
    session.flush()
    history.write(session, workspace, kind="cash.plan.delete", entity="cash_plan", entity_id=plan.id,
                  title=_title(plan), before=before, actor=actor.email)
    return before


def undo(session: Session, workspace: Workspace, access: Access, actor: Actor, kind: str, payload: dict[str, Any],
         done: list[str], skipped: list[str]) -> None:
    """Вернуть шаг пачки `cash_*` (см. `contracts/restore._restore_step`)."""
    if kind == "cash_settings":
        restore_settings(session, workspace, access, actor, payload.get("before"))
        done.append("настройки платёжного календаря - как были")
        return
    if kind == "cash_import":
        for raw_id in payload.get("created") or []:
            plan = session.get(CashPlan, _uuid(raw_id))
            if plan is not None and plan.workspace_id == workspace.id:
                plan.deleted_at = plan.deleted_at or _now()
        for raw_id, state in (payload.get("before") or {}).items():
            plan = session.get(CashPlan, _uuid(raw_id))
            if plan is not None and plan.workspace_id == workspace.id:
                _revive(plan, state)
        session.flush()
        done.append(f"загрузка «{payload.get('name') or 'таблицы'}» - как было")
        return
    plan = session.get(CashPlan, _uuid(payload.get("id")))
    if plan is None or plan.workspace_id != workspace.id:
        skipped.append("ожидания больше нет - вернуть нечего")
        return
    if kind == "cash_created":
        plan.deleted_at = plan.deleted_at or _now()
        done.append(f"{_title(plan)} - убрано")
    else:
        _revive(plan, payload.get("before") or {})
        done.append(f"{_title(plan)} - как было")
    plan.updated_by = actor.user_id
    plan.updated_at = _now()
    session.flush()


def _title(plan: CashPlan) -> str:
    what = "обещание" if plan.kind == "promise" else ("приход" if plan.direction == "in" else "расход")
    day = f" {plan.day:%d.%m.%Y}" if plan.day else " без даты"
    amount = f" {plan.amount:,.0f}".replace(",", " ") if plan.amount is not None else ""
    label = plan.title or plan.line or plan.party
    return f"{what}{day}{amount}" + (f" - {label}" if label else "")


def active(session: Session, workspace_id: uuid.UUID) -> list[CashPlan]:
    """Ожидания, которые календарь считает: не сняты и не отменены."""
    return list(session.scalars(
        sa.select(CashPlan).where(
            CashPlan.workspace_id == workspace_id, CashPlan.deleted_at.is_(None), CashPlan.cancelled_at.is_(None)
        ).order_by(CashPlan.day, CashPlan.created_at)
    ))


def out(plan: CashPlan, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    data = snapshot(plan)
    data.update({
        "id": str(plan.id), "source": plan.source, "source_name": plan.source_name,
        "entity": names.get(plan.entity_id, "") if plan.entity_id else "",
        "label": plan.line or plan.title or plan.party,
    })
    return data


# ── Загрузка из таблицы: «платёж по плану» и «обещание по долгу» ────────────


def _forms(words: dict[str, dict[str, list[str]]] | None = None) -> tuple[Any, Any]:
    """Две формы календаря со словарём компании (`core/settings.intake_words`).

    Своё название колонки «наше юрлицо», «клиент», «№ договора» компания уже
    назвала в шапке сводной таблицы (форма «договор за месяц»): те же
    написания узнают и листы календаря - без вопроса и без слов одной компании
    в ядре.
    """
    from app.finance.intake.forms import Form, _f, with_words

    words = words or {}
    month = words.get("contract_month") or {}
    shared = {"entity": month.get("own", []), "client": month.get("customer", []), "number": month.get("number", [])}

    plan = Form(
        "cash_payment", "платёж по плану", "платежи (план), внеплановые поступления и расходы, бюджет платежей",
        (
            _f("day", "Дата оплаты", "дата", "дата платежа", "дата поступления", "ожидаемая дата", "срок оплаты", "когда"),
            _f("entity", "Фирма", "фирма (код)", "наше юрлицо", "юрлицо", "организация", "компания", "тоо"),
            _f("line", "Строка календаря", "строка", "статья календаря", "группа расходов"),
            _f("title", "Статья", "назначение", "статья расхода", "статья дохода", "что"),
            _f("party", "Получатель", "контрагент", "что / от кого", "от кого", "кому", "плательщик", "поставщик"),
            _f("amount", "Сумма", "сумма платежа", "сумма к оплате", money=True),
            _f("income", "Поступление", "приход", "поступления", money=True),
            _f("expense", "Расход", "расходы", "списание", money=True),
            _f("direction", "Вид", "тип", "направление", "вид операции"),
            _f("comment", "Комментарий", "примечание", "коммент"),
        ),
        needs=(("day",), ("amount", "income", "expense"), ("entity", "line", "title", "party")),
        words=("платеж", "платёж", "план", "внеплан", "бюджет", "календар"),
    )
    promise = Form(
        "cash_promise", "обещание по долгу", "долги с ожидаемой датой оплаты, итоги переговоров с должниками",
        (
            _f("entity", "Наше юрлицо", "фирма", "юрлицо", "организация", "тоо"),
            _f("client", "Клиент", "заказчик", "должник", "контрагент"),
            _f("number", "Договор", "№ договора", "номер договора"),
            _f("day", "Ожидаемая дата оплаты", "обещанная дата", "дата обещания", "обещал оплатить", "ожидаемая дата"),
            _f("amount", "Ожидаемая сумма", "обещанная сумма", "сумма обещания", money=True),
            _f("debt", "Итого долг", "долг", "сумма долга", money=True),
            _f("note", "Итоги переговоров", "результат переговоров", "переговоры"),
            _f("comment", "Комментарий", "примечание"),
        ),
        needs=(("number",), ("day",)),
        words=("долг", "должник", "обещан", "переговор"),
    )

    def own(form: Any) -> Any:
        merged = {key: [*names, *(words.get(form.key) or {}).get(key, [])] for key, names in shared.items()}
        for key, names in (words.get(form.key) or {}).items():
            merged.setdefault(key, list(names))
        return with_words(form, {key: names for key, names in merged.items() if form.field(key) is not None})

    return own(plan), own(promise)


@dataclass
class SheetRows:
    """Лист, прочитанный одной из двух форм: строки ключ → ячейка."""

    name: str
    form: str
    rows: list[tuple[int, dict[str, Any]]]
    columns: dict[str, str]


def read_tables(tables: Sequence[Any], words: dict[str, dict[str, list[str]]] | None = None) -> tuple[list[SheetRows], list[str]]:
    """Листы → строки форм календаря; лист, не похожий ни на одну, - в отказы словами. `words` - словарь компании."""
    from app.finance.intake import recognize
    from app.finance.intake.rows import is_empty, is_label_total

    plan_form, promise_form = _forms(words)
    found: list[SheetRows] = []
    skipped: list[str] = []
    for table in tables:
        readings = [item for item in (recognize.read_as(table, form) for form in (promise_form, plan_form)) if item and item.needs_ok]
        if not readings:
            skipped.append(f"«{table.name}» - не платежи по плану и не долги с ожидаемой датой")
            continue
        reading = max(readings, key=lambda item: item.score)
        if reading.refusals:
            skipped.append(f"«{table.name}»: {reading.refusals[0]['text']}")
            continue
        at = {column.key: column.index for column in reading.columns if column.key}
        rows: list[tuple[int, dict[str, Any]]] = []
        for index in range(reading.body_start, len(table.rows)):
            cells = table.rows[index]
            if index in table.hidden or is_empty(cells) or is_label_total(cells):
                continue
            rows.append((index + 1, {key: (cells[position] if position < len(cells) else None) for key, position in at.items()}))
        found.append(SheetRows(table.name, reading.form.key, rows,
                               {column.key: column.header.strip() for column in reading.columns if column.key}))
    return found, skipped


def _date(value: Any) -> date | None:
    from app.finance.core import book

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 30000 < value < 80000:
        return date.fromordinal(date(1899, 12, 30).toordinal() + int(value))
    text = book.text_of(value)
    if len(text) >= 10 and text[4] == "-":
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
    return book.date_of(text)


def _sum(value: Any) -> Decimal | None:
    from app.finance.core import book

    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value))
    return book.money_of(value)


class Entities:
    """Наше юрлицо по слову таблицы: код («ТМС»), имя или полное имя - только однозначно."""

    def __init__(self, session: Session, workspace_id: uuid.UUID) -> None:
        self.by_key: dict[str, set[uuid.UUID]] = defaultdict(set)
        rows = session.execute(
            sa.select(GroupEntity.counterparty_id, GroupEntity.code, Counterparty.name, Counterparty.full_name)
            .join(Counterparty, Counterparty.id == GroupEntity.counterparty_id)
            .where(GroupEntity.workspace_id == workspace_id, GroupEntity.archived_at.is_(None))
        ).all()
        self.ids = {row[0] for row in rows}
        for entity_id, code, name, full_name in rows:
            for text in (code, name, full_name):
                if text and text.strip():
                    self.by_key[norm(text)].add(entity_id)
                    self.by_key[party_key(text)].add(entity_id)
        for entity_id, normalized in session.execute(
            sa.select(CounterpartyName.counterparty_id, CounterpartyName.normalized)
            .where(CounterpartyName.workspace_id == workspace_id, CounterpartyName.counterparty_id.in_(self.ids or {uuid.uuid4()}))
        ):
            self.by_key[normalized].add(entity_id)

    def find(self, text: Any) -> uuid.UUID | None:
        raw = str(text or "").strip()
        if not raw:
            return None
        for key in (norm(raw), party_key(raw)):
            hit = self.by_key.get(key) or set()
            if len(hit) == 1:
                return next(iter(hit))
        # «ТОО «Х» (КОД)» - код в скобках.
        if "(" in raw and raw.endswith(")"):
            return self.find(raw[raw.rfind("(") + 1:-1])
        return None


_IN_WORDS = ("поступлен", "приход", "доход", "займ от", "заем от", "возврат от")
_OUT_WORDS = ("расход", "списан", "платеж", "платёж", "оплата", "выплат")


def _direction(value: Any, income: Decimal | None, expense: Decimal | None) -> str | None:
    text = norm(value)
    if any(word in text for word in _IN_WORDS):
        return "in"
    if any(word in text for word in _OUT_WORDS):
        return "out"
    if income and not expense:
        return "in"
    if expense and not income:
        return "out"
    return None


def _key(*parts: Any) -> str:
    return hashlib.sha1("|".join(norm(part) for part in parts).encode()).hexdigest()[:20]


@dataclass
class Planned:
    """Строка листа, готовая стать ожиданием, или отказ словами."""

    line: int
    values: dict[str, Any]
    kind: str
    key: str = ""
    refusal: str = ""


def plan_rows(session: Session, workspace: Workspace, access: Access, sheets: Sequence[SheetRows], *, name: str) -> list[Planned]:
    """Строки листов → ожидания (ещё не записанные): юрлицо, договор, направление; спорное - отказом."""
    from app.finance.core import book as book_module
    entities = Entities(session, workspace.id)
    single = next(iter(entities.ids)) if len(entities.ids) == 1 else None
    matcher = book_module.Matcher(session, workspace.id)
    visible = visible_ids(session, workspace, access)
    out: list[Planned] = []
    for sheet in sheets:
        source = f"{name} · {sheet.name}" if name and name != sheet.name else sheet.name
        seen: dict[str, int] = defaultdict(int)
        for line, cells in sheet.rows:
            if sheet.form == "cash_promise":
                number = book_module.text_of(cells.get("number"))
                client = book_module.text_of(cells.get("client"))
                if not number and not client:
                    continue
                day = _date(cells.get("day"))
                if day is None:
                    # Долг без ожидаемой даты - не обещание: календарь сам покажет его «без даты».
                    continue
                own = book_module.text_of(cells.get("entity"))
                row = book_module.BookRow(line=line, own=own, customer=client, number=number, month="", month_label="")
                match = matcher.match(row)
                note = " · ".join(text for text in (book_module.text_of(cells.get("note")), book_module.text_of(cells.get("comment"))) if text)
                if match.state != "found" or match.contract_id not in visible:
                    reason = {"missing": "договора с таким номером нет в KORT", "ambiguous": "номер подходит к нескольким договорам",
                              "other_client": "номер есть, но у другого клиента"}.get(match.state, "договор вам не открыт")
                    out.append(Planned(line, {"number": number, "client": client}, "promise", refusal=f"{number or client}: {reason}"))
                    continue
                # Пусто - «весь долг», как его видел человек, обещая: колонка долга той же строки, иначе долг на сегодня.
                amount = _sum(cells.get("amount")) or _sum(cells.get("debt"))
                # Рассрочка - несколько строк одного договора со своими датами: каждая - своё обещание.
                identity = _key(source, match.contract_id)
                seen[identity] += 1
                out.append(Planned(
                    line,
                    {"contract_id": match.contract_id, "day": day, "amount": abs(amount) if amount else None, "note": note,
                     "entity_id": entities.find(own), "source_name": source},
                    "promise", key=f"table:promise:{identity}:{seen[identity]}",
                ))
                continue
            day = _date(cells.get("day"))
            income, expense = _sum(cells.get("income")), _sum(cells.get("expense"))
            amount = _sum(cells.get("amount"))
            if amount is None:
                amount = income or expense
            if amount is None or amount == 0:
                continue
            # Лист плановых платежей - расходы, если строка сама не говорит «поступление».
            direction = _direction(cells.get("direction"), income, expense) or "out"
            entity_text = book_module.text_of(cells.get("entity"))
            entity = entities.find(entity_text) if entity_text else single
            label = book_module.text_of(cells.get("line"))
            title = book_module.text_of(cells.get("title"))
            party = book_module.text_of(cells.get("party"))
            if entity is None:
                out.append(Planned(line, {"entity": entity_text}, "payment",
                                   refusal=f"строка {line}: «{entity_text}» - не наше юрлицо" if entity_text else f"строка {line}: чьи деньги - юрлицо не указано"))
                continue
            identity = _key(source, entity, direction, label, title, party)
            seen[identity] += 1
            out.append(Planned(
                line,
                {"direction": direction, "entity_id": entity, "day": day, "amount": abs(amount), "line": label or title,
                 "title": title if label else "", "party": party, "note": book_module.text_of(cells.get("comment")),
                 "source_name": source},
                "payment", key=f"table:{identity}:{seen[identity]}",
            ))
    return out


def _plain(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _money(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value if value is not None else None


def _same(state: dict[str, Any], values: dict[str, Any]) -> bool:
    return all((state.get(key) or None) == (_plain(value) or None) for key, value in values.items())


def import_sheets(
    session: Session, workspace: Workspace, access: Access, actor: Actor, sheets: Sequence[SheetRows], *, name: str, apply: bool
) -> dict[str, Any]:
    """Предпросмотр или запись: что появится, изменится, снимется; отказы словами.

    Лист - правда для своих строк (`source_name`): строки, которых в нём больше
    нет, снимаются, а не висят вечным планом.
    """
    planned = plan_rows(session, workspace, access, sheets, name=name)
    good = [item for item in planned if not item.refusal]
    names = {item.values.get("source_name") for item in good} | {
        f"{name} · {sheet.name}" if name and name != sheet.name else sheet.name for sheet in sheets
    }
    existing = {
        plan.source_key: plan
        for plan in session.scalars(sa.select(CashPlan).where(
            CashPlan.workspace_id == workspace.id, CashPlan.source == "table", CashPlan.deleted_at.is_(None),
            CashPlan.source_name.in_([item for item in names if item]),
        ))
    }
    created: list[str] = []
    changed: dict[str, dict[str, Any]] = {}
    same = 0
    wanted = {item.key for item in good}
    for item in good:
        plan = existing.get(item.key)
        if plan is None:
            if apply:
                values = dict(item.values)
                made = add(session, workspace, access, actor, kind=item.kind, source="table", source_key=item.key, **values)
                created.append(str(made.id))
            else:
                created.append(item.key)
            continue
        fresh = {key: value for key, value in item.values.items() if key in FIELDS}
        differs = not _same(snapshot(plan), fresh)
        if not differs:
            same += 1
            continue
        changed[str(plan.id)] = snapshot(plan)
        if apply:
            update(session, workspace, access, actor, plan.id, fresh)
    gone = [plan for key, plan in existing.items() if key not in wanted]
    removed: dict[str, dict[str, Any]] = {}
    for plan in gone:
        removed[str(plan.id)] = snapshot(plan)
        if apply:
            remove(session, workspace, access, actor, plan.id)
    totals: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for item in good:
        if item.kind == "payment":
            totals[item.values["direction"]] += item.values["amount"]
    return {
        "sheets": [{"name": sheet.name, "form": sheet.form, "rows": len(sheet.rows), "columns": sheet.columns} for sheet in sheets],
        "new": len(created), "changed": len(changed), "same": same, "removed": len(removed),
        "promises": sum(1 for item in good if item.kind == "promise"),
        "payments": sum(1 for item in good if item.kind == "payment"),
        "income": _money(totals["in"]), "expense": _money(totals["out"]),
        "refusals": [item.refusal for item in planned if item.refusal][:200],
        "created": created if apply else [],
        "before": {**changed, **removed} if apply else {},
    }


def tables_of_file(data: bytes, file_name: str) -> list[Any]:
    from app.finance.intake import tables

    try:
        return tables.read(data, file_name)
    except tables.TableError as exc:
        raise FinanceError(str(exc)) from exc


def tables_of_google(link: str, tabs: Iterable[str]) -> list[Any]:
    """Листы таблицы Google по ссылке или id: значения как видит человек."""
    import re

    from app.finance import google
    from app.finance.intake import tables

    found = re.search(r"/d/([A-Za-z0-9_-]{20,})", link or "")
    spreadsheet_id = found.group(1) if found else (link or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", spreadsheet_id):
        raise FinanceError("Ссылка на таблицу Google - вида https://docs.google.com/spreadsheets/d/…")
    out = []
    try:
        for tab in tabs:
            out.append(tables.from_values(tab, google.fetch_tab_values(spreadsheet_id, tab, fresh=True)))
    except google.GoogleError as exc:
        raise FinanceError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - отказ Google словами
        raise FinanceError(google.humanize(exc)) from exc
    return out


__all__ = [
    "Entities", "MATCH_AFTER", "MATCH_BEFORE", "MATCH_SHARE", "OLD_AFTER", "Settings", "SheetRows", "active", "add", "get",
    "import_sheets", "out", "plan_rows", "read_tables", "remove", "restore_settings", "set_calendar", "settings_of",
    "snapshot", "tables_of_file", "tables_of_google", "undo", "update",
]
