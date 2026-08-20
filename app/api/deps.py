import secrets
from datetime import timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request, Response
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.base import get_db
from app.adapters.db.models import Session, User
from app.adapters.db.preview import (
    PreviewEnrollmentRepo,
    VisibilityRepos,
    visibility_repos,
)
from app.adapters.db.repos import (
    AttemptRepo,
    AuthCodeRepo,
    CategoryRepo,
    CertificateRepo,
    CourseAdminRepo,
    CourseRepo,
    EnrollmentRepo,
    LeadRepo,
    LessonAdminRepo,
    NotificationRepo,
    ProgressRepo,
    QuizAdminRepo,
    ReviewRepo,
    SessionRepo,
    SettingRepo,
    SubmissionRepo,
    TaskAdminRepo,
    TeacherAdminRepo,
    ThreadMessageRepo,
    UserRepo,
    now_utc,
)
from app.adapters.sms.log_sms import LogSms
from app.adapters.storage.local_storage import LocalStorage
from app.adapters.telegram.bot import TelegramBot
from app.adapters.telegram.log_telegram import LogTelegram
from app.application.admin_notify import AdminNotifier
from app.application.auth import AuthService, hash_token
from app.application.categories import CategoriesService
from app.application.certificate_pdf import CertificatePdfService
from app.application.certificates import CertificatesService
from app.application.courses import CoursesService
from app.application.courses_admin import CoursesAdminService
from app.application.files import FilesService
from app.application.leads import LeadsService
from app.application.lessons import LessonsService
from app.application.lessons_admin import LessonsAdminService
from app.application.notifications import NotificationsService
from app.application.overview import OverviewService
from app.application.ports import SmsPort, StoragePort, TelegramPort
from app.application.preview import Preview, PreviewService
from app.application.preview_quiz import PreviewAttemptStore, SessionAttempts
from app.application.questions import QuestionsService
from app.application.quizzes import QuizzesService
from app.application.quizzes_admin import QuizzesAdminService
from app.application.ratelimit import SlidingWindowLimiter
from app.application.reports import ReportsService
from app.application.reviews_admin import ReviewsAdminService
from app.application.settings import TELEGRAM_KEY, SettingsService
from app.application.submissions_admin import SubmissionsAdminService
from app.application.tasks import TasksService
from app.application.tasks_admin import TasksAdminService
from app.application.teachers_admin import TeachersAdminService
from app.application.telegram_bind import TelegramBindService
from app.application.users import UsersService
from app.config import Settings, get_settings
from app.domain.errors import BlockedError, ForbiddenError, UnauthorizedError

COOKIE_NAME = "sid"
# last_seen_at пишем не чаще раза в 5 минут — не превращать каждый GET в UPDATE
TOUCH_EVERY = timedelta(minutes=5)

# Счётчик частоты живёт в памяти процесса, значит он один на всё приложение:
# создавать его на каждый запрос — значит не считать ничего
_playback_limiter = SlidingWindowLimiter(get_settings().playback_per_min, 60)
# Проверка сертификата публична: без лимита номера перебираются с одного адреса
_verify_limiter = SlidingWindowLimiter(get_settings().verify_per_min, 60)
# Форма вопроса под уроком капчи не имеет — лимит на пользователя
_thread_limiter = SlidingWindowLimiter(get_settings().thread_messages_per_min, 60)
# Попытки предпросмотра живут в памяти процесса, значит хранилище одно на всё
# приложение: заводить его на каждый запрос — значит не хранить ничего
_preview_attempts = PreviewAttemptStore()


def get_sms() -> SmsPort:
    # Провайдер пока один — заглушка; настоящий добавится строчкой
    # конфигурации. Что в SMS_PROVIDER стоит именно `log`, проверено
    # при старте (`check_providers`): иначе настройка обещала бы отправку,
    # а код входа уходил бы в лог
    return LogSms()


def get_telegram(db: Annotated[DbSession, Depends(get_db)]) -> TelegramPort:
    """Выбор адаптера — конфигурацией, а не `if` в месте вызова.

    Настоящему боту нужен чат, куда слать «админам», а он лежит в настройках
    площадки — отсюда зависимость от базы. Заглушке чат не нужен, и строку
    настроек она не читает вовсе.

    Значений ровно два, и проверены они при старте (`check_providers`):
    опечатка в TELEGRAM_PROVIDER роняла бы каждую заявку, а сервис до того
    выглядел бы здоровым.
    """
    cfg = get_settings()
    if cfg.telegram_provider == "log":
        return LogTelegram()
    # chat_id приходит только от вебхука привязки: вписанный руками чужой чат —
    # это заявки с телефонами учителей, ушедшие незнакомому человеку
    return TelegramBot(cfg.telegram_bot_token, SettingRepo(db).get(TELEGRAM_KEY).get("chat_id"))


def get_admin_notifier(
    db: Annotated[DbSession, Depends(get_db)],
    telegram: Annotated[TelegramPort, Depends(get_telegram)],
) -> AdminNotifier:
    """Уведомления админам идут через обёртку с флагами типов сообщений:
    выключенный переключатель на экране обязан что-то значить."""
    return AdminNotifier(telegram=telegram, settings=SettingRepo(db), commit=db.commit)


def get_storage() -> StoragePort:
    """Выбор адаптера — конфигурацией, а не `if` в месте вызова. Провайдер
    пока один: локальный каталог, он же версия для разработки. Что в
    STORAGE_PROVIDER стоит именно он, проверено при старте
    (`check_providers`)."""
    return LocalStorage(Path(get_settings().storage_dir))


def get_playback_limiter() -> SlidingWindowLimiter:
    return _playback_limiter


def get_verify_limiter() -> SlidingWindowLimiter:
    return _verify_limiter


def get_thread_limiter() -> SlidingWindowLimiter:
    return _thread_limiter


def get_client_ip(request: Request) -> str | None:
    """Адрес клиента для лимитов.

    Заголовку верим только при `trust_forwarded_for`: иначе перебор реестра
    сертификатов обходится одной строчкой в заголовке, а словарь лимитера
    растёт на каждый выдуманный адрес.
    """
    if get_settings().trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def get_current_session_optional(
    request: Request, db: Annotated[DbSession, Depends(get_db)]
) -> Session | None:
    """Сессия по куке или её отсутствие. Поиск живёт здесь, а не в строгой
    версии: сессию спрашивают и публичные экраны, и признак предпросмотра,
    и второй такой же запрос в базу на каждый вызов был бы лишним."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    session = SessionRepo(db).by_token_hash(hash_token(token))
    if session is None:
        return None
    if now_utc() - session.last_seen_at > TOUCH_EVERY:
        session.last_seen_at = now_utc()
    return session


def get_current_session(
    session: Annotated[Session | None, Depends(get_current_session_optional)],
) -> Session:
    if session is None:
        raise UnauthorizedError()
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
    session: Annotated[Session | None, Depends(get_current_session_optional)],
    db: Annotated[DbSession, Depends(get_db)],
) -> User | None:
    """Публичные экраны показывают состояние доступа, если человек вошёл.

    Нет куки или сессия умерла — не ошибка, а анонимный просмотр.
    Блокировка действует и здесь: залогиненный заблокированный получает 403.
    """
    if session is None:
        return None
    try:
        return get_current_user(session, db)
    except UnauthorizedError:
        return None


def get_current_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    # Отдельного входа в админку нет: та же кука, проверка is_admin на сервере
    if not user.is_admin:
        raise ForbiddenError("Доступно только администратору")
    return user


def verify_telegram_secret(request: Request) -> None:
    """Подлинность вебхука: заголовок сверяется с конфигурацией.

    Пустой секрет в конфигурации означает «отвергать всё», а не «пускать
    всех»: иначе привязка чужого чата открыта всему интернету, а вписать
    туда свой chat_id — значит получать заявки с телефонами учителей.
    Байты, а не строки: заголовок приходит снаружи и бывает не-ASCII.
    """
    cfg = get_settings()
    header = request.headers.get("x-telegram-bot-api-secret-token") or ""
    if not cfg.telegram_webhook_secret or not secrets.compare_digest(
        header.encode(), cfg.telegram_webhook_secret.encode()
    ):
        raise ForbiddenError("Неверный секрет вебхука")


def get_preview(
    session: Annotated[Session | None, Depends(get_current_session_optional)],
    db: Annotated[DbSession, Depends(get_db)],
) -> Preview | None:
    """Включённый режим предпросмотра — или None, если его нет.

    Право проверяется здесь, а не только на входе в режим: сняли у человека
    is_admin, пока флаг стоял, — режим гаснет вместе с правом.
    """
    if session is None or session.preview_course_id is None:
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.is_admin:
        return None
    return Preview(
        session_id=str(session.id),
        user_id=session.user_id,
        course_id=session.preview_course_id,
    )


def get_preview_course_id(preview: Annotated[Preview | None, Depends(get_preview)]) -> int | None:
    """Знание о режиме, которое получает сценарий: какой курс предпросматривают."""
    return preview.course_id if preview is not None else None


def get_enrollments(
    db: Annotated[DbSession, Depends(get_db)],
    preview: Annotated[Preview | None, Depends(get_preview)],
) -> EnrollmentRepo:
    """Доступ к курсу подменяется в одном месте — здесь, а не проверками
    в каждом сценарии (BACKEND_NOTES, раздел 12)."""
    if preview is None:
        return EnrollmentRepo(db)
    return PreviewEnrollmentRepo(db, user_id=preview.user_id, course_id=preview.course_id)


def get_visibility_repos(
    db: Annotated[DbSession, Depends(get_db)],
    preview_course_id: Annotated[int | None, Depends(get_preview_course_id)],
) -> VisibilityRepos:
    """Видимость курса подменяется там же, где доступ: в режиме сценариям
    достаются репозитории, для которых предпросматриваемый курс существует
    в любом статусе — иначе черновик отдавал бы 404 на каждом экране,
    а смотреть админ приходит именно черновик (BACKEND_NOTES, раздел 12)."""
    return visibility_repos(db, preview_course_id)


def get_preview_attempts() -> PreviewAttemptStore:
    return _preview_attempts


def get_session_attempts(
    preview: Annotated[Preview | None, Depends(get_preview)],
    store: Annotated[PreviewAttemptStore, Depends(get_preview_attempts)],
) -> SessionAttempts | None:
    # Вне режима попытка в памяти не нужна вовсе: тесты идут через базу
    if preview is None:
        return None
    return SessionAttempts(store, preview.session_id)


def get_preview_service(
    db: Annotated[DbSession, Depends(get_db)],
    store: Annotated[PreviewAttemptStore, Depends(get_preview_attempts)],
) -> PreviewService:
    return PreviewService(courses=CourseRepo(db), attempts=store)


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


def get_courses_service(
    db: Annotated[DbSession, Depends(get_db)],
    storage: Annotated[StoragePort, Depends(get_storage)],
    enrollments: Annotated[EnrollmentRepo, Depends(get_enrollments)],
    preview_course_id: Annotated[int | None, Depends(get_preview_course_id)],
    repos: Annotated[VisibilityRepos, Depends(get_visibility_repos)],
) -> CoursesService:
    # Хранилище нужно раздаче обложки: байты лежат в приватном хранилище,
    # а наружу от курса уходит адрес публичного маршрута
    return CoursesService(
        courses=repos.courses,
        reviews=ReviewRepo(db),
        leads=LeadRepo(db),
        enrollments=enrollments,
        progress=ProgressRepo(db),
        storage=storage,
        cfg=get_settings(),
        preview_course_id=preview_course_id,
    )


def get_lessons_service(
    db: Annotated[DbSession, Depends(get_db)],
    playback_limiter: Annotated[SlidingWindowLimiter, Depends(get_playback_limiter)],
    enrollments: Annotated[EnrollmentRepo, Depends(get_enrollments)],
    preview_course_id: Annotated[int | None, Depends(get_preview_course_id)],
    repos: Annotated[VisibilityRepos, Depends(get_visibility_repos)],
) -> LessonsService:
    return LessonsService(
        courses=repos.courses,
        lessons=repos.lessons,
        enrollments=enrollments,
        progress=ProgressRepo(db),
        playback_limiter=playback_limiter,
        preview_course_id=preview_course_id,
    )


def get_quizzes_service(
    db: Annotated[DbSession, Depends(get_db)],
    enrollments: Annotated[EnrollmentRepo, Depends(get_enrollments)],
    preview_course_id: Annotated[int | None, Depends(get_preview_course_id)],
    preview_attempts: Annotated[SessionAttempts | None, Depends(get_session_attempts)],
    repos: Annotated[VisibilityRepos, Depends(get_visibility_repos)],
) -> QuizzesService:
    return QuizzesService(
        quizzes=repos.quizzes,
        attempts=repos.attempts,
        enrollments=enrollments,
        certificates=CertificateRepo(db),
        # Репозитории видимости, а не настоящие: замок на старте попытки
        # считается по той же программе, что видит учитель на экране
        courses=repos.courses,
        progress=ProgressRepo(db),
        preview_course_id=preview_course_id,
        preview_attempts=preview_attempts,
    )


def get_certificates_service(
    db: Annotated[DbSession, Depends(get_db)],
    verify_limiter: Annotated[SlidingWindowLimiter, Depends(get_verify_limiter)],
    enrollments: Annotated[EnrollmentRepo, Depends(get_enrollments)],
    preview_course_id: Annotated[int | None, Depends(get_preview_course_id)],
    repos: Annotated[VisibilityRepos, Depends(get_visibility_repos)],
) -> CertificatesService:
    return CertificatesService(
        certificates=CertificateRepo(db),
        courses=repos.courses,
        enrollments=enrollments,
        progress=ProgressRepo(db),
        notifications=NotificationRepo(db),
        verify_limiter=verify_limiter,
        preview_course_id=preview_course_id,
    )


def get_tasks_service(
    db: Annotated[DbSession, Depends(get_db)],
    storage: Annotated[StoragePort, Depends(get_storage)],
    notifier: Annotated[AdminNotifier, Depends(get_admin_notifier)],
    enrollments: Annotated[EnrollmentRepo, Depends(get_enrollments)],
    preview_course_id: Annotated[int | None, Depends(get_preview_course_id)],
    repos: Annotated[VisibilityRepos, Depends(get_visibility_repos)],
) -> TasksService:
    return TasksService(
        tasks=repos.tasks,
        submissions=SubmissionRepo(db),
        enrollments=enrollments,
        storage=storage,
        telegram=notifier,
        cfg=get_settings(),
        commit=db.commit,
        preview_course_id=preview_course_id,
    )


def get_submissions_admin_service(
    db: Annotated[DbSession, Depends(get_db)],
    storage: Annotated[StoragePort, Depends(get_storage)],
) -> SubmissionsAdminService:
    return SubmissionsAdminService(
        submissions=SubmissionRepo(db),
        notifications=NotificationRepo(db),
        storage=storage,
        cfg=get_settings(),
    )


def get_files_service(
    storage: Annotated[StoragePort, Depends(get_storage)],
    enrollments: Annotated[EnrollmentRepo, Depends(get_enrollments)],
    preview_course_id: Annotated[int | None, Depends(get_preview_course_id)],
    repos: Annotated[VisibilityRepos, Depends(get_visibility_repos)],
) -> FilesService:
    return FilesService(
        lessons=repos.lessons,
        enrollments=enrollments,
        storage=storage,
        cfg=get_settings(),
        preview_course_id=preview_course_id,
    )


def get_notifications_service(
    db: Annotated[DbSession, Depends(get_db)],
) -> NotificationsService:
    return NotificationsService(notifications=NotificationRepo(db))


def get_questions_service(
    db: Annotated[DbSession, Depends(get_db)],
    thread_limiter: Annotated[SlidingWindowLimiter, Depends(get_thread_limiter)],
    enrollments: Annotated[EnrollmentRepo, Depends(get_enrollments)],
    preview_course_id: Annotated[int | None, Depends(get_preview_course_id)],
    repos: Annotated[VisibilityRepos, Depends(get_visibility_repos)],
) -> QuestionsService:
    return QuestionsService(
        courses=repos.courses,
        lessons=repos.lessons,
        enrollments=enrollments,
        messages=ThreadMessageRepo(db),
        notifications=NotificationRepo(db),
        limiter=thread_limiter,
        preview_course_id=preview_course_id,
    )


def get_leads_service(
    db: Annotated[DbSession, Depends(get_db)],
    notifier: Annotated[AdminNotifier, Depends(get_admin_notifier)],
    enrollments: Annotated[EnrollmentRepo, Depends(get_enrollments)],
    preview_course_id: Annotated[int | None, Depends(get_preview_course_id)],
    repos: Annotated[VisibilityRepos, Depends(get_visibility_repos)],
) -> LeadsService:
    return LeadsService(
        users=UserRepo(db),
        courses=repos.courses,
        leads=LeadRepo(db),
        enrollments=enrollments,
        notifications=NotificationRepo(db),
        telegram=notifier,
        commit=db.commit,
        preview_course_id=preview_course_id,
        admin_base_url=get_settings().admin_base_url,
    )


def get_courses_admin_service(
    db: Annotated[DbSession, Depends(get_db)],
    storage: Annotated[StoragePort, Depends(get_storage)],
) -> CoursesAdminService:
    # Настоящий репозиторий, а не подменённый предпросмотром: редактор
    # показывает курс как он есть, в любом статусе. Хранилище нужно обложке:
    # что объект есть и что это картинка, сервер берёт у него
    return CoursesAdminService(
        courses=CourseAdminRepo(db),
        categories=CategoryRepo(db),
        storage=storage,
        cfg=get_settings(),
    )


def get_lessons_admin_service(
    db: Annotated[DbSession, Depends(get_db)],
    storage: Annotated[StoragePort, Depends(get_storage)],
) -> LessonsAdminService:
    # Настоящие репозитории, не подменённые предпросмотром: редактор правит
    # урок в любом статусе курса, в том числе скрытый
    return LessonsAdminService(
        lessons=LessonAdminRepo(db), courses=CourseAdminRepo(db), storage=storage
    )


def get_quizzes_admin_service(
    db: Annotated[DbSession, Depends(get_db)],
) -> QuizzesAdminService:
    # Настоящие репозитории, не подменённые предпросмотром: редактор правит
    # тест в любом статусе курса, вместе со скрытыми вопросами
    return QuizzesAdminService(quizzes=QuizAdminRepo(db), courses=CourseAdminRepo(db))


def get_tasks_admin_service(
    db: Annotated[DbSession, Depends(get_db)],
    storage: Annotated[StoragePort, Depends(get_storage)],
) -> TasksAdminService:
    # Хранилище нужно файлу-шаблону: размер и наличие объекта сервер берёт
    # у него, а не у браузера
    return TasksAdminService(
        tasks=TaskAdminRepo(db),
        courses=CourseAdminRepo(db),
        storage=storage,
        cfg=get_settings(),
    )


def get_teachers_admin_service(
    db: Annotated[DbSession, Depends(get_db)],
) -> TeachersAdminService:
    # Настоящие репозитории, не подменённые предпросмотром: карточка учителя
    # показывает его доступы и попытки как они есть, а не глазами режима
    return TeachersAdminService(
        users=UserRepo(db),
        teachers=TeacherAdminRepo(db),
        quizzes=QuizAdminRepo(db),
        enrollments=EnrollmentRepo(db),
        attempts=AttemptRepo(db),
        certificates=CertificateRepo(db),
        sessions=SessionRepo(db),
        notifications=NotificationRepo(db),
    )


def get_reviews_admin_service(db: Annotated[DbSession, Depends(get_db)]) -> ReviewsAdminService:
    return ReviewsAdminService(reviews=ReviewRepo(db))


def get_categories_service(db: Annotated[DbSession, Depends(get_db)]) -> CategoriesService:
    # Один сервис на публичный справочник и на его редактор: список категорий
    # у обоих один и тот же
    return CategoriesService(categories=CategoryRepo(db))


def get_settings_service(
    db: Annotated[DbSession, Depends(get_db)],
    storage: Annotated[StoragePort, Depends(get_storage)],
) -> SettingsService:
    # Хранилище нужно картинкам настроек: наличие объекта и его размер сервер
    # берёт у него, а не у браузера
    return SettingsService(settings=SettingRepo(db), storage=storage, cfg=get_settings())


def get_certificate_pdf_service(
    db: Annotated[DbSession, Depends(get_db)],
    storage: Annotated[StoragePort, Depends(get_storage)],
    site: Annotated[SettingsService, Depends(get_settings_service)],
) -> CertificatePdfService:
    # Настоящий репозиторий, не подменённый предпросмотром: бумагой печатается
    # выданный документ, а в режиме предпросмотра документов не заводится вовсе
    return CertificatePdfService(
        certificates=CertificateRepo(db),
        settings=site,
        storage=storage,
        # Адрес страницы проверки для QR живёт в конфигурации, а не в настройках
        # площадки: он про домен, а не про то, что правит админ
        cfg=get_settings(),
    )


def get_telegram_bind_service(
    db: Annotated[DbSession, Depends(get_db)],
    telegram: Annotated[TelegramPort, Depends(get_telegram)],
) -> TelegramBindService:
    # Порт напрямую, без флагов: привязка отвечает боту на его же команду,
    # а тестовое сообщение админ послал сам — обоих переключатель не касается
    return TelegramBindService(
        settings=SettingRepo(db), telegram=telegram, cfg=get_settings(), commit=db.commit
    )


def get_overview_service(db: Annotated[DbSession, Depends(get_db)]) -> OverviewService:
    return OverviewService(
        users=UserRepo(db),
        courses=CourseRepo(db),
        leads=LeadRepo(db),
        submissions=SubmissionRepo(db),
        messages=ThreadMessageRepo(db),
        certificates=CertificateRepo(db),
    )


def get_reports_service(
    db: Annotated[DbSession, Depends(get_db)],
    completion: Annotated[CertificatesService, Depends(get_certificates_service)],
) -> ReportsService:
    return ReportsService(
        courses=CourseRepo(db),
        # Отчёт админа читает настоящие доступы: синтетический участник
        # предпросмотра не должен появиться в таблице участников курса
        enrollments=EnrollmentRepo(db),
        progress=ProgressRepo(db),
        attempts=AttemptRepo(db),
        certificates=CertificateRepo(db),
        completion=completion,
    )


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
