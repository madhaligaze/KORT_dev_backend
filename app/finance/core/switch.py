"""Переход с таблицы на KORT (фаза 10): звено за звеном, юрлицо за юрлицом.

Пока правда в сводной таблице, KORT считает ту же цепочку сам и сверяет её
по звеньям: начисления (`core/shadow.py`), счета к выставлению
(`core/billing.py`), оплаты (`core/payments.py`), АВР 1С против таблицы
(`core/acts.py`), сальдо (`core/debts.py`). «Переход» складывает эти сверки по
нашим юрлицам - сколько месяцев договоров совпало (`link_tally`) - и хранит,
какое звено какого юрлица уже ведётся в KORT (`sources`).

Переключает человек, а не порог: расхождение бывает и ошибкой таблицы,
которую KORT уже не повторяет, и наоборот. Поэтому перед переключением окно
показывает, что именно изменится (`preview_bills`), а вернуть звено на
таблицу можно той же кнопкой.

Первое переключаемое звено - счета к выставлению (`bills_board`): у
юрлица, переведённого на KORT, «Счета → Не выставлены», число у пункта, бот
и шаблон Excel берут состояние счёта не словами колонки «Счет», а из
начислений договора и счетов KORT. Строка таблицы, которую KORT не связал с
договором, остаётся в списке словами таблицы: пропавший счёт - потерянные
деньги, а не порядок.
"""
from __future__ import annotations

import dataclasses
import threading
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts.models import (
    Contract,
    ContractDepartment,
    ContractPerson,
    Department,
    Employee,
    GroupEntity,
    ListValue,
)
from app.finance.contracts.service import Access, Actor
from app.finance.core import book as book_module
from app.finance.core import settings as core_settings
from app.finance.core.accruals import Ledger, month_key, month_start
from app.finance.core.models import CoreSettings, Document
from app.finance.layout import norm
from app.finance.models import ActionLog, Counterparty, Workspace
from app.finance.service import FinanceError

LINKS = ("accruals", "bills", "payments", "acts", "saldo")
LINK_TITLES = {
    "accruals": "Начисления",
    "bills": "Счета к выставлению",
    "payments": "Оплаты",
    "acts": "АВР: 1С и таблица",
    "saldo": "Сальдо",
}
#: Какие звенья уже умеют читать KORT вместо таблицы - их и можно переключать:
#: счета - «Не выставлены», число у пункта, бот, шаблон; оплаты - «Оплачено» и
#: «Остаток» реестра, «Разовых», листов и выгрузки (`Registry.summary_of`).
SWITCHABLE = ("bills", "payments")
SOURCES = ("book", "kort")

#: Весь реестр - список счетов и бот общие для компании, как и сводная таблица.
_EVERYTHING = Access(view=True, admin=True, full_contracts=True)


def sources(session: Session, workspace_id: uuid.UUID) -> dict[str, dict[str, str]]:
    """Звено → наше юрлицо → источник (`kort`); нет в словаре - таблица."""
    row = session.get(CoreSettings, workspace_id)
    raw = ((row.data if row is not None else None) or {}).get("sources") or {}
    return {
        link: {str(entity): value for entity, value in (items or {}).items() if value in SOURCES}
        for link, items in raw.items()
        if link in LINKS
    }


def kort_entities(session: Session, workspace_id: uuid.UUID, link: str) -> set[uuid.UUID]:
    """Наши юрлица, у которых звено ведётся в KORT."""
    out: set[uuid.UUID] = set()
    for key, value in sources(session, workspace_id).get(link, {}).items():
        if value == "kort":
            try:
                out.add(uuid.UUID(key))
            except ValueError:
                continue
    return out


def set_source(
    session: Session, workspace: Workspace, access: Access, actor: Actor, *, link: str, entity_id: uuid.UUID, source: str
) -> dict[str, dict[str, str]]:
    if not access.setup:
        raise PermissionError("Переводит учёт на KORT владелец или администратор")
    if link not in SWITCHABLE:
        raise FinanceError("Это звено пока ведётся только в таблице")
    if source not in SOURCES:
        raise FinanceError("Источник - таблица или KORT")
    entity = session.get(GroupEntity, entity_id)
    if entity is None or entity.workspace_id != workspace.id:
        raise FinanceError("Такого нашего юрлица нет")
    row = session.get(CoreSettings, workspace.id)
    if row is None:
        row = CoreSettings(workspace_id=workspace.id, data={})
        session.add(row)
    data = dict(row.data or {})
    all_links = {key: dict(value) for key, value in (data.get("sources") or {}).items()}
    items = all_links.setdefault(link, {})
    before = items.get(str(entity_id), "book")
    if before == source:
        return sources(session, workspace.id)
    if source == "book":
        items.pop(str(entity_id), None)
    else:
        items[str(entity_id)] = source
    data["sources"] = all_links
    row.data = data
    row.updated_by = actor.user_id
    row.updated_at = datetime.now(timezone.utc)
    session.flush()
    history.write(
        session,
        workspace,
        kind="core.settings",
        entity="core_settings",
        entity_id=None,
        title=f"{LINK_TITLES[link]} {entity.code or 'юрлица'} - {'из KORT' if source == 'kort' else 'снова из таблицы'}",
        before={"sources": {link: {str(entity_id): before}}},
        after={"sources": {link: {str(entity_id): source}}},
        actor=actor.email,
    )
    return sources(session, workspace.id)


# ── Сводка сверок по юрлицам ────────────────────────────────────────────────


def _accruals(session: Session, workspace: Workspace, access: Access, book: Any) -> tuple[list[str], dict[str, dict[str, int]]]:
    from app.finance.core.shadow import EntityTally, Reconciler, month_owner

    reconciler = Reconciler(session, workspace, access, book)
    tally = EntityTally()
    for item in reconciler.run().contracts.values():
        for line in item.lines:
            tally.add(month_owner(item.contract, line.kort, line.book), item.contract.id, line.same, line.month)
    return reconciler.window, tally.out()


def _unmatched(session: Session, workspace: Workspace, book: Any, window: list[str]) -> dict[str, dict[str, int]]:
    """Строки таблицы, которые KORT не связал ни с одним договором, - по юрлицу строки и месяцу.

    В сверках звеньев их нет (сверять не с чем), а при переходе это самые
    опасные строки: у KORT такого договора нет, и сам он счёт не напомнит.
    """
    matcher = book_module.Matcher(session, workspace.id)
    months = set(window)
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in book.rows:
        if not row.month or row.month not in months:
            continue
        match = matcher.match(row)
        if match.state == "found" and match.contract_id is not None:
            continue
        entity = matcher.entity_by_code.get(norm(row.own))
        if entity is not None:
            out[str(entity)][row.month] += 1
    return out


#: Сколько последних закрытых месяцев без расхождений нужно, чтобы звено юрлица было готово к переходу.
CLOSED_MONTHS = 2


def link_tally(session: Session, workspace: Workspace, access: Access, link: str, *, today: date) -> dict[str, Any]:
    """Звено по нашим юрлицам: месяцев сверено, совпало, строк без договора, откуда сейчас."""
    from app.finance.core.debts import entity_codes

    if link not in LINKS:
        raise FinanceError("Такого звена нет")
    out: dict[str, Any] = {"link": link, "title": LINK_TITLES[link], "switchable": link in SWITCHABLE, "window": [], "entities": [], "error": ""}
    book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
    if book is None:
        out["error"] = error or "Сводная таблица компании не подключена"
        return out
    if link == "accruals":
        window, by = _accruals(session, workspace, access, book)
    elif link == "bills":
        from app.finance.core.billing import reconcile_bills

        result = reconcile_bills(session, workspace, access, book, today=today)
        window, by = result["window"], result.get("by_entity", {})
    elif link == "payments":
        from app.finance.core.payments import reconcile_paid

        result = reconcile_paid(session, workspace, access, book, today=today)
        window, by = result["window"], result.get("by_entity", {})
    elif link == "acts":
        from app.finance.core.acts import Acts
        from app.finance.core.shadow import _visible

        acts = Acts(session, workspace, access, book, today=today)
        window = [month for month in (acts.reconciler.window if acts.reconciler else []) if month < month_key(today)]
        contracts = {item.id: item for item in _visible(session, workspace, access)}
        result = acts.reconcile(contracts, window=window)
        by = result.get("by_entity", {})
    else:
        from app.finance.core.debts import reconcile_saldo

        result = reconcile_saldo(session, workspace, access, book, today=today)
        window, by = result["window"], result.get("by_entity", {})
    # Строки без договора важны там, где звено переключают и где таблица - вся
    # правда о договоре: начисления и счета.
    unknown = _unmatched(session, workspace, book, window) if link in ("accruals", "bills") else {}
    codes = entity_codes(session, workspace)
    current = sources(session, workspace.id).get(link, {})
    # Переход - после закрытых месяцев без расхождений (страница «Ядро KORT», R21):
    # месяц закрыт, когда он уже прошёл.
    closed = [month for month in window if month < month_key(today)][-CLOSED_MONTHS:]
    entities = []
    for key in sorted(set(by) | set(unknown)):
        try:
            entity_id = uuid.UUID(key)
        except ValueError:
            continue
        if entity_id not in codes:
            continue
        tally = by.get(key, {})
        months = tally.get("months", 0)
        same = tally.get("same", 0)
        missing = sum(unknown.get(key, {}).values())
        checked = months + missing
        last = []
        for month in closed:
            seen, matched = tally.get("by_month", {}).get(month, [0, 0])
            last.append({"month": month, "checked": seen + unknown.get(key, {}).get(month, 0), "same": matched})
        entities.append(
            {
                "id": key,
                "code": codes[entity_id],
                "months": months,
                "same": same,
                "unknown": missing,
                "share": format((Decimal(same) / Decimal(checked)).quantize(Decimal("0.0001")), "f") if checked else None,
                "contracts": tally.get("contracts", 0),
                "contracts_same": tally.get("contracts_same", 0),
                "clean": checked > 0 and same == months and not missing,
                "closed": last,
                "ready": len(last) == CLOSED_MONTHS and all(item["checked"] and item["same"] == item["checked"] for item in last),
                "source": current.get(key, "book"),
            }
        )
    entities.sort(key=lambda item: item["code"])
    out.update(window=window, entities=entities)
    return out


# ── «Счета → Не выставлены» из KORT ─────────────────────────────────────────

_MONTHS = (
    "ЯНВАРЬ", "ФЕВРАЛЬ", "МАРТ", "АПРЕЛЬ", "МАЙ", "ИЮНЬ",
    "ИЮЛЬ", "АВГУСТ", "СЕНТЯБРЬ", "ОКТЯБРЬ", "НОЯБРЬ", "ДЕКАБРЬ",
)


def _days(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return "дней"
    return {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(count % 10, "дней")


def words(group: str, period_from: date | None, today: date) -> str:
    """Состояние словами формулы таблицы - чтобы список читался так же, как до перехода."""
    from app.finance.contracts import bills as bills_module

    if group == bills_module.OVERDUE and period_from is not None:
        late = (today - period_from).days
        return "Просрочка более 5 дней" if late > 5 else f"Просрочка {late} {_days(late)}"
    if group == bills_module.SOON and period_from is not None:
        left = (period_from - today).days
        return "Сегодня день выставления" if left <= 0 else f"{left} {_days(left)} до выставления"
    if group == bills_module.EARLY:
        return "Еще рано"
    return ""


def _shown(day: date | None) -> str:
    return f"{day:%d.%m.%Y}" if day else ""


@dataclasses.dataclass
class _Context:
    """Что нужно, чтобы назвать счёт KORT словами строки таблицы."""

    codes: dict[uuid.UUID, str]
    names: dict[uuid.UUID, str]
    values: dict[uuid.UUID, str]
    staff: dict[uuid.UUID, str]
    departments: dict[uuid.UUID, str]
    #: Слова «Вида Услуги» компании → пост бота (`core/settings.table_words`).
    posts: dict[str, str] = dataclasses.field(default_factory=dict)


def _context(session: Session, workspace_id: uuid.UUID, contracts: list[Contract]) -> _Context:
    ids = [item.id for item in contracts]
    staff: dict[uuid.UUID, list[str]] = defaultdict(list)
    departments: dict[uuid.UUID, list[str]] = defaultdict(list)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for contract_id, name in session.execute(
            sa.select(ContractPerson.contract_id, Employee.full_name)
            .join(Employee, Employee.id == ContractPerson.employee_id)
            .where(ContractPerson.contract_id.in_(chunk))
            .order_by(ContractPerson.position)
        ):
            staff[contract_id].append(name)
        for contract_id, code, title in session.execute(
            sa.select(ContractDepartment.contract_id, Department.code, Department.title)
            .join(Department, Department.id == ContractDepartment.department_id)
            .where(ContractDepartment.contract_id.in_(chunk))
            .order_by(ContractDepartment.position)
        ):
            departments[contract_id].append(code or title)
    return _Context(
        codes={
            entity_id: code
            for entity_id, code in session.execute(
                sa.select(GroupEntity.counterparty_id, GroupEntity.code).where(GroupEntity.workspace_id == workspace_id)
            )
            if code
        },
        names=dict(session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace_id)).all()),
        values=dict(session.execute(sa.select(ListValue.id, ListValue.value).where(ListValue.workspace_id == workspace_id)).all()),
        staff={key: ", ".join(value) for key, value in staff.items()},
        departments={key: value[0] for key, value in departments.items() if value},
        posts=core_settings.table_words(session, workspace_id).get("kind", {}),
    )


def _made(contract: Contract, month: str, accrual: Any, owner: uuid.UUID, doc: Document | None, context: _Context, group: str, text: str) -> Any:
    """Счёт месяца, которого в таблице нет: KORT называет его теми же полями, что строка таблицы."""
    from app.finance.contracts import bills as bills_module

    meta = (doc.meta or {}) if doc is not None else {}
    kind = context.values.get(contract.type_id, "") if contract.type_id else ""
    return bills_module.Bill(
        line=0,
        group=group,
        text=text,
        own=context.codes.get(owner, ""),
        customer=context.names.get(contract.customer_id, "") if contract.customer_id else "",
        number=contract.number,
        month=f"{_MONTHS[int(month[5:7]) - 1]} {month[:4]}",
        month_no=int(month[5:7]),
        kind=kind,
        staff=context.staff.get(contract.id, ""),
        department=context.departments.get(contract.id, ""),
        amount=accrual.amount,
        period_from=accrual.period_from,
        period_to=accrual.period_to,
        paid="",
        status=context.values.get(contract.status_id, "") if contract.status_id else "",
        note=str(meta.get("note") or ""),
        bill_no=doc.number if doc is not None else "",
        bill_date=_shown(doc.issued_on) if doc is not None else "",
        bill_period=str(meta.get("bill_period") or ""),
        signed_at=_shown(contract.signed_at),
        source="kort",
        contract_id=str(contract.id),
        post=context.posts.get(norm(kind), ""),
    )


def build_bills(session: Session, workspace_id: uuid.UUID, board: Any, switched: set[uuid.UUID], today: date) -> Any:
    """Сводка счетов, где счета юрлиц `switched` посчитаны KORT.

    Месяц договора, который KORT начисляет юрлицу из `switched`, получает
    состояние KORT: выставлен - если счёт знает KORT или таблица (№ счёта,
    галочка); нет - срок по «Периоду с» начисления тем же правилом, что
    формула таблицы (`bills._group_by_date`). Строка таблицы этого месяца
    остаётся адресом счёта (карточка пишет в неё), а нет строки - счёт без
    неё (`line = 0`). Вторая строка того же месяца в невыставленных не
    повторяется: счёт один.

    Строка юрлица из `switched`, за месяц которой KORT не начисляет ничего, из
    невыставленных уходит: по условиям договора счёт не нужен (что уйдёт,
    показывает окно перехода). Всё остальное - словами таблицы: строки
    юрлиц на таблице, строки без договора в KORT и месяцы, которые KORT
    начисляет другому юрлицу, чем таблица.
    """
    from app.finance.contracts import bills as bills_module
    from app.finance.core.billing import kort_bills
    from app.finance.core.shadow import _visible

    done_group = bills_module.DONE
    workspace = session.get(Workspace, workspace_id)
    matcher = book_module.Matcher(session, workspace_id)
    bills: list[Any] = []
    done: list[Any] = []
    table: dict[tuple[uuid.UUID, str], list[Any]] = defaultdict(list)
    months: set[str] = set()
    for bill in [*board.bills, *board.done]:
        month = book_module.month_of(bill.month, bill.period_from)
        match = None
        if month:
            months.add(month)
            match = matcher.match(
                book_module.BookRow(
                    line=bill.line, own=bill.own, customer=bill.customer, number=bill.number, month=month,
                    month_label=bill.month, onec_name=bill.onec_name, period_from=bill.period_from,
                )
            )
        if match is not None and match.state == "found" and match.contract_id is not None:
            table[(match.contract_id, month)].append(bill)
        else:
            (done if bill.group == done_group else bills).append(bill)
    current = month_key(today)
    first = min(months) if months else current
    contracts = _visible(session, workspace, _EVERYTHING)
    ledger = Ledger(session, workspace_id, contracts)
    docs = kort_bills(session, workspace_id)
    context = _context(session, workspace_id, contracts)
    accrued: set[tuple[uuid.UUID, str]] = set()
    for contract in contracts:
        schedule = ledger.schedule(contract, since=month_start(first), until=month_start(current))
        for month, accrual in schedule.by_month().items():
            if accrual.skipped or not (accrual.amount or 0):
                continue
            accrued.add((contract.id, month))
            owner = accrual.executor_id or contract.executor_id
            if owner not in switched:
                continue
            rows = table.pop((contract.id, month), [])
            row = rows[0] if rows else None
            doc = docs.get((contract.id, month))
            issued = doc is not None or any(item.group == done_group or item.bill_no for item in rows)
            group = done_group if issued else bills_module._group_by_date(accrual.period_from, today)
            text = "TRUE" if issued else words(group, accrual.period_from, today)
            if row is None:
                made = _made(contract, month, accrual, owner, doc, context, group, text)
            else:
                fields: dict[str, Any] = {"group": group, "text": text, "source": "kort", "contract_id": str(contract.id)}
                if not issued:
                    fields.update(amount=accrual.amount, period_from=accrual.period_from, period_to=accrual.period_to)
                elif doc is not None and not row.bill_no:
                    fields.update(bill_no=doc.number, bill_date=_shown(doc.issued_on))
                made = dataclasses.replace(row, **fields)
            (done if issued else bills).append(made)
            # Остальные строки месяца: выставленные - история, невыставленные - тот же счёт.
            done.extend(item for item in rows[1:] if item.group == done_group)
    for (contract_id, month), rows in table.items():
        for row in rows:
            if row.group == done_group:
                done.append(row)
            elif (contract_id, month) in accrued or matcher.entity_by_code.get(norm(row.own)) not in switched:
                bills.append(row)
    bills.sort(key=bills_module._order)
    codes = sorted(context.codes[entity] for entity in switched if entity in context.codes)
    return dataclasses.replace(board, bills=bills, done=done, issued={bill.key for bill in done}, kort=tuple(codes), kort_error="")


_boards: dict[uuid.UUID, tuple[tuple[Any, ...], Any]] = {}
_boards_guard = threading.Lock()


def _version(session: Session, workspace_id: uuid.UUID) -> Any:
    """Последняя правка данных компании: каждая правка пишет журнал (проверка обхода маршрутов)."""
    return session.scalar(
        sa.select(sa.func.max(ActionLog.at)).where(ActionLog.workspace_id == workspace_id, ActionLog.category.in_(("data", "import")))
    )


def bills_board(session: Session, workspace_id: uuid.UUID, board: Any, *, today: date | None = None) -> Any:
    """Сводка счетов с юрлицами, переведёнными на KORT; ни одного - та же сводка.

    Посчитанное держится до следующего чтения таблицы или правки данных: число
    у пункта «Счета» спрашивают на каждой странице.
    """
    from app.finance.contracts import bills as bills_module

    switched = kort_entities(session, workspace_id, "bills")
    if not switched:
        return board
    today = today or datetime.now(bills_module.TZ).date()
    key = (board.read_at, frozenset(switched), today, _version(session, workspace_id))
    with _boards_guard:
        cached = _boards.get(workspace_id)
    if cached is not None and cached[0] == key:
        return cached[1]
    built = build_bills(session, workspace_id, board, switched, today)
    with _boards_guard:
        _boards[workspace_id] = (key, built)
    return built


def forget(workspace_id: uuid.UUID) -> None:
    with _boards_guard:
        _boards.pop(workspace_id, None)
    with _paid_guard:
        _paid.pop(workspace_id, None)


def preview_bills(
    session: Session, workspace_id: uuid.UUID, entity_id: uuid.UUID, source: str, *, today: date | None = None, board: Any = None
) -> dict[str, Any]:
    """Что станет со списком счетов юрлица, если перевести его на `source`: появятся, уйдут, сменят срок.

    `board` - прочитанная сводка как есть (без пересчёта KORT); нет - из кэша чтения.
    """
    from app.finance.contracts import bills as bills_module

    if source not in SOURCES:
        raise FinanceError("Источник - таблица или KORT")
    entity = session.get(GroupEntity, entity_id)
    if entity is None or entity.workspace_id != workspace_id:
        raise FinanceError("Такого нашего юрлица нет")
    raw = board
    if raw is None:
        slot = bills_module.get(session, workspace_id)
        if slot is None:
            raise FinanceError("Сводная таблица компании не подключена")
        with bills_module._guard:
            raw, error = slot.board, slot.error
        if raw is not None:
            # Как его видят список и бот: со словами компании (`bills.prepared`).
            raw = bills_module.worded(raw, core_settings.table_words(session, workspace_id))
        if raw is None:
            raise FinanceError(error or "Сводная таблица ещё читается - попробуйте через минуту")
    today = today or datetime.now(bills_module.TZ).date()
    now = kort_entities(session, workspace_id, "bills")
    then = (now | {entity_id}) if source == "kort" else (now - {entity_id})
    before = build_bills(session, workspace_id, raw, now, today) if now else raw
    after = build_bills(session, workspace_id, raw, then, today) if then else raw
    code = norm(entity.code)
    mine = lambda bills: {bill.key: bill for bill in bills if norm(bill.own) == code}  # noqa: E731
    was, will = mine(before.bills), mine(after.bills)
    appear = [will[key] for key in will if key not in was]
    vanish = [was[key] for key in was if key not in will]
    regroup = [
        {"bill": will[key].out(), "before": was[key].group, "before_text": was[key].text}
        for key in will
        if key in was and was[key].group != will[key].group
    ]
    order = lambda bill: bills_module._order(bill)  # noqa: E731
    return {
        "entity": {"id": str(entity_id), "code": entity.code},
        "source": source,
        "counts": {
            "before": len(was),
            "after": len(will),
            "appear": len(appear),
            "vanish": len(vanish),
            "regroup": len(regroup),
            "overdue_before": sum(1 for bill in was.values() if bill.group == bills_module.OVERDUE),
            "overdue_after": sum(1 for bill in will.values() if bill.group == bills_module.OVERDUE),
        },
        "appear": [bill.out() for bill in sorted(appear, key=order)],
        "vanish": [bill.out() for bill in sorted(vanish, key=order)],
        "regroup": regroup,
    }


# ── «Оплачено» из KORT ───────────────────────────────────────────────────────

_paid: dict[uuid.UUID, tuple[tuple[Any, ...], dict[uuid.UUID, Decimal]]] = {}
_paid_guard = threading.Lock()


def kort_paid(session: Session, workspace_id: uuid.UUID, *, today: date | None = None) -> dict[uuid.UUID, Decimal]:
    """Оплачено по договорам в учёте KORT - для «Оплачено/Остаток» реестра у юрлиц на KORT.

    Считается целиком (меньше секунды на 500 договоров) и держится до правки
    данных или нового дня: реестр спрашивает его на каждом открытии.
    """
    from app.finance.contracts.service import today as contracts_today
    from app.finance.core.payments import Money

    day = today or contracts_today()
    key = (_version(session, workspace_id), day)
    with _paid_guard:
        cached = _paid.get(workspace_id)
    if cached is not None and cached[0] == key:
        return cached[1]
    workspace = session.get(Workspace, workspace_id)
    money = Money(session, workspace, today=day)
    values = {contract_id: money.paid_of(contract_id) for contract_id in money.contracts}
    with _paid_guard:
        _paid[workspace_id] = (key, values)
    return values


def preview_payments(
    session: Session, workspace_id: uuid.UUID, entity_id: uuid.UUID, source: str, *, today: date | None = None
) -> dict[str, Any]:
    """Что станет с «Оплачено» договоров юрлица после перехода: где таблица и KORT расходятся и на сколько."""
    from app.finance.contracts import summary as summary_module
    from app.finance.contracts.service import Registry

    if source not in SOURCES:
        raise FinanceError("Источник - таблица или KORT")
    entity = session.get(GroupEntity, entity_id)
    if entity is None or entity.workspace_id != workspace_id:
        raise FinanceError("Такого нашего юрлица нет")
    slot = summary_module.get(session, workspace_id)
    if slot is None or slot.index is None:
        raise FinanceError((slot.error if slot is not None else "") or "Сводная таблица компании не подключена")
    workspace = session.get(Workspace, workspace_id)
    registry = Registry(session, workspace)
    registry._summary = slot.index
    registry._kort_payments = set()  # таблица как есть - чтобы сравнить с KORT
    paid = kort_paid(session, workspace_id, today=today)
    contracts = list(
        session.scalars(sa.select(Contract).where(Contract.workspace_id == workspace_id, Contract.deleted_at.is_(None)))
    )
    registry.parties_for({party for item in contracts for party in (item.executor_id, item.customer_id)})
    items: list[dict[str, Any]] = []
    counts = {"contracts": 0, "same": 0, "differ": 0, "table_only": 0, "kort_only": 0}
    totals = {"table": Decimal("0"), "kort": Decimal("0")}
    for contract in contracts:
        ours = contract.executor_id if registry.is_own(contract.executor_id) else contract.customer_id
        if ours != entity_id:
            continue
        match = registry.summary_of(contract)
        table = match.paid if match is not None and match.state == "found" else None
        kort = paid.get(contract.id, Decimal("0"))
        if table is None and not kort:
            continue
        counts["contracts"] += 1
        totals["table"] += table or Decimal("0")
        totals["kort"] += kort
        if table is not None and table == kort:
            counts["same"] += 1
            continue
        code = "kort_only" if table is None else "table_only" if not kort else "differ"
        counts[code] += 1
        other = contract.customer_id if ours == contract.executor_id else contract.executor_id
        party = registry.parties_for([other]).get(other) if other else None
        items.append(
            {
                "id": str(contract.id),
                "number": contract.number,
                "customer": party.name if party is not None else "",
                "table": _money(table) if table is not None else None,
                "kort": _money(kort),
                "code": code,
            }
        )
    items.sort(key=lambda item: -abs(Decimal(item["kort"]) - Decimal(item["table"] or "0")))
    return {
        "entity": {"id": str(entity_id), "code": entity.code},
        "source": source,
        "counts": counts,
        "totals": {"table": _money(totals["table"]), "kort": _money(totals["kort"])},
        "items": items,
    }


def _money(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


__all__ = [
    "CLOSED_MONTHS",
    "LINKS",
    "LINK_TITLES",
    "SWITCHABLE",
    "bills_board",
    "build_bills",
    "forget",
    "kort_entities",
    "kort_paid",
    "link_tally",
    "preview_bills",
    "preview_payments",
    "set_source",
    "sources",
    "words",
]
