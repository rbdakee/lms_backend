from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_error_handlers
from app.api.routers import health
from app.config import get_settings


def create_app() -> FastAPI:
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
    return app


app = create_app()
