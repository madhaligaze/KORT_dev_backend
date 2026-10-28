"""Подписаться - что изменилось после курсора, одним курсором на всё.

До шага 7 у KORT было три опроса со своими номерами: реестр
(`/contracts/changes`, счётчик `contracts`), лист «Таблица» (`/grid/changes`,
`operations`), журнал фактов (`/core/facts`, `facts`), и отдельно номер схемы
(`schema`). Клиенту, которому нужно всё сразу (холст, бот, десктоп), пришлось
бы держать четыре курсора. Здесь курсор один: четыре номера в одной строке.

Курсор непрозрачен (base64url от JSON), но устроен просто: `c` - договоры,
`f` (и `fk` - ключ внутри проведения, если оно не уместилось) - факты, `o` -
операции, `s` - схема. Пустой курсор - «с этой минуты»: ответ без изменений и
курсор на сейчас; что есть сейчас, клиент читает вызовом «спросить».

Номера читаются до выборки (как у реестра): правка, закоммиченная между
ними, придёт следующим запросом, а не пропадёт. Каждый номер ставится до
коммита одной блокировкой счётчика (`fields.bump`, `live.py`), поэтому
номера видны строго по порядку.

Что отдаётся - по правам: договоры - видимые человеку (`service.changes`),
факты журнала - владельцу и администратору (поток всей компании), операции -
тому, кому открыта «Таблица».
"""
from __future__ import annotations

import base64
import json
from typing import Any

from app.finance.contracts.fields import current
from app.finance.core import journal
from app.finance.model import VERSION
from app.finance.model.context import Ctx
from app.finance.service import FinanceError

#: Сколько фактов отдаёт одна выборка - дальше `more` и курсор внутри проведения.
FACTS_LIMIT = 500
#: Сколько секунд держать запрос, если ничего не менялось (`wait`).
WAIT_LIMIT = 25.0


def counters(ctx: Ctx) -> dict[str, int]:
    from app.finance import live

    workspace_id = ctx.workspace.id
    return {
        "c": current(ctx.session, workspace_id, "contracts"),
        "f": current(ctx.session, workspace_id, journal.SEQ),
        "o": live.current_seq(ctx.session, workspace_id),
        "s": current(ctx.session, workspace_id, "schema"),
    }


def encode(data: dict[str, Any]) -> str:
    raw = json.dumps({key: value for key, value in data.items() if value not in (None, "")}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode(cursor: str | None) -> dict[str, Any] | None:
    text = (cursor or "").strip()
    if not text:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FinanceError("Курсор записан неверно - возьмите его из прошлого ответа") from exc
    if not isinstance(data, dict):
        raise FinanceError("Курсор записан неверно - возьмите его из прошлого ответа")
    try:
        return {"c": int(data.get("c", 0)), "f": int(data.get("f", 0)), "fk": str(data.get("fk") or ""), "o": int(data.get("o", 0)), "s": int(data.get("s", 0))}
    except (TypeError, ValueError) as exc:
        raise FinanceError("Курсор записан неверно - возьмите его из прошлого ответа") from exc


def cursor_now(ctx: Ctx) -> str:
    return encode(counters(ctx))


def moved(ctx: Ctx, cursor: str | None) -> bool:
    """Сдвинулось ли что-нибудь после курсора - два-четыре чтения счётчиков, без сборки."""
    data = decode(cursor)
    if data is None:
        return False
    now = counters(ctx)
    return any(now[key] > data[key] for key in ("c", "f", "o", "s")) or bool(data.get("fk"))


def changes(
    ctx: Ctx, cursor: str | None, *, include: frozenset[str] = frozenset(), contracts_since: int | None = None
) -> dict[str, Any]:
    """Изменения после курсора. `include`: `contracts` - строки договоров целиком, как `/contracts/changes`.

    `contracts_since` - вместо курсора: номер реестра (`seq` ответа
    `/contracts`), который у клиента уже есть. Договоры придут с него, остальное
    - с этой минуты: десктоп продолжает сохранённый реестр без дыры между ним и
    подпиской.
    """
    from app.finance import live
    from app.finance.contracts import service
    from app.finance.core import calls

    since = decode(cursor)
    if since is None and contracts_since is not None:
        journal.ensure(ctx.session, ctx.workspace.id)
        since = {**counters(ctx), "c": max(0, int(contracts_since)), "fk": ""}
    if since is not None and contracts_since is None:
        # Журнал проводится по запросу: правка договора или новая операция
        # становится фактом, когда его кто-то читает. Подписчик - тоже читатель,
        # иначе факты записанного пришли бы только после чужого экрана.
        journal.ensure(ctx.session, ctx.workspace.id)
    now = counters(ctx)
    if since is None:
        return {"version": VERSION, "cursor": encode(now), "start": True, "more": False}
    out: dict[str, Any] = {
        "version": VERSION,
        "start": False,
        "more": False,
        # Что сдвинулось - и тому, кому подробности не открыты: долг и оплачено
        # меняются от фактов журнала, и клиенту пора спросить вид заново.
        "moved": {
            "contracts": now["c"] > since["c"],
            "facts": now["f"] > since["f"] or bool(since.get("fk")),
            "operations": now["o"] > since["o"],
            "schema": now["s"] > since["s"],
        },
    }
    next_cursor = dict(now)
    # Договоры: видимые человеку, вместе со сторонами и людьми.
    if now["c"] > since["c"] and ctx.access.view:
        batch = service.changes(ctx.session, ctx.workspace, ctx.access, since["c"], seq_now=now["c"], schema_now=now["s"])
        out["contracts"] = {
            "changed": [item["id"] for item in batch["contracts"]],
            "removed": list(batch["removed"]),
            # Номер реестра, по который пришли изменения, - как `seq` у `/contracts/changes`.
            "seq": batch["seq"],
            **({"rows": batch["contracts"], "parties": batch["parties"], "people": batch["people"]} if "contracts" in include else {}),
        }
    # Факты журнала - поток всей компании: владельцу и администратору.
    if (now["f"] > since["f"] or since.get("fk")) and ctx.access.setup:
        after = f"{since['f']}:{since['fk']}" if since.get("fk") else str(since["f"])
        found = calls.changes(ctx.session, ctx.workspace, ctx.access, since=after, limit=FACTS_LIMIT)
        out["facts"] = found["facts"]
        if found["more"]:
            seq, _, key = found["cursor"].partition(":")
            next_cursor["f"], next_cursor["fk"] = int(seq), key
            out["more"] = True
    # Операции - тем, кому открыта «Таблица».
    if now["o"] > since["o"] and ctx.can("table"):
        found_ops = live.changes(ctx.session, ctx.workspace, since["o"])
        out["operations"] = {"changed": [row.get("id") for row in found_ops["rows"]], "removed": found_ops["removed"]}
    if now["s"] > since["s"]:
        out["schema"] = True
    out["cursor"] = encode(next_cursor)
    return out


__all__ = ["FACTS_LIMIT", "WAIT_LIMIT", "changes", "counters", "cursor_now", "decode", "encode", "moved"]
