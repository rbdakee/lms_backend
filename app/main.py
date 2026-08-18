import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_error_handlers
from app.api.routers import admin, auth, courses, dictionaries, files, health, lessons, me
from app.config import get_settings


def create_app() -> FastAPI:
    # INFO, иначе заглушка SMS молчит, а без кода в логе не войти
    logging.basicConfig(level=logging.INFO)
    cfg = get_settings()
    app = FastAPI(title="LMS API", version="0.1.0")

    # Два фронта — клиентское приложение и админка — на разных доменах.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(dictionaries.router)
    app.include_router(courses.router)
    app.include_router(lessons.router)
    app.include_router(files.router)
    app.include_router(admin.router)
    return app


app = create_app()
