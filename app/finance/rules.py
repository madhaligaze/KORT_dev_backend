"""Автоправила: «в комментарии есть Magnum — это статья „Продукты“».

Зачем они нужны именно здесь
────────────────────────────
Выписка Kaspi Gold за год — это 2050 строк, где в каждой написано «Покупка ·
Magnum Cash&Carry» и сумма. Без раскладки по статьям отчёт о движении денег
покажет один столбец «Без категории» на два миллиона тенге, то есть ничего.
Размечать две тысячи строк руками никто не будет — и не должен.

Finmap решает это автоправилами, и решение правильное: правило задаётся один
раз и работает на всех будущих загрузках. Мы повторяем механику и добавляем к
ней то, чего у них нет: **правила показываются на предпросмотре импорта**, до
записи. Человек видит не «мы что-то разложили», а «вот эти 412 строк лягут в
„Продукты“ по правилу „Magnum“» — и может передумать до того, как это окажется
в учёте.

Устройство
──────────
Правило — это условия и действия. Условия соединяются «и» либо «или»
(`match`), действие проставляет статью, контрагента, проект или теги. Порядок
важен: правила применяются по `position`, первое сработавшее ставит значение,
следующие его не перетирают — иначе результат зависел бы от того, в каком
порядке лежат строки в таблице.

Чего правила не делают
──────────────────────
**Не меняют сумму, дату, счёт и вид операции.** Правило — это разметка, а не
исправление факта. Ошибиться в разметке не страшно: её видно и её правят.
Ошибка в сумме — это неверные деньги, и автоматике там не место.
"""
from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.layout import norm
from app.finance.models import Category, Counterparty, Operation, Project, Rule

log = logging.getLogger(__name__)

#: Поля операции, по которым можно спрашивать.
RULE_FIELDS = ("comment", "counterparty", "account", "amount", "kind")
#: Как спрашивать.
RULE_OPS = ("contains", "not_contains", "equals", "starts_with", "regex", "gt", "lt")
#: Что ставить.
RULE_TARGETS = ("category", "counterparty", "project", "tags")


class RuleError(RuntimeError):
    """Правило описано так, что применить его нельзя."""


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: str

    def check(self, row: dict[str, Any]) -> bool:
        raw = _field_value(row, self.field)
        if self.op in ("gt", "lt"):
            try:
                left = float(str(raw).replace(" ", "").replace(",", "."))
                right = float(str(self.value).replace(" ", "").replace(",", "."))
            except (TypeError, ValueError):
                return False
            return left > right if self.op == "gt" else left < right

        text = norm(raw if raw is not None else "")
        needle = norm(self.value)
        if self.op == "contains":
            return needle in text
        if self.op == "not_contains":
            return needle not in text
        if self.op == "equals":
            return text == needle
        if self.op == "starts_with":
            return text.startswith(needle)
        if self.op == "regex":
            try:
                return re.search(self.value, str(raw or ""), re.IGNORECASE) is not None
            except re.error:
                # Сломанное выражение — это не «правило не сработало», а
                # ошибка в правиле. Молчать нельзя, ронять импорт тоже.
                log.warning("finance: правило с негодным выражением: %r", self.value)
                return False
        return False


def _field_value(row: dict[str, Any], field: str) -> Any:
    """Значение поля строки.

    Счёт в строке импорта лежит в «со счёта» или «на счёт» — смотря куда шли
    деньги. Правилу это различие не нужно: оно спрашивает «какой счёт», а не
    «какая из двух колонок заполнена».
    """
    if field == "account":
        return row.get("account") or row.get("account_from") or row.get("account_to") or ""
    return row.get(field)


def _conditions(rule: Rule) -> list[Condition]:
    out: list[Condition] = []
    for item in rule.conditions or []:
        field = str(item.get("field", "comment"))
        op = str(item.get("op", "contains"))
        if field not in RULE_FIELDS or op not in RULE_OPS:
            continue
        out.append(Condition(field=field, op=op, value=str(item.get("value", ""))))
    return out


def matches(rule: Rule, row: dict[str, Any]) -> bool:
    """Подходит ли строка под правило."""
    conditions = _conditions(rule)
    if not conditions:
        return False
    if (rule.match or "all") == "any":
        return any(condition.check(row) for condition in conditions)
    return all(condition.check(row) for condition in conditions)


def list_rules(session: Session, workspace_id: uuid.UUID, *, only_active: bool = False) -> list[Rule]:
    query = sa.select(Rule).where(Rule.workspace_id == workspace_id)
    if only_active:
        query = query.where(Rule.active.is_(True))
    return list(session.scalars(query.order_by(Rule.position, Rule.created_at)))


def apply_to_row(rules: Sequence[Rule], row: dict[str, Any]) -> dict[str, Any]:
    """Разметить одну строку. Возвращает, что проставлено и каким правилом.

    Уже заполненное не трогаем: разметка из файла важнее нашей догадки — её
    поставил человек или банк, а мы только достраиваем недостающее.
    """
    applied: dict[str, Any] = {}
    for rule in rules:
        if not matches(rule, row):
            continue
        for target, value in (rule.actions or {}).items():
            if target not in RULE_TARGETS or not value:
                continue
            if row.get(target):
                continue  # значение уже есть — первое сработавшее правило не спорит
            if target in applied:
                continue  # поставило правило выше по порядку
            applied[target] = {"value": value, "rule": rule.name}
    return applied


def preview_rows(rules: Sequence[Rule], rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Разметить строки предпросмотра импорта прямо в них же.

    Возвращает счётчик «сколько строк разметило каждое правило» — по нему
    экран показывает «412 строк лягут в „Продукты“ по правилу „Magnum“».
    """
    counter: Counter[str] = Counter()
    for row in rows:
        applied = apply_to_row(rules, row)
        for target, item in applied.items():
            row[target] = item["value"]
            counter[item["rule"]] += 1
        if applied:
            row["applied_rules"] = sorted({item["rule"] for item in applied.values()})
    return dict(counter)


def apply_to_operations(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    only_uncategorized: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """Применить правила к уже заведённым операциям.

    Нужно ровно для случая «загрузили выписку за год, потом придумали правила».
    По умолчанию трогаем только неразмеченное: переразметка всего задним числом
    меняет отчёты, которые человек уже видел и, возможно, кому-то показал.
    """
    from app.finance import service

    rules = list_rules(session, workspace_id, only_active=True)
    if not rules:
        return {"rules": 0, "updated": 0, "by_rule": {}}

    query = sa.select(Operation).where(
        Operation.workspace_id == workspace_id,
        Operation.deleted_at.is_(None),
        Operation.kind != "transfer",
    )
    if only_uncategorized:
        query = query.where(Operation.category_id.is_(None))
    if limit:
        query = query.limit(limit)

    accounts = {
        account.id: account.name for account in service.list_accounts(session, workspace_id, with_archived=True)
    }
    counter: Counter[str] = Counter()
    updated = 0

    for operation in session.scalars(query):
        row = {
            "comment": operation.comment or "",
            "counterparty": "",
            "account": accounts.get(operation.account_from_id) or accounts.get(operation.account_to_id) or "",
            "amount": str(operation.amount),
            "kind": operation.kind,
            "category": "",
            "project": "",
        }
        applied = apply_to_row(rules, row)
        if not applied:
            continue

        side = "income" if operation.kind == "income" else "expense"
        touched = False
        if "category" in applied and operation.category_id is None:
            category = service.ensure_category(session, workspace_id, side, applied["category"]["value"])
            if category is not None:
                operation.category_id = category.id
                touched = True
        if "counterparty" in applied and operation.counterparty_id is None:
            role = "client" if operation.kind == "income" else "supplier"
            party = service.ensure_counterparty(
                session, workspace_id, applied["counterparty"]["value"], role=role
            )
            if party is not None:
                operation.counterparty_id = party.id
                touched = True
        if "project" in applied and operation.split_state == "none":
            project = service.ensure_project(session, workspace_id, applied["project"]["value"])
            if project is not None:
                from app.finance.models import OperationProject

                session.add(
                    OperationProject(
                        operation_id=operation.id,
                        project_id=project.id,
                        amount=operation.amount,
                    )
                )
                operation.split_state = "exact"
                touched = True

        if touched:
            operation.version += 1
            updated += 1
            for item in applied.values():
                counter[item["rule"]] += 1

    session.flush()
    for rule in rules:
        if counter.get(rule.name):
            rule.applied_count = (rule.applied_count or 0) + counter[rule.name]
    session.flush()
    return {"rules": len(rules), "updated": updated, "by_rule": dict(counter)}


#: Слова, которые в деталях выписки не называют магазин: банковские глаголы,
#: имена людей в переводах, служебные пометки. По ним правило не предлагается.
_NOISE = {
    "покупка", "перевод", "пополнение", "снятие", "платеж", "платёж", "оплата",
    "разное", "прочее", "зачисление", "списание", "комиссия", "kaspi", "gold",
}


def suggest(
    session: Session, workspace_id: uuid.UUID, *, limit: int = 12, min_count: int = 3
) -> list[dict[str, Any]]:
    """Что стоит превратить в правило — по неразмеченным операциям.

    Считаем частые «опорные слова» в комментариях операций без статьи. Это не
    искусственный интеллект и не выдаёт себя за него: просто частотность, но
    именно она отвечает на вопрос «с чего начать разметку двух тысяч строк».

    Предлагаем только то, что встречается не реже `min_count` раз: правило ради
    одной операции не экономит ничего, а список предложений превращает в шум.
    """
    rows = session.execute(
        sa.select(Operation.comment, Operation.kind, Operation.amount_base).where(
            Operation.workspace_id == workspace_id,
            Operation.deleted_at.is_(None),
            Operation.category_id.is_(None),
            Operation.kind != "transfer",
        )
    )
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for comment, kind, amount in rows:
        key_text = _keyword(comment or "")
        if not key_text:
            continue
        key = (kind, key_text)
        bucket = buckets.setdefault(
            key, {"keyword": key_text, "kind": kind, "count": 0, "amount": 0.0, "examples": []}
        )
        bucket["count"] += 1
        bucket["amount"] += float(amount or 0)
        if len(bucket["examples"]) < 3 and comment:
            bucket["examples"].append(comment[:80])

    ranked = [item for item in buckets.values() if item["count"] >= min_count]
    ranked.sort(key=lambda item: (item["amount"], item["count"]), reverse=True)
    for item in ranked:
        item["amount"] = f"{item['amount']:.2f}"
    return ranked[:limit]


def _keyword(comment: str) -> str:
    """Опорная часть комментария — название места, а не одно слово из него.

    Первая версия брала самое длинное слово, и на «Пополнение · С карты другого
    банка» предлагала правило по слову «другого». Правило по случайному слову
    ловит что попало, а человек видит подсказку, которой нельзя верить.

    У банков деталь операции стоит после разделителя: «Покупка · Magnum
    Cash&Carry» → «Magnum Cash&Carry». Её и берём целиком, обрезая номера карт
    и прочие хвосты цифр: они у каждой операции свои и правило по ним не
    соберётся.
    """
    text = comment.split("·")[-1] if "·" in comment else comment
    text = re.sub(r"[*•]{2,}\s*\d+", " ", text)  # «*6271» — номер карты
    text = re.sub(r"\s+", " ", text).strip(" \"«».,;:-")
    if len(text) < 4:
        return ""
    if norm(text) in _NOISE:
        return ""
    # Слишком длинную деталь («Оплата по договору №14 от 03.09.2026») в правило
    # целиком класть нельзя — она уникальна. Берём первые три слова.
    words = text.split(" ")
    if len(words) > 3:
        text = " ".join(words[:3])
    return text


def create_rule(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    name: str,
    conditions: list[dict[str, Any]],
    actions: dict[str, Any],
    match: str = "all",
    actor: str = "",
) -> Rule:
    clean_name = (name or "").strip()
    if not clean_name:
        raise RuleError("У правила должно быть название — по нему видно, что оно делало")
    if not conditions:
        raise RuleError("Правило без условий срабатывало бы на всём")
    if not any(target in RULE_TARGETS and value for target, value in (actions or {}).items()):
        raise RuleError("Правило без действия ничего не меняет")
    if match not in ("all", "any"):
        raise RuleError("Условия соединяются «и» либо «или»")

    top = session.scalar(
        sa.select(sa.func.max(Rule.position)).where(Rule.workspace_id == workspace_id)
    )
    rule = Rule(
        workspace_id=workspace_id,
        name=clean_name,
        match=match,
        conditions=conditions,
        actions=actions,
        position=int(top or 0) + 10,
        created_by=actor,
    )
    session.add(rule)
    session.flush()
    return rule


def delete_rule(session: Session, workspace_id: uuid.UUID, rule_id: uuid.UUID) -> None:
    rule = session.get(Rule, rule_id)
    if rule is None or rule.workspace_id != workspace_id:
        raise RuleError("Правило не найдено")
    session.delete(rule)
    session.flush()


def toggle_rule(session: Session, workspace_id: uuid.UUID, rule_id: uuid.UUID, active: bool) -> Rule:
    rule = session.get(Rule, rule_id)
    if rule is None or rule.workspace_id != workspace_id:
        raise RuleError("Правило не найдено")
    rule.active = active
    session.flush()
    return rule


__all__ = [
    "RULE_FIELDS",
    "RULE_OPS",
    "RULE_TARGETS",
    "Condition",
    "RuleError",
    "apply_to_operations",
    "apply_to_row",
    "create_rule",
    "delete_rule",
    "list_rules",
    "matches",
    "preview_rows",
    "suggest",
    "toggle_rule",
]
