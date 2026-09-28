import asyncio
import contextlib
import logging
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.database import get_resolved_database_url

log = logging.getLogger(__name__)


#: Пути к alembic считаются от каталога `backend`, а не от рабочего каталога
#: процесса. Раньше конфиг открывался как `Config("alembic.ini")`, и миграции
#: молча не находились, если приложение запускали не из `backend/`.
_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _run_migrations() -> None:
    """Привести схему в соответствие с кодом. Не смогли — не поднимаемся.

    Здесь раньше стоял молчаливый откат на `create_all`: если alembic падал,
    таблицы всё равно создавались, `alembic_version` оставалась на старой
    ревизии, приложение отвечало 200 — и схема расходилась с миграциями
    навсегда. Следующая ревизия падала уже на «таблица существует» и снова
    уходила в откат. Наружу это не выходило ничем: ни ошибки, ни симптома.

    В базе живут деньги компаний, которых больше нигде нет, и приложение, не
    сумевшее привести схему в порядок, обязано не подняться, а не делать вид,
    что всё хорошо.

    Для SQLite alembic не гоняется намеренно: ревизии написаны под Postgres со
    схемами, которых у SQLite нет. Там схема собирается через `create_all` — и
    это законный путь, а не запасной.
    """
    db_url = get_resolved_database_url()
    driver = db_url.split("://")[0] if "://" in db_url else db_url

    if db_url.startswith("sqlite"):
        from app.finance.db import init_finance_database

        log.info("Схема: SQLite (%s) — create_all вместо ревизий", driver)
        init_finance_database()
        return

    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(_BACKEND_DIR / "app" / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    # Не трогать логирование процесса: секции [logger_*] из alembic.ini
    # предназначены для запуска из командной строки, а здесь они затирали
    # настройки uvicorn — после миграций пропадал журнал запросов.
    alembic_cfg.attributes["configure_logger"] = False

    try:
        command.upgrade(alembic_cfg, "head")
    except Exception as exc:
        log.error(
            "Миграции не применились (%s: %s). Приложение не поднимется: схема "
            "не соответствует коду, а работать на расходящейся схеме опаснее, "
            "чем не работать вовсе.",
            type(exc).__name__,
            exc,
        )
        log.error(traceback.format_exc())
        raise

    log.info("Схема приведена к последней ревизии")


async def _contract_amendments_loop() -> None:
    """Раз в час: соглашения «с даты», чей день настал, становятся значением договора.

    Первый проход — через минуту после старта, а не сразу: старт не должен
    ждать базы ради того, что спокойно подождёт. Проход — один запрос по
    индексу `(applied_at, effective_from)` на все компании; между проходами
    ничего не держится в памяти.

    Тем же проходом журнал действий теряет просмотры старше 180 дней — одно
    `DELETE` по индексу `(category, at)`. Своего вечного цикла у журнала нет:
    фоновая нагрузка не должна расти от каждого нового правила хранения.
    """
    from app.finance import audit as finance_audit
    from app.finance.contracts import service as contracts_service
    from app.finance.db import finance_session

    def one_pass() -> int:
        with finance_session() as session:
            applied = contracts_service.apply_due(session)
            purged = finance_audit.purge_views(session)
            if purged:
                log.info("finance: из журнала действий убрано старых просмотров: %s", purged)
            return applied

    await asyncio.sleep(60)
    while True:
        try:
            applied = await asyncio.to_thread(one_pass)
            if applied:
                log.info("finance: применено соглашений по договорам: %s", applied)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — фоновый проход не роняет приложение
            log.warning("finance: соглашения по договорам не применились (%s)", exc)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Схема — первым делом, до всего остального.
    #
    # В контейнере миграции гоняет ENTRYPOINT (backend/Dockerfile), и там этот
    # вызов — повторный и почти бесплатный: `upgrade head` на актуальной базе
    # стоит один запрос. А локально их не гоняет никто: разработчик запускает
    # `uv run python main.py`, минуя ENTRYPOINT. Один вызов здесь избавляет от
    # целого класса «у меня не воспроизводится».
    _run_migrations()

    contracts_task: asyncio.Task | None = None
    try:
        from app.finance.config import finance_settings

        if finance_settings.enabled:
            contracts_task = asyncio.create_task(_contract_amendments_loop())
    except Exception as exc:  # noqa: BLE001
        log.warning("finance: фоновая задача договоров не запустилась: %s", exc)

    log.info("Application startup complete")
    try:
        yield
    finally:
        if contracts_task is not None:
            contracts_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await contracts_task


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "name": settings.app_name,
        "docs": "/docs",
        "health": f"{settings.api_v1_prefix}/health",
    }
