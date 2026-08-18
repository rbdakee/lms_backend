from datetime import timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request, Response
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.base import get_db
from app.adapters.db.models import Session, User
from app.adapters.db.repos import (
    AuthCodeRepo,
    CourseRepo,
    EnrollmentRepo,
    LeadRepo,
    LessonRepo,
    NotificationRepo,
    ProgressRepo,
    ReviewRepo,
    SessionRepo,
    UserRepo,
    now_utc,
)
from app.adapters.sms.log_sms import LogSms
from app.adapters.storage.local_storage import LocalStorage
from app.adapters.telegram.log_telegram import LogTelegram
from app.application.auth import AuthService, hash_token
from app.application.courses import CoursesService
from app.application.files import FilesService
from app.application.leads import LeadsService
from app.application.lessons import LessonsService
from app.application.ports import SmsPort, StoragePort, TelegramPort
from app.application.ratelimit import SlidingWindowLimiter
from app.application.users import UsersService
from app.config import Settings, get_settings
from app.domain.errors import BlockedError, ForbiddenError, UnauthorizedError

COOKIE_NAME = "sid"
# last_seen_at пишем не чаще раза в 5 минут — не превращать каждый GET в UPDATE
TOUCH_EVERY = timedelta(minutes=5)

# Счётчик частоты живёт в памяти процесса, значит он один на всё приложение:
# создавать его на каждый запрос — значит не считать ничего
_playback_limiter = SlidingWindowLimiter(get_settings().playback_per_min, 60)


def get_sms() -> SmsPort:
    # Провайдер пока один — заглушка; настоящий добавится строчкой конфигурации
    return LogSms()


def get_telegram() -> TelegramPort:
    # Провайдер пока один — заглушка; настоящий бот добавится строчкой конфигурации
    return LogTelegram()


def get_storage() -> StoragePort:
    """Выбор адаптера — конфигурацией, а не `if` в месте вызова. Провайдер
    пока один: локальный каталог, он же версия для разработки."""
    cfg = get_settings()
    if cfg.storage_provider != "local":
        raise ValueError(f"Неизвестное хранилище: {cfg.storage_provider}")
    return LocalStorage(Path(cfg.storage_dir))


def get_playback_limiter() -> SlidingWindowLimiter:
    return _playback_limiter


def get_client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def get_auth_service(
    db: Annotated[DbSession, Depends(get_db)],
    sms: Annotated[SmsPort, Depends(get_sms)],
) -> AuthService:
    return AuthService(
        users=UserRepo(db),
        sessions=SessionRepo(db),
        codes=AuthCodeRepo(db),
        sms=sms,
        cfg=get_settings(),
        commit=db.commit,
    )


def get_users_service(db: Annotated[DbSession, Depends(get_db)]) -> UsersService:
    return UsersService(sessions=SessionRepo(db))


def get_courses_service(db: Annotated[DbSession, Depends(get_db)]) -> CoursesService:
    return CoursesService(
        courses=CourseRepo(db),
        reviews=ReviewRepo(db),
        leads=LeadRepo(db),
        enrollments=EnrollmentRepo(db),
        progress=ProgressRepo(db),
    )


def get_lessons_service(
    db: Annotated[DbSession, Depends(get_db)],
    playback_limiter: Annotated[SlidingWindowLimiter, Depends(get_playback_limiter)],
) -> LessonsService:
    return LessonsService(
        courses=CourseRepo(db),
        lessons=LessonRepo(db),
        enrollments=EnrollmentRepo(db),
        progress=ProgressRepo(db),
        playback_limiter=playback_limiter,
    )


def get_files_service(
    db: Annotated[DbSession, Depends(get_db)],
    storage: Annotated[StoragePort, Depends(get_storage)],
) -> FilesService:
    return FilesService(
        lessons=LessonRepo(db),
        enrollments=EnrollmentRepo(db),
        storage=storage,
        cfg=get_settings(),
    )


def get_leads_service(
    db: Annotated[DbSession, Depends(get_db)],
    telegram: Annotated[TelegramPort, Depends(get_telegram)],
) -> LeadsService:
    return LeadsService(
        users=UserRepo(db),
        courses=CourseRepo(db),
        leads=LeadRepo(db),
        enrollments=EnrollmentRepo(db),
        notifications=NotificationRepo(db),
        telegram=telegram,
        commit=db.commit,
    )


def get_current_session(
    request: Request, db: Annotated[DbSession, Depends(get_db)]
) -> Session:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise UnauthorizedError()
    session = SessionRepo(db).by_token_hash(hash_token(token))
    if session is None:
        raise UnauthorizedError()
    if now_utc() - session.last_seen_at > TOUCH_EVERY:
        session.last_seen_at = now_utc()
    return session


def get_current_user(
    session: Annotated[Session, Depends(get_current_session)],
    db: Annotated[DbSession, Depends(get_db)],
) -> User:
    user = db.get(User, session.user_id)
    if user is None:
        raise UnauthorizedError()
    if user.is_blocked:
        # Блокировка действует сразу, не дожидаясь конца сессии
        raise BlockedError()
    return user


def get_current_user_optional(
    request: Request, db: Annotated[DbSession, Depends(get_db)]
) -> User | None:
    """Публичные экраны показывают состояние доступа, если человек вошёл.

    Нет куки или сессия умерла — не ошибка, а анонимный просмотр.
    Блокировка действует и здесь: залогиненный заблокированный получает 403.
    """
    try:
        return get_current_user(get_current_session(request, db), db)
    except UnauthorizedError:
        return None


def get_current_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    # Отдельного входа в админку нет: та же кука, проверка is_admin на сервере
    if not user.is_admin:
        raise ForbiddenError("Доступно только администратору")
    return user


def set_session_cookie(response: Response, token: str, cfg: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=cfg.session_ttl_days * 86400,
        httponly=True,
        samesite="lax",
        secure=cfg.cookie_secure,
        domain=cfg.cookie_domain,
        path="/",
    )


def clear_session_cookie(response: Response, cfg: Settings) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=cfg.cookie_secure,
        domain=cfg.cookie_domain,
        path="/",
    )
