import uvicorn

from app.core.config import settings


def reload_enabled() -> bool:
    """`APP_RELOAD`, если задан; иначе — перезапуск только в development."""
    if settings.app_reload is not None:
        return settings.app_reload
    return settings.environment == "development"


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=reload_enabled(),
    )


if __name__ == "__main__":
    main()
