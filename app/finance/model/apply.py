"""Предложить, применить, отменить.

Предложить
──────────
Команды исполняются по-настоящему, но в транзакции, которую маршрут потом
откатывает целиком (`propose` записи не оставляет - проверяет тест). Так
предложение показывает ровно то, что сделает «применить», а не то, что о
записи думает отдельный код предпросмотра: какие поля станут какими, какие
договоры заведутся, какие факты журнала появятся и снимутся, долг затронутых
договоров на сегодня до и после. Команда, которая откажет (нужен режим
«опечатка или с даты», чужая правка поля, нет права), - в `problems`
словами, остальные всё равно показаны.

`base` - отпечаток: договоры и их `seq` до команд. «Применить» с `expect`
сверяет его: если договор успел поменяться, записи нет, а ответ - 409 с
новым предложением.

Применить
─────────
Вся пачка - одна транзакция: откажет одна команда - не записано ничего.
Одна точка восстановления (`batch`): шаги - обратные шаги команд, а точки,
которые команды ставят сами (перевод клиентов), вливаются в неё же
(`restore.COLLECT`). Ответ - итог каждой команды, точка и курсор подписки
после записи.

Отменить
────────
По точке (`restore.restore_point`): пачка - шагами в обратном порядке; поле,
которое после точки поменял другой, не перезаписывается.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from app.finance.contracts import restore, service
from app.finance.model import VERSION
from app.finance.model.commands import Command, CommandError, Done, check_right, parse
from app.finance.model.context import Ctx
from app.finance.service import FinanceError

#: Команд в одной пачке: больше - это уже загрузка, у неё свой путь (приёмник).
LIMIT = 200


class Changed(FinanceError):
    """Договоры поменялись после предложения - записи нет, есть новое предложение."""

    def __init__(self, proposal: dict[str, Any]) -> None:
        super().__init__("После предложения договоры успели поменяться - посмотрите новое предложение")
        self.proposal = proposal


class Failed(FinanceError):
    """Команда пачки отказала - ничего не записано; номер команды и причина."""

    def __init__(self, index: int, command: str, exc: Exception) -> None:
        super().__init__(f"Команда {index + 1} ({command}): {exc}")
        self.index, self.command, self.cause = index, command, exc


def _parse_all(ctx: Ctx, raw: Any) -> list[tuple[Command, Any]]:
    items = raw if isinstance(raw, list) else [raw]
    if not items:
        raise CommandError("Пачка пуста")
    if len(items) > LIMIT:
        raise CommandError(f"В пачке не больше {LIMIT} команд")
    parsed = []
    for index, item in enumerate(items):
        try:
            command, params = parse(item)
        except CommandError as exc:
            raise CommandError(f"Команда {index + 1}: {exc}") from exc
        parsed.append((command, params))
    return parsed


def _seqs(ctx: Ctx, ids: set[str]) -> dict[str, int]:
    wanted = []
    for raw in ids:
        try:
            wanted.append(uuid.UUID(raw))
        except ValueError:
            continue
    if not wanted:
        return {}
    rows = ctx.session.execute(
        sa.select(service.Contract.id, service.Contract.seq).where(
            service.Contract.workspace_id == ctx.workspace.id, service.Contract.id.in_(wanted)
        )
    ).all()
    return {str(row.id): int(row.seq or 0) for row in rows}


def fingerprint(seqs: dict[str, int]) -> str:
    raw = json.dumps(sorted(seqs.items()), separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _state(ctx: Ctx, ids: list[str], day: date) -> dict[str, dict[str, Any]]:
    """Договоры как их видит человек, факты журнала и долг на день - для «до» и «после»."""
    from app.finance.core import journal
    from app.finance.core.accruals import Ledger
    from app.finance.core.payments import Money
    from app.finance.core.registers import Settlement

    out: dict[str, dict[str, Any]] = {}
    contracts = []
    for raw in ids:
        try:
            contract = ctx.session.get(service.Contract, uuid.UUID(raw))
        except ValueError:
            continue
        if contract is None or contract.workspace_id != ctx.workspace.id:
            continue
        try:
            body = service.one(ctx.session, ctx.workspace, ctx.access, contract)["contract"]
        except service.NotFound:
            continue
        out[raw] = {"values": body.get("values") or {}, "deleted": bool(body.get("deleted")), "seq": body.get("seq"), "debt": None, "facts": {}}
        contracts.append(contract)
    if not contracts:
        return out
    money_open = not ({"amount", "paid", "remaining"} & ctx.access.hidden)
    if money_open:
        live = [item for item in contracts if item.deleted_at is None]
        if live:
            money = Money(ctx.session, ctx.workspace, today=day, only=[item.id for item in live])
            ledger = Ledger(ctx.session, ctx.workspace.id, live)
            for item in live:
                settled = Settlement.of(money.contract(item, ledger))
                if settled.accrues and settled.since is not None:
                    out[str(item.id)]["debt"] = _money(settled.debt_on(day))
        from app.finance.core.models import FACT_KINDS

        journal.ensure(ctx.session, ctx.workspace.id)
        for fact in journal.current(ctx.session, ctx.workspace.id, FACT_KINDS, contracts=[item.id for item in contracts]):
            out[str(fact.contract_id)]["facts"][fact.key] = {
                "kind": fact.kind, "day": fact.day.isoformat() if fact.day else None, "amount": _money(fact.amount), "seq": fact.seq,
            }
    return out


def _diff(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for contract_id in sorted(set(before) | set(after)):
        was, now = before.get(contract_id), after.get(contract_id)
        entry: dict[str, Any] = {"id": contract_id}
        if was is None and now is not None:
            entry["created"] = True
        if now is not None and now.get("deleted") and not (was or {}).get("deleted"):
            entry["deleted"] = True
        values_was = (was or {}).get("values") or {}
        values_now = (now or {}).get("values") or {}
        fields = {
            key: {"before": values_was.get(key), "after": values_now.get(key)}
            for key in sorted(set(values_was) | set(values_now))
            if values_was.get(key) != values_now.get(key)
        }
        if fields:
            entry["fields"] = fields
        debt_was, debt_now = (was or {}).get("debt"), (now or {}).get("debt")
        if debt_was != debt_now:
            entry["debt"] = {"before": debt_was, "after": debt_now}
        facts_was = (was or {}).get("facts") or {}
        facts_now = (now or {}).get("facts") or {}
        added = [{"key": key, **facts_now[key]} for key in sorted(set(facts_now) - set(facts_was))]
        ended = [{"key": key, **facts_was[key]} for key in sorted(set(facts_was) - set(facts_now))]
        changed = [
            {"key": key, **facts_now[key]}
            for key in sorted(set(facts_now) & set(facts_was))
            if {k: v for k, v in facts_now[key].items() if k != "seq"} != {k: v for k, v in facts_was[key].items() if k != "seq"}
        ]
        if added or ended or changed:
            entry["facts"] = {"added": added, "ended": ended, "changed": changed}
        if len(entry) > 1:
            out.append(entry)
    return out


def _problem(index: int, command: Command, exc: Exception) -> dict[str, Any]:
    item: dict[str, Any] = {"command": index, "name": command.name, "detail": str(exc)}
    if isinstance(exc, service.ModeRequired):
        item.update(code="mode_required", fields=list(exc.fields))
    elif isinstance(exc, service.FieldConflict):
        item.update(code="conflict", conflicts=list(exc.conflicts))
    elif isinstance(exc, PermissionError):
        item["code"] = "forbidden"
    elif isinstance(exc, service.NotFound):
        item["code"] = "not_found"
    else:
        item["code"] = "refused"
    return item


def _run(ctx: Ctx, command: Command, params: Any) -> Done:
    check_right(ctx, command)
    return command.run(ctx, params)


def _begin(ctx: Ctx) -> None:
    """Открыть транзакцию по-настоящему до первой точки сохранения команд.

    pysqlite начинает транзакцию только перед изменением, и `SAVEPOINT`
    внутри команды (перевод клиентов, удаление пачкой) на SQLite открывал бы
    её сам, а `RELEASE` - коммитил: прогон «предложить» записывался бы.
    Пустое изменение счётчиков начинает её заранее; Postgres это не задевает.
    """
    from app.finance.contracts.models import Counter

    if ctx.session.get_bind().dialect.name == "sqlite":
        ctx.session.execute(sa.update(Counter).where(sa.false()).values(value=Counter.value))


def _reset(ctx: Ctx) -> None:
    """Откатить прогон и забыть посчитанное в нём: регистры и журнал сессии - уже о несбывшемся."""
    from app.finance.core import journal

    ctx.session.rollback()
    ctx.session.info.pop(journal.REGISTERS, None)
    _begin(ctx)


def propose(ctx: Ctx, raw: Any) -> dict[str, Any]:
    """Что сделают команды - прогоном. Маршрут откатывает транзакцию после ответа.

    Своих точек сохранения на команду здесь нет: в SQLite `SAVEPOINT` вне
    начатой транзакции открывает её сам, и `RELEASE` её коммитит - прогон
    записывался (28.10.2026, тест «предложение без следа»; см. `_begin`).
    Отказала команда - прогон откатывается и идёт заново без неё.
    """
    from app.finance.contracts.service import today

    _begin(ctx)
    parsed = _parse_all(ctx, raw)
    day = today()
    problems: dict[int, dict[str, Any]] = {}
    while True:
        touched = {contract for command, params in parsed for contract in command.touches(params)}
        base = _seqs(ctx, touched)
        before = _state(ctx, sorted(touched), day)
        results: list[dict[str, Any]] = []
        failed: tuple[int, Command, Exception] | None = None
        for index, (command, params) in enumerate(parsed):
            if index in problems:
                continue
            try:
                done = _run(ctx, command, params)
            except (FinanceError, PermissionError, service.NotFound) as exc:
                failed = (index, command, exc)
                break
            touched.update(done.contracts)
            results.append({"command": index, "name": command.name, "title": done.title, "contracts": done.contracts})
        if failed is None:
            break
        problems[failed[0]] = _problem(*failed)
        _reset(ctx)
    after = _state(ctx, sorted(touched), day)
    return {
        "version": VERSION,
        "as_of": day.isoformat(),
        "commands": results,
        "problems": [problems[index] for index in sorted(problems)],
        "changes": _diff(before, after),
        "base": fingerprint(base),
        "ready": not problems,
    }


def apply(ctx: Ctx, raw: Any, *, expect: str | None = None) -> dict[str, Any]:
    """Пачка одной транзакцией и одной точкой; `expect` - отпечаток предложения."""
    from app.finance.model.stream import cursor_now

    actor = ctx.actor
    if actor is None:
        raise PermissionError("Записывать может только вошедший")
    _begin(ctx)
    parsed = _parse_all(ctx, raw)
    if expect:
        touched = {contract for command, params in parsed for contract in command.touches(params)}
        if fingerprint(_seqs(ctx, touched)) != expect:
            # Новое предложение - тем же прогоном; маршрут откатит его вместе с отказом.
            raise Changed(propose(ctx, raw))
    collected: list[Any] = []
    token = restore.COLLECT.set(collected)
    results: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    try:
        for index, (command, params) in enumerate(parsed):
            mark = len(collected)
            try:
                done = _run(ctx, command, params)
            except (FinanceError, PermissionError, service.NotFound) as exc:
                # Записанное прежними командами пачки откатит маршрут вместе с отказом.
                raise Failed(index, command.name, exc) from exc
            # Точки, которые поставила сама команда, - шагами пачки.
            for point in collected[mark:]:
                steps.append({"kind": point.kind, "payload": point.payload or {}, "title": point.title})
            steps.extend({"kind": step.kind, "payload": step.payload, "title": done.title} for step in done.steps)
            results.append({"command": index, "name": command.name, "title": done.title, "contracts": done.contracts, "result": done.result})
    finally:
        restore.COLLECT.reset(token)
    for point in collected:
        ctx.session.delete(point)
    point_out = None
    if steps:
        titles = [item["title"] for item in results]
        title = titles[0] if len(titles) == 1 else f"{len(titles)} изменений: " + "; ".join(titles[:3]) + ("…" if len(titles) > 3 else "")
        point = restore._point(ctx.session, ctx.workspace, actor, "batch", title, {"steps": steps})
        point_out = {"id": str(point.id), "title": point.title}
    ctx.session.flush()
    return {"version": VERSION, "commands": results, "point": point_out, "cursor": cursor_now(ctx)}


def undo(ctx: Ctx, point_id: uuid.UUID) -> dict[str, Any]:
    from app.finance.model.stream import cursor_now

    if ctx.actor is None:
        raise PermissionError("Возвращать может только вошедший")
    out = restore.restore_point(ctx.session, ctx.workspace, ctx.access, ctx.actor, point_id)
    ctx.session.flush()
    return {"version": VERSION, **out, "cursor": cursor_now(ctx)}


def points(ctx: Ctx, *, limit: int = 60) -> dict[str, Any]:
    if ctx.actor is None:
        raise PermissionError("Точки восстановления - у вошедшего")
    return {"version": VERSION, **restore.listing(ctx.session, ctx.workspace, ctx.access, ctx.actor, limit=limit)}


__all__ = ["Changed", "Failed", "LIMIT", "apply", "fingerprint", "points", "propose", "undo"]
