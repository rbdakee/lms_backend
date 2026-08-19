"""Вопросы под уроком и сводная очередь вопросов в админке.

Тред ровно в два уровня: вопрос и плоский список ответов. Отвечает и админ,
и любой учитель с доступом к курсу — «часто коллега отвечает быстрее»
(DESIGN_BRIEF). Отдельного права на ответ нет.
"""

from app.adapters.db.models import Course, Lesson, ThreadMessage, User
from app.adapters.db.repos import (
    CourseRepo,
    EnrollmentRepo,
    LessonRepo,
    NotificationRepo,
    ThreadMessageRepo,
    now_utc,
)
from app.application.ratelimit import SlidingWindowLimiter
from app.domain.errors import FieldError, ForbiddenError, NotFoundError, RateLimitedError

# Отказ показывается прямо в блоке вопросов, поэтому текст свой,
# не тот, что у закрытого урока (CONTRACT, сессия 6).
QUESTIONS_DENIED = "Доступ к курсу не открыт"
TEXT_MAX = 2000


def _author_name(author: User) -> str:
    # Как в отзывах: имя собирается на сервере, инициалы фронт считает сам
    return " ".join(
        part for part in (author.last_name, author.first_name, author.middle_name) if part
    )


def _message_out(message: ThreadMessage, author: User) -> dict:
    return {
        "id": message.id,
        "text": message.text,
        "author_name": _author_name(author),
        # Отдельной колонки под это нет: признак берётся у автора в момент чтения
        "author_is_admin": author.is_admin,
        "created_at": message.created_at,
    }


class QuestionsService:
    def __init__(
        self,
        courses: CourseRepo,
        lessons: LessonRepo,
        enrollments: EnrollmentRepo,
        messages: ThreadMessageRepo,
        notifications: NotificationRepo,
        limiter: SlidingWindowLimiter,
        preview_course_id: int | None,
    ):
        self.courses = courses
        self.lessons = lessons
        self.enrollments = enrollments
        self.messages = messages
        self.notifications = notifications
        self.limiter = limiter
        # Курс, который админ смотрит «как учитель»: по нему ничего не пишется
        self.preview_course_id = preview_course_id

    # -- GET /lessons/{id}/questions ------------------------------------

    def lesson_questions(self, user: User, lesson_id: int, offset: int, limit: int) -> dict:
        self._accessible(user, lesson_id)
        roots, total = self.messages.roots_page(lesson_id, offset, limit)
        replies = self.messages.replies_for([root.id for root, _ in roots])
        return {
            "items": [
                {
                    **_message_out(root, author),
                    # Пустой список и есть признак «ждёт ответа»: отдельного
                    # статуса у вопроса нет (CONTRACT, сессия 6)
                    "replies": [
                        _message_out(reply, reply_author)
                        for reply, reply_author in replies.get(root.id, [])
                    ],
                }
                for root, author in roots
            ],
            "total": total,
        }

    # -- POST /lessons/{id}/questions -----------------------------------

    def add_message(
        self, user: User, lesson_id: int, text: str, parent_id: int | None
    ) -> dict:
        lesson, course = self._accessible(user, lesson_id)
        # Лимит стоит до разбора текста: иначе спам мимо валидации бесплатен.
        # Админа он не касается: тот отвечает из очереди вопросов подряд
        # и упёрся бы в потолок на четвёртом ответе (CONTRACT, сессия 6).
        if not user.is_admin:
            retry_after = self.limiter.hit(str(user.id))
            if retry_after:
                raise RateLimitedError(
                    "Слишком часто, попробуйте позже", retry_after_sec=retry_after
                )

        text = text.strip()
        if not text:
            raise FieldError("text", "Введите текст")
        if len(text) > TEXT_MAX:
            raise FieldError("text", f"Не длиннее {TEXT_MAX} символов")

        parent = self._parent(lesson_id, parent_id)
        if course.id == self.preview_course_id:
            # Ранний выход: ни сообщения в треде, ни уведомления автору вопроса
            # (BACKEND_NOTES, раздел 12). Режим привязан к курсу, поэтому ответы
            # админа из очереди вопросов по остальным курсам пишутся как всегда.
            # Объект создан в памяти и в сессию SQLAlchemy не добавлен
            preview_message = ThreadMessage(
                id=0,
                parent_id=parent.id if parent is not None else None,
                lesson_id=lesson.id,
                course_id=course.id,
                user_id=user.id,
                text=text,
                created_at=now_utc(),
            )
            return {**_message_out(preview_message, user), "replies": []}
        message = self.messages.create(
            lesson_id=lesson.id,
            course_id=course.id,
            user_id=user.id,
            text_=text,
            parent_id=parent.id if parent is not None else None,
        )
        if parent is not None and parent.user_id != user.id:
            # На собственный ответ уведомление не приходит (CONTRACT, сессия 6)
            self.notifications.create(
                parent.user_id,
                "answer_posted",
                {
                    "lesson_id": lesson.id,
                    "lesson_title": lesson.title,
                    "course_id": course.id,
                    "message_id": message.id,
                },
            )
        return {**_message_out(message, user), "replies": []}

    # -- GET /admin/questions -------------------------------------------

    def admin_list(
        self,
        *,
        answered: bool | None,
        course_id: int | None,
        q: str | None,
        offset: int,
        limit: int,
    ) -> dict:
        rows, total = self.messages.admin_page(
            answered=answered, course_id=course_id, q=q, offset=offset, limit=limit
        )
        replies = self.messages.replies_for([message.id for message, _, _, _ in rows])
        numbers = self.courses.lesson_numbers(sorted({course.id for _, _, course, _ in rows}))
        return {
            "items": [
                self._admin_out(message, author, course, lesson, numbers, replies)
                for message, author, course, lesson in rows
            ],
            "total": total,
        }

    # -- общее -----------------------------------------------------------

    def _accessible(self, user: User, lesson_id: int) -> tuple[Lesson, Course]:
        """Те же правила, что у самого урока (`application/lessons.py`):
        сначала существование, потом доступ, — поэтому у чужого курса
        приходит 403, а не 404.

        Админу enrollment не нужен: отвечает он из своей очереди вопросов,
        а доступа к каждому курсу у него нет и не будет (CONTRACT, сессия 6).
        """
        found = self.lessons.visible_with_course(lesson_id)
        if found is None:
            raise NotFoundError("Урок не найден")
        lesson, course = found
        if not user.is_admin and self.enrollments.active_for(user.id, course.id) is None:
            raise ForbiddenError(QUESTIONS_DENIED)
        return lesson, course

    def _parent(self, lesson_id: int, parent_id: int | None) -> ThreadMessage | None:
        """Корень этого же урока — или отказ. Второго уровня вложенности
        нет и не будет, поэтому проверка серверная, а не на клиенте."""
        if parent_id is None:
            return None
        parent = self.messages.by_id(parent_id)
        if parent is None or parent.parent_id is not None or parent.lesson_id != lesson_id:
            raise FieldError("parent_id", "Отвечать можно только на вопрос")
        return parent

    @staticmethod
    def _admin_out(
        message: ThreadMessage,
        author: User,
        course: Course,
        lesson: Lesson,
        numbers: dict[int, int],
        replies: dict[int, list[tuple[ThreadMessage, User]]],
    ) -> dict:
        return {
            "id": message.id,
            "text": message.text,
            "created_at": message.created_at,
            # ФИО собирает фронт, как в заявках и очереди работ
            "teacher": {
                "id": author.id,
                "last_name": author.last_name,
                "first_name": author.first_name,
                "middle_name": author.middle_name,
            },
            "course": {"id": course.id, "title": course.title},
            "lesson": {
                "id": lesson.id,
                # Скрытый урок в нумерации не участвует — номера у него нет
                "number": numbers.get(lesson.id, 0),
                "title": lesson.title,
            },
            "replies": [
                _message_out(reply, reply_author)
                for reply, reply_author in replies.get(message.id, [])
            ],
        }
