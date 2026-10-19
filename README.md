# KORT - API

FastAPI + SQLAlchemy + alembic. Учёт - пакет `app/finance/` (его README -
главный документ), маршруты - `app/api/routes/finance*.py`, разбор выписок -
`app/services/`.

```
uv sync --extra ocr_local
uv run python main.py                 # :8080, миграции на старте
uv run pytest                         # ≈290 тестов
TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5432/kort_test uv run pytest tests/test_finance_contracts_pg.py
uv run python -m app.finance.cli reset-password owner@company.kz
```

Ревизии - `app/migrations/versions/0012…0023`; корень цепочки - 0012.
Проверка «модели совпадают со схемой»: `uv run alembic check` на базе после
`alembic upgrade head`.
