"""Вызовы ядра - черновик API модели (этап 2, шаг 1; целиком - шаг 7).

Ядро как операционная система: оболочки (веб, десктоп, бот, выгрузка, будущий
холст, чужой сервис) приходят и уходят, а вызовы остаются. Сейчас у KORT 252
маршрута, нарезанных под экраны; новый фронт пришлось бы писать под них.
Вызовов семь, маршрутов у них будет больше, но новый экран должен складываться
из них без новой работы на сервере.

1. **Спросить** - вид на дату: отбор, меры, группировка. «Долг по разовым
   договорам ЮО на 01.10 по сотрудникам». Сейчас - отдельный расчёт на экран
   (`Debts`, `Revenue`, `Turnover`); станет одним вызовом по регистрам итогов
   (шаг 2) и видам-данным (шаг 6).
2. **Объяснить** - любое число до фактов, из которых оно сложилось. Здесь
   работает для долга договора по начислениям (`explain`): сальдо, начисления
   месяцев с соглашениями и поправками, оплаты с разнесением - каждый кусок
   со ссылкой на факт журнала.
3. **Предложить** - что изменится, до записи: из источника (сверка, «Первые
   шаги», перенос сальдо) или из действия словами (перевод клиентов). Сейчас -
   у каждого экрана своё; станет приёмником (шаг 3).
4. **Применить** - предложение или команда; пачка - одна точка
   восстановления. Сейчас - `changes.apply`, `opening.apply`, перевод клиентов.
5. **Отменить** - по точке восстановления (`contracts/restore.py`) - есть.
6. **Подписаться** - поток изменений с курсора. Здесь работает выборкой
   (`changes`): факты журнала после номера проведения - бот, десктоп и холст
   узнают, что изменилось, не перечитывая всё.
7. **Описать** - схема компании: поля, смыслы, словарь, виды, права. Сейчас -
   схема реестра; виды и словарь станут данными (шаги 6 и 8).

Кроме того здесь - «что было с договором» (`contract_facts`): все факты
договора одним списком, с версиями. Раньше на этот вопрос отвечали шесть
таблиц каждая по-своему.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.contracts.models import Contract
from app.finance.contracts.service import Access
from app.finance.core import journal
from app.finance.core.accruals import Ledger, month_key, month_start
from app.finance.core.models import Fact
from app.finance.core.payments import AHEAD_MONTHS, OPENING, Money, _shift
from app.finance.models import Counterparty, Workspace
from app.finance.service import FinanceError

#: Сколько фактов отдаёт одна выборка «что изменилось».
CHANGES_LIMIT = 500


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _check_money(access: Access) -> None:
    if not access.view or "paid" in access.hidden:
        raise PermissionError("Факты договора с деньгами вам не открыты - нужен доступ к журналу")


class Labels:
    """Подписи фактов: стороны и наши юрлица - именем, автор - именем или почтой."""

    def __init__(self, session: Session, workspace: Workspace, facts: list[Fact]) -> None:
        from app.finance.accounts_model import FinanceUser

        self.names = dict(
            session.execute(sa.select(Counterparty.id, Counterparty.name).where(Counterparty.workspace_id == workspace.id)).all()
        )
        authors = {fact.author for fact in facts if fact.author is not None}
        self.users = {
            row.id: row.full_name or row.email or ""
            for row in (session.scalars(sa.select(FinanceUser).where(FinanceUser.id.in_(list(authors)))) if authors else [])
        }

    def fact(self, fact: Fact) -> dict[str, Any]:
        return {
            "id": str(fact.id),
            "key": fact.key,
            "seq": fact.seq,
            "kind": fact.kind,
            "source": fact.source,
            "day": fact.day.isoformat() if fact.day else None,
            "month": month_key(fact.period) if fact.period else None,
            "amount": _money(fact.amount),
            "contract_id": str(fact.contract_id) if fact.contract_id else None,
            "party": self.names.get(fact.party_id, "") if fact.party_id else "",
            "entity": self.names.get(fact.entity_id, "") if fact.entity_id else "",
            "data": fact.data or {},
            "author": self.users.get(fact.author, "") if fact.author else "",
            "recorded_at": fact.recorded_at.isoformat() if fact.recorded_at else None,
            "replaces": str(fact.replaces) if fact.replaces else None,
            "void": fact.void,
            "ended_seq": fact.ended_seq,
        }


# ── Что было с договором ─────────────────────────────────────────────────────


def contract_facts(
    session: Session, workspace: Workspace, access: Access, contract: Contract, *, history: bool = False
) -> dict[str, Any]:
    """Факты договора: действующие по дате, а с `history` - и прежние версии и снятые."""
    _check_money(access)
    journal.ensure(session, workspace.id)
    query = sa.select(Fact).where(Fact.workspace_id == workspace.id, Fact.contract_id == contract.id)
    if not history:
        query = query.where(Fact.ended_seq.is_(None), Fact.void.is_(False))
    facts = list(session.scalars(query))
    facts.sort(key=lambda fact: (fact.day or fact.period or date.min, fact.kind, fact.key, fact.seq))
    labels = Labels(session, workspace, facts)
    return {
        "contract_id": str(contract.id),
        "number": contract.number,
        "history": history,
        "cursor": _cursor_now(session, workspace.id),
        "facts": [labels.fact(fact) for fact in facts],
    }


# ── Подписаться: что изменилось после номера ─────────────────────────────────


def _cursor_now(session: Session, workspace_id: uuid.UUID) -> int:
    from app.finance.contracts.fields import current

    return current(session, workspace_id, journal.SEQ)


def changes(session: Session, workspace: Workspace, access: Access, *, since: str = "0", limit: int = CHANGES_LIMIT) -> dict[str, Any]:
    """Факты, записанные после курсора: новые, новые версии и пометки «снят».

    Курсор - номер проведения (`12`) или номер и ключ (`12:op:…`), если одно
    проведение не уместилось в выборку. Номер берётся до выборки: проведение,
    закоммиченное между ними, придёт следующим запросом, а не пропадёт.
    """
    if not access.setup:
        raise PermissionError("Поток фактов всей компании - владельцу и администратору")
    journal.ensure(session, workspace.id)
    seq_text, _, after = (since or "0").partition(":")
    try:
        seq = int(seq_text or 0)
    except ValueError as exc:
        raise FinanceError("Курсор - номер проведения или «номер:ключ»") from exc
    now = _cursor_now(session, workspace.id)
    condition = Fact.seq > seq
    if after:
        condition = sa.or_(Fact.seq > seq, sa.and_(Fact.seq == seq, Fact.key > after))
    rows = list(
        session.scalars(
            sa.select(Fact)
            .where(Fact.workspace_id == workspace.id, Fact.seq <= now, condition)
            .order_by(Fact.seq, Fact.key)
            .limit(limit + 1)
        )
    )
    more = len(rows) > limit
    rows = rows[:limit]
    if more:
        cursor = f"{rows[-1].seq}:{rows[-1].key}"
    else:
        cursor = str(now if not rows else max(now, rows[-1].seq))
    labels = Labels(session, workspace, rows)
    return {"cursor": cursor, "more": more, "facts": [labels.fact(fact) for fact in rows]}


# ── Объяснить: долг договора по начислениям ──────────────────────────────────


def explain(session: Session, workspace: Workspace, access: Access, contract: Contract, *, day: date) -> dict[str, Any]:
    """Долг договора на день по начислениям - по кускам, каждый со ссылкой на факт.

    Долг = входящее сальдо + начисления, чей период начался до дня, - оплаты до
    дня (`core/payments.py`). Начисление - из условий договора на день периода:
    соглашения, вступившие к этому дню, и поправка месяца - факты журнала.
    """
    _check_money(access)
    # Оплаты одного договора: «объяснить» спрашивают на каждое число экрана.
    money = Money(session, workspace, today=day, only=[contract.id])
    item = money.contract(contract)
    if money.since is None or not item.accrues:
        return {"day": day.isoformat(), "since": money.since.isoformat() if money.since else None, "known": False}
    facts = journal.current(session, workspace.id, ("agreement", "adjustment", "opening"), contracts=[contract.id])
    agreements = sorted((fact for fact in facts if fact.kind == "agreement" and fact.day), key=lambda fact: (fact.day, fact.key))
    adjustments = {month_key(fact.period): fact.key for fact in facts if fact.kind == "adjustment" and fact.period}
    opening = next(
        (fact for fact in facts if fact.kind == "opening" and fact.period == money.since and journal.basis_of(fact) == "accruals"),
        None,
    )
    last = max([day, *(payment.paid_at for payment in item.payments)])
    schedule = Ledger(session, workspace.id, [contract]).schedule(
        contract, since=money.since, until=month_start(_shift(month_key(last), AHEAD_MONTHS))
    )
    accrued = []
    for accrual in schedule.accruals:
        if accrual.period_from > day:
            continue
        accrued.append(
            {
                "month": accrual.month,
                "period_from": accrual.period_from.isoformat(),
                "amount": _money(Decimal("0") if accrual.skipped else accrual.amount),
                "base": _money(accrual.base),
                "agreements": [fact.key for fact in agreements if fact.day <= accrual.period_from],
                "adjustment": adjustments.get(accrual.month),
            }
        )
    paid = [payment for payment in item.payments if payment.paid_at <= day]
    parts: dict[str, list[dict[str, Any]]] = {}
    for key, bucket in item.buckets.items():
        for part in bucket.parts:
            parts.setdefault(part.payment.key, []).append(
                {"month": None if key == OPENING else key, "amount": _money(part.amount), "by": part.by}
            )
    for part in item.over:
        parts.setdefault(part.payment.key, []).append({"month": None, "amount": _money(part.amount), "by": part.by})
    opening_amount = item.opening or Decimal("0")
    total_accrued = sum((Decimal(row["amount"] or "0") for row in accrued), Decimal("0"))
    total_paid = sum((payment.amount for payment in paid), Decimal("0"))
    return {
        "day": day.isoformat(),
        "since": money.since.isoformat(),
        "known": True,
        "debt": _money(item.debt_on(day)),
        "opening": {"amount": _money(item.opening), "fact": opening.key if opening is not None else None},
        "accrued": _money(total_accrued),
        "paid": _money(total_paid),
        "check": _money(opening_amount + total_accrued - total_paid),
        "accruals": accrued,
        "payments": [
            {"fact": payment.key, "paid_at": payment.paid_at.isoformat(), "amount": _money(payment.amount), "source": payment.source,
             "parts": parts.get(payment.key, [])}
            for payment in sorted(paid, key=lambda payment: (payment.paid_at, payment.key))
        ],
    }


__all__ = ["CHANGES_LIMIT", "Labels", "changes", "contract_facts", "explain"]
