import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.base import get_engine
from app.adapters.db.repos import UserRepo
from app.adapters.telegram.poller import run_poller
from app.api.errors import register_error_handlers
from app.api.routers import (
    admin,
    admin_admins,
    admin_categories,
    admin_courses,
    admin_lessons,
    admin_quizzes,
    admin_reviews,
    admin_tasks,
    admin_teachers,
    auth,
    certificates,
    courses,
    dictionaries,
    files,
    health,
    lessons,
    me,
    notifications,
    preview,
    questions,
    quizzes,
    settings,
    tasks,
    telegram,
)
from app.application.bootstrap import ensure_bootstrap_admin
from app.config import check_providers, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Поллинг Telegram живёт здесь, а не отдельным процессом: воркер
    один и тот же uvicorn, задача — просто корутина в его цикле событий.

    `TELEGRAM_UPDATES=poll` — только локальная разработка без туннеля
    наружу (`app/adapters/telegram/poller.py`); в бою условие ложно
    и до `create_task` дело не доходит вовсе.

    Здесь же — первый админ на чистой базе. База к этому моменту обязана
    отвечать: не отвечает — сервис не поднимается, и это правильнее, чем
    здоровый инстанс, в который некому войти.
    """
    cfg = get_settings()
    with DbSession(get_engine()) as db:
        ensure_bootstrap_admin(UserRepo(db), cfg, db.commit)
    task = None
    if cfg.telegram_provider == "bot" and cfg.telegram_updates == "poll":
        task = asyncio.create_task(run_poller(cfg))
    yield
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    # INFO, иначе заглушка SMS молчит, а без кода в логе не войти
    logging.basicConfig(level=logging.INFO)
    cfg = get_settings()
    # До первого запроса: опечатка в провайдере иначе всплывает на заявке
    # учителя, а сервис до того выглядит здоровым
    check_providers(cfg)
    app = FastAPI(title="LMS API", version="0.1.0", lifespan=lifespan)

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
    app.include_router(settings.router)
    app.include_router(courses.router)
    app.include_router(certificates.router)
    app.include_router(certificates.me_router)
    app.include_router(certificates.verify_router)
    app.include_router(certificates.pdf_router)
    app.include_router(lessons.router)
    app.include_router(questions.router)
    app.include_router(notifications.router)
    app.include_router(quizzes.router)
    app.include_router(quizzes.attempts_router)
    app.include_router(tasks.router)
    app.include_router(files.router)
    # Не для фронта: сюда стучится сам Telegram, входа здесь нет
    app.include_router(telegram.router)
    app.include_router(admin.router)
    app.include_router(admin_courses.router)
    app.include_router(admin_lessons.router)
    app.include_router(admin_quizzes.router)
    app.include_router(admin_tasks.router)
    app.include_router(admin_teachers.router)
    app.include_router(admin_reviews.router)
    app.include_router(admin_categories.router)
    app.include_router(admin_admins.router)
    app.include_router(settings.admin_router)
    app.include_router(telegram.admin_router)
    app.include_router(questions.admin_router)
    app.include_router(preview.router)
    return app


app = create_app()
