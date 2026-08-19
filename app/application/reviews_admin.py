"""Отзывы глазами админа: лента по всей платформе, ответ и удаление.

Отдельно от `CoursesService` потому, что тот показывает отзывы странице
курса — читателю, который курс выбирает. Здесь на них смотрят как
на модерацию: чей отзыв, по какому курсу и что с ним делать.

Премодерации нет — отзыв виден сразу, админ отвечает или удаляет постфактум
(DESIGN_BRIEF, 5.24). Поэтому «неопубликованных» здесь не бывает, а удаление
мягкое: у действий админа хранится actor_id (backend/CLAUDE.md).
"""

from app.adapters.db.models import Course, Review, User
from app.adapters.db.repos import ReviewRepo, now_utc
from app.domain.errors import FieldError, NotFoundError

# Тот же потолок, что у отзыва и у сообщения под уроком: длинных текстов
# в этих полях не бывает, а форма ответа — обычное поле ввода.
REPLY_MAX = 2000


class ReviewsAdminService:
    def __init__(self, reviews: ReviewRepo):
        self.reviews = reviews

    # -- GET /admin/reviews ----------------------------------------------

    def admin_list(
        self, *, course_id: int | None, rating: int | None, offset: int, limit: int
    ) -> dict:
        rows, total = self.reviews.admin_page(
            course_id=course_id, rating=rating, offset=offset, limit=limit
        )
        return {
            "items": [
                self._item_out(review, author, course) for review, author, course in rows
            ],
            "total": total,
        }

    # -- POST /admin/reviews/{id}/reply -----------------------------------

    def reply(self, admin: User, review_id: int, text: str) -> dict:
        """Ответ у отзыва один: повторный вызов его меняет, на экране это
        «Изменить ответ». Вторых уровней у ответа не бывает — в отличие
        от вопросов под уроком, поэтому он лежит колонками у самого отзыва."""
        review, author, course = self._found(review_id)
        text = text.strip()
        if not text:
            raise FieldError("text", "Введите текст ответа")
        if len(text) > REPLY_MAX:
            raise FieldError("text", f"Не длиннее {REPLY_MAX} символов")
        review.reply_text = text
        review.reply_by = admin.id
        review.reply_at = now_utc()
        return self._item_out(review, author, course)

    # -- DELETE /admin/reviews/{id} ---------------------------------------

    def delete(self, admin: User, review_id: int) -> None:
        """Удаление мягкое: строка остаётся с отметкой, кто и когда её убрал.
        Наружу удалённый отзыв не приходит нигде и ни при каких фильтрах —
        ни в ленту, ни на страницу курса, ни в среднюю оценку."""
        review = self.reviews.by_id(review_id)
        if review is None:
            raise NotFoundError("Отзыв не найден")
        if review.deleted_at is not None:
            # Повторный вызов ничего не меняет: время остаётся временем
            # первого удаления
            return
        review.deleted_at = now_utc()
        review.deleted_by = admin.id

    # -- сборка ответа ----------------------------------------------------

    def _found(self, review_id: int) -> tuple[Review, User, Course]:
        found = self.reviews.with_author_and_course(review_id)
        if found is None or found[0].deleted_at is not None:
            # Удалённый отзыв для админа тоже не существует: отвечать на него
            # некому и негде — со страницы курса он уже снят
            raise NotFoundError("Отзыв не найден")
        return found

    @staticmethod
    def _item_out(review: Review, author: User, course: Course) -> dict:
        return {
            "id": review.id,
            "rating": review.rating,
            "text": review.text,
            "created_at": review.created_at,
            "updated_at": review.updated_at,
            "course": {"id": course.id, "lang": course.lang, "title": course.title},
            # ФИО тремя полями, как в заявках и очереди работ: собирает его фронт
            "teacher": {
                "id": author.id,
                "last_name": author.last_name,
                "first_name": author.first_name,
                "middle_name": author.middle_name,
                "school": author.school,
                "city": author.city,
            },
            "reply": (
                {"text": review.reply_text, "created_at": review.reply_at}
                if review.reply_text is not None
                else None
            ),
        }
