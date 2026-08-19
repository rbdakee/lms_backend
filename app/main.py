import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_error_handlers
from app.api.routers import (
    admin,
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
    app.include_router(settings.admin_router)
    app.include_router(telegram.admin_router)
    app.include_router(questions.admin_router)
    app.include_router(preview.router)
    return app


app = create_app()
