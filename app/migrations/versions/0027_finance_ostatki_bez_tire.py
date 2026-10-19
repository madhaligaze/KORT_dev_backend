"""finance: названия частей листа «Остатки» - с коротким дефисом

30.09.2026 длинное тире заменено во всём продукте (правило в CLAUDE.md), но
листы «Разовых» засеяны раньше, и в базе части листа «Остатки» так и
назывались «Работа идёт» и «Работа завершена» через длинное тире. В тот же
день в название части встал итог («· договоров: 19 · остаток: …», как в
книге юротдела) - и тире стояло бы на виду в карточках, листе и выгрузке.

Меняются только эти два названия засева, слово в слово. Название, которое
человек задал сам, миграция не трогает.

Revision ID: 0027
Revises: 0026
"""
from __future__ import annotations

import json
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0027'
down_revision: Union[str, None] = '0026'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"
#: Длинное тире (U+2014) - экранированием: массовая замена символа по проекту
#: не должна задеть то, что эта миграция ищет в базе.
LONG_DASH = "\u2014"
SEEDED = {
    f"Работа идёт {LONG_DASH} есть остаток": "Работа идёт - есть остаток",
    f"Работа завершена {LONG_DASH} есть остаток": "Работа завершена - есть остаток",
}


def _as_list(raw: Any) -> list:
    if isinstance(raw, str):
        raw = json.loads(raw)
    return list(raw or [])


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    views = bind.execute(
        sa.text(f"select id, workspace_id, blocks from {SCHEMA}.entity_views where book = 'oneoff'")
    ).all()
    touched: set[Any] = set()
    for view in views:
        blocks = _as_list(view.blocks)
        fresh = []
        for block in blocks:
            block = dict(block or {})
            title = str(block.get("title") or "")
            if title in SEEDED:
                block["title"] = SEEDED[title]
            fresh.append(block)
        if fresh != blocks:
            bind.execute(
                sa.text(f"update {SCHEMA}.entity_views set blocks = cast(:blocks as jsonb) where id = :id"),
                {"blocks": json.dumps(fresh, ensure_ascii=False), "id": view.id},
            )
            touched.add(view.workspace_id)
    for workspace_id in touched:
        # Схема поменялась - открытые листы перечитают её при следующем опросе.
        bind.execute(
            sa.text(f"update {SCHEMA}.counters set value = value + 1 where workspace_id = :ws and name = 'schema'"),
            {"ws": workspace_id},
        )


def downgrade() -> None:
    """Возвращать длинное тире в названия незачем."""
