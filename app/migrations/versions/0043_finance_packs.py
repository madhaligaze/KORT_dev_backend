"""finance: пакеты страны и отрасли у компании, шапка таблиц компании - её словарём

Ядро KORT, этап 2, п.9 страницы «Пакеты» (28.10.2026). Ядро больше не держит
шапку таблиц одной компании: у колонок «договор за месяц» (сводная таблица,
вкладки таблиц юрлиц, шаблон счетов) в коде - ключ, подпись и общие слова
отрасли (`core/columns.py`), а как колонки названы у компании - её словарь
`core_settings.data.book_columns`.

* `workspaces.country` ('KZ') и `workspaces.industry` ('services') - пакеты
  компании (`app/finance/packs`).
* Компаниям, у которых подключена сводная таблица (`contract_summary_sources`),
  в словарь дописываются названия, которые до этой ревизии знал код, - первым
  то, как колонку подписывал код (подпись в отказах и в шапке шаблона).
  Таблицы этих компаний читаются так же, как до ревизии; написания, которые
  компания уже записала сама, остаются, и стоят первыми.
* Написания полей реестра, убранные из кода («наша фирма» у исполнителя,
  «заказчик (название фирмы)» у заказчика), дописываются в поля существующих
  компаний, как в 0041 (засев уже положил их туда - здесь страховка).

Revision ID: 0043
Revises: 0042
"""
from __future__ import annotations

import json
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "finance"
#: Названия колонок таблиц, которые код знал до этой ревизии: ключ ядра →
#: написания, первое - подпись колонки.
COMPANY_COLUMNS: dict[str, list[str]] = {
    "month_no": ["Мес"],
    "month": ["Месяц строки (Техн. 1)", "Техн. 1"],
    "own": ["Наша Фирма"],
    "kind": ["Вид Услуги"],
    "staff": ["Наш Сотрудник"],
    "customer": ["Заказчик (Название Фирмы)", "клиент"],
    "bank_name": ["По Выписке Банка"],
    "onec_name": ["По 1C", "по 1с"],
    "bill_day": ["Число Выставления Счета"],
    "amount": ["Сумма Договора"],
    "department": ["Отдел"],
    "subject": ["Предмет договора", "предмет договра"],
    "signed_at": ["Дата заключения Договора"],
    "number": ["№ Договора"],
    "amendments": ["Доп Соглашения"],
    "period_from": ["Период с (нач.)"],
    "period_to": ["Период по (зав.)"],
    "note": ["Доп. Коммент.", "доп. комментарий"],
    "opening_acts": ["Сальдо Начало (АВР)"],
    "bill_state": ["Счет"],
    "bill_period": ["Счет за Период (Коммент)"],
    "bill_no": ["№ Счета"],
    "bill_date": ["Дата выстав. Счета", "дата выставления счета"],
    "paid_state": ["Факт Оплата"],
    "paid": ["Сумма Факт Поступ.", "сумма факт поступления", "факт поступление"],
    "pay1_date": ["Дата (часть 1)"],
    "pay1_sum": ["Сумма (часть 1)"],
    "pay2_date": ["Дата (часть 2)"],
    "pay2_sum": ["Сумма (часть 2)"],
    "pay3_date": ["Дата (часть 3)"],
    "pay3_sum": ["Сумма (часть 3)"],
    "act_state": ["АВР (наша)"],
    "act_sum": ["Сумма (наша)"],
    "act_no": ["№ АВР (наша)"],
    "act_date": ["Дата (наша)"],
    "accepted_state": ["АВР (клиент принял)"],
    "accepted_date": ["Дата АВР (клиент принял)"],
    "esf_state": ["ЭСФ (отпр.)"],
    "esf_date": ["Дата ЭСФ (отпр.)"],
    "comment": ["Коммент"],
    "closing_acts": ["Сальдо Конец (АВР)"],
    "status": ["Статус", "Статус договора", "статус (продл./приостн./разв./на исп.)"],
    "opening_accruals": ["Сальдо Начало (без АВР)"],
    "closing_accruals": ["Сальдо Конец (без АВР)"],
    "refund_sum": ["Сумма Возврата"],
    "refund_date": ["Дата Возврата"],
}

FIELD_NAMES = {
    "executor": ["наша фирма"],
    "customer": ["заказчик (название фирмы)"],
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (тесты); миграции - для Postgres.
        return
    op.add_column("workspaces", sa.Column("country", sa.Text(), server_default=sa.text("'KZ'"), nullable=False), schema=SCHEMA)
    op.add_column(
        "workspaces", sa.Column("industry", sa.Text(), server_default=sa.text("'services'"), nullable=False), schema=SCHEMA
    )
    for (workspace_id,) in bind.execute(sa.text(f"select workspace_id from {SCHEMA}.contract_summary_sources")).all():
        row = bind.execute(
            sa.text(f"select data from {SCHEMA}.core_settings where workspace_id = :id"), {"id": workspace_id}
        ).first()
        data = dict((row[0] if row is not None else None) or {})
        columns = {key: list(names) for key, names in (data.get("book_columns") or {}).items()}
        for key, names in COMPANY_COLUMNS.items():
            own = columns.get(key, [])
            known = {_norm(name) for name in own}
            columns[key] = own + [name for name in names if _norm(name) not in known]
        data["book_columns"] = columns
        if row is None:
            bind.execute(
                sa.text(f"insert into {SCHEMA}.core_settings (workspace_id, data) values (:id, cast(:data as jsonb))"),
                {"id": workspace_id, "data": _json(data)},
            )
        else:
            bind.execute(
                sa.text(f"update {SCHEMA}.core_settings set data = cast(:data as jsonb) where workspace_id = :id"),
                {"id": workspace_id, "data": _json(data)},
            )
    for key, names in FIELD_NAMES.items():
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
    # Словарь колонок не трогаем: прежний код знает эти названия сам, а
    # лишние написания в словаре ему не мешают.
    op.drop_column("workspaces", "industry", schema=SCHEMA)
    op.drop_column("workspaces", "country", schema=SCHEMA)
