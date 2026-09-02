"""Репозитории режима предпросмотра: доступ к курсу и видимость курса.

Экран учителя закрыт двумя разными замками, и оба снимаются подменой
репозитория, а не проверкой «а не предпросмотр ли у нас» в каждом сценарии
(BACKEND_NOTES, раздел 12):

- **доступ** — `EnrollmentRepo.active_for`: админу на предпросматриваемый курс
  отдаётся синтетический доступ, которого в базе нет;
- **видимость** — `CourseVisibility.visible_course`: черновик и скрытая версия
  для площадки не существуют, а смотреть админ приходит именно их.

Снимаются они ровно с одного курса — предпросматриваемого: черновик соседнего
курса в режиме остаётся недоступным.
"""

from dataclasses import dataclass

from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.models import Course, Enrollment
from app.adapters.db.repos import (
    AttemptRepo,
    CourseRepo,
    CourseVisibility,
    EnrollmentRepo,
    LessonRepo,
    QuizRepo,
    TaskRepo,
    now_utc,
)


class PreviewEnrollmentRepo(EnrollmentRepo):
    """Тот же репозиторий, но на пару «админ в режиме + предпросматриваемый
    курс» отдаёт доступ, которого в базе нет и не появится."""

    def __init__(self, db: DbSession, *, user_id: int, course_id: int):
        super().__init__(db)
        self.preview_user_id = user_id
        self.preview_course_id = course_id

    def active_for(self, user_id: int, course_id: int, platform: str) -> Enrollment | None:
        # Площадку режим не различает: предпросмотр показывает содержание курса,
        # а не оформление площадки (PLATFORMS_BRIEF, решение 16). Параметр здесь
        # ради общей сигнатуры — синтетический доступ отдаётся на любую
        # запрошенную площадку, и в объект пишется именно она, чтобы дальше
        # сценарий читал у доступа ту же платформу, что спрашивал.
        real = super().active_for(user_id, course_id, platform)
        if real is not None:
            # Настоящий доступ главнее: у админа он по этому курсу быть может,
            # и подменять его синтетическим незачем
            return real
        if user_id != self.preview_user_id or course_id != self.preview_course_id:
            # Режим привязан к курсу и к сессии админа: соседние курсы и чужие
            # доступы считаются как обычно (CONTRACT, сессия 6)
            return None
        # Объект создаётся в памяти и в сессию SQLAlchemy не добавляется:
        # добавленный уехал бы в базу на ближайшем flush, а обещание режима —
        # «счётчик участников не меняется»
        return Enrollment(
            user_id=user_id,
            course_id=course_id,
            granted_by=user_id,
            granted_at=now_utc(),
            platform=platform,
        )


class PreviewVisibility(CourseVisibility):
    """Тот же репозиторий, но предпросматриваемый курс существует для площадки
    в любом статусе. Правило одно на все запросы репозитория, поэтому и
    переопределяется одно условие, а не каждый метод по отдельности."""

    def __init__(self, db: DbSession, *, course_id: int):
        # Дальше по MRO стоит настоящий репозиторий — он и получает сессию
        super().__init__(db)
        self.preview_course_id = course_id

    def visible_course(self) -> ColumnElement[bool]:
        # Общее правило остаётся в силе: снимается статус ровно с одного курса,
        # черновик соседнего в режиме по-прежнему не существует
        return or_(super().visible_course(), Course.id == self.preview_course_id)


class PreviewCourseRepo(PreviewVisibility, CourseRepo):
    """Страница курса, программа и языковые версии — вместе с черновиком."""


class PreviewLessonRepo(PreviewVisibility, LessonRepo):
    """Урок и его материалы у курса, который смотрят."""


class PreviewQuizRepo(PreviewVisibility, QuizRepo):
    """Тест курса, который смотрят."""


class PreviewAttemptRepo(PreviewVisibility, AttemptRepo):
    """Своя попытка теста: в режиме она живёт в памяти, но настоящая попытка
    админа по этому курсу видимостью курса запираться тоже не должна."""


class PreviewTaskRepo(PreviewVisibility, TaskRepo):
    """Задание курса, который смотрят."""


@dataclass(frozen=True)
class VisibilityRepos:
    """Пять репозиториев, которые спрашивают «существует ли курс для площадки».

    Собраны вместе, чтобы `if preview` жил в одном месте: сценарии получают
    готовые объекты и про режим не знают.
    """

    courses: CourseRepo
    lessons: LessonRepo
    quizzes: QuizRepo
    attempts: AttemptRepo
    tasks: TaskRepo


def visibility_repos(db: DbSession, preview_course_id: int | None) -> VisibilityRepos:
    """Набор репозиториев на запрос: вне режима обычные, в режиме — те же,
    но с одним курсом, видимым в любом статусе."""
    if preview_course_id is None:
        return VisibilityRepos(
            courses=CourseRepo(db),
            lessons=LessonRepo(db),
            quizzes=QuizRepo(db),
            attempts=AttemptRepo(db),
            tasks=TaskRepo(db),
        )
    return VisibilityRepos(
        courses=PreviewCourseRepo(db, course_id=preview_course_id),
        lessons=PreviewLessonRepo(db, course_id=preview_course_id),
        quizzes=PreviewQuizRepo(db, course_id=preview_course_id),
        attempts=PreviewAttemptRepo(db, course_id=preview_course_id),
        tasks=PreviewTaskRepo(db, course_id=preview_course_id),
    )
