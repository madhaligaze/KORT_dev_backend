"""Все метаданные, за которыми следит alembic — в одном месте.

Вынесено из `env.py` не ради красоты. `env.py` при импорте запускает миграции:
внизу файла стоит `if context.is_offline_mode(): ... else: run_migrations_online()`,
и это выполняется на уровне модуля. Значит импортировать из него список
метаданных нельзя — тест, которому этот список нужен, запустил бы миграции
самим фактом импорта.

В KORT схема одна — `finance`. Имя осталось от раздела «Финансы», из которого
продукт выделен 28.09.2026: данные с прода переносятся дампом этой схемы, и
переименование означало бы переписать и дамп, и одиннадцать ревизий.
"""
from __future__ import annotations

from app.finance.db import FinanceBase
import app.finance.models  # noqa: F401 — учёт
import app.finance.accounts_model  # noqa: F401 — учётки
import app.finance.contracts.models  # noqa: F401 — реестр договоров

target_metadata = [FinanceBase.metadata]

__all__ = ["target_metadata"]
