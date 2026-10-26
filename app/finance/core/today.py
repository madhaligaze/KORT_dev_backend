"""«Сегодня» - главная (фаза 8): что сделать, числами из итогов ядра.

Каждая карточка - свой запрос (`GET /core/today/{card}`): медленная (сверка
читает всю сводную таблицу) не держит быструю, и страница собирается по мере
ответов. Числа - из тех же модулей, что и разделы, куда карточка ведёт: долг
карточки и долг раздела «Долги» - одно число, иначе карточке перестали бы
верить с первого же расхождения.

Карточка: заголовок, главное число (сумма или счёт), строки с числами и
переход - раздел и то, что в нём выбрать (`go`: раздел и значения состояния
экрана). Делать нечего - карточка честно это говорит (`empty`), а не
прячется: пропавшую карточку принимали бы за сломанную.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.finance.contracts.service import Access
from app.finance.core import book as book_module
from app.finance.core.accruals import month_key, month_start
from app.finance.models import Workspace

_ZERO = Decimal("0")
CARDS = ("debts", "payments", "acts", "vat", "shadow", "participants")


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _card(key: str, title: str, *, go: dict[str, Any], **values: Any) -> dict[str, Any]:
    return {"key": key, "title": title, "go": go, "lines": [], "amount": None, "count": None, "unit": "", "empty": "", "note": "", **values}


def debts_card(session: Session, workspace: Workspace, access: Access, *, today: date) -> dict[str, Any]:
    """Нам должны: неоплаченное, старше 90 дней и самые большие должники."""
    from app.finance.core.debts import Debts

    debts = Debts(session, workspace, access, day=today)
    totals = debts.totals()
    old = [item for item in debts.items.values() if item.aging.get("d90plus")]
    middle = sum((item.aging.get("d60", _ZERO) + item.aging.get("d90", _ZERO) for item in debts.items.values()), _ZERO)
    rows = [row for row in debts.rows("client") if not row["special"] and Decimal(row["owed"]) > 0][:3]
    card = _card(
        "debts", "Нам должны",
        go={"section": "debts", "session": {"core.debts.view": "contracts", "core.debts.side": "receivable", "core.debts.group": "client", "core.debts.basis": "accruals"}},
        amount=totals["owed"], count=sum(1 for item in debts.items.values() if item.owed > 0), unit="договоров",
    )
    if not Decimal(totals["owed"]):
        card["empty"] = "Долгов по договорам нет"
        return card
    card["lines"] = [
        {"label": "больше 90 дней", "amount": totals["aging"].get("d90plus"), "count": len(old)},
        {"label": "31-90 дней", "amount": _money(middle)},
        *({"label": row["title"], "amount": row["owed"]} for row in rows),
    ]
    if Decimal(totals["advance"]):
        card["note"] = f"аванс {totals['advance']}"
    return card


def payments_card(session: Session, workspace: Workspace, access: Access, *, today: date) -> dict[str, Any]:
    """Поступления без договора: деньги пришли, а чьи - KORT не знает."""
    from app.finance.core.payments import Money

    money = Money(session, workspace, today=today)
    items = money.without_contract()
    total = sum((Decimal(item["amount"]) for item in items), _ZERO)
    disputed = sum(1 for item in items if item.get("disputed"))
    card = _card(
        "payments", "Оплаты без договора",
        go={"section": "shadow", "session": {"core.shadow.what": "payments", "core.shadow.paid.view": "without"}},
        amount=_money(total), count=len(items), unit="поступлений",
    )
    if not items:
        card["empty"] = "Все поступления разнесены по договорам"
        return card
    with_candidates = sum(1 for item in items if item.get("candidates"))
    card["lines"] = [
        {"label": "спорные - номер и плательщик спорят", "count": disputed},
        {"label": "есть кандидаты - решить в один щелчок", "count": with_candidates},
    ]
    return card


def acts_card(session: Session, workspace: Workspace, access: Access, *, today: date) -> dict[str, Any]:
    """АВР: не подписаны, без ЭСФ после срока, месяц начислен - акта нет."""
    from app.finance.core import acts as acts_module
    from app.finance.core.registers import ledger_on
    from app.finance.core.shadow import _visible

    # Ждать таблицу, как раздел: без её АВР «месяц без АВР» был бы втрое больше правды.
    book, _error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
    acts = acts_module.Acts(session, workspace, access, book, today=today)
    visible = _visible(session, workspace, access)
    contracts = {item.id: item for item in visible}
    until = acts_module.previous_month(today)
    window = [month for month in (acts.reconciler.window if acts.reconciler else []) if month < month_key(today)]
    since = month_start(window[0]) if window else month_start(month_key(until.replace(day=1) - timedelta(days=150)))
    accrued = acts_module.accrued_months(ledger_on(session, workspace, visible, day=today), visible, since=since, until=until)
    work = acts.worklists(contracts, accrued)
    count = len(work["unsigned"]) + len(work["no_esf"]) + len(work["missing"])
    card = _card(
        "acts", "АВР и ЭСФ",
        go={"section": "shadow", "session": {"core.shadow.what": "acts", "core.shadow.acts.view": "missing"}},
        count=count, unit="дел",
    )
    if book is None:
        card["note"] = "сводная таблица ещё читается - АВР только KORT и 1С"
    if not count:
        card["empty"] = "Акты подписаны, ЭСФ выписаны, у каждого месяца есть АВР"
        return card
    card["lines"] = [
        {"label": "месяц начислен - АВР нет", "count": len(work["missing"]), "go": {"core.shadow.acts.view": "missing"}},
        {"label": "АВР не подписан клиентом", "count": len(work["unsigned"]), "go": {"core.shadow.acts.view": "unsigned"}},
        {"label": f"ЭСФ не выписан дольше {acts_module.ESF_DAYS} дней", "count": len(work["no_esf"]), "go": {"core.shadow.acts.view": "no_esf"}},
    ]
    return card


def vat_card(session: Session, workspace: Workspace, access: Access, *, today: date) -> dict[str, Any]:
    """НДС: режим не записан, порог пройден или скоро - по юрлицам, которые не плательщики."""
    from app.finance.core import vat

    turnover = vat.Turnover(session, workspace, access, today.year, today=today)
    years = turnover.run()
    threshold = turnover.tax_year.threshold
    unknown = [item for item in years.values() if item.status == "unknown" and item.forecast > 0]
    soon_limit = month_key(today + timedelta(days=92))
    lines: list[dict[str, Any]] = []
    attention = {item.entity_id for item in unknown}
    for item in sorted(years.values(), key=lambda row: -row.forecast):
        if item.status in ("payer",):
            continue
        crossing = item.crossing(threshold)
        if crossing is None:
            continue
        name = item.code or item.name
        if crossing <= month_key(today):
            lines.append({"label": f"{name}: порог пройден", "month": crossing, "regime": item.status})
            attention.add(item.entity_id)
        elif crossing <= soon_limit:
            lines.append({"label": f"{name}: порог впереди", "month": crossing, "regime": item.status})
            attention.add(item.entity_id)
    # Число - разных юрлиц, которым нужен взгляд: «режим не записан» и «порог пройден» у одного - одно.
    card = _card(
        "vat", "НДС",
        go={"section": "vat", "session": {}},
        count=len(attention), unit="юрлиц",
    )
    if unknown:
        lines.insert(0, {"label": "режим НДС не записан", "count": len(unknown), "names": [item.code or item.name for item in unknown][:6]})
    if not lines:
        card["empty"] = "Режимы записаны, до порога далеко"
        return card
    card["lines"] = lines[:6]
    return card


def shadow_card(session: Session, workspace: Workspace, access: Access, *, today: date) -> dict[str, Any]:
    """Сверка начислений с таблицей: предложения дописать условия и договоры таблицы, которых нет в реестре."""
    from app.finance.core.shadow import Reconciler

    card = _card(
        "shadow", "Сверка с таблицей",
        go={"section": "shadow", "session": {"core.shadow.what": "accruals", "core.shadow.view": "proposals"}},
        unit="дел",
    )
    book, error = book_module.current(session, workspace.id, wait=book_module.FIRST_READ_WAIT)
    if book is None:
        card["empty"] = error or "Сводная таблица ещё читается"
        return card
    shadow = Reconciler(session, workspace, access, book).run()
    counts = shadow.counts()
    # Дела сверки - то, что решает человек: предложение, расходящийся месяц без
    # предложения («Верно у KORT» или правка договора), договор таблицы без пары.
    # Месяц только с подсказкой - тоже дело: без него карточка говорила бы
    # «совпадают», пока месяцы расходятся.
    proposed = {(proposal.contract_id, month) for proposal in shadow.proposals() for month in proposal.months}
    alone = sum(
        1 for line in shadow.lines() if line.codes and line.verified is None and (line.contract_id, line.month) not in proposed
    )
    card["count"] = counts["proposals"] + alone + counts["unknown"]
    card["lines"] = [
        {"label": "договоров совпадает с таблицей", "count": counts["clean"], "of": counts["contracts"]},
        {"label": "предложений дописать условия", "count": counts["proposals"], "go": {"core.shadow.view": "proposals"}},
        {"label": "месяцев расходится без предложения", "count": alone, "go": {"core.shadow.view": "lines", "core.shadow.code": "alone"}},
        {"label": "договоров таблицы нет в реестре", "count": counts["unknown"], "go": {"core.shadow.view": "unknown"}},
    ]
    if not card["count"]:
        card["empty"] = "Начисления KORT и таблицы совпадают"
    return card


def participants_card(session: Session, workspace: Workspace, access: Access, *, today: date) -> dict[str, Any]:
    """Участники: двойники и стороны без БИН."""
    from app.finance.core import participants

    counts = participants.overview(session, workspace, access)
    card = _card(
        "participants", "Участники",
        go={"section": "participants", "session": {"core.parties.view": "twins" if counts["twins"] else "all", "core.parties.q": ""}},
        count=counts["twins"] + counts["proposals"], unit="дел",
    )
    lines = [
        {"label": "двойники - один клиент под двумя именами", "count": counts["twins"], "go": {"core.parties.view": "twins"}},
        {"label": "БИН найден в источниках - подтвердить", "count": counts["proposals"], "go": {"core.parties.view": "all"}},
        {"label": "без БИН", "count": counts["no_bin"], "of": counts["all"], "go": {"core.parties.view": "no_bin"}},
    ]
    card["lines"] = [line for line in lines if line["count"]]
    if not counts["twins"] and not counts["proposals"]:
        card["empty"] = "Двойников нет"
    return card


BUILDERS: dict[str, Callable[..., dict[str, Any]]] = {
    "debts": debts_card,
    "payments": payments_card,
    "acts": acts_card,
    "vat": vat_card,
    "shadow": shadow_card,
    "participants": participants_card,
}


__all__ = ["BUILDERS", "CARDS"]
