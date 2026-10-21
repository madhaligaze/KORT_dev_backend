import asyncio
import contextlib
import json
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
    """Привести схему в соответствие с кодом. Не смогли - не поднимаемся.

    Здесь раньше стоял молчаливый откат на `create_all`: если alembic падал,
    таблицы всё равно создавались, `alembic_version` оставалась на старой
    ревизии, приложение отвечало 200 - и схема расходилась с миграциями
    навсегда. Следующая ревизия падала уже на «таблица существует» и снова
    уходила в откат. Наружу это не выходило ничем: ни ошибки, ни симптома.

    В базе живут деньги компаний, которых больше нигде нет, и приложение, не
    сумевшее привести схему в порядок, обязано не подняться, а не делать вид,
    что всё хорошо.

    Для SQLite alembic не гоняется намеренно: ревизии написаны под Postgres со
    схемами, которых у SQLite нет. Там схема собирается через `create_all` - и
    это законный путь, а не запасной.
    """
    db_url = get_resolved_database_url()
    driver = db_url.split("://")[0] if "://" in db_url else db_url

    if db_url.startswith("sqlite"):
        from app.finance.db import init_finance_database

        log.info("Схема: SQLite (%s) - create_all вместо ревизий", driver)
        init_finance_database()
        return

    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(_BACKEND_DIR / "app" / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    # Не трогать логирование процесса: секции [logger_*] из alembic.ini
    # предназначены для запуска из командной строки, а здесь они затирали
    # настройки uvicorn - после миграций пропадал журнал запросов.
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

    Первый проход - через минуту после старта, а не сразу: старт не должен
    ждать базы ради того, что спокойно подождёт. Проход - один запрос по
    индексу `(applied_at, effective_from)` на все компании; между проходами
    ничего не держится в памяти.

    Тем же проходом журнал действий теряет просмотры старше 180 дней - одно
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
        except Exception as exc:  # noqa: BLE001 - фоновый проход не роняет приложение
            log.warning("finance: соглашения по договорам не применились (%s)", exc)
        await asyncio.sleep(3600)


async def _onec_loop(minutes: int) -> None:
    """Базы 1С перечитываются сами: раз в `minutes` каждая включённая база.

    Проверка - каждые пять минут, чтение - только тех баз, что читались давно,
    поэтому «Обновить» человека не задваивается фоновым проходом. Отказ одной
    базы пишется в неё (`last_error`) и не мешает остальным.
    """
    from app.finance.onec import service as onec_service

    await asyncio.sleep(90)
    while True:
        try:
            done = await asyncio.to_thread(onec_service.sync_due, minutes)
            if done:
                log.info("finance: перечитано баз 1С: %s", done)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - фоновый проход не роняет приложение
            log.warning("finance: базы 1С не перечитались (%s)", exc)
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Схема - первым делом, до всего остального.
    #
    # В контейнере миграции гоняет ENTRYPOINT (backend/Dockerfile), и там этот
    # вызов - повторный и почти бесплатный: `upgrade head` на актуальной базе
    # стоит один запрос. А локально их не гоняет никто: разработчик запускает
    # `uv run python main.py`, минуя ENTRYPOINT. Один вызов здесь избавляет от
    # целого класса «у меня не воспроизводится».
    _run_migrations()

    contracts_task: asyncio.Task | None = None
    support_task: asyncio.Task | None = None
    onec_task: asyncio.Task | None = None
    bills_task: asyncio.Task | None = None
    try:
        from app.finance.config import finance_settings

        if finance_settings.enabled:
            contracts_task = asyncio.create_task(_contract_amendments_loop())
        if finance_settings.enabled and finance_settings.onec_sync_minutes > 0:
            onec_task = asyncio.create_task(_onec_loop(finance_settings.onec_sync_minutes))
        # Бот поддержки: обращения - в канал Telegram, ответы - обратно.
        # Без токена и канала обращения копятся в базе и уйдут, когда бот появится.
        if finance_settings.enabled and finance_settings.support_ready:
            from app.finance import support_bot

            support_task = asyncio.create_task(support_bot.run_forever())
        # Бот «Не выставлены счета»: сводка дня по книге-сводке - в канал.
        if finance_settings.enabled and finance_settings.bills_ready:
            from app.finance import bills_bot

            bills_task = asyncio.create_task(bills_bot.run_forever())
        elif finance_settings.bills_bot_token.strip() and (
            finance_settings.bills_hour_value is None or finance_settings.bills_every_value is None
        ):
            log.error(
                "finance: бот счетов выключен - FINANCE_BILLS_HOUR=%r (нужен час 0-23 по Астане), "
                "FINANCE_BILLS_EVERY=%r (нужно часов 1-24)",
                finance_settings.bills_hour,
                finance_settings.bills_every,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("finance: фоновая задача не запустилась: %s", exc)

    log.info("Application startup complete")
    try:
        yield
    finally:
        for task in (contracts_task, support_task, onec_task, bills_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


#: Описание API (`/docs`, `/redoc`, `/openapi.json`) - только вне прода. На
#: проде оно отдавало без входа все 149 маршрутов с полями: готовая карта для
#: того, кто ищет, куда ударить (аудит 19.10.2026). Фронту оно не нужно.
_PUBLIC_DOCS = (settings.environment or "").strip().lower() not in {"production", "prod"}

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _PUBLIC_DOCS else None,
    redoc_url="/redoc" if _PUBLIC_DOCS else None,
    openapi_url="/openapi.json" if _PUBLIC_DOCS else None,
)

#: Потолок тела запроса. Самое большое законное тело - обращение в поддержку
#: (40 МБ файлов, `support.MAX_TOTAL_BYTES`) с обвязкой формы. Без потолка
#: тело любой длины дочитывалось до конца, прежде чем маршрут его отвергал, -
#: а регистрация открыта, и «вошедший» - это любой.
MAX_BODY_BYTES = 48 * 1024 * 1024


class _BodyTooLarge(Exception):
    pass


class BodyLimit:
    """Отказ телу больше потолка - до того, как его прочтёт маршрут.

    Чистый ASGI, а не `@app.middleware`: тот оборачивает ответ заново и
    плохо уживается с фоновыми задачами (поддержка шлёт в Telegram после
    ответа). Объявленная длина проверяется сразу; тело без длины (chunked)
    считается по ходу чтения и обрывается на потолке.
    """

    def __init__(self, app, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def _refuse(self, send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        too_big = f"Запрос больше {self.limit // (1024 * 1024)} МБ"
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                return await self._refuse(send, 400, "Неверная длина запроса")
            if length > self.limit:
                return await self._refuse(send, 413, too_big)
            return await self.app(scope, receive, send)

        received = 0
        started = False

        async def counted():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise _BodyTooLarge
            return message

        async def tracked(message):
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted, tracked)
        except _BodyTooLarge:
            if started:
                raise
            await self._refuse(send, 413, too_big)


class SecurityHeaders:
    """API отдаёт JSON и файлы выгрузки: в рамку его не встраивают, тип не угадывают."""

    HEADERS = (
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
    )

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def with_headers(message):
            if message.get("type") == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers") or []}
                message["headers"] = list(message.get("headers") or []) + [
                    item for item in self.HEADERS if item[0] not in present
                ]
            await send(message)

        await self.app(scope, receive, with_headers)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Добавленный последним - снаружи: потолок тела отвечает раньше всех.
app.add_middleware(SecurityHeaders)
app.add_middleware(BodyLimit)

app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/")
async def root() -> dict[str, str]:
    out = {"name": settings.app_name, "health": f"{settings.api_v1_prefix}/health"}
    if _PUBLIC_DOCS:
        out["docs"] = "/docs"
    return out
