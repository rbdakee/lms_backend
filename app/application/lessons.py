"""Экран урока: содержимое, материалы и отметка «пройден».

Строгий порядок содержимое не закрывает — это правило показа: `locked`
в программе рисует замок, а платный курс от чужого защищает enrollment,
который проверяется здесь на каждый запрос.
"""

from app.adapters.db.models import Course, Lesson, User
from app.adapters.db.repos import CourseRepo, EnrollmentRepo, LessonRepo, ProgressRepo
from app.application.program import course_progress
from app.application.ratelimit import SlidingWindowLimiter
from app.domain.errors import ForbiddenError, NotFoundError, RateLimitedError

# Отказ в доступе к содержимому урока и к его видео экран показывает
# в разных местах, поэтому и тексты разные (CONTRACT, сессия 4).
LESSON_DENIED = "Урок откроется после выдачи доступа к курсу"
PLAYBACK_DENIED = "Доступ к курсу закрыт"


class LessonsService:
    def __init__(
        self,
        courses: CourseRepo,
        lessons: LessonRepo,
        enrollments: EnrollmentRepo,
        progress: ProgressRepo,
        playback_limiter: SlidingWindowLimiter,
        preview_course_id: int | None,
    ):
        self.courses = courses
        self.lessons = lessons
        self.enrollments = enrollments
        self.progress = progress
        self.playback_limiter = playback_limiter
        # Курс, который админ смотрит «как учитель»: по нему ничего не пишется
        self.preview_course_id = preview_course_id

    def lesson_page(self, user: User, lesson_id: int) -> dict:
        lesson, _ = self._accessible(user, lesson_id)
        return {
            "id": lesson.id,
            "module_id": lesson.module_id,
            "title": lesson.title,
            "kind": lesson.kind,
            # JSON как его положил админ: форма содержимого фиксируется в сессии 7
            "body": lesson.body,
            "duration_label": lesson.duration_label,
            "time_required_min": lesson.time_required_min,
            "is_completed": self.progress.is_lesson_done(user.id, lesson.id),
            # Ссылки на файл здесь нет — за ней идут отдельно, GET /files/{id}
            "files": [
                {
                    "id": file.id,
                    "name": file.name,
                    "size_bytes": file.size_bytes,
                    "mime": file.mime,
                }
                for file in self.lessons.files(lesson.id)
            ],
        }

    def complete(self, user: User, lesson_id: int) -> dict:
        lesson, course = self._accessible(user, lesson_id)
        preview = course.id == self.preview_course_id
        if not preview:
            self.progress.mark_lesson_done(user.id, lesson.id)
        # Прогресс считаем уже с новой отметкой: экран сразу перерисовывает
        # «N из M» и кнопку «Далее», не дожидаясь второго запроса. В режиме
        # предпросмотра отметки нет, и счётчики придут прежними: писать их
        # некуда (BACKEND_NOTES, раздел 12)
        return {
            "is_completed": True,
            **course_progress(
                self.courses, self.progress, course, user.id, preview=preview
            ),
        }

    def playback(self, user: User, lesson_id: int) -> dict:
        """Ссылка для плеера. Доступ проверяется на каждый вызов: отозвал админ
        доступ посреди просмотра — следующий запрос вернёт отказ, а не ссылку."""
        # Лимит впереди всего остального: он затем и нужен, чтобы качалка
        # не ходила в базу на каждый запрос
        retry_after = self.playback_limiter.hit(str(user.id))
        if retry_after:
            raise RateLimitedError(
                "Слишком много запросов — попробуйте через несколько секунд",
                retry_after_sec=retry_after,
            )
        lesson, _ = self._accessible(user, lesson_id, denied=PLAYBACK_DENIED)
        if lesson.kind != "video" or lesson.video_url is None:
            # Нечего отдавать. Спрашиваем вид, а не одну ссылку: редактор
            # не стирает video_url при смене вида на текстовый, и оставшаяся
            # от прежней жизни ссылка не должна уходить в плеер.
            # Проверка после доступа: у чужого курса приходит 403,
            # содержимое урока в отказ не просачивается
            raise NotFoundError("Урок не найден")
        return {
            "provider": lesson.video_provider,
            "url": lesson.video_url,
            # YouTube-ссылка не протухает; со своим хостингом здесь будет срок
            "expires_at": None,
        }

    def _accessible(
        self, user: User, lesson_id: int, denied: str = LESSON_DENIED
    ) -> tuple[Lesson, Course]:
        """Порядок проверок один на все эндпоинты урока: существование, потом
        доступ, — поэтому у чужого курса приходит 403, а не 404."""
        found = self.lessons.visible_with_course(lesson_id)
        if found is None:
            raise NotFoundError("Урок не найден")
        lesson, course = found
        if self.enrollments.active_for(user.id, course.id) is None:
            raise ForbiddenError(denied)
        return lesson, course
