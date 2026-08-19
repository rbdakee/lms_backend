"""Режим «Предпросмотр как учитель»: вход, выход и признак включённого режима.

Флаг живёт в серверной сессии, а не в адресе и не в куке: обещание «ничего
не записывается» держит сервер, а `?preview=1` был времянкой прототипа
(BACKEND_NOTES, раздел 12).
"""

from dataclasses import dataclass

from app.adapters.db.models import Session
from app.adapters.db.repos import CourseRepo
from app.application.preview_quiz import PreviewAttemptStore
from app.domain.errors import NotFoundError


@dataclass(frozen=True)
class Preview:
    """Включённый режим: чья сессия и какой курс. `None` вместо этого объекта —
    режим выключен, и вся система работает как обычно."""

    session_id: str
    user_id: int
    course_id: int


class PreviewService:
    def __init__(self, courses: CourseRepo, attempts: PreviewAttemptStore):
        self.courses = courses
        self.attempts = attempts

    def enter(self, session: Session, course_id: int) -> None:
        # by_id, а не visible_by_id: черновик и скрытую версию админ и приходит
        # смотреть — ради этого режим и заведён (CONTRACT, сессия 6)
        if self.courses.by_id(course_id) is None:
            raise NotFoundError("Курс не найден")
        # Смена курса выбрасывает попытку прошлого предпросмотра: попытка
        # у сессии одна, и от прежнего курса ей остаться нечего
        if session.preview_course_id != course_id:
            # Только смена курса: фронт зовёт enter при открытии экрана,
            # и повторный вход в тот же курс не должен ронять начатый тест
            self.attempts.drop(str(session.id))
        session.preview_course_id = course_id

    def exit(self, session: Session) -> None:
        """Выход чистит и флаг сессии, и попытку из памяти. Идемпотентен:
        кнопку «Выйти» нажимают дважды (CONTRACT, сессия 6)."""
        session.preview_course_id = None
        self.attempts.drop(str(session.id))
