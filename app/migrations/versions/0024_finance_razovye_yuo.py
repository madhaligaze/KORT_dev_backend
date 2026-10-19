"""finance: «Разовые ЮО» - только договоры юротдела, два статуса, строка «исполнен»

28.09.2026 владелец разделил реестр в колонке на два: общий реестр и
«Разовые ЮО». Книга «Разовые» повторяет книгу юротдела BBC «Реестр ЮО -
Разовые» (`1RbkFiG0…`), а в ней:

* **только договоры ЮО** - в листе «Разовые» 61 строка ЮО и одна HR, а в
  реестре KORT вид «Разовая услуга» стоит ещё у НО, ОБО, HR (149 договоров
  против 90 у ЮО). К каждой группе условий каждого блока книги добавляется
  «отдел - ЮО», новая строка книги получает отдел ЮО;
* **два статуса** - «на исполнении» и «исполнен» (лист «Справочник», B2:B3).
  Выбор статуса в блоках книги ограничивается ими (`choices`), новая строка -
  «на исполнении». Сами договоры не трогаются: у 12 договоров ЮО статус
  другой («Не состоялся» и т. п.), и менять его - решение людей, а не
  миграции;
* **строка «исполнен» - зелёная**: условное форматирование листа
  (`=TRIM($E2)="исполнен"`), здесь - `paint` блока.

Отдел ищется по коду «ЮО» (без регистра и пробелов), статусы - по смыслу
(фаза `in_progress` / `fulfilled`). Нет отдела - книга остаётся для всех
отделов; нет статусов - без выбора и подсветки. Повторный запуск ничего не
дублирует.

Revision ID: 0024
Revises: 0023
"""
from __future__ import annotations

import json
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0024'
down_revision: Union[str, None] = '0023'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"
LEGAL_CODES = {"юо"}


def _norm(text: str) -> str:
    return "".join(str(text or "").lower().split())


def _as_list(raw: Any) -> list:
    if isinstance(raw, str):
        raw = json.loads(raw)
    return list(raw or [])


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    workspaces = bind.execute(
        sa.text(f"select distinct workspace_id from {SCHEMA}.entity_views where book = 'oneoff'")
    ).scalars().all()
    for workspace_id in workspaces:
        departments = bind.execute(
            sa.text(
                f"select id, code from {SCHEMA}.departments "
                "where workspace_id = :ws and archived_at is null order by position"
            ),
            {"ws": workspace_id},
        ).all()
        legal = next((str(row.id) for row in departments if _norm(row.code) in LEGAL_CODES), None)
        statuses = bind.execute(
            sa.text(
                f"select id, meaning from {SCHEMA}.list_values "
                "where workspace_id = :ws and field_key = 'status' and archived_at is null "
                "order by position, created_at"
            ),
            {"ws": workspace_id},
        ).all()
        first: dict[str, str] = {}
        for row in statuses:
            meaning = row.meaning if isinstance(row.meaning, dict) else json.loads(row.meaning or "{}")
            phase = meaning.get("phase")
            if phase in ("in_progress", "fulfilled") and phase not in first:
                first[phase] = str(row.id)
        both = len(first) == 2

        views = bind.execute(
            sa.text(f"select id, blocks from {SCHEMA}.entity_views where workspace_id = :ws and book = 'oneoff'"),
            {"ws": workspace_id},
        ).all()
        changed = False
        for view in views:
            blocks = _as_list(view.blocks)
            fresh = []
            for block in blocks:
                block = dict(block or {})
                if legal:
                    rule = dict(block.get("filter") or {"any": []})
                    groups = []
                    for group in rule.get("any") or []:
                        conditions = list((group or {}).get("all") or [])
                        if not any(item.get("field") == "department" for item in conditions):
                            conditions.append({"field": "department", "op": "in", "value": [legal]})
                        groups.append({"all": conditions})
                    block["filter"] = {"any": groups}
                    defaults = dict(block.get("defaults") or {})
                    defaults.setdefault("department", legal)
                    block["defaults"] = defaults
                if both:
                    block.setdefault("choices", {"status": [first["in_progress"], first["fulfilled"]]})
                    block.setdefault(
                        "paint",
                        [
                            {
                                "filter": {
                                    "any": [{"all": [{"field": "status", "op": "in", "value": [first["fulfilled"]]}]}]
                                },
                                "tone": "done",
                            }
                        ],
                    )
                    defaults = dict(block.get("defaults") or {})
                    defaults.setdefault("status", first["in_progress"])
                    block["defaults"] = defaults
                fresh.append(block)
            if fresh != blocks:
                bind.execute(
                    sa.text(f"update {SCHEMA}.entity_views set blocks = cast(:blocks as jsonb) where id = :id"),
                    {"blocks": json.dumps(fresh, ensure_ascii=False), "id": view.id},
                )
                changed = True
        if changed:
            # Схема поменялась - открытые листы перечитают её при следующем опросе.
            bind.execute(
                sa.text(
                    f"update {SCHEMA}.counters set value = value + 1 where workspace_id = :ws and name = 'schema'"
                ),
                {"ws": workspace_id},
            )


def downgrade() -> None:
    # Правила листов - данные компании, их правят в «Настроить реестр».
    # Откат вернул бы их к состоянию, которого уже может не быть.
    pass
