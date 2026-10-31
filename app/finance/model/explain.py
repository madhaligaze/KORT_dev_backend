"""Объяснить - число по ссылке до слагаемых, слагаемые - до фактов.

Ответ: `{title, value, formula, parts, check, exact}`. Часть - слагаемое со
знаком (`+`, `-`), у неё либо своя ссылка (`ref` - раскрыть дальше), либо
факт (`fact` - факт журнала или условие договора: дальше раскрывать нечего),
либо вложенные части. `check` - сумма частей со знаками; `exact` - совпала
ли она с числом до копейки (итог из долей может разойтись на округлении, и
это видно, а не спрятано).

Цепочки (что раскрывается во что):

* итог части листа, строка «По сотрудникам», «По возрасту», итог колонки -
  договоры с их числом (`left`, `amount`, `paid`);
* остаток договора - сумма договора минус оплачено;
* оплачено - оплаты KORT (факты журнала) или строки сводной таблицы;
* итог «Долгов» - группы; группа - договоры (доля - «долг договора × доля»);
* долг договора по начислениям - входящее сальдо, начисления месяцев (условие
  договора, соглашения и поправка месяца - факты), оплаты (факты);
  неоплаченное - месяцы с остатком, у каждого - начисление и оплаты в его счёт;
* долг по АВР - сальдо по АВР, акты (KORT, 1С, таблица), оплаты.

Считается заново с правами того, кто спрашивает: ссылка только говорит, что
считать.
"""
from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

import sqlalchemy as sa

from app.finance.contracts.models import Contract
from app.finance.contracts.service import NotFound, Registry, get_contract, people_of, visible_to
from app.finance.core import journal
from app.finance.core.calls import Labels
from app.finance.core.models import Fact
from app.finance.model import VERSION, refs
from app.finance.model.context import Ctx
from app.finance.service import FinanceError

_ZERO = Decimal("0")
_CENT = Decimal("0.01")


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(_CENT), "f") if value is not None else None


def _num(value: Any) -> Decimal:
    if value in (None, ""):
        return _ZERO
    return Decimal(str(value))


def _part(title: str, value: Decimal | str | None, sign: str = "+", **extra: Any) -> dict[str, Any]:
    amount = value if isinstance(value, str) or value is None else _money(value)
    return {"title": title, "value": amount, "sign": sign, **{key: item for key, item in extra.items() if item is not None}}


def _answer(ref: str, title: str, value: Decimal | str | None, formula: str, parts: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    total = _ZERO
    for part in parts:
        if part.get("sign") in ("+", "-") and part.get("value") is not None:
            total += _num(part["value"]) * (1 if part["sign"] == "+" else -1)
    number = _num(value) if value is not None else None
    return {
        "version": VERSION,
        "ref": ref,
        "title": title,
        "value": value if isinstance(value, str) or value is None else _money(value),
        "formula": formula,
        "parts": parts,
        "check": _money(total),
        "exact": number is not None and abs(total - number) < _CENT,
        **extra,
    }


def _label(contract: Contract) -> str:
    number = (contract.number or "").strip()
    if not number:
        return "договор без номера"
    return number if number.startswith("№") else f"№ {number}"


def _contract(ctx: Ctx, raw: Any) -> tuple[Contract, Registry]:
    try:
        contract_id = uuid.UUID(str(raw))
    except ValueError as exc:
        raise FinanceError("В ссылке нет договора") from exc
    contract = get_contract(ctx.session, ctx.workspace, contract_id)
    registry = Registry(ctx.session, ctx.workspace)
    if not visible_to(contract, registry, ctx.access, people_of(ctx.session, [contract.id]).get(contract.id, [])):
        raise NotFound("Договор не найден")
    return contract, registry


def _day(raw: Any) -> date:
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise FinanceError("В ссылке нет дня") from exc


def _facts(ctx: Ctx, keys: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Факты журнала по ключам - действующие версии, подписанные именами."""
    wanted = sorted({key for key in keys if key})
    if not wanted:
        return {}
    rows = list(
        ctx.session.scalars(
            sa.select(Fact).where(
                Fact.workspace_id == ctx.workspace.id, Fact.key.in_(wanted), Fact.ended_seq.is_(None), Fact.void.is_(False)
            )
        )
    )
    labels = Labels(ctx.session, ctx.workspace, rows)
    return {row.key: labels.fact(row) for row in rows}


def _fraction(raw: Any) -> Decimal | None:
    """Доля из ссылки: `0.35` (часть) или `35.00` (проценты долга группы)."""
    if raw in (None, ""):
        return None
    return Decimal(str(raw))


def explain(ctx: Ctx, ref: str) -> dict[str, Any]:
    data = refs.read(ref)
    kind = data["k"]
    handler = HANDLERS.get(kind)
    if handler is None:
        raise FinanceError("Это число не раскрывается - ссылка не из ответа «спросить»")
    return handler(ctx, ref, data)


# ── Договор: сумма, оплачено, остаток ────────────────────────────────────────


def _amount(ctx: Ctx, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    if "amount" in ctx.access.hidden:
        raise PermissionError("Сумма договора вам не открыта")
    contract, _registry = _contract(ctx, data.get("c"))
    whole = contract.amount
    terms = {
        "kind": "terms",
        "contract_id": str(contract.id),
        "field": "amount",
        "value": _money(whole),
        "source": (contract.provenance or {}).get("amount") if isinstance(contract.provenance, dict) else None,
    }
    fraction = _fraction(data.get("f"))
    if fraction is None:
        return _answer(ref, f"Сумма {_label(contract)}", whole, "условие договора", [_part("сумма договора", whole, fact=terms)])
    value = whole * fraction if whole is not None else None
    return _answer(
        ref, f"Сумма {_label(contract)} - доля вида", value, f"сумма договора × {_percent(fraction)}",
        [_part(f"сумма договора × {_percent(fraction)}", value, fact=terms, whole=_money(whole))],
    )


def _percent(fraction: Decimal) -> str:
    text = format((fraction * 100).quantize(_CENT), "f").rstrip("0").rstrip(".")
    return f"{text}%"


def _paid_parts(ctx: Ctx, contract: Contract, registry: Registry, day: date) -> tuple[Decimal | None, list[dict[str, Any]], str]:
    """Оплачено договора на день - число, слагаемые и откуда (`kort` / `table`)."""
    from app.finance.contracts.service import today
    from app.finance.core.payments import Money

    if day != today():
        registry.paid_on = day
    if registry.summary is None and registry.summary_connected:
        from app.finance.contracts import summary as summary_module

        slot = summary_module.get(ctx.session, ctx.workspace.id)
        if slot is not None and slot.index is not None:
            registry._summary = slot.index
    match = registry.summary_of(contract)
    if match is None or match.state != "found":
        return None, [], ""
    if match.source == "kort":
        item = Money(ctx.session, ctx.workspace, today=day, only=[contract.id]).contract(contract)
        facts = _facts(ctx, [payment.key for payment in item.payments])
        parts = [
            _part(f"оплата {payment.paid_at:%d.%m.%Y}", payment.amount, fact=facts.get(payment.key) or {"key": payment.key, "source": payment.source})
            for payment in sorted(item.payments, key=lambda payment: (payment.paid_at, payment.key))
        ]
        return match.paid, parts, "kort"
    rows = registry.summary.rows_of(contract.number, _names(registry, contract), _own_code(registry, contract)) if registry.summary else []
    parts = [
        _part(
            f"сводная таблица, {row.month or 'без месяца'}", row.paid,
            fact={"kind": "table_row", "own": row.own, "customer": row.customer, "number": row.number, "month": row.month, "paid": _money(row.paid)},
        )
        for row in rows
    ]
    return match.paid, parts, "table"


def _names(registry: Registry, contract: Contract) -> list[str]:
    ours, theirs = contract.executor_id, contract.customer_id
    if not registry.is_own(ours) and registry.is_own(theirs):
        ours, theirs = theirs, ours
    party = registry.parties_for([theirs]).get(theirs) if theirs else None
    return ([party.name] if party is not None else []) + registry.aliases(theirs)


def _own_code(registry: Registry, contract: Contract) -> str:
    ours = contract.executor_id if registry.is_own(contract.executor_id) else contract.customer_id
    entity = registry.own.get(ours) if ours else None
    return entity.code if entity is not None else ""


def _paid(ctx: Ctx, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    if {"amount", "summary_paid"} & ctx.access.hidden:
        raise PermissionError("Оплачено вам не открыто")
    contract, registry = _contract(ctx, data.get("c"))
    day = _day(data.get("d"))
    paid, parts, source = _paid_parts(ctx, contract, registry, day)
    formula = "оплаты KORT" if source == "kort" else "строки договора в сводной таблице" if source == "table" else "оплат не найдено"
    fraction = _fraction(data.get("f"))
    if fraction is None or paid is None:
        return _answer(ref, f"Оплачено {_label(contract)}", paid, formula, parts, source=source)
    value = paid * fraction
    return _answer(
        ref, f"Оплачено {_label(contract)} - доля вида", value, f"({formula}) × {_percent(fraction)}",
        [_part(f"оплачено × {_percent(fraction)}", value, parts=parts, whole=_money(paid))], source=source,
    )


def _left(ctx: Ctx, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    if {"amount", "summary_paid"} & ctx.access.hidden:
        raise PermissionError("Остаток вам не открыт")
    contract, registry = _contract(ctx, data.get("c"))
    day = _day(data.get("d"))
    paid, _parts, _source = _paid_parts(ctx, contract, registry, day)
    fraction = _fraction(data.get("f")) or Decimal(1)
    amount = contract.amount * fraction if contract.amount is not None else None
    paid_part = paid * fraction if paid is not None else None
    value = amount - paid_part if amount is not None and paid_part is not None else None
    link = {"c": data.get("c"), "f": data.get("f")}
    return _answer(
        ref, f"Остаток {_label(contract)}", value, "сумма договора - оплачено",
        [
            _part("сумма договора", amount, ref=refs.make("amount", **link)),
            _part("оплачено", paid_part, "-", ref=refs.make("paid", d=data.get("d"), **link)),
        ],
    )


def _formula(ctx: Ctx, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    """Формула компании у договора: её меры с их ссылками и то, что из них вышло (`core/formulas.py`)."""
    from app.finance.contracts.service import age_months
    from app.finance.core import formulas

    found = next((item for item in formulas.of(ctx.session, ctx.workspace.id) if item.key == data.get("m")), None)
    if found is None:
        raise FinanceError("Такой формулы у компании больше нет")
    contract, registry = _contract(ctx, data.get("c"))
    day = _day(data.get("d"))
    fraction = _fraction(data.get("f")) or Decimal(1)
    hidden = ctx.access.hidden
    amount = contract.amount * fraction if contract.amount is not None and "amount" not in hidden else None
    paid = None
    if not {"amount", "summary_paid"} & hidden:
        whole, _parts, _source = _paid_parts(ctx, contract, registry, day)
        paid = whole * fraction if whole is not None else None
    remaining = amount - paid if amount is not None and paid is not None else None
    values = {"amount": amount, "paid": paid, "remaining": remaining, "age": Decimal(age_months(contract.signed_at, day) or 0)}
    link = {"c": data.get("c"), "f": data.get("f")}
    ref_of = {
        "amount": refs.make("amount", **link),
        "paid": refs.make("paid", d=data.get("d"), **link),
        "remaining": refs.make("left", d=data.get("d"), **link),
    }
    used = formulas.names(found.text)
    parts = [
        {"title": formulas.VARIABLES[name][1], "name": name,
         "value": _money(values[name]) if name != "age" else format(values[name], "f"),
         "sign": "=", **({"ref": ref_of[name]} if name in ref_of and values[name] is not None else {})}
        for name in formulas.VARIABLES if name in used
    ]
    value = formulas.shown(found, formulas.evaluate(found.text, values))
    words = found.text
    for name, (_key, title) in formulas.VARIABLES.items():
        words = re.sub(rf"\b{name}\b", title, words)
    return {
        "version": VERSION, "ref": ref, "title": f"{found.title} {_label(contract)}", "value": value, "formula": words,
        "parts": parts, "check": value, "exact": value is not None, "type": found.type,
    }


# ── Вид договоров: части листа, сотрудники, возраст, итог колонки ────────────


def _asked(ctx: Ctx, data: dict[str, Any]) -> tuple[Any, dict[str, Any], date]:
    from app.finance.model import ask as ask_module

    question = dict(data.get("q") or {})
    question["as_of"] = data.get("d")
    return ask_module.question_of(ctx, question)


def _block(ctx: Ctx, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    from app.finance.core import views as views_module

    view, _asked_q, day = _asked(ctx, data)
    found = views_module.ask(ctx.session, ctx.workspace, ctx.access, view, day=day)
    sheet_key, block = data.get("s"), int(data.get("b") or 0)
    sheet = next((item for item in found["sheets"] if item["key"] == sheet_key), None)
    if sheet is None or block >= len(sheet["blocks"]):
        raise FinanceError("Этой части листа больше нет")
    parts = []
    for row in found["rows"]:
        if not any(place["view"] == sheet_key and place["block"] == block for place in row["places"]):
            continue
        left = row["measures"].get("summary_remaining")
        if left is None:
            continue
        parts.append(
            _part(
                " · ".join(item for item in (row["number"] or "без номера", row.get("client", "")) if item), left,
                ref=refs.make("left", c=row["id"], d=day.isoformat(), f=row.get("fraction")),
            )
        )
    title = sheet["blocks"][block]["title"] or sheet["title"]
    value = sheet["blocks"][block].get("remaining")
    return _answer(ref, f"Остаток - {title}", value, "сумма остатков договоров части", parts, contracts=len(parts))


def _person(ctx: Ctx, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    from app.finance.model import ask as ask_module

    view, asked, day = _asked(ctx, data)
    members: dict[str, list[dict[str, Any]]] = {}
    found = ask_module.contracts(ctx, view, asked, day, members=members)
    name, measure = data.get("n"), data.get("m")
    people = found["groups"].get("people") or {"rows": []}
    row = next((item for item in people["rows"] if item["name"] == name), None)
    if row is None:
        raise FinanceError("Такой строки «По сотрудникам» больше нет")
    parts = []
    on = day.isoformat()
    for item in members.get(name, []):
        if measure == "open_amount" and not item["open"]:
            continue
        value = item["remaining"] if measure == "remaining" else item["amount"]
        parts.append(
            _part(
                item["number"] or "без номера", value,
                ref=refs.make("left" if measure == "remaining" else "amount", c=item["id"], d=on if measure == "remaining" else None),
            )
        )
    words = {"amount": "Сумма", "remaining": "Остаток", "open_amount": "В работе"}
    return _answer(ref, f"{words.get(measure, measure)} - {name}", row.get(measure), "договоры человека: своя доля или договор целиком", parts)


def _people(ctx: Ctx, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    from app.finance.model import ask as ask_module

    view, asked, day = _asked(ctx, data)
    members: dict[str, list[dict[str, Any]]] = {}
    found = ask_module.contracts(ctx, view, asked, day, members=members)
    measure = data.get("m")
    totals = (found["groups"].get("people") or {}).get("totals") or {}
    seen: dict[str, dict[str, Any]] = {}
    # Договор с двумя ответственными - в строке каждого, в итоге один раз (`staff.ts`).
    for items in members.values():
        for item in items:
            if measure == "open_amount" and not item["open"]:
                continue
            seen.setdefault(item["id"], item)
    by_id = {row["id"]: row for row in found["rows"]}
    parts = []
    for contract_id, item in seen.items():
        row = by_id.get(contract_id, {})
        if measure == "remaining":
            value = row.get("values", {}).get("summary_remaining")
            if value is None:
                value = _whole_left(ctx, view, day, contract_id)
        else:
            # «Сумма» и «В работе» - сумма договора вида (доля отдела-владельца), один раз.
            value = row.get("values", {}).get("amount")
        parts.append(_part(item["number"] or "без номера", value, ref=refs.make("left" if measure == "remaining" else "amount", c=contract_id, d=day.isoformat() if measure == "remaining" else None)))
    return _answer(ref, "По сотрудникам - итог", totals.get(measure), "каждый договор один раз", parts)


def _whole_left(ctx: Ctx, view: Any, day: date, contract_id: str) -> str | None:
    from app.finance.core import views as views_module

    found = views_module.ask(ctx.session, ctx.workspace, ctx.access, view, day=day)
    row = next((item for item in found["rows"] if item["id"] == contract_id), None)
    return row["measures"].get("summary_remaining") if row else None


def _age(ctx: Ctx, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    from app.finance.core import views as views_module

    view, _asked_q, day = _asked(ctx, data)
    found = views_module.ask(ctx.session, ctx.workspace, ctx.access, view, day=day)
    title, measure = data.get("t"), data.get("m")
    bucket = next((item for item in views_module.AGE_BUCKETS if item[0] == title), None)
    group = next((item for item in found["groups"].get("age") or [] if item["title"] == title), None)
    if bucket is None or group is None:
        raise FinanceError("Такой строки «По возрасту» больше нет")
    _title, low, high = bucket
    parts = []
    for row in found["rows"]:
        if row.get("phase") in ("fulfilled", "terminated", "failed"):
            continue
        months = row["measures"].get("age_months")
        if months is None or months < low or (high is not None and months >= high):
            continue
        value = row["measures"].get("summary_remaining") if measure == "remaining" else row["measures"].get("amount")
        if measure == "remaining" and value is None:
            continue
        parts.append(_part(row["number"] or "без номера", value, ref=refs.make("left" if measure == "remaining" else "amount", c=row["id"], d=day.isoformat() if measure == "remaining" else None, f=row.get("fraction"))))
    return _answer(ref, f"{title}", group.get(measure), "договоры в работе этого срока", parts)


def _sum(ctx: Ctx, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    from app.finance.model import ask as ask_module

    view, asked, day = _asked(ctx, data)
    found = ask_module.contracts(ctx, view, asked, day)
    measure = data.get("m")
    parts = [
        _part(" · ".join(item for item in (row["number"] or "без номера", row.get("client", "")) if item), row["values"].get(measure), ref=row["refs"].get(measure))
        for row in found["rows"]
        if row["values"].get(measure) is not None
    ]
    return _answer(ref, f"Итог - {view.title}", found["totals"].get(measure), "сумма по договорам вида", parts)


# ── Долги ────────────────────────────────────────────────────────────────────


def _debt(ctx: Ctx, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    from app.finance.model.ask import debts_access

    debts_access(ctx)
    contract, _registry = _contract(ctx, data.get("c"))
    day = _day(data.get("d"))
    basis, measure = data.get("b") or "accruals", data.get("m") or "debt"
    fraction = _fraction(data.get("f"))
    if fraction is not None:
        # Доля отдела или человека: долг договора целиком × доля (проценты).
        whole_ref = refs.make("debt", **{key: value for key, value in data.items() if key not in ("k", "f")})
        whole = explain(ctx, whole_ref)
        part = fraction / 100
        value = _num(whole["value"]) * part if whole["value"] is not None else None
        return _answer(
            ref, f"Долг {_label(contract)} - доля {_percent(part)}", value, f"долг договора × {_percent(part)}",
            [_part(f"долг договора целиком × {_percent(part)}", value, ref=whole_ref, whole=whole["value"])],
        )
    if basis == "acts":
        return _debt_acts(ctx, ref, contract, day, measure)
    return _debt_accruals(ctx, ref, contract, day, measure, owner=data.get("o"))


def _debt_accruals(ctx: Ctx, ref: str, contract: Contract, day: date, measure: str, *, owner: Any = None) -> dict[str, Any]:
    from app.finance.core import calls
    from app.finance.core.payments import OPENING, Money

    found = calls.explain(ctx.session, ctx.workspace, ctx.access, contract, day=day)
    if not found.get("known"):
        return _answer(ref, f"Долг {_label(contract)}", None, "KORT начислений по договору не считает", [], known=False)
    fact_keys = [found["opening"]["fact"]] + [payment["fact"] for payment in found["payments"]]
    for accrual in found["accruals"]:
        fact_keys += accrual["agreements"] + ([accrual["adjustment"]] if accrual["adjustment"] else [])
    facts = _facts(ctx, fact_keys)

    def accrual_part(accrual: dict[str, Any], sign: str = "+") -> dict[str, Any]:
        sources = [facts[key] for key in [*accrual["agreements"], accrual["adjustment"]] if key and key in facts]
        return _part(
            f"начислено за {accrual['month']}", accrual["amount"], sign,
            fact={"kind": "accrual", "contract_id": str(contract.id), "month": accrual["month"], "base": accrual["base"], "from": sources},
        )

    if measure == "debt" and owner is None:
        parts = []
        if found["opening"]["amount"] is not None:
            parts.append(_part("входящее сальдо", found["opening"]["amount"], fact=facts.get(found["opening"]["fact"] or "")))
        parts += [accrual_part(accrual) for accrual in found["accruals"]]
        parts += [
            _part(f"оплата {date.fromisoformat(payment['paid_at']):%d.%m.%Y}", payment["amount"], "-", fact=facts.get(payment["fact"]) or {"key": payment["fact"], "source": payment["source"]})
            for payment in found["payments"]
        ]
        return _answer(ref, f"Долг {_label(contract)} на {day:%d.%m.%Y}", found["debt"], "входящее сальдо + начислено - оплачено", parts)

    # Неоплаченное: месяцы с остатком после разнесения оплат; у юрлица месяца - только его месяцы.
    item = Money(ctx.session, ctx.workspace, today=day, only=[contract.id]).contract(contract)
    current = contract.executor_id
    wanted = None if owner in (None, "", "none") else uuid.UUID(str(owner))
    by_month = {accrual["month"]: accrual for accrual in found["accruals"]}
    parts = []
    advance = _ZERO
    owed = _ZERO
    for key, bucket in sorted(item.buckets.items()):
        if key != OPENING and bucket.starts > day:
            advance += sum((part.amount for part in bucket.parts if part.payment.source != "opening"), _ZERO)
            continue
        left = bucket.left
        executor = bucket.executor or current
        if left < 0:
            advance += -left
            continue
        if left == 0 or (wanted is not None and executor != wanted):
            continue
        owed += left
        inner = []
        if key == OPENING:
            inner.append(_part("входящее сальдо", bucket.accrued, fact=facts.get(found["opening"]["fact"] or "")))
        elif key in by_month:
            inner.append(accrual_part(by_month[key]))
        inner += [
            _part(f"оплата {part.payment.paid_at:%d.%m.%Y} в счёт месяца", part.amount, "-", fact=facts.get(part.payment.key) or {"key": part.payment.key})
            for part in bucket.parts
        ]
        parts.append(_part("до учёта" if key == OPENING else f"неоплачено за {key}", left, parts=inner))
    advance += sum((part.amount for part in item.over if part.payment.source != "opening"), _ZERO)
    if measure == "debt" and (wanted is None or wanted == current) and advance:
        parts.append(_part("аванс и переплата", advance, "-"))
    value = owed - (advance if measure == "debt" and (wanted is None or wanted == current) else _ZERO)
    formula = "неоплаченные месяцы" + (" - аванс" if measure == "debt" else "")
    return _answer(ref, f"{'Неоплачено' if measure == 'owed' else 'Долг'} {_label(contract)} на {day:%d.%m.%Y}", value, formula, parts)


def _debt_acts(ctx: Ctx, ref: str, contract: Contract, day: date, measure: str) -> dict[str, Any]:
    from app.finance.core import book as book_module
    from app.finance.core.accruals import month_key, month_start
    from app.finance.core.acts import Acts
    from app.finance.core.debts import _chosen
    from app.finance.core.payments import Money
    from app.finance.core.registers import totals

    book, _error = book_module.current(ctx.session, ctx.workspace.id, wait=book_module.FIRST_READ_WAIT)
    found = totals(ctx.session, ctx.workspace, [contract], day=day)
    settled = found.settlements.get(contract.id)
    if settled is None or settled.since is None:
        return _answer(ref, f"Долг по АВР {_label(contract)}", None, "учёт договора не начат", [], known=False)
    acts = Acts(ctx.session, ctx.workspace, ctx.access, book, today=day, only=contract)
    opening = journal.openings(ctx.session, ctx.workspace.id, settled.since, "acts").get(contract.id)
    first = month_key(settled.since)
    dated = sorted(
        (act for act in _chosen(acts, contract.id) if act.amount is not None and act.month >= first and (act.day or month_start(act.month)) <= day),
        key=lambda act: act.day or month_start(act.month),
    )
    payments = Money(ctx.session, ctx.workspace, today=day, only=[contract.id]).contract(contract).payments
    facts = _facts(ctx, [payment.key for payment in payments] + ([opening.key] if opening is not None else []))

    def act_fact(act: Any) -> dict[str, Any]:
        return {"kind": "act", "source": act.source, "month": act.month, "number": act.number,
                "day": act.day.isoformat() if act.day else None, "amount": _money(act.amount), "ref": act.ref}

    if measure == "debt":
        parts = []
        if opening is not None:
            parts.append(_part("входящее сальдо по АВР", opening.amount, fact=facts.get(opening.key)))
        parts += [_part(f"АВР {act.number or act.month}", act.amount, fact=act_fact(act)) for act in dated]
        parts += [
            _part(f"оплата {payment.paid_at:%d.%m.%Y}", payment.amount, "-", fact=facts.get(payment.key) or {"key": payment.key})
            for payment in sorted(payments, key=lambda payment: (payment.paid_at, payment.key))
        ]
        value = (opening.amount if opening is not None else _ZERO) + sum((act.amount for act in dated), _ZERO) - settled.paid
        return _answer(ref, f"Долг по АВР {_label(contract)} на {day:%d.%m.%Y}", value, "сальдо по АВР + акты - оплачено", parts)
    credit = settled.paid + (-opening.amount if opening is not None and opening.amount < 0 else _ZERO)
    owed: list[tuple[str, Decimal, Any]] = [("до учёта", opening.amount, None)] if opening is not None and opening.amount > 0 else []
    owed += [(f"АВР {act.number or act.month}", act.amount, act) for act in dated]
    parts = []
    total = _ZERO
    for title, amount, act in owed:
        taken = min(credit, amount) if credit > 0 else _ZERO
        credit -= taken
        if amount - taken > 0:
            total += amount - taken
            parts.append(_part(f"{title} - не оплачено", amount - taken, fact=act_fact(act) if act is not None else facts.get(opening.key) if opening is not None else None))
    return _answer(ref, f"Неоплачено по АВР {_label(contract)} на {day:%d.%m.%Y}", total, "акты, не покрытые оплатами по порядку дат", parts)


def _dgroup(ctx: Ctx, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    from app.finance.model import ask as ask_module

    view, asked, day = _asked(ctx, data)
    found = ask_module.debts(ctx, view, asked, day)
    measure = data.get("m") or "owed"
    group = next((item for item in found["rows"] if item["key"] == data.get("g")), None)
    if group is None:
        raise FinanceError("Такой группы в «Долгах» больше нет")
    parts = [
        _part(" · ".join(item for item in (row["number"] or "без номера", row.get("party", "")) if item), row.get(measure), ref=row["refs"].get(measure))
        for row in group["contracts"]
        if row.get(measure) is not None
    ]
    words = {"owed": "Неоплачено", "debt": "Долг"}
    return _answer(ref, f"{words.get(measure, measure)} - {group['title']}", group.get(measure), "договоры группы", parts)


def _dtotal(ctx: Ctx, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    from app.finance.model import ask as ask_module

    view, asked, day = _asked(ctx, data)
    found = ask_module.debts(ctx, view, asked, day)
    measure = data.get("m") or "owed"
    parts = [_part(group["title"], group.get(measure), ref=group["refs"].get(measure)) for group in found["rows"] if group.get(measure) is not None]
    words = {"owed": "Неоплачено", "debt": "Долг"}
    return _answer(ref, f"{words.get(measure, measure)} - всего", found["totals"].get(measure), "по группам", parts)


# ── Счета ────────────────────────────────────────────────────────────────────


def _bill(ctx: Ctx, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    from app.finance.model.ask import unbilled_board

    if not ctx.can("invoices"):
        raise PermissionError("Счета вам не открыты")
    board, _error, _origin, _source = unbilled_board(ctx)
    bill = board.find(str(data.get("id"))) if board is not None else None
    if bill is None:
        raise FinanceError("Такого счёта в таблице больше нет")
    fact = {
        "kind": "accrual" if bill.source == "kort" else "table_row",
        "line": bill.line, "own": bill.own, "customer": bill.customer, "number": bill.number, "month": bill.month,
        "period_from": bill.period_from.isoformat() if bill.period_from else None, "amount": _money(bill.amount),
        "text": bill.text, "contract_id": bill.contract_id or None,
    }
    formula = "начисление договора за месяц (KORT)" if bill.source == "kort" else f"строка {bill.line} сводной таблицы"
    return _answer(ref, f"Счёт {bill.customer} за {bill.month}", bill.amount, formula, [_part(bill.month or "месяц", bill.amount, fact=fact)])


HANDLERS = {
    "formula": _formula,
    "amount": _amount,
    "paid": _paid,
    "left": _left,
    "block": _block,
    "person": _person,
    "people": _people,
    "age": _age,
    "sum": _sum,
    "debt": _debt,
    "dgroup": _dgroup,
    "dtotal": _dtotal,
    "bill": _bill,
}


__all__ = ["HANDLERS", "explain"]
