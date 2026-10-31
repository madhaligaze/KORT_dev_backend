"""Спросить - любой вид на любой день одним конвертом.

Вопрос - сохранённый вид (`{"view": "oneoff"}`) или вопрос без сохранения
(`{"source": "debts", "filter": …, "params": {"group": "department"}}`), и
день (`as_of`). Ответ один по форме для всех источников:

* `question` - вопрос как его понял сервер (вид, отбор, меры, группировки,
  параметры) - его можно прислать снова;
* `columns` - что в строке и какого типа;
* `rows` - строки: договоры (`contracts`), группы долгов с договорами
  внутри (`debts`), счета (`unbilled`); `values` - числа строки, `refs` -
  ссылки на те, что раскрывает «объяснить»;
* `totals`, `groups`, `parts` - итоги, группировки и части листов.

Числа не пересчитываются здесь: договоры - `core/views.ask` (то же, что экран
вида), долги - `core/debts.Debts` (то же, что «Долги»), счета - книга-сводка
(`contracts/bills.py`, то же, что «Счета» и бот).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.finance.contracts import views as rules
from app.finance.contracts.service import today
from app.finance.core import views as views_module
from app.finance.core.models import View
from app.finance.model import VERSION, refs
from app.finance.model.context import Ctx
from app.finance.service import FinanceError

#: Ключ вопроса без сохранения: такого вида нет и быть не может (ключи - `[a-z0-9_-]`).
INLINE = "~"

#: Колонки договора в ответе: что есть у каждого договора, кроме мер вида.
CONTRACT_COLUMNS = (
    ("number", "Номер", "text"),
    ("client", "Клиент", "text"),
    ("entity", "Наше юрлицо", "text"),
    ("status", "Статус", "text"),
    ("amount", "Сумма", "money"),
)


def _day(raw: Any) -> date:
    if raw in (None, ""):
        return today()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise FinanceError("День вопроса - ГГГГ-ММ-ДД") from exc


def _clean_filter(raw: Any) -> dict[str, Any]:
    if raw in (None, "", {}):
        return {}
    try:
        return rules.validate(raw)
    except rules.FilterError as exc:
        raise FinanceError(f"Отбор вопроса: {exc}") from exc


def question_of(ctx: Ctx, raw: dict[str, Any]) -> tuple[View, dict[str, Any], date]:
    """Вид, на который отвечать, вопрос для ссылок и день.

    Сохранённый вид можно спросить с другими параметрами источника (у долгов -
    сторона, основание, группа) - сам вид от этого не меняется.
    """
    day = _day(raw.get("as_of"))
    departments = views_module._departments(ctx.session, ctx.workspace.id)
    if raw.get("view") is not None:
        view = views_module.find(ctx.session, ctx.workspace, str(raw.get("view")))
        if not views_module.can_see(ctx.viewer, view):
            raise PermissionError("Этот вид вам не открыт")
        params = dict(view.params or {})
        if raw.get("params"):
            params = views_module._clean_params(view.source, {**params, **dict(raw["params"])}, departments)
            # Вид отвечает с параметрами вопроса, а в базе остаётся прежним.
            view = _transient(view, params=params, keep_key=True)
        asked = {"view": view.key}
        if raw.get("params"):
            asked["params"] = params
        return view, asked, day
    source = str(raw.get("source") or "")
    if source not in views_module.SOURCES:
        raise FinanceError(
            "Источник вопроса - contracts, debts, unbilled, calendar, questions, attention, arrived, documents, participants или payments"
        )
    if not ctx.viewer.sees(views_module.SOURCES[source].resource):
        raise PermissionError(f"Источник «{views_module.SOURCES[source].title}» вам не открыт")
    spec = views_module.SOURCES[source]
    view = View(
        key=INLINE,
        title=str(raw.get("title") or spec.title)[:120],
        source=source,
        filter=_clean_filter(raw.get("filter")),
        measures=views_module._clean_list(
            raw.get("measures"),
            [*(item.key for item in views_module.MEASURES if item.type != "share"), *views_module.formula_keys(ctx.session, ctx.workspace.id)],
            "Меры",
        ),
        groups=views_module._clean_list(raw.get("groups"), spec.groups, "Группировки"),
        forms=[],
        params=views_module._clean_params(source, raw.get("params"), departments),
        pin="",
        position=0,
        template="",
    )
    asked = {
        "source": view.source,
        "title": view.title,
        "filter": view.filter,
        "measures": list(view.measures),
        "groups": list(view.groups),
        "params": dict(view.params),
    }
    return view, asked, day


def _transient(view: View, *, params: dict[str, Any], keep_key: bool) -> View:
    """Копия вида вне сессии - чтобы спросить его иначе, ничего не записав."""
    copy = View(
        key=view.key if keep_key else INLINE,
        title=view.title,
        source=view.source,
        owner_department_id=view.owner_department_id,
        owner_user_id=view.owner_user_id,
        filter=dict(view.filter or {}),
        measures=list(view.measures or []),
        groups=list(view.groups or []),
        forms=list(view.forms or []),
        params=params,
        pin=view.pin,
        position=view.position,
        template=view.template,
    )
    # Листы вида ищутся по ключу у вида с id: копия отвечает теми же листами.
    copy.id = view.id
    return copy


def question_out(view: View, asked: dict[str, Any]) -> dict[str, Any]:
    return {
        **asked,
        "view": view.key if view.key != INLINE else None,
        "title": view.title,
        "source": view.source,
        "filter": view.filter or {"any": []},
        "measures": list(view.measures or []),
        "groups": list(view.groups or []),
        "params": dict(view.params or {}),
    }


def ask(ctx: Ctx, raw: dict[str, Any], *, force: bool = False, wait: float | None = None) -> dict[str, Any]:
    """Ответ на вопрос - см. описание модуля.

    `force` - перечитать сводную таблицу (счета), `wait` - сколько ждать её
    чтения; без них - как раздел «Счета».
    """
    if raw.get("source") == "calendar":
        # Платёжный календарь - не вид договоров: свой ответ (`model/cash.py`).
        from app.finance.model import cash

        return cash.ask(ctx, raw)
    if raw.get("source") in ("questions", "attention", "arrived", "documents"):
        # Главный экран и дела по АВР - свои ответы (`model/questions.py`).
        from app.finance.model import questions

        return questions.SOURCES[str(raw["source"])](ctx, raw)
    if raw.get("source") in ("participants", "payments"):
        # Участники и оплаты списком (`model/lists.py`).
        from app.finance.model import lists

        return lists.SOURCES[str(raw["source"])](ctx, raw)
    view, asked, day = question_of(ctx, raw)
    if view.source == "contracts":
        body = contracts(ctx, view, asked, day)
    elif view.source == "debts":
        body = debts(ctx, view, asked, day)
    else:
        body = unbilled(ctx, view, asked, force=force, wait=wait)
    return {
        "version": VERSION,
        "question": question_out(view, asked),
        "as_of": day.isoformat(),
        "source": view.source,
        **body,
    }


# ── Договоры ─────────────────────────────────────────────────────────────────


def contracts(ctx: Ctx, view: View, asked: dict[str, Any], day: date, *, members: dict | None = None) -> dict[str, Any]:
    wanted = members if members is not None else ({} if "people" in (view.groups or []) else None)
    found = views_module.ask(ctx.session, ctx.workspace, ctx.access, view, day=day, members=wanted)
    from app.finance.core import formulas as formulas_module

    hidden = ctx.access.hidden
    company = {item.key: item for item in formulas_module.of(ctx.session, ctx.workspace.id)}
    measures = [
        key for key in (view.measures or []) if (key in views_module.COLUMN_MEASURES and key not in hidden) or key in company
    ]
    columns = [
        {"key": key, "title": title, "type": kind}
        for key, title, kind in CONTRACT_COLUMNS
        if not (key == "amount" and "amount" in hidden)
        and not (key in ("client", "entity") and {"customer", "executor"} & hidden)
        and not (key == "status" and "status" in hidden)
    ]
    columns += [
        {"key": key, "title": company[key].title, "type": company[key].type, "formula": company[key].text}
        if key in company
        else {"key": key, "title": views_module.MEASURE_BY_KEY[key].title, "type": views_module.MEASURE_BY_KEY[key].type}
        for key in measures
    ]
    on = day.isoformat()
    rows = []
    for row in found["rows"]:
        numbers = row["measures"]
        fraction = row.get("fraction")
        values: dict[str, Any] = {"amount": numbers.get("amount")}
        links: dict[str, str] = {}
        if values["amount"] is not None:
            links["amount"] = refs.make("amount", c=row["id"], f=fraction)
        for key in measures:
            values[key] = numbers.get(key)
            if key == "summary_paid" and values[key] is not None:
                links[key] = refs.make("paid", c=row["id"], d=on, f=fraction)
            elif key == "summary_remaining" and values[key] is not None:
                links[key] = refs.make("left", c=row["id"], d=on, f=fraction)
            elif key in company and values[key] is not None:
                links[key] = refs.make("formula", c=row["id"], d=on, f=fraction, m=key)
        rows.append(
            {
                "id": row["id"],
                "number": row["number"],
                **{key: row[key] for key in ("client", "entity", "status") if key in row},
                "places": row["places"],
                "values": values,
                "refs": links,
                **{key: row[key] for key in ("fraction", "share") if key in row},
                **({"paid_source": numbers["paid_source"]} if "paid_source" in numbers else {}),
                **({"now_only": True} if numbers.get("now_only") else {}),
                "month": numbers.get("month") or "",
            }
        )
    parts = []
    for sheet in found["sheets"]:
        blocks = []
        for index, block in enumerate(sheet["blocks"]):
            item = dict(block)
            if "remaining" in item:
                item["refs"] = {"remaining": refs.make("block", q=asked, s=sheet["key"], b=index, d=on)}
            blocks.append(item)
        parts.append({**sheet, "blocks": blocks})
    groups: dict[str, Any] = {}
    people = found["groups"].get("people")
    if people is not None:
        out_rows = []
        for item in people["rows"]:
            links = {
                key: refs.make("person", q=asked, n=item["name"], m=key, d=on)
                for key in ("amount", "remaining", "open_amount")
                if item.get(key) is not None
            }
            out_rows.append({**item, "refs": links})
        totals = people["totals"]
        groups["people"] = {
            "rows": out_rows,
            "totals": {
                **totals,
                "refs": {
                    key: refs.make("people", q=asked, m=key, d=on)
                    for key in ("amount", "remaining", "open_amount")
                    if totals.get(key) is not None
                },
            },
        }
    if "age" in found["groups"]:
        groups["age"] = [
            {
                **item,
                "refs": {
                    key: refs.make("age", q=asked, t=item["title"], m=key, d=on)
                    for key in ("amount", "remaining")
                    if item.get(key) is not None
                },
            }
            for item in found["groups"]["age"]
        ]
    money = [row["values"].get("amount") for row in rows if row["values"].get("amount") is not None]
    totals_out: dict[str, Any] = {"contracts": len(rows)}
    if money:
        totals_out["amount"] = _sum(money)
        totals_out["refs"] = {"amount": refs.make("sum", q=asked, m="amount", d=on)}
    for key in ("summary_paid", "summary_remaining"):
        known = [row["values"].get(key) for row in rows if row["values"].get(key) is not None]
        if key in measures and known:
            totals_out[key] = _sum(known)
            totals_out.setdefault("refs", {})[key] = refs.make("sum", q=asked, m=key, d=on)
    return {
        "columns": columns,
        "rows": rows,
        "totals": totals_out,
        "parts": parts,
        "groups": groups,
        **({"summary": found["summary"]} if "summary" in found else {}),
    }


def _sum(values: list[str]) -> str:
    total = sum((Decimal(value) for value in values), Decimal("0"))
    return format(total.quantize(Decimal("0.01")), "f")


# ── Долги ────────────────────────────────────────────────────────────────────


def debts_access(ctx: Ctx) -> None:
    # Долг - сумма договора минус оплаты по выписке: кому они скрыты, тому и долг.
    if {"amount", "paid", "remaining"} & ctx.access.hidden:
        raise PermissionError("Долги по договорам видит тот, кому открыты сумма договора и оплаты по выписке")


def debts_of(ctx: Ctx, view: View, day: date) -> tuple[Any, dict[str, Any], str]:
    """`Debts` вопроса, его параметры и что не так со сводной: АВР таблицы - часть долга по АВР
    у юрлиц без 1С, и без неё долг был бы меньше молча."""
    from app.finance.core import book as book_module
    from app.finance.core.debts import Debts

    debts_access(ctx)
    params = views_module._clean_params("debts", view.params, {})
    book, error = None, ""
    if params["basis"] == "acts":
        book, error = book_module.current(ctx.session, ctx.workspace.id, wait=book_module.FIRST_READ_WAIT)
    found = Debts(
        ctx.session, ctx.workspace, ctx.access, day=day, basis=params["basis"], book=book,
        side=params["side"], rule=(view.filter or None),
    )
    note = f"{error or 'Сводная таблица компании не подключена'} - АВР только KORT и 1С" if params["basis"] == "acts" and book is None else ""
    return found, params, note


def debts(ctx: Ctx, view: View, asked: dict[str, Any], day: date) -> dict[str, Any]:
    found, params, note = debts_of(ctx, view, day)
    out = found.out(params["group"])
    on = day.isoformat()
    basis = params["basis"]
    rows = []
    for group in out["rows"]:
        contracts_out = []
        for item in group["contracts"]:
            # Юрлицо месяца: после перевода клиента старые месяцы - долг прежнему ТОО
            # (`Debts._entity_pieces`); у покупки наше юрлицо - заказчик, делить нечего.
            owner = group["key"] if params["group"] == "entity" and params["side"] != "payable" else None
            share = item.get("fraction") if params["group"] in ("department", "person") else None
            link = {"c": item["id"], "b": basis, "d": on, "s": params["side"], "o": owner, "f": share}
            contracts_out.append(
                {**item, "refs": {key: refs.make("debt", m=key, **link) for key in ("debt", "owed") if item.get(key) is not None}}
            )
        rows.append(
            {
                **group,
                "contracts": contracts_out,
                "refs": {key: refs.make("dgroup", q=asked, g=group["key"], m=key, d=on) for key in ("debt", "owed")},
            }
        )
    totals = out["totals"]
    return {
        "columns": [
            {"key": "title", "title": "Кто", "type": "text"},
            {"key": "owed", "title": "Неоплачено", "type": "money"},
            {"key": "advance", "title": "Аванс", "type": "money"},
            {"key": "debt", "title": "Долг", "type": "money"},
        ],
        "rows": rows,
        "totals": {**totals, "refs": {key: refs.make("dtotal", q=asked, m=key, d=on) for key in ("debt", "owed")}},
        "params": params,
        "ages": out["ages"],
        "sides": out["sides"],
        "since": out["since"],
        **({"error": note} if note else {}),
    }


# ── Счета к выставлению ──────────────────────────────────────────────────────


def unbilled_board(ctx: Ctx, *, force: bool = False, wait: float | None = None) -> tuple[Any, str, str, Any]:
    """Доска счетов компании: из сводной таблицы, если она подключена, иначе из начислений и счетов KORT.

    Возвращает доску (или `None` - таблица ещё читается), отказ чтения, откуда
    (`table` / `kort`) и подключённую таблицу.
    """
    from app.finance.contracts import bills
    from app.finance.contracts.summary import source_of
    from app.finance.core import billing

    source = source_of(ctx.session, ctx.workspace.id)
    if source is None:
        return billing.kort_board(ctx.session, ctx.workspace, today=today()), "", "kort", None
    board, error = bills.current(ctx.session, ctx.workspace.id, force=force, **({"wait": wait} if wait is not None else {}))
    return board, error, "table", source


def unbilled(ctx: Ctx, view: View, asked: dict[str, Any], *, force: bool = False, wait: float | None = None) -> dict[str, Any]:
    """Счета, которые пора выставить, и ключи выставленных - то же, что раздел «Счета» и бот.

    Компания без сводной таблицы получает счета KORT: начисления её договоров,
    по которым KORT не знает счёта (`core/billing.kort_board`).
    """
    from app.finance.contracts import bills

    if not ctx.can("invoices"):
        raise PermissionError("Счета вам не открыты")
    columns = [
        {"key": "own", "title": "Наше юрлицо", "type": "text"},
        {"key": "customer", "title": "Клиент", "type": "text"},
        {"key": "number", "title": "Договор", "type": "text"},
        {"key": "month", "title": "Месяц", "type": "text"},
        {"key": "period_from", "title": "Период с", "type": "date"},
        {"key": "text", "title": "Состояние", "type": "text"},
        {"key": "amount", "title": "Сумма", "type": "money"},
    ]
    board, error, origin, source = unbilled_board(ctx, force=force, wait=wait)
    if board is None:
        return {
            "columns": columns, "rows": [], "totals": {"bills": 0}, "issued": [], "done": [],
            "board": {"title": source.title, "worksheet": source.worksheet, "read_at": None, "error": error or "Таблица ещё читается"},
        }
    params = view.params or {}
    own = str(params.get("own") or "").strip().casefold()
    department = str(params.get("department") or "").strip().casefold()

    def wanted(bill: Any) -> bool:
        return (not own or bill.own.strip().casefold() == own) and (not department or bill.department.strip().casefold() == department)

    shown = [bill for bill in board.bills if wanted(bill)]
    rows = [{**bill.out(), "refs": {"amount": refs.make("bill", id=bill.id)} if bill.amount is not None else {}} for bill in shown]
    counts = {group: 0 for group in bills.GROUPS}
    for bill in shown:
        counts[bill.group] += 1
    return {
        "columns": columns,
        "rows": rows,
        "totals": {
            "bills": len(shown),
            "amount": _sum([str(bill.amount) for bill in shown if bill.amount is not None]) if shown else "0.00",
            "groups": counts,
        },
        "board": {
            "title": board.title,
            "worksheet": board.worksheet,
            "read_at": board.read_at.isoformat(),
            "drift": board.drift,
            "error": error,
            "kort": list(board.kort),
            "kort_error": board.kort_error,
            # Откуда счета: сводная таблица компании или начисления KORT (компания без таблицы).
            "source": origin,
        },
        # Выставленные - ключами (бот убирает их из постов), строками - за прошлый
        # и текущий месяц, как «Выставлены» раздела: весь год - тысяча строк.
        "issued": sorted(board.issued),
        "done": [bill.out() for bill in bills.recent(board) if wanted(bill)],
    }


__all__ = ["INLINE", "ask", "contracts", "debts", "debts_access", "debts_of", "question_of", "question_out", "unbilled"]
