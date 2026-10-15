import asyncio
import importlib
import logging
from contextlib import AsyncExitStack, asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session as DbSession
from starlette.routing import Route

from app.adapters.curriculum.parser import SUBJECT_CODES, subject_icon
from app.adapters.db.base import get_engine
from app.adapters.db.repos import UserRepo
from app.adapters.telegram.poller import run_poller
from app.api.errors import register_error_handlers
from app.api.routers import (
    admin,
    admin_admins,
    admin_categories,
    admin_certificates,
    admin_courses,
    admin_curriculum,
    admin_lesson_ai,
    admin_lessons,
    admin_mcp,
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
    lesson_ai,
    lessons,
    mcp_oauth,
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
from app.lesson_ai.images import catalog as image_catalog
from app.lesson_ai.worker import start_attachment_workers, start_workers
from app.mcp_server.server import McpEndpoint

# Один на процесс: менеджер SDK внутри него заводится в lifespan приложения
mcp_endpoint = McpEndpoint()


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
    # Значок каждого предмета обязан быть в каталоге картинок: иначе чип без
    # картинки заметят в школе, а не при выкате
    missing = [c for c in SUBJECT_CODES if subject_icon(c) not in image_catalog()]
    if missing:
        raise RuntimeError(f"у предметов нет значка в каталоге картинок: {missing}")
    with DbSession(get_engine()) as db:
        ensure_bootstrap_admin(UserRepo(db), cfg, db.commit)
    task = None
    if cfg.telegram_provider == "bot" and cfg.telegram_updates == "poll":
        task = asyncio.create_task(run_poller(cfg))
    # Очередь генерации «Жоспара» — тоже здесь: отдельного сервиса бриф
    # не разрешает (TEACHER_AI_BRIEF, раздел 7). Выключенный продукт воркеров
    # не поднимает вовсе, тесты — тоже (`LESSON_AI_WORKERS=0`). Рядом — разбор
    # вложений: выжимка снимается фоном, а не в запросе загрузки
    workers = None
    attach_workers = None
    if cfg.lesson_ai_on and cfg.lesson_ai_workers > 0:
        workers = start_workers(cfg)
        attach_workers = start_attachment_workers(cfg)
    async with AsyncExitStack() as stack:
        # MCP-менеджер SDK держит свою группу задач — её жизнь привязана к приложению
        if cfg.mcp_enabled:
            await stack.enter_async_context(mcp_endpoint.lifespan())
        yield
    if workers is not None:
        workers.stop()
    if attach_workers is not None:
        attach_workers.stop()
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
        # Без этого браузер прячет имя файла из скачивания от скрипта фронта
        expose_headers=["Content-Disposition"],
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
    app.include_router(admin_certificates.router)
    app.include_router(admin_reviews.router)
    app.include_router(admin_categories.router)
    app.include_router(admin_admins.router)
    app.include_router(admin_curriculum.router)
    app.include_router(admin_lesson_ai.router)
    app.include_router(lesson_ai.access_router)
    app.include_router(lesson_ai.router)
    app.include_router(settings.admin_router)
    app.include_router(telegram.admin_router)
    app.include_router(questions.admin_router)
    app.include_router(preview.router)
    if cfg.mcp_enabled:
        # Реестр действий грузится только здесь: выключенный MCP его не тянет вовсе
        importlib.import_module("app.assistant.tools")

        app.include_router(mcp_oauth.router)
        app.include_router(admin_mcp.router)
        app.router.routes.append(Route("/mcp", endpoint=mcp_endpoint))
    return app


app = create_app()
