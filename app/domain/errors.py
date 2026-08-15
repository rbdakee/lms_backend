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
