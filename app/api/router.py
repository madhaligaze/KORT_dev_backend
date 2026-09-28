from fastapi import APIRouter

from app.api.routes.finance import router as finance_router
from app.api.routes.finance_contracts import router as finance_contracts_router
from app.api.routes.finance_looks import router as finance_looks_router
from app.api.routes.finance_people import router as finance_people_router
from app.api.routes.finance_trash import router as finance_trash_router
from app.api.routes.health import router as health_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
# Маршруты лежат снаружи пакета `app.finance`: пакет про HTTP и учётки не
# знает, и это проверяет tests/test_finance_isolated.py.
api_router.include_router(finance_router)
api_router.include_router(finance_contracts_router)
api_router.include_router(finance_people_router)
api_router.include_router(finance_trash_router)
api_router.include_router(finance_looks_router)
