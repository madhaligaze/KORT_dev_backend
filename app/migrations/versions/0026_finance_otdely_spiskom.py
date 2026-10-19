"""finance: «Отдел» договора - список, общий с долями отделов

30.09.2026 финансисты BBC: разнесли доли отделов HR 70% / ЮО 30%, а поле
«Отдел» показывало одно «HR»; договор «4 в 1» с «ОБО, НО, ЮО, HR» из книги
стоял с пустым «Отделом» и замечанием «нет в списке», хотя доли всех четырёх
отделов уже были разнесены. Отдел у договора был один
(`contracts.department_id`), а доли отделов - отдельным списком рядом.

Теперь отделы договора - `contract_departments`, как люди в
`contract_people`: поле «Отдел» и есть этот список, доля - на той же строке.
Миграция:

1. отдел договора встаёт в список первым, если его там ещё нет; отделы из
   долей - за ним, в прежнем порядке;
2. непрочитанный список через запятую (`attrs.__raw__.department`,
   «ОБО, НО,⏎ ЮО, HR») разбирается: нашлись все части - они становятся
   списком, замечание снимается; хоть одна не нашлась - договор не
   трогается. Если у договора уже есть отделы и набор другой - тоже не
   трогается: сводить написанное с долями за человека нельзя;
3. колонка `contracts.department_id` снимается;
4. `contract_departments.added_by` - кто вписал отдел: сотрудник убирает
   только вписанный им самим и пока без доли (исправить свою ошибку), всё
   остальное - администратор или владелец. У перенесённых строк автора нет.

На копии прода 30.09: список через запятую - у одного договора (№ДД/010426,
те же четыре отдела, что в его долях), доли отделов - у двух.

Revision ID: 0026
Revises: 0025
"""
from __future__ import annotations

import json
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0026'
down_revision: Union[str, None] = '0025'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"
RAW_KEY = "__raw__"
ISSUE = "unread_department"

_SPACES = re.compile(r"[\s   ]+")
_PARTS = re.compile(r"[,;\n/]+")


def _norm(text: object) -> str:
    """Как `layout.norm`: без переносов, лишних пробелов, регистра и «ё»."""
    return _SPACES.sub(" ", str(text or "")).strip().lower().replace("ё", "е")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    inspector = sa.inspect(bind)
    shares_columns = {column["name"] for column in inspector.get_columns("contract_departments", schema=SCHEMA)}
    if "added_by" not in shares_columns:
        op.add_column("contract_departments", sa.Column("added_by", sa.Uuid(), nullable=True), schema=SCHEMA)
        op.create_foreign_key(
            op.f("fk_contract_departments_added_by"),
            "contract_departments",
            "users",
            ["added_by"],
            ["id"],
            source_schema=SCHEMA,
            referent_schema=SCHEMA,
            ondelete="SET NULL",
        )
    columns = {column["name"] for column in inspector.get_columns("contracts", schema=SCHEMA)}
    if "department_id" not in columns:
        return

    # 1. Отдел договора - первым в список отделов.
    missing = (
        f"c.department_id is not null and not exists (select 1 from {SCHEMA}.contract_departments x "
        "where x.contract_id = c.id and x.department_id = c.department_id)"
    )
    bind.execute(
        sa.text(
            f"update {SCHEMA}.contract_departments cd set position = cd.position + 1 "
            f"where exists (select 1 from {SCHEMA}.contracts c where c.id = cd.contract_id and {missing})"
        )
    )
    bind.execute(
        sa.text(
            f"insert into {SCHEMA}.contract_departments (contract_id, department_id, position) "
            f"select c.id, c.department_id, 0 from {SCHEMA}.contracts c where {missing}"
        )
    )

    # 2. Непрочитанный список через запятую - без угадывания.
    rows = bind.execute(
        sa.text(
            f"select id, workspace_id, attrs, acknowledged from {SCHEMA}.contracts "
            f"where (attrs -> '{RAW_KEY}' -> 'department') is not null"
        )
    ).all()
    lookups: dict[object, dict[str, object]] = {}
    for contract_id, workspace_id, attrs, acknowledged in rows:
        attrs = dict(attrs or {})
        raw = dict(attrs.get(RAW_KEY) or {})
        parts = [part.strip() for part in _PARTS.split(str(raw.get("department") or "")) if part.strip()]
        if not parts:
            continue
        if workspace_id not in lookups:
            found: dict[str, object] = {}
            doubled: set[str] = set()
            departments = bind.execute(
                sa.text(
                    f"select id, code, title from {SCHEMA}.departments "
                    "where workspace_id = :workspace and archived_at is null order by position"
                ),
                {"workspace": workspace_id},
            ).all()
            for department_id, code, title in departments:
                for key in {_norm(code), _norm(title)} - {""}:
                    if key in found and found[key] != department_id:
                        doubled.add(key)
                    found.setdefault(key, department_id)
            lookups[workspace_id] = {key: value for key, value in found.items() if key not in doubled}
        lookup = lookups[workspace_id]
        wanted: list[object] = []
        for part in parts:
            department_id = lookup.get(_norm(part))
            if department_id is None:
                wanted = []
                break
            if department_id not in wanted:
                wanted.append(department_id)
        if not wanted:
            continue
        existing = bind.execute(
            sa.text(
                f"select department_id from {SCHEMA}.contract_departments "
                "where contract_id = :contract order by position"
            ),
            {"contract": contract_id},
        ).scalars().all()
        if existing and set(existing) != set(wanted):
            continue
        if not existing:
            for position, department_id in enumerate(wanted):
                bind.execute(
                    sa.text(
                        f"insert into {SCHEMA}.contract_departments (contract_id, department_id, position) "
                        "values (:contract, :department, :position)"
                    ),
                    {"contract": contract_id, "department": department_id, "position": position},
                )
        raw.pop("department", None)
        attrs.pop(RAW_KEY, None)
        if raw:
            attrs[RAW_KEY] = raw
        marks = {key: value for key, value in dict(acknowledged or {}).items() if key != ISSUE}
        bind.execute(
            sa.text(
                f"update {SCHEMA}.contracts set attrs = cast(:attrs as jsonb), "
                "acknowledged = cast(:marks as jsonb) where id = :contract"
            ),
            {"attrs": json.dumps(attrs, ensure_ascii=False), "marks": json.dumps(marks, ensure_ascii=False), "contract": contract_id},
        )

    # 3. Колонка больше не правда: вместе с ней уходит и её внешний ключ.
    op.drop_column("contracts", "department_id", schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_column("contract_departments", "added_by", schema=SCHEMA)
    op.add_column("contracts", sa.Column("department_id", sa.Uuid(), nullable=True), schema=SCHEMA)
    op.create_foreign_key(
        op.f("fk_contracts_department_id"),
        "contracts",
        "departments",
        ["department_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )
    # Назад - первый отдел списка; остальные остаются долями, как до 0026.
    bind.execute(
        sa.text(
            f"update {SCHEMA}.contracts c set department_id = (select cd.department_id from "
            f"{SCHEMA}.contract_departments cd where cd.contract_id = c.id order by cd.position limit 1)"
        )
    )
