"""Схема `finance` — управленческий учёт внутри приложения.

Отдельная схема по той же причине, что у `bbc`, `webexcel` и `books`: модуль
должен сниматься одним `DROP SCHEMA finance CASCADE`, не задев ни одну чужую
таблицу. Пока раздел обкатывают на финансистах, это не теория: его состав
поменяется несколько раз, и выкинуть неудачную версию целиком должно быть
дешевле, чем разбирать, какие из семнадцати таблиц чьи.

Схемой владеет alembic. `create_all` ниже нужен ровно для одного — собрать
таблицы в чистой временной базе теста, где SQLite схем не знает. Списка
«колонки, которые надо досоздать руками», здесь нет и не будет: колонка,
добавленная в модель, попадает в ревизию, другого пути нет (урок `bbc/db.py`,
где такой список ведут руками и забывают).
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import MetaData, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.database import get_engine

log = logging.getLogger(__name__)

FINANCE_SCHEMA = "finance"

#: Единая схема имён ограничений и индексов — как в `books`, и по той же
#: причине: без неё модели и ревизии называют индексы по-разному, и проверка
#: «схема совпадает с моделями» не может пройти никогда.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class FinanceBase(DeclarativeBase):
    """Declarative base, привязанный к схеме `finance`."""

    metadata = MetaData(schema=FINANCE_SCHEMA, naming_convention=NAMING_CONVENTION)


_session_factory: sessionmaker[Session] | None = None
_initialized = False


def finance_engine() -> Engine:
    """Общий движок; на SQLite схема стирается — там её не существует."""
    engine = get_engine()
    if engine.dialect.name == "sqlite":
        return engine.execution_options(schema_translate_map={FINANCE_SCHEMA: None})
    return engine


def init_finance_database() -> None:
    """Создать схему и недостающие таблицы. Только для чистой базы."""
    global _initialized
    if _initialized:
        return

    # Импорт моделей до `create_all`: таблица попадает в метаданные только
    # когда её модуль прочитан. Учётки лежат отдельным модулем, и забыть его
    # здесь означало бы «вход не работает, а таблицы users нет» — без ошибки
    # при старте.
    from app.finance import accounts_model as _accounts  # noqa: F401
    from app.finance import models as _models  # noqa: F401
    from app.finance.contracts import models as _contracts  # noqa: F401

    engine = get_engine()
    if engine.dialect.name != "sqlite":
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{FINANCE_SCHEMA}"'))

    FinanceBase.metadata.create_all(bind=finance_engine())
    _initialized = True


def get_finance_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=finance_engine(), autoflush=False, autocommit=False, future=True
        )
        # Номер изменения у каждой записанной операции — для живого режима
        # листа «Таблица» (см. `app/finance/live.py`).
        from app.finance import live

        live.register(_session_factory)
    init_finance_database()
    return _session_factory


@contextmanager
def finance_session() -> Iterator[Session]:
    """Транзакция вокруг сессии модуля."""
    session = get_finance_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "FINANCE_SCHEMA",
    "FinanceBase",
    "NAMING_CONVENTION",
    "finance_engine",
    "finance_session",
    "init_finance_database",
]
