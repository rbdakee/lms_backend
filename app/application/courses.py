"""Каталог, страница курса, отзывы и «мои курсы». Формы словарей здесь —
ровно формы ответов из CONTRACT.md: роутеры их только заворачивают в схемы.
"""

from collections.abc import Iterator

from app.adapters.db.models import Course, Review, User
from app.adapters.db.repos import (
    CourseRepo,
    EnrollmentRepo,
    LeadRepo,
    ProgressRepo,
    ReviewRepo,
    now_utc,
)
from app.application.ports import StoragePort
from app.application.program import build_program, course_progress, with_statuses
from app.config import Settings
from app.domain.errors import ForbiddenError, NotFoundError
from app.domain.kz_time import waiting_days
from app.domain.program import progress_of
from app.domain.submission import mime_of

NO_COVER = "Обложка не найдена"


def cover_url(course: Course, base_url: str) -> str | None:
    """Обложка курса наружу — всегда строка-адрес: её рисует и каталог,
    и карточка «моих курсов», и превью в редакторе. Форма поля от того,
    что обложку теперь загружают файлом, не меняется.

    Загружена картинка — отдаём адрес публичной раздачи; ключ объекта наружу
    не уходит, как и у картинок настроек. Не загружена — отдаём то, что лежит
    в старой колонке `cover`: у курсов, заведённых до сессии 7в, там внешний
    адрес, вписанный руками. Это переходное чтение, а не вторая равноправная
    возможность — задать такой адрес через API уже нельзя.
    """
    if course.cover_key:
        return f"{base_url}/courses/{course.id}/cover"
    return course.cover


def _lang_order(versions: list[Course]) -> list[Course]:
    # ru перед kz — в порядке бейджа RU·KZ на карточке
    return sorted(versions, key=lambda c: 0 if c.lang == "ru" else 1)


def _review_out(review: Review, author: User) -> dict:
    name = " ".join(
        part for part in (author.last_name, author.first_name, author.middle_name) if part
    )
    return {
        "id": review.id,
        "author_name": name,
        "school": author.school,
        "city": author.city,
        "rating": review.rating,
        "text": review.text,
        "created_at": review.created_at,
        # Ответ админа лежит колонками у самого отзыва: он один, вторых
        # уровней у него не бывает. Имени отвечающего наружу нет — на экране
        # он подписан просто «Администратор».
        "reply": (
            {"text": review.reply_text, "created_at": review.reply_at}
            if review.reply_text is not None
            else None
        ),
    }


class CoursesService:
    def __init__(
        self,
        courses: CourseRepo,
        reviews: ReviewRepo,
        leads: LeadRepo,
        enrollments: EnrollmentRepo,
        progress: ProgressRepo,
        storage: StoragePort,
        cfg: Settings,
        preview_course_id: int | None,
    ):
        self.courses = courses
        self.reviews = reviews
        self.leads = leads
        self.enrollments = enrollments
        self.progress = progress
        # Хранилище и адрес раздачи нужны обложке: байты лежат в приватном
        # хранилище, а наружу уходит адрес публичного маршрута
        self.storage = storage
        self.cfg = cfg
        # Курс, который админ смотрит «как учитель»: по нему ничего не пишется
        self.preview_course_id = preview_course_id

    # -- каталог --------------------------------------------------------

    def catalog(self, platform: str) -> list[dict]:
        versions = self.courses.catalog()
        if not versions:
            return []
        ids = [c.id for c in versions]
        lessons = self.courses.lessons_count(ids)
        students = self.courses.students_count(ids, platform)
        ratings = self.courses.group_ratings(list({c.group_id for c in versions}))

        groups: dict[int, list[Course]] = {}
        for course in versions:
            groups.setdefault(course.group_id, []).append(course)

        items = []
        # Свежие курсы сверху: группы по самой новой версии
        ordered = sorted(
            groups.items(), key=lambda kv: max(c.created_at for c in kv[1]), reverse=True
        )
        for group_id, group in ordered:
            group = _lang_order(group)
            rating, reviews_count = ratings.get(group_id, (None, 0))
            items.append(
                {
                    "group_id": group_id,
                    "langs": [c.lang for c in group],
                    "rating": rating,
                    "reviews_count": reviews_count,
                    "versions": [self._card(c, lessons, students) for c in group],
                }
            )
        return items

    def _card(
        self, course: Course, lessons_count: dict[int, int], students_count: dict[int, int]
    ) -> dict:
        return {
            "id": course.id,
            "lang": course.lang,
            "title": course.title,
            "category_id": course.category_id,
            "cover": cover_url(course, self.cfg.public_base_url),
            "hours": course.hours,
            "duration_text": course.duration_text,
            "price": course.price,
            "status": course.status,
            "starts_at": course.starts_at,
            "created_at": course.created_at,
            "lessons_count": lessons_count.get(course.id, 0),
            "students_count": students_count.get(course.id, 0),
        }

    # -- GET /courses/{id}/cover ----------------------------------------

    def cover(self, course_id: int) -> tuple[str, int, Iterator[bytes]]:
        """Байты обложки: тип, размер и поток. Курса нет, обложку не загружали,
        объект из хранилища пропал — для открывшего адрес это один и тот же
        404, и перебирать номера курсов незачем.

        Статус курса здесь не проверяется, в отличие от страницы курса:
        ту же картинку показывает превью в редакторе, а редактируют как раз
        черновик. Наружу от этого открыта картинка, а не содержимое курса.
        """
        course = self.courses.by_id(course_id)
        if course is None or not course.cover_key:
            raise NotFoundError(NO_COVER)
        size = self.storage.size(course.cover_key)
        if size is None:
            # Ключ у курса есть, объекта нет: для открывшего тот же 404
            raise NotFoundError(NO_COVER)
        return (
            mime_of(course.cover_name or ""),
            size,
            self.storage.read(course.cover_key),
        )

    # -- страница курса -------------------------------------------------

    def _visible(self, course_id: int) -> Course:
        course = self.courses.visible_by_id(course_id)
        if course is None:
            raise NotFoundError("Курс не найден")
        return course

    def course_page(self, course_id: int, user: User | None, platform: str) -> dict:
        course = self._visible(course_id)
        lessons = self.courses.lessons_count([course.id])
        students = self.courses.students_count([course.id], platform)
        rating, reviews_count = self.courses.group_ratings([course.group_id]).get(
            course.group_id, (None, 0)
        )
        chips = [
            {"id": c.id, "lang": c.lang, "title": c.title, "status": c.status}
            for c in _lang_order(self.courses.group_versions(course.group_id))
        ]
        # Программа собирается один раз: она же нужна счётчикам прогресса
        program = build_program(self.courses, course.id)
        data = self._card(course, lessons, students)
        data.update(
            {
                "short": course.short,
                "full": course.full,
                "versions": chips,
                "rating": rating,
                "reviews_count": reviews_count,
                # Порядок прохождения виден ещё до выдачи доступа; сами статусы
                # элементов считает GET /courses/{id}/program
                "strict_order": course.strict_order,
                "program": program,
                "access": self._access(course, user, program, platform),
            }
        )
        return data

    def program_page(self, user: User, course_id: int, platform: str) -> dict:
        """Сайдбар экрана урока. Порядок проверок общий: вход (роутер),
        существование курса, потом доступ."""
        course = self._visible(course_id)
        if self.enrollments.active_for(user.id, course.id, platform) is None:
            raise ForbiddenError("Доступ к курсу не открыт")
        program = build_program(self.courses, course.id)
        return {
            "program": with_statuses(
                self.progress,
                course,
                user.id,
                program,
                platform,
                preview=course.id == self.preview_course_id,
            )
        }

    def _access(
        self, course: Course, user: User | None, program: list[dict], platform: str
    ) -> dict:
        if user is None:
            return {"state": "none"}
        if self.enrollments.active_for(user.id, course.id, platform) is not None:
            return {
                "state": "granted",
                **progress_of(
                    with_statuses(
                        self.progress,
                        course,
                        user.id,
                        program,
                        platform,
                        preview=course.id == self.preview_course_id,
                    )
                ),
            }
        lead = self.leads.open_for(user.id, course.id, platform)
        if lead is not None:
            return {
                "state": "requested",
                "waiting_days": waiting_days(lead.created_at, now_utc()),
            }
        return {"state": "none"}

    # -- отзывы ---------------------------------------------------------

    def reviews_page(self, course_id: int, offset: int, limit: int) -> dict:
        self._visible(course_id)
        breakdown = self.reviews.breakdown(course_id)
        authors_total = sum(breakdown.values())
        rating = (
            round(sum(star * n for star, n in breakdown.items()) / authors_total, 1)
            if authors_total
            else None
        )
        return {
            "items": [
                _review_out(review, author)
                for review, author in self.reviews.page(course_id, offset, limit)
            ],
            "total": self.reviews.count(course_id),
            "rating": rating,
            "breakdown": {str(star): breakdown.get(star, 0) for star in (5, 4, 3, 2, 1)},
        }

    def add_review(
        self, user: User, course_id: int, rating: int, text: str, platform: str
    ) -> dict:
        self._visible(course_id)
        if self.enrollments.active_for(user.id, course_id, platform) is None:
            raise ForbiddenError("Отзыв может оставить только учитель с доступом к курсу")
        if course_id == self.preview_course_id:
            # Ранний выход: иначе на курсе появится отзыв от админа, а обещание
            # режима — «вышел, и состояние как до входа» (BACKEND_NOTES, 12).
            # Объект создан в памяти и в сессию SQLAlchemy не добавлен
            return _review_out(
                Review(
                    id=0,
                    course_id=course_id,
                    user_id=user.id,
                    rating=rating,
                    text=text,
                    created_at=now_utc(),
                    platform=platform,
                ),
                user,
            )
        # Премодерации нет: отзыв виден сразу, админ отвечает или удаляет постфактум
        review = self.reviews.create(course_id, user.id, rating, text, platform)
        return _review_out(review, user)

    # -- мои курсы ------------------------------------------------------

    def my_courses(self, user: User, platform: str) -> dict:
        items = []
        # Доступы и заявки — обе половины одного экрана, поэтому площадка
        # отсекается у обеих: иначе курса в «Моих» нет, а заявка на него висит
        for enrollment, course in self.enrollments.active_for_user(user.id, platform):
            items.append(
                {
                    "id": course.id,
                    "lang": course.lang,
                    "title": course.title,
                    "category_id": course.category_id,
                    "cover": cover_url(course, self.cfg.public_base_url),
                    "hours": course.hours,
                    "price": course.price,
                    "status": course.status,
                    **course_progress(
                        self.courses, self.progress, course, user.id, platform
                    ),
                    "completed_at": enrollment.completed_at,
                }
            )
        leads = []
        for lead, course in self.leads.open_for_user(user.id, platform):
            leads.append(
                {
                    "id": lead.id,
                    "status": lead.status,
                    "created_at": lead.created_at,
                    "waiting_days": waiting_days(lead.created_at, now_utc()),
                    "course": {
                        "id": course.id,
                        "title": course.title,
                        "cover": cover_url(course, self.cfg.public_base_url),
                        # Снимок на момент заявки: человеку показывали эту цену
                        "price": lead.price_snapshot,
                    },
                }
            )
        return {"items": items, "leads": leads}
