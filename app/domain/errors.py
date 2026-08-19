"""Ошибки домена. Каждая уходит фронту в едином формате:

    {"error": {"code": "...", "message": "...", "details": {...}}}

`code` — машинное имя для ветвления на фронте, `message` — готовый текст
по-русски, `details` — данные для экрана (сколько попыток осталось и т.п.).
"""

from app.domain.plural import plural


class AppError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationAppError(AppError):
    status = 422
    code = "validation_error"


class FieldError(ValidationAppError):
    """Ошибка одного поля формы: экран подсвечивает поле по имени, поэтому
    текст уходит внутри details.fields, а не только общим сообщением."""

    def __init__(self, field: str, message: str):
        super().__init__(
            "Проверьте заполнение полей",
            details={"fields": [{"field": field, "message": message}]},
        )


class UnauthorizedError(AppError):
    status = 401
    code = "unauthorized"

    def __init__(self, message: str = "Нужно войти", **kw):
        super().__init__(message, **kw)


class ForbiddenError(AppError):
    status = 403
    code = "forbidden"


class BlockedError(AppError):
    """Учитель заблокирован админом — is_blocked у пользователя."""

    status = 403
    code = "blocked"

    def __init__(self, message: str = "Доступ заблокирован. Напишите нам, если это ошибка.", **kw):
        super().__init__(message, **kw)


class NotFoundError(AppError):
    status = 404
    code = "not_found"

    def __init__(self, message: str = "Не найдено", **kw):
        super().__init__(message, **kw)


class ConflictError(AppError):
    status = 409
    code = "conflict"


# Состояния кнопки «Записаться»: доступ уже есть / набор закрыт.


class AlreadyEnrolledError(AppError):
    status = 409
    code = "already_enrolled"

    def __init__(self, message: str = "Доступ к курсу уже открыт"):
        super().__init__(message)


class EnrollmentClosedError(AppError):
    status = 409
    code = "enrollment_closed"

    def __init__(self):
        super().__init__("Набор на курс закрыт — заявку отправить нельзя")


# Состояния экрана теста: попытка потрачена, сертификат выдан, тест пуст.


class AttemptUsedError(AppError):
    status = 409
    code = "attempt_used"

    def __init__(self):
        super().__init__("Попытка уже использована — тест нельзя пройти повторно")


class CertificateIssuedError(AppError):
    """Сертификат выдан — результат под ним больше не меняется, даже
    у пересдаваемого теста."""

    status = 409
    code = "certificate_issued"

    def __init__(self, message: str = "Сертификат уже выдан — результаты теста изменить нельзя"):
        super().__init__(message)


class QuizEmptyError(AppError):
    status = 409
    code = "quiz_empty"

    def __init__(self):
        super().__init__("Тест ещё наполняется — вопросов пока нет")


class AttemptFinishedError(AppError):
    status = 409
    code = "attempt_finished"

    def __init__(self):
        super().__init__("Попытка уже завершена")


class TimeExpiredError(AppError):
    """Ответ не сохранён: время вышло. Фронт зовёт finish — счёт пойдёт
    по тому, что человек успел ответить."""

    status = 409
    code = "time_expired"

    def __init__(self):
        super().__init__("Время истекло — ответы отправлены на подсчёт")


class AttemptNotFinishedError(AppError):
    status = 409
    code = "attempt_not_finished"

    def __init__(self):
        super().__init__("Сначала завершите тест")


class ReviewUnavailableError(AppError):
    status = 403
    code = "review_unavailable"

    def __init__(self):
        super().__init__("Разбор у этого теста не показывается")


# Состояния экрана задания: работа ждёт проверки, задание закрыто, вердикт стоит.


class SubmissionPendingError(AppError):
    status = 409
    code = "submission_pending"

    def __init__(self):
        super().__init__("Работа уже на проверке — дождитесь ответа")


class TaskAcceptedError(AppError):
    status = 409
    code = "task_accepted"

    def __init__(self):
        super().__init__("Задание уже зачтено")


class AlreadyReviewedError(AppError):
    """Вердикт ставится один раз: передумал — учитель пришлёт доработку,
    и у неё будет свой вердикт."""

    status = 409
    code = "already_reviewed"

    def __init__(self):
        super().__init__("Работа уже проверена")


class NoFileError(ValidationAppError):
    """Поля file нет или файл пустой: для человека это один и тот же случай —
    он не выбрал файл, — поэтому и ответ один."""

    def __init__(self):
        super().__init__(
            "Файл не выбран",
            details={"fields": [{"field": "file", "message": "Field required"}]},
        )


class FileTooLargeError(AppError):
    status = 413
    code = "file_too_large"

    def __init__(self, max_size_mb: int):
        super().__init__(
            f"Файл больше {max_size_mb} МБ", details={"max_size_mb": max_size_mb}
        )


class RateLimitedError(AppError):
    """Слишком часто. В details всегда retry_after_sec — фронт рисует таймер."""

    status = 429
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after_sec: int):
        super().__init__(message, details={"retry_after_sec": retry_after_sec})


# Состояния экрана входа: неверный код / код истёк / ввод заблокирован.


class WrongCodeError(AppError):
    status = 400
    code = "wrong_code"

    def __init__(self, attempts_left: int):
        super().__init__("Неверный код", details={"attempts_left": attempts_left})


class CodeExpiredError(AppError):
    status = 400
    code = "code_expired"

    def __init__(self):
        super().__init__("Код устарел — он действует 5 минут. Запросите новый.")


class TooManyAttemptsError(AppError):
    status = 429
    code = "too_many_attempts"

    def __init__(self, retry_after_sec: int):
        super().__init__(
            "Ввод кода заблокирован на 10 минут",
            details={"retry_after_sec": retry_after_sec},
        )


# Состояния выдачи сертификата: условия не выполнены, попытка ещё идёт.


class ConditionsNotMetError(AppError):
    """Чек-лист уходит в details целиком: экран должен показать, чего не
    хватает, а не просто отказ."""

    status = 409
    code = "conditions_not_met"

    def __init__(self, conditions: list[dict]):
        super().__init__(
            "Условия сертификата ещё не выполнены",
            details={"conditions": conditions},
        )


class AttemptInProgressError(AppError):
    """Незавершённая попытка ещё может поменять зачёт: выдать сертификат
    сейчас — значит выдать его по неокончательному результату."""

    status = 409
    code = "attempt_in_progress"

    def __init__(self, message: str = "Завершите начатый тест — он ещё может изменить зачёт"):
        super().__init__(message)


class ProfileIncompleteError(AppError):
    """Имя печатается на бумаге и остаётся снимком: выдать документ с пустым
    ФИО — значит потом только отзывать его и выдавать заново."""

    status = 409
    code = "profile_incomplete"

    def __init__(self):
        super().__init__("Заполните фамилию и имя — они печатаются на сертификате")


# Правки курса, где уже учатся (BACKEND_NOTES, раздел 10). Все пятеро говорят
# одно и то же разными словами: у этой строки есть чужие данные. Отдельные
# коды нужны потому, что дальше экран предлагает разное — скрыть, создать
# новый или не трогать вовсе.


class HasProgressError(AppError):
    """Урок с чьим-то прогрессом не удаляется, а скрывается: иначе «12 из 18»
    превратится в «12 из 17», а условия сертификата выполнятся сами собой."""

    status = 409
    code = "has_progress"

    def __init__(self, progress_count: int):
        super().__init__(
            f"Урок {plural(progress_count, 'прошёл', 'прошли', 'прошли')} "
            f"{progress_count} {plural(progress_count, 'человек', 'человека', 'человек')}"
            " — его можно скрыть, но не удалить",
            details={"progress_count": progress_count},
        )


class HasAttemptsError(AppError):
    """Вопрос, попавший в состав попытки, не редактируется: разбор ответов
    покажет человеку то, чего он не видел. Тот же код — у теста с попытками,
    там он запрещает удаление."""

    status = 409
    code = "has_attempts"

    def __init__(self, message: str, attempts_count: int):
        super().__init__(message, details={"attempts_count": attempts_count})


class HasSubmissionsError(AppError):
    """Задание со сдачами не удаляется: работы людей ссылались бы в пустоту."""

    status = 409
    code = "has_submissions"

    def __init__(self, submissions_count: int):
        super().__init__(
            f"На задание сдали {submissions_count} "
            f"{plural(submissions_count, 'работу', 'работы', 'работ')}"
            " — удалить его нельзя",
            details={"submissions_count": submissions_count},
        )


class CourseInUseError(AppError):
    """Курс, которого кто-то коснулся, не удаляется: строка отчёта, ведущая
    на несуществующий курс, дороже лишней кнопки в меню."""

    status = 409
    code = "course_in_use"

    def __init__(self, *, enrollments: int, leads: int, certificates: int):
        super().__init__(
            "Курс нельзя удалить: "
            f"{enrollments} {plural(enrollments, 'доступ', 'доступа', 'доступов')}, "
            f"{leads} {plural(leads, 'заявка', 'заявки', 'заявок')}, "
            f"{certificates} {plural(certificates, 'сертификат', 'сертификата', 'сертификатов')}",
            details={
                "enrollments": enrollments,
                "leads": leads,
                "certificates": certificates,
            },
        )


class ModuleInUseError(AppError):
    """Что именно держит модуль — уходит списком: иначе методист удаляет
    элементы наугад, пока не угадает."""

    status = 409
    code = "module_in_use"

    def __init__(self, items: list[dict]):
        super().__init__(
            "В модуле есть элементы с чужими данными — сначала разберитесь с ними",
            details={"items": items},
        )


# Состояния редактора курса: вторая языковая версия и итоговый тест.


class VersionExistsError(AppError):
    """Версий на одном языке в группе не бывает — так устроен уникальный
    индекс (group_id, lang), и переключатель РУС|ҚАЗ на этом держится."""

    status = 409
    code = "version_exists"

    def __init__(self, message: str, course_id: int):
        super().__init__(message, details={"course_id": course_id})


class FinalQuizExistsError(AppError):
    """Итоговый тест в курсе один: отчёт берёт первый попавшийся, а чек-лист
    сертификата пишет «Сдать итоговый тест» в единственном числе."""

    status = 409
    code = "final_quiz_exists"

    def __init__(self, title: str, quiz_id: int):
        super().__init__(
            f"Итоговый тест в курсе уже есть: «{title}»",
            details={"quiz_id": quiz_id},
        )


# Карточка учителя: блокировка, смена номера, пересдача.


class PhoneTakenError(AppError):
    """Номер уникален: вход идёт по нему, и два человека на одном номере —
    это два человека, входящих друг за друга."""

    status = 409
    code = "phone_taken"

    def __init__(self, user_id: int):
        super().__init__("Этот номер уже у другого учителя", details={"user_id": user_id})


class SelfBlockError(AppError):
    """Иначе платформа остаётся без администратора, а чинить это будет некому:
    заблокированный не войдёт и снять блокировку с себя не сможет."""

    status = 409
    code = "self_block"

    def __init__(self):
        super().__init__("Нельзя заблокировать самого себя")


class QuizRetakableError(AppError):
    """У пересдаваемого теста попыток не ограничено — разрешать нечего."""

    status = 409
    code = "quiz_retakable"

    def __init__(self):
        super().__init__("Тест и так пересдаваемый — попыток не ограничено")


class NoAttemptError(AppError):
    status = 409
    code = "no_attempt"

    def __init__(self):
        super().__init__("Человек ещё не проходил этот тест")


# Настройки платформы: категории и бот.


class CategoryInUseError(AppError):
    """category_id у курса обязателен: осиротевший курс исчезнет из каталога."""

    status = 409
    code = "category_in_use"

    def __init__(self, message: str, courses_count: int):
        super().__init__(message, details={"courses_count": courses_count})


class CategoryExistsError(AppError):
    status = 409
    code = "category_exists"

    def __init__(self, category_id: int):
        super().__init__("Такая категория уже есть", details={"category_id": category_id})


class BotNotConfiguredError(AppError):
    """Токен бота живёт в конфигурации сервиса, а не в настройках платформы:
    это ключ доступа, и его место рядом с паролем базы, а не в админке."""

    status = 409
    code = "bot_not_configured"

    def __init__(self):
        super().__init__("Бот не настроен на сервере — задайте токен в конфигурации")


class TelegramNotConnectedError(AppError):
    status = 409
    code = "telegram_not_connected"

    def __init__(self):
        super().__init__("Бот не подключён — сначала привяжите чат")


class TelegramFailedError(AppError):
    """Единственное место, где сбой доставки виден админу: он сам нажал
    «отправить тестовое». Настоящие уведомления так себя не ведут — заявка
    пишется в базу до отправки и недоступный бот ей не мешает."""

    status = 502
    code = "telegram_failed"

    def __init__(self):
        super().__init__("Telegram не принял сообщение — попробуйте позже")


class NotReadyError(AppError):
    """Сервис поднят, но работать не может: база недоступна.

    Отдельно от `internal_error`, потому что читает это не человек,
    а балансировщик: пятисотый он считает случайной ошибкой запроса,
    а 503 — поводом снять инстанс с трафика.
    """

    status = 503
    code = "not_ready"

    def __init__(self):
        super().__init__("Сервис не готов принимать запросы")
