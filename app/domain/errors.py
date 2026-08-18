"""Ошибки домена. Каждая уходит фронту в едином формате:

    {"error": {"code": "...", "message": "...", "details": {...}}}

`code` — машинное имя для ветвления на фронте, `message` — готовый текст
по-русски, `details` — данные для экрана (сколько попыток осталось и т.п.).
"""


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

    def __init__(self):
        super().__init__("Сертификат уже выдан — результаты теста изменить нельзя")


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
