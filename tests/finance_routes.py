"""Маршруты раздела для тестов обхода — на любой версии FastAPI.

Тесты «каждый маршрут объявляет раздел» и «каждое изменение пишет событие»
обходили `app.routes`. До FastAPI 0.141 `include_router` раскладывал туда
копии маршрутов; с 0.141 там лежит один `_IncludedRouter`, и обход не находил
ни одного маршрута раздела — первая проверка молча проходила на пустоте, и
падала только сверка закрытых списков (28.09.2026, локальный `uv` без
закрепления поставил 0.141). Прод закреплён на 0.135.3 в requirements-prod.txt,
но тест не должен слепнуть от обновления.

Поэтому маршруты берутся из самих роутеров — с тем же префиксом, с которым их
подключает приложение теста. Зависимости уровня роутера FastAPI вписывает в
маршрут при его объявлении, так что `route.dependant` у оригинала полный.
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

PREFIX = "/api/v1"


def finance_routers() -> tuple[APIRouter, ...]:
    """Все роутеры раздела — один список и для приложения, и для обхода."""
    from app.api.routes.finance import router as finance_router
    from app.api.routes.finance_contracts import router as contracts_router
    from app.api.routes.finance_looks import router as looks_router
    from app.api.routes.finance_people import router as people_router
    from app.api.routes.finance_trash import router as trash_router

    return (finance_router, contracts_router, people_router, trash_router, looks_router)


def finance_app() -> FastAPI:
    application = FastAPI()
    for router in finance_routers():
        application.include_router(router, prefix=PREFIX)
    return application


def api_routes() -> list[tuple[str, APIRoute]]:
    """(полный путь, маршрут) по всем роутерам раздела."""
    return [
        (PREFIX + route.path, route)
        for router in finance_routers()
        for route in router.routes
        if isinstance(route, APIRoute)
    ]
