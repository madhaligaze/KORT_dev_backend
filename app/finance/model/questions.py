"""Решить, под угрозой, пришло - три ответа главного экрана (этап 3, шаг 2).

Ядро не угадывает: где данных не хватает, оно спрашивает человека. До этапа 3
эти вопросы жили в экранах - «Оплаты без договора», предложения БИН и
двойники «Участников», «Сверка», - и новая оболочка собирала бы их сама, а
каждая форма по-своему. Здесь они - источники вызова «спросить», одной формой:

* `questions` - что ждёт решения человека. У вопроса варианты, у варианта -
  пачка команд «применить» с готовыми параметрами (`commands`), чего не
  хватает от человека (`needs`), открыта ли она спрашивающему (`open`), и
  какой вариант KORT предлагает сам (`suggested`). Ответ - обычная пачка
  «предложить» / «применить» с «Вернуть»: вопрос уходит из списка, потому
  что ушла его причина в данных, а не потому что его пометили.
* `attention` - что под угрозой: разрыв денег, порог НДС, долги, счета к
  выставлению, АВР и ЭСФ. У пункта числа и `open` - вопрос «спросить»
  (`source`, `params`), который раскрывает его списком, или место, у которого
  источника пока нет (`place`).
* `arrived` - что пришло: факты журнала, впервые записанные с дня `since`.
* `documents` - дела по АВР и ЭСФ списком: не подписан, без ЭСФ после срока,
  месяц начислен - АВР нет.

Вопросы файла (порядок частей даты, скрытая строка реестра, сумма с даты)
остаются в загрузке: без ответа файл не положить, и команда спрашивает
отказом `question` (`model/entry.Question`).

Каждый вид вопросов и каждый пункт угроз считается отдельно (`params.kind`,
`params.item`): сверка читает всю сводную таблицу, и медленный пункт не
должен держать быстрые - оболочка спрашивает их порознь.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

import sqlalchemy as sa

from app.finance.contracts.service import today
from app.finance.model import VERSION
from app.finance.model.commands import COMMANDS, open_to
from app.finance.model.context import Ctx
from app.finance.service import FinanceError

QUESTION_KINDS = ("payments", "bins", "twins", "reconcile")
#: Сколько вопросов одного вида отдавать: остальные - числом (`kinds.<вид>.count`) и списком в своём разделе.
#: Копия прода: тысячи старых поступлений без договора - очередь в тысячу строк не читается.
LIMIT = 30
ATTENTION_ITEMS = ("cash", "vat", "debts", "unbilled", "acts")
DOCUMENT_VIEWS = ("missing", "unsigned", "no_esf")

#: Почему договор - кандидат оплаты, словами (`core/payments.WHY_ORDER`).
WHY_TITLES = {
    "спор": "номер и плательщик спорят",
    "за другого": "платит не сторона договора",
    "возврат": "возврат по договору",
    "номер": "номер в назначении",
    "клиент": "тот же клиент",
    "сумма": "та же сумма",
}
FACT_TITLES = {
    "payment": "оплаты", "bill": "счета", "act": "АВР", "esf": "ЭСФ", "agreement": "соглашения",
    "adjustment": "поправки месяца", "opening": "входящее сальдо", "decision": "решения по оплатам",
}
SOURCE_TITLES = {"kort": "KORT", "journal": "выписки", "1c": "1С", "book": "таблица"}
#: Сколько последних фактов отдавать в «пришло» списком.
LATEST = 30
#: Что «приходит»: события источников и людей; сальдо и решения по оплатам - нет.
ARRIVED_KINDS = ("payment", "bill", "act", "esf", "agreement", "adjustment")


def _money(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")


def _words(value: Any) -> str:
    """«200 000», «1 500,50» - сумма в тексте вопроса."""
    number = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    return f"{number:,.2f}".replace(",", " ").replace(".00", "").replace(".", ",")


def _day(raw: Any, default: date) -> date:
    if raw in (None, ""):
        return default
    try:
        return raw if isinstance(raw, date) else date.fromisoformat(str(raw))
    except ValueError as exc:
        raise FinanceError("День - ГГГГ-ММ-ДД") from exc


def _option(ctx: Ctx, key: str, title: str, commands: list[tuple[str, dict[str, Any]]], *, hint: str = "",
            needs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Вариант ответа: пачка команд «применить». `needs` - чего не хватает от человека: оно дописывается
    в параметры каждой команды пачки (договор для «другой договор»)."""
    return {
        "key": key,
        "title": title,
        "hint": hint,
        "commands": [{"command": name, "params": params} for name, params in commands],
        "needs": needs or [],
        "open": all(open_to(ctx, COMMANDS[name]) for name, _params in commands),
    }


def _contract_title(candidate: dict[str, Any]) -> str:
    number = (candidate.get("number") or "").strip() or "договор без номера"
    customer = (candidate.get("customer") or "").strip()
    return f"{number} · {customer}" if customer else number


# ── Решить ───────────────────────────────────────────────────────────────────


def _payments(ctx: Ctx, day: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Оплаты без договора (и возвраты клиентам, к которым KORT предлагает договор) - то же, что экран оплат.

    Оплаты одного плательщика «за другого» с теми же договорами - один вопрос:
    ответ на первую ложится прецедентом и на остальные (полигон, У7), и
    спрашивать пять раз о том, на что человек отвечает один раз, - мучить его.
    """
    from app.finance.core.payments import Money

    if not ctx.access.setup:
        return [], {}
    money = Money(ctx.session, ctx.workspace, today=day)
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for item in money.without_contract():
        candidates = item.get("candidates") or []
        if item.get("direction") != "out" and any(candidate.get("why") == "за другого" for candidate in candidates):
            key = ("third", item.get("payer") or "", *sorted(candidate["id"] for candidate in candidates))
        else:
            key = ("one", item["operation_id"])
        groups.setdefault(key, []).append(item)
    rows = [_payment_row(ctx, items) for items in groups.values()]
    return rows, {"hidden": dict(money.hidden)}


def _payment_row(ctx: Ctx, items: list[dict[str, Any]]) -> dict[str, Any]:
    first = items[0]
    operations = [item["operation_id"] for item in items]
    candidates = first.get("candidates") or []
    refund = first.get("direction") == "out"

    def each(params: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        return [("payment.decide", {"operation": operation, **params}) for operation in operations]

    options: list[dict[str, Any]] = []
    suggested = None
    if first.get("split") and len(items) == 1:
        parts = [{"contract": part["id"], "amount": part["amount"]} for part in first["split"]]
        words = " · ".join(f"{part['number'] or 'без номера'} {_words(part['amount'])}" for part in first["split"])
        options.append(_option(ctx, "split", f"Разделить, как в 1С: {words}",
                               [("payment.split", {"operation": first["operation_id"], "parts": parts})], hint="так разделил бухгалтер в 1С"))
        suggested = "split"
    for candidate in candidates:
        title = f"Возврат по {_contract_title(candidate)}" if refund else f"К {_contract_title(candidate)}"
        options.append(_option(ctx, f"link:{candidate['id']}", title, each({"action": "link", "contract": candidate["id"]}),
                               hint=WHY_TITLES.get(candidate.get("why") or "", "")))
        if refund and suggested is None:
            suggested = f"link:{candidate['id']}"
    options.append(_option(ctx, "pick", "Другой договор", each({"action": "link"}),
                           needs=[{"param": "contract", "type": "contract", "title": "Договор"}]))
    options.append(_option(ctx, "none", "Не возврат" if refund else "Не по договору", each({"action": "none"})))
    whys = {candidate.get("why") for candidate in candidates}
    if refund:
        reason = "списание похоже на возврат клиенту - долг по договору вырастет обратно"
    elif "за другого" in whys:
        reason = "платит не сторона договора: ответ ляжет и на следующие оплаты этого плательщика"
    elif first.get("disputed"):
        reason = "номер в назначении и плательщик указывают на разные договоры"
    elif candidates:
        reason = "договор не найден однозначно - есть похожие"
    else:
        reason = "договор не найден ни по номеру, ни по клиенту, ни по сумме"
    payer = first.get("payer") or "плательщик не указан"
    total = sum((Decimal(str(item["amount"])) for item in items), Decimal("0"))
    purpose = f" - «{first['comment']}»" if first.get("comment") else ""
    if len(items) == 1:
        text = f"{payer}: {_words(total)}{purpose}"
    else:
        text = f"{payer}: {len(items)} оплат на {_words(total)}{purpose}"
    return {
        "key": f"payment:{min(operations)}",
        "kind": "payment",
        "title": "Возврат клиенту?" if refund else "Оплата без договора",
        "text": text,
        "reason": reason,
        "day": max(item.get("paid_at") or "" for item in items) or None,
        "amount": _money(total),
        "direction": first.get("direction"),
        "party": first.get("payer") or "",
        "account": first.get("account") or "",
        "count": len(items),
        "objects": [
            {"type": "operation", "id": operation, "day": item.get("paid_at"), "amount": item.get("amount"), "comment": item.get("comment") or ""}
            for operation, item in zip(operations, items)
        ]
        + [{"type": "contract", "id": candidate["id"], "title": _contract_title(candidate)} for candidate in candidates],
        "options": options,
        "suggested": suggested,
    }


def _bins(ctx: Ctx, day: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """БИН, найденный в 1С, выписках и таблице у участника без БИН."""
    from app.finance.core import book as book_module
    from app.finance.core import participants

    if not ctx.access.view:
        return [], {}
    book, _error = book_module.current(ctx.session, ctx.workspace.id, wait=0)
    rows: list[dict[str, Any]] = []
    for item in participants.bin_proposals(ctx.session, ctx.workspace, ctx.access, book):
        party = item["party_id"]
        free = [number for number in item["numbers"] if not number.get("taken_by")]
        options = [
            _option(ctx, f"bin:{number['bin']}", f"Записать {number['bin']}", [("participant.update", {"id": party, "bin": number["bin"]})],
                    hint=", ".join(number.get("evidence") or []))
            for number in free
        ]
        numbers = [number["bin"] for number in item["numbers"]]
        options.append(_option(ctx, "reject", "Не его номер" if len(numbers) == 1 else "Ни один не его",
                               [("participant.bin_reject", {"id": party, "bins": numbers})]))
        taken = [f"{number['bin']} уже у «{number['taken_by']['name']}»" for number in item["numbers"] if number.get("taken_by")]
        rows.append({
            "key": f"bin:{party}",
            "kind": "participant_bin",
            "title": "БИН участника",
            "text": f"«{item['name']}»: в источниках " + ", ".join(numbers),
            "reason": "; ".join(taken) if taken else ("номер один и ни у кого не записан" if item.get("clear") else "номеров несколько - выбрать"),
            "day": None,
            "amount": None,
            "direction": None,
            "party": item["name"],
            "objects": [{"type": "participant", "id": party, "title": item["name"]}],
            "options": options,
            "suggested": f"bin:{free[0]['bin']}" if item.get("clear") and free else None,
        })
    return rows, {}


def _twins(ctx: Ctx, day: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Похоже на одного участника: тот же БИН или то же имя без формы и кавычек."""
    from app.finance.core import participants

    if not ctx.access.view:
        return [], {}
    rows: list[dict[str, Any]] = []
    for group in participants.duplicates(ctx.session, ctx.workspace, ctx.access):
        members = group["members"]
        keep = next((item for item in members if item["id"] == group["keep"]), members[0])
        drop = [item["id"] for item in members if item["id"] != keep["id"]]
        names = " и ".join(f"«{item['name']}»" for item in members)
        options = [
            _option(ctx, "merge", f"Свести в «{keep['name']}»", [("participant.merge", {"keep": keep["id"], "drop": drop})],
                    hint="договоры, оплаты и написания переедут"),
            _option(ctx, "distinct", "Это разные", [("participant.distinct", {"ids": [item["id"] for item in members]})]),
        ]
        rows.append({
            "key": f"twins:{group['key']}",
            "kind": "participant_twins",
            "title": "Похоже на одного участника",
            "text": names,
            "reason": ", ".join(group.get("why") or []),
            "day": None,
            "amount": None,
            "direction": None,
            "party": keep["name"],
            "objects": [{"type": "participant", "id": item["id"], "title": item["name"]} for item in members],
            "options": options,
            "suggested": "merge" if any("БИН" in why for why in group.get("why") or []) else None,
        })
    return rows, {}


def _reconcile(ctx: Ctx, day: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Сверка с подключённой сводной таблицей: сколько дел ждёт человека - одним вопросом с переходом в сверку."""
    from app.finance.contracts.summary import source_of
    from app.finance.core import today as today_module

    if not ctx.access.setup or source_of(ctx.session, ctx.workspace.id) is None:
        return [], {}
    card = today_module.shadow_card(ctx.session, ctx.workspace, ctx.access, today=day)
    if not card.get("count"):
        return [], {"empty": card.get("empty") or ""}
    lines = [line for line in card.get("lines") or [] if line.get("count") and line.get("label") != "договоров совпадает с таблицей"]
    return [{
        "key": "reconcile",
        "kind": "reconcile",
        "title": "Сверка с таблицей",
        "text": "; ".join(f"{line['label']}: {line['count']}" for line in lines),
        "reason": "таблица и начисления KORT расходятся - решить по каждому договору",
        "day": None,
        "amount": None,
        "direction": None,
        "party": "",
        "objects": [],
        "options": [{"key": "open", "title": "Открыть сверку", "hint": "", "commands": [], "needs": [], "open": True,
                     "place": "reconcile"}],
        "suggested": None,
        "count": card["count"],
    }], {}


QUESTIONS: dict[str, Callable[[Ctx, date], tuple[list[dict[str, Any]], dict[str, Any]]]] = {
    "payments": _payments,
    "bins": _bins,
    "twins": _twins,
    "reconcile": _reconcile,
}


def questions(ctx: Ctx, raw: dict[str, Any]) -> dict[str, Any]:
    params = dict(raw.get("params") or {})
    day = _day(raw.get("as_of"), today())
    kind = str(params.get("kind") or "")
    if kind and kind not in QUESTIONS:
        raise FinanceError("Вид вопросов - " + ", ".join(QUESTION_KINDS))
    try:
        limit = max(1, min(500, int(params.get("limit") or LIMIT)))
    except (TypeError, ValueError) as exc:
        raise FinanceError("Сколько вопросов - число от 1 до 500") from exc
    rows: list[dict[str, Any]] = []
    kinds: dict[str, Any] = {}
    for key in ([kind] if kind else QUESTION_KINDS):
        found, extra = QUESTIONS[key](ctx, day)
        rows.extend(found[:limit])
        kinds[key] = {"count": len(found), "shown": min(len(found), limit), **extra}
    return {
        "version": VERSION,
        "question": {"source": "questions", "params": {"kind": kind} if kind else {}},
        "as_of": day.isoformat(),
        "source": "questions",
        "rows": rows,
        "kinds": kinds,
        "totals": {"questions": len(rows)},
    }


# ── Под угрозой ──────────────────────────────────────────────────────────────


def _strip(card: dict[str, Any]) -> dict[str, Any]:
    """Карточка «Сегодня» без адреса экрана: куда вести человека, решает оболочка по `open`."""
    out = {key: value for key, value in card.items() if key != "go"}
    out["lines"] = [{key: value for key, value in line.items() if key != "go"} for line in card.get("lines") or []]
    return out


def _cash(ctx: Ctx, day: date) -> dict[str, Any] | None:
    from app.finance.model import cash

    if not ctx.viewer.sees(cash.VIEW_RESOURCE):
        return None
    answer = cash.ask(ctx, {"source": "calendar", "as_of": day.isoformat()})
    gaps = answer.get("gaps") or []
    item: dict[str, Any] = {
        "key": "cash", "title": "Деньги вперёд", "amount": None, "count": len(gaps), "unit": "разрывов", "lines": [], "empty": "",
        "note": "", "open": {"source": "calendar"},
    }
    if not gaps:
        item["empty"] = "Разрывов в окне календаря нет"
        return item
    first = gaps[0]
    item["amount"] = first["deficit"]
    item["lines"] = [
        {"label": f"{gap['entity_title']}: с {date.fromisoformat(gap['day']):%d.%m} не хватит", "amount": gap["deficit"]}
        for gap in gaps[:6]
    ]
    return item


def _vat(ctx: Ctx, day: date) -> dict[str, Any] | None:
    from app.finance.core import today as today_module

    if not ctx.viewer.sees("reports.summary"):
        return None
    card = _strip(today_module.vat_card(ctx.session, ctx.workspace, ctx.access, today=day))
    card["open"] = {"place": "vat"}
    return card


def _debts(ctx: Ctx, day: date) -> dict[str, Any] | None:
    from app.finance.core import today as today_module
    from app.finance.model.ask import debts_access

    if not ctx.viewer.sees("reports.debts"):
        return None
    try:
        debts_access(ctx)
    except PermissionError:
        return None
    card = _strip(today_module.debts_card(ctx.session, ctx.workspace, ctx.access, today=day))
    card["open"] = {"source": "debts", "params": {"side": "receivable", "basis": "accruals", "group": "client"}}
    return card


def _unbilled(ctx: Ctx, day: date) -> dict[str, Any] | None:
    from app.finance.model import ask as ask_module

    if not ctx.can("invoices"):
        return None
    answer = ask_module.ask(ctx, {"source": "unbilled"})
    rows = answer.get("rows") or []
    total = sum((Decimal(str(row.get("amount") or 0)) for row in rows), Decimal("0"))
    item: dict[str, Any] = {
        "key": "unbilled", "title": "Счета пора выставить", "amount": _money(total), "count": len(rows), "unit": "счетов",
        "lines": [], "empty": "", "note": answer.get("error") or "", "open": {"source": "unbilled"},
    }
    if not rows:
        item["empty"] = "Все счета выставлены"
    return item


def _acts(ctx: Ctx, day: date) -> dict[str, Any] | None:
    from app.finance import packs
    from app.finance.core import today as today_module

    if not ctx.access.view:
        return None
    work, _contracts, book = today_module.acts_work(ctx.session, ctx.workspace, ctx.access, today=day)
    count = len(work["unsigned"]) + len(work["no_esf"]) + len(work["missing"])
    item: dict[str, Any] = {
        "key": "acts", "title": "АВР и ЭСФ", "amount": None, "count": count, "unit": "дел", "lines": [], "empty": "",
        "note": "сводная таблица ещё читается - АВР только KORT и 1С" if book is None else "",
        "open": {"source": "documents", "params": {"view": "missing"}},
    }
    if not count:
        item["empty"] = "Акты подписаны, ЭСФ выписаны, у каждого месяца есть АВР"
        return item
    esf_days = packs.country_of(ctx.workspace).esf_days
    item["lines"] = [
        {"label": "месяц начислен - АВР нет", "count": len(work["missing"]), "open": {"source": "documents", "params": {"view": "missing"}}},
        {"label": "АВР не подписан клиентом", "count": len(work["unsigned"]), "open": {"source": "documents", "params": {"view": "unsigned"}}},
        {"label": f"ЭСФ не выписан дольше {esf_days} дней", "count": len(work["no_esf"]),
         "open": {"source": "documents", "params": {"view": "no_esf"}}},
    ]
    return item


ATTENTION: dict[str, Callable[[Ctx, date], dict[str, Any] | None]] = {
    "cash": _cash,
    "vat": _vat,
    "debts": _debts,
    "unbilled": _unbilled,
    "acts": _acts,
}


def attention(ctx: Ctx, raw: dict[str, Any]) -> dict[str, Any]:
    params = dict(raw.get("params") or {})
    day = _day(raw.get("as_of"), today())
    item = str(params.get("item") or "")
    if item and item not in ATTENTION:
        raise FinanceError("Пункт - " + ", ".join(ATTENTION_ITEMS))
    rows: list[dict[str, Any]] = []
    for key in ([item] if item else ATTENTION_ITEMS):
        found = ATTENTION[key](ctx, day)
        if found is not None:
            rows.append(found)
    return {
        "version": VERSION,
        "question": {"source": "attention", "params": {"item": item} if item else {}},
        "as_of": day.isoformat(),
        "source": "attention",
        "rows": rows,
        # Какие пункты открыты спрашивающему - оболочка спрашивает их порознь.
        "items": [key for key in ATTENTION_ITEMS if _visible_item(ctx, key)],
    }


def _visible_item(ctx: Ctx, key: str) -> bool:
    from app.finance.model import cash

    return {
        "cash": ctx.viewer.sees(cash.VIEW_RESOURCE),
        "vat": ctx.viewer.sees("reports.summary"),
        "debts": ctx.viewer.sees("reports.debts"),
        "unbilled": ctx.can("invoices"),
        "acts": bool(ctx.access.view),
    }[key]


# ── Пришло ───────────────────────────────────────────────────────────────────


def arrived(ctx: Ctx, raw: dict[str, Any]) -> dict[str, Any]:
    """Что пришло с дня `since` (по умолчанию - неделя): оплаты, счета, АВР, ЭСФ, соглашения, поправки.

    День прихода - когда запись появилась в своём хранилище, а не когда её
    провели в журнал фактов: журнал проводится заново при выкладке и при
    правке договора, и по дню записи факта «пришедшим» выглядело бы всё
    прошлое (копия прода: 3 945 оплат «за месяц» сразу после проведения).
    Оплата - день записи строки выписки или оплаты руками; документ и поправка
    KORT, соглашение - день, когда их завели; документ 1С - его дата: своего
    дня появления у снимка 1С нет, а онлайн-база приходит в тот же день.
    Сальдо и решения людей - не «пришло». Человек без права настройки видит
    факты своих договоров; оплаты без договора - владелец и администратор.
    """
    from app.finance.contracts.models import Contract, ContractAmendment
    from app.finance.core import journal
    from app.finance.core.models import Document, Fact
    from app.finance.core.shadow import _visible
    from app.finance.models import Counterparty, Operation

    params = dict(raw.get("params") or {})
    day = _day(raw.get("as_of"), today())
    since = _day(params.get("since"), day - timedelta(days=7))
    if not ctx.access.view:
        raise PermissionError("Договоры вам не открыты")
    journal.ensure(ctx.session, ctx.workspace.id)
    start = datetime.combine(since, time.min, tzinfo=timezone.utc)
    workspace_id = ctx.workspace.id
    new_ops = {str(item) for item in ctx.session.scalars(
        sa.select(Operation.id).where(Operation.workspace_id == workspace_id, Operation.created_at >= start)
    )}
    new_docs = {str(item) for item in ctx.session.scalars(
        sa.select(Document.id).where(Document.workspace_id == workspace_id, Document.created_at >= start)
    )}
    new_amends = {str(item) for item in ctx.session.scalars(
        sa.select(ContractAmendment.id).where(ContractAmendment.workspace_id == workspace_id, ContractAmendment.created_at >= start)
    )}

    def came(fact: Fact) -> bool:
        prefix, _, rest = fact.key.partition(":")
        ident = rest.split(":")[0]
        if prefix == "op":
            return ident in new_ops
        if prefix == "doc":
            return ident in new_docs
        if prefix == "amend":
            return ident in new_amends
        if prefix == "1c":
            return fact.day is not None and fact.day >= since
        return False

    facts = ctx.session.scalars(
        sa.select(Fact).where(
            Fact.workspace_id == workspace_id, Fact.ended_seq.is_(None), Fact.void.is_(False), Fact.kind.in_(ARRIVED_KINDS),
        )
    )
    visible: set[uuid.UUID] | None = None
    if not ctx.access.setup:
        visible = {item.id for item in _visible(ctx.session, ctx.workspace, ctx.access)}
    fresh = [
        fact for fact in facts
        if came(fact) and (visible is None or (fact.contract_id is not None and fact.contract_id in visible))
    ]
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    payments = {"count": 0, "amount": Decimal("0"), "placed": 0, "without": 0}
    for fact in fresh:
        group = groups.setdefault((fact.kind, fact.source), {
            "kind": fact.kind, "source": fact.source,
            "title": f"{FACT_TITLES.get(fact.kind, fact.kind)} · {SOURCE_TITLES.get(fact.source, fact.source)}",
            "count": 0, "amount": Decimal("0"),
        })
        group["count"] += 1
        group["amount"] += fact.amount or Decimal("0")
        if fact.kind == "payment" and (fact.amount or 0) > 0:
            payments["count"] += 1
            payments["amount"] += fact.amount or Decimal("0")
            payments["placed" if fact.contract_id else "without"] += 1
    latest = sorted(fresh, key=lambda fact: (fact.day or date.min, fact.recorded_at), reverse=True)[:LATEST]
    contract_ids = {fact.contract_id for fact in latest if fact.contract_id}
    numbers = dict(ctx.session.execute(sa.select(Contract.id, Contract.number).where(Contract.id.in_(contract_ids))).all()) if contract_ids else {}
    party_ids = {fact.party_id for fact in latest if fact.party_id}
    names = dict(ctx.session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.id.in_(party_ids))).all()) if party_ids else {}
    return {
        "version": VERSION,
        "question": {"source": "arrived", "params": {"since": since.isoformat()}},
        "as_of": day.isoformat(),
        "source": "arrived",
        "since": since.isoformat(),
        "rows": [
            {**group, "amount": _money(group["amount"])}
            for group in sorted(groups.values(), key=lambda item: (-item["count"], item["kind"]))
        ],
        "payments": {**payments, "amount": _money(payments["amount"])},
        "latest": [
            {
                "kind": fact.kind,
                "source": fact.source,
                "title": FACT_TITLES.get(fact.kind, fact.kind),
                "day": fact.day.isoformat() if fact.day else None,
                "period": fact.period.isoformat() if fact.period else None,
                "amount": _money(fact.amount),
                "contract": {"id": str(fact.contract_id), "number": numbers.get(fact.contract_id) or ""} if fact.contract_id else None,
                "party": names.get(fact.party_id, "") if fact.party_id else "",
                "recorded_at": fact.recorded_at.isoformat() if fact.recorded_at else None,
            }
            for fact in latest
        ],
        "totals": {"facts": len(fresh)},
    }


# ── Документы ────────────────────────────────────────────────────────────────


def documents(ctx: Ctx, raw: dict[str, Any]) -> dict[str, Any]:
    """Дела по АВР и ЭСФ списком - те же числа, что пункт «АВР и ЭСФ» угроз и прежний экран актов."""
    from app.finance import packs
    from app.finance.core import today as today_module
    from app.finance.models import Counterparty

    if not ctx.access.view:
        raise PermissionError("Договоры вам не открыты")
    params = dict(raw.get("params") or {})
    day = _day(raw.get("as_of"), today())
    view = str(params.get("view") or "missing")
    if view not in DOCUMENT_VIEWS:
        raise FinanceError("Список - " + ", ".join(DOCUMENT_VIEWS))
    work, contracts, book = today_module.acts_work(ctx.session, ctx.workspace, ctx.access, today=day)
    party_ids = {item.customer_id for item in contracts.values() if item.customer_id} | {
        item.executor_id for item in contracts.values() if item.executor_id
    }
    names = dict(ctx.session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.id.in_(party_ids))).all()) if party_ids else {}
    rows: list[dict[str, Any]] = []
    for entry in work[view]:
        contract = contracts.get(uuid.UUID(entry["contract_id"]))
        if contract is None:
            continue
        act = entry.get("act") or {}
        rows.append({
            "contract": {"id": str(contract.id), "number": contract.number or ""},
            "client": names.get(contract.customer_id, "") if contract.customer_id else "",
            "entity": names.get(contract.executor_id, "") if contract.executor_id else "",
            "month": entry.get("month") or act.get("month"),
            "act": act or None,
            "days": entry.get("days"),
        })
    columns = [
        {"key": "contract", "title": "Договор", "type": "text"},
        {"key": "client", "title": "Клиент", "type": "text"},
        {"key": "entity", "title": "Наше юрлицо", "type": "text"},
        {"key": "month", "title": "Месяц", "type": "text"},
    ]
    if view != "missing":
        columns += [{"key": "act", "title": "АВР", "type": "text"}, {"key": "days", "title": "Дней", "type": "number"}]
    return {
        "version": VERSION,
        "question": {"source": "documents", "params": {"view": view}},
        "as_of": day.isoformat(),
        "source": "documents",
        "view": view,
        "esf_days": packs.country_of(ctx.workspace).esf_days,
        "columns": columns,
        "rows": rows,
        "counts": {key: len(work[key]) for key in DOCUMENT_VIEWS},
        "note": "сводная таблица ещё читается - АВР только KORT и 1С" if book is None else "",
    }


SOURCES: dict[str, Callable[[Ctx, dict[str, Any]], dict[str, Any]]] = {
    "questions": questions,
    "attention": attention,
    "arrived": arrived,
    "documents": documents,
}


__all__ = ["ATTENTION_ITEMS", "DOCUMENT_VIEWS", "QUESTION_KINDS", "SOURCES", "arrived", "attention", "documents", "questions"]
