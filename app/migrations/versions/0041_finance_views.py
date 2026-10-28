"""finance: виды как данные

Ядро KORT, этап 2, шаг 6 (28.10.2026).

Вид - сохранённый вопрос к ядру: отбор, меры, группировки, форма, владелец
(`core/views.py`). Колонка разделов собирается из закреплённых видов, а не
из кода: «Реестр», «Долги» и «Разовые ЮО» - строки `views`.

Что делает миграция с данными:
* у каждой компании - вид «Реестр» (ключ `""`, книга листов реестра) и вид
  «Долги»;
* книга листов `oneoff` («Разовые», засевалась каждой компании до этого
  шага), которую сделали книгой отдела (в каждой группе каждого блока -
  «отдел - …», у BBC - «Разовые ЮО»), - вид договоров с тем же ключом.
  Условия, которые стоят в каждой группе каждого блока её листов (вид
  договора, два статуса, отдел), уходят в отбор вида - договоры в листах те
  же: блок без своих условий у вида с отбором - весь вид. Отдел из отбора -
  владелец вида и доля («Разовые ЮО» - доля ЮО). Название - первый лист и код
  отдела: «Разовые ЮО». Меры - те, что стоят колонками и условиями листов;
  «По сотрудникам» - как было. Нетронутый засев (без отдела - так у всех
  компаний, кроме BBC) уходит в корзину: новая и чужая компания не держат
  листов юротдела BBC;
* «Оплачено (сводка)», «Остаток (сводка)», «Срок, мес» - больше не поля
  договора, а меры вида: их строки полей удаляются (листы, личный вид и права
  держат прежние ключи);
* написания шапок, которые были в коде под одну компанию («исполнитель bbc»,
  «ссылка на битрикс 24»), дописываются в поля существующих компаний: у них
  разбор узнаёт те же шапки, а новая компания чужих слов не получает.

Revision ID: 0041
Revises: 0040
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"
MEASURE_KEYS = ("summary_paid", "summary_remaining", "age_months")
COMPANY_NAMES = {
    "folder_url": ["ссылка на битрикс 24", "ссылка на битрикс", "битрикс"],
    "executor": ["исполнитель bbc", "арендодатель bbc", "арендадатель bbc"],
}
LEGACY_BOOK = "oneoff"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _canon(condition: dict[str, Any]) -> str:
    return json.dumps(
        {"field": condition.get("field"), "op": condition.get("op", "in"), "value": condition.get("value")},
        ensure_ascii=False,
        sort_keys=True,
    )


def _adopt(bind: Any, workspace_id: Any, sheets: list[tuple[Any, str, int, Any, Any]], position: int) -> None:
    """Книга `oneoff` компании - вид договоров: общие условия в отбор вида, отдел - владелец."""
    live = [sheet for sheet in sheets if sheet[4] is None]
    groups_all: list[list[dict[str, Any]]] = []
    for _id, _title, _position, blocks, _archived in live:
        for block in blocks or []:
            for group in ((block or {}).get("filter") or {}).get("any") or []:
                groups_all.append(list(group.get("all") or []))
    common: list[dict[str, Any]] = []
    if groups_all and all(groups_all):
        keys = [{_canon(item) for item in group} for group in groups_all]
        shared = set.intersection(*keys)
        common = [item for item in groups_all[0] if _canon(item) in shared]
    base = {"any": [{"all": common}]} if common else {}
    hoisted = {_canon(item) for item in common}
    department = next(
        (
            item["value"][0]
            for item in common
            if item.get("field") == "department"
            and item.get("op") == "in"
            and isinstance(item.get("value"), list)
            and len(item["value"]) == 1
        ),
        None,
    )
    code = ""
    if department is not None:
        code = bind.execute(
            sa.text(f"select code from {SCHEMA}.departments where id = cast(:id as uuid)"), {"id": str(department)}
        ).scalar() or ""
        if not code:
            department = None
    if department is None:
        # Засев, которого не касались: листы - в корзину, вида нет.
        bind.execute(
            sa.text(
                f"""update {SCHEMA}.entity_views set archived_at = now()
                where workspace_id = :ws and book = :book and archived_at is null"""
            ),
            {"ws": workspace_id, "book": LEGACY_BOOK},
        )
        return
    used: set[str] = set()
    for sheet_id, _title, _position, blocks, _archived in sheets:
        changed = []
        for block in blocks or []:
            block = dict(block or {})
            for column in block.get("columns") or []:
                if (column or {}).get("key") in MEASURE_KEYS:
                    used.add(column["key"])
            rule = block.get("filter") or {}
            groups = []
            whole = False
            for group in rule.get("any") or []:
                rest = [item for item in group.get("all") or [] if _canon(item) not in hoisted]
                for item in rest:
                    if item.get("field") in MEASURE_KEYS:
                        used.add(item["field"])
                if not rest:
                    whole = True
                groups.append({"all": rest})
            if hoisted and (whole or not groups):
                block["filter"] = {"any": []}
            elif hoisted:
                block["filter"] = {"any": groups}
            changed.append(block)
        if hoisted:
            bind.execute(
                sa.text(f"update {SCHEMA}.entity_views set blocks = cast(:blocks as jsonb) where id = :id"),
                {"blocks": _json(changed), "id": sheet_id},
            )
    first = sorted(live or sheets, key=lambda sheet: sheet[2])[0]
    title = f"{first[1]} {code}".strip() if department is not None else first[1]
    measures = (["share"] if department is not None else []) + [key for key in MEASURE_KEYS if key in used]
    bind.execute(
        sa.text(
            f"""insert into {SCHEMA}.views
            (id, workspace_id, key, title, source, owner_department_id, filter, measures, groups, forms, params,
             pin, position, template, archived_at)
            values (cast(:id as uuid), :ws, :key, :title, 'contracts', cast(:department as uuid), cast(:filter as jsonb),
             cast(:measures as jsonb), cast(:groups as jsonb), cast(:forms as jsonb), '{{}}'::jsonb, 'all', :position,
             'oneoff_age', case when :archived then now() else null end)"""
        ),
        {
            "id": str(uuid.uuid4()),
            "ws": workspace_id,
            "key": LEGACY_BOOK,
            "title": title,
            "department": str(department) if department is not None else None,
            "filter": _json(base),
            "measures": _json(measures),
            "groups": _json(["people"]),
            "forms": _json(["cards", "table"]),
            "position": position,
            "archived": not live,
        },
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    op.create_table(
        "views",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey(f"{SCHEMA}.workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), server_default=sa.text("'contracts'"), nullable=False),
        sa.Column("owner_department_id", sa.Uuid(), sa.ForeignKey(f"{SCHEMA}.departments.id", ondelete="SET NULL")),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE")),
        sa.Column("filter", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("measures", postgresql.JSONB(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("groups", postgresql.JSONB(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("forms", postgresql.JSONB(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("params", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("pin", sa.Text(), server_default=sa.text("'all'"), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("template", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey(f"{SCHEMA}.users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("workspace_id", "key", name="uq_views_key"),
        sa.CheckConstraint("source IN ('contracts', 'debts', 'unbilled')", name=op.f("ck_views_view_source")),
        sa.CheckConstraint("pin IN ('all', 'owner', '')", name=op.f("ck_views_view_pin")),
        schema=SCHEMA,
    )
    op.create_index("ix_views_workspace_id", "views", ["workspace_id"], schema=SCHEMA)

    workspaces = [row[0] for row in bind.execute(sa.text(f"select id from {SCHEMA}.workspaces"))]
    for workspace_id in workspaces:
        bind.execute(
            sa.text(
                f"""insert into {SCHEMA}.views (id, workspace_id, key, title, source, forms, params, pin, position, template)
                values (cast(:a as uuid), :ws, '', 'Реестр', 'contracts', '["cards", "table"]'::jsonb, '{{}}'::jsonb, 'all', 10, 'registry'),
                       (cast(:b as uuid), :ws, 'dolgi', 'Долги', 'debts', '["list"]'::jsonb,
                        '{{"side": "receivable", "basis": "accruals", "group": "client"}}'::jsonb, 'all', 20, 'debts')"""
            ),
            {"a": str(uuid.uuid4()), "b": str(uuid.uuid4()), "ws": workspace_id},
        )
        sheets = [
            (row[0], row[1], row[2], row[3], row[4])
            for row in bind.execute(
                sa.text(
                    f"""select id, title, position, blocks, archived_at from {SCHEMA}.entity_views
                    where workspace_id = :ws and book = :book order by position, created_at"""
                ),
                {"ws": workspace_id, "book": LEGACY_BOOK},
            )
        ]
        if sheets:
            _adopt(bind, workspace_id, sheets, 30)

    bind.execute(
        sa.text(f"delete from {SCHEMA}.entity_fields where entity = 'contract' and key in :keys").bindparams(
            sa.bindparam("keys", expanding=True)
        ),
        {"keys": list(MEASURE_KEYS)},
    )
    for key, names in COMPANY_NAMES.items():
        for row_id, stored in bind.execute(
            sa.text(f"select id, names from {SCHEMA}.entity_fields where entity = 'contract' and key = :key"), {"key": key}
        ).all():
            current = list(stored or [])
            missing = [name for name in names if name not in current]
            if missing:
                bind.execute(
                    sa.text(f"update {SCHEMA}.entity_fields set names = cast(:names as jsonb) where id = :id"),
                    {"names": _json(current + missing), "id": row_id},
                )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    # Строки полей мер досеет прежний код сам (`ensure_registry`); отбор вида,
    # поднятый из блоков «Разовых», возвращается в каждый блок.
    for workspace_id, base in bind.execute(
        sa.text(f"select workspace_id, filter from {SCHEMA}.views where key = :book"), {"book": LEGACY_BOOK}
    ).all():
        common = [item for group in (base or {}).get("any") or [] for item in group.get("all") or []]
        if not common:
            continue
        for sheet_id, blocks in bind.execute(
            sa.text(f"select id, blocks from {SCHEMA}.entity_views where workspace_id = :ws and book = :book"),
            {"ws": workspace_id, "book": LEGACY_BOOK},
        ).all():
            out = []
            for block in blocks or []:
                block = dict(block or {})
                groups = ((block.get("filter") or {}).get("any")) or [{"all": []}]
                block["filter"] = {"any": [{"all": common + list(group.get("all") or [])} for group in groups]}
                out.append(block)
            bind.execute(
                sa.text(f"update {SCHEMA}.entity_views set blocks = cast(:blocks as jsonb) where id = :id"),
                {"blocks": _json(out), "id": sheet_id},
            )
    op.drop_index("ix_views_workspace_id", table_name="views", schema=SCHEMA)
    op.drop_table("views", schema=SCHEMA)
