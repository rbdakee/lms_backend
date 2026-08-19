"""Отчёт по версии курса — единственный экран с показателями.

Всё считается в момент запроса и нигде не хранится: один курс, никаких
периодов, сравнений и накопленной истории (BACKEND_NOTES, раздел 13).
Пояснительных выводов сервер не даёт — числа, а текст к ним пишет экран.

В отчёте есть ФИО, школа и регион — это персональные данные, поэтому
эндпоинт только для админа.
"""

from app.adapters.db.models import Course, QuizAttempt, User
from app.adapters.db.repos import (
    AttemptRepo,
    CertificateRepo,
    CourseRepo,
    EnrollmentRepo,
    ProgressRepo,
    now_utc,
)
from app.application.certificates import CertificatesService, course_conditions
from app.application.program import build_program
from app.domain.certificate import all_done
from app.domain.errors import NotFoundError
from app.domain.kz_time import waiting_days
from app.domain.program import item_key
from app.domain.quiz import score_percent


def _percent(done_count: int, total_count: int) -> int:
    """Тот же расчёт, что у прогресса курса на экране учителя
    (`domain/program.py`): в отчёте и в кабинете должно стоять одно число."""
    return round(done_count * 100 / total_count) if total_count else 0


def _final_quiz(items: list[dict]) -> dict | None:
    """Итоговый тест курса. Их в программе один; если всё же несколько, берём
    первый по порядку — как и чек-лист сертификата."""
    return next(
        (item for item in items if item["kind"] == "quiz" and item["is_final"]), None
    )


class ReportsService:
    def __init__(
        self,
        courses: CourseRepo,
        enrollments: EnrollmentRepo,
        progress: ProgressRepo,
        attempts: AttemptRepo,
        certificates: CertificateRepo,
        # Чек-лист условий сертификата — там же, где его видит учитель:
        # второй экземпляр правил разошёлся бы с экраном завершения
        completion: CertificatesService,
    ):
        self.courses = courses
        self.enrollments = enrollments
        self.progress = progress
        self.attempts = attempts
        self.certificates = certificates
        self.completion = completion

    # -- GET /admin/reports/{course_id} ----------------------------------

    def report(self, course_id: int, *, q: str | None, offset: int, limit: int) -> dict:
        # Видимость курса не проверяется: черновик и скрытый админу видны,
        # отчёт по ним и открывают из редактора (CONTRACT, сессия 6)
        course = self.courses.by_id(course_id)
        if course is None:
            raise NotFoundError("Курс не найден")
        # Сквозной порядок программы — тот же, что у сайдбара учителя:
        # скрытые уроки не показываются и в подсчётах не участвуют
        items = [
            item
            for module in build_program(self.courses, course.id)
            for item in module["items"]
        ]
        done_by_user = self.progress.done_by_user(course.id)
        # Условия сертификата считаются на каждую строку таблицы, а участников
        # на странице до сотни: состав курса и пройденное грузим один раз.
        # Скрытое приходит вместе со всем — чек-лист оставит из него то,
        # что человек успел пройти, и делает это без похода в базу на строку
        course_items = {
            "lessons": self.courses.lessons(course.id, include_hidden=True),
            "tasks": self.courses.tasks(course.id, include_hidden=True),
            "quizzes": self.courses.quizzes(course.id, include_hidden=True),
        }
        done_items = self.progress.done_items_by_user(course.id)
        return {
            "course": {"id": course.id, "lang": course.lang, "title": course.title},
            # Отчёт считается на лету: момент запроса и есть его дата
            "generated_at": now_utc(),
            "summary": self._summary(course, items, done_by_user),
            "funnel": self._funnel(course, items),
            "participants": self._participants(
                course,
                items,
                done_by_user,
                course_items=course_items,
                done_items=done_items,
                q=q,
                offset=offset,
                limit=limit,
            ),
        }

    # -- сводка ------------------------------------------------------------

    def _summary(self, course: Course, items: list[dict], done_by_user: dict[int, int]) -> dict:
        granted = self.enrollments.participants_count(course.id)
        spans = self.enrollments.completed_spans(course.id)
        days = [waiting_days(granted_at, completed_at) for granted_at, completed_at in spans]
        # Не начавшие входят в среднее нулями: они такие же участники курса,
        # и без них средний прогресс выглядел бы лучше, чем есть
        progress_sum = sum(
            _percent(done_count, len(items)) for done_count in done_by_user.values()
        )
        return {
            "granted": granted,
            # Начал — у кого есть хоть один пройденный элемент программы;
            # в done_by_user другие и не попадают
            "started": len(done_by_user),
            "completed": len(spans),
            "avg_progress_percent": round(progress_sum / granted) if granted else None,
            "avg_final_score": self._avg_final_score(course, items),
            "certificates": self.certificates.active_count(course.id),
            "avg_days_to_complete": round(sum(days) / len(days)) if days else None,
        }

    def _avg_final_score(self, course: Course, items: list[dict]) -> int | None:
        """Средний балл итогового теста — процентами, как на экране результата.

        Процент считает Python той же функцией, что и сам тест: максимум
        у попытки свой (снимок вопросов), и в SQL этот расчёт пришлось бы
        писать заново.
        """
        final = _final_quiz(items)
        if final is None:
            return None
        attempts = self.attempts.counted_for_quiz(course.id, final["id"])
        if not attempts:
            return None
        max_scores = self.attempts.max_scores(attempts)
        percents = [
            score_percent(attempt.score or 0, max_scores[attempt.id]) for attempt in attempts
        ]
        return round(sum(percents) / len(percents))

    # -- воронка -----------------------------------------------------------

    def _funnel(self, course: Course, items: list[dict]) -> list[dict]:
        """Все видимые элементы программы в сквозном порядке. Не выборка:
        решение, какие показать, принимает экран (CONTRACT, сессия 6)."""
        reached = self.progress.done_by_item(course.id)
        return [
            {
                "kind": item["kind"],
                "id": item["id"],
                "number": number,
                "title": item["title"],
                "reached": reached.get(item_key(item), 0),
            }
            for number, item in enumerate(items, start=1)
        ]

    # -- таблица участников ------------------------------------------------

    def _participants(
        self,
        course: Course,
        items: list[dict],
        done_by_user: dict[int, int],
        *,
        course_items: dict[str, list],
        done_items: dict[int, set[tuple[str, int]]],
        q: str | None,
        offset: int,
        limit: int,
    ) -> dict:
        users, total = self.enrollments.participants_page(
            course.id, q=q, offset=offset, limit=limit
        )
        user_ids = [user.id for user in users]
        # Попытки и выданные сертификаты — по запросу на страницу, а не на строку
        attempts = self.attempts.course_attempts(course.id, user_ids)
        max_scores = self.attempts.max_scores(attempts)
        by_user: dict[int, dict[int, list[QuizAttempt]]] = {}
        for attempt in attempts:
            by_user.setdefault(attempt.user_id, {}).setdefault(attempt.quiz_id, []).append(
                attempt
            )
        issued = self.certificates.active_user_ids(course.id, user_ids)
        module_quizzes = [
            item for item in items if item["kind"] == "quiz" and not item["is_final"]
        ]
        final = _final_quiz(items)
        return {
            "items": [
                {
                    "user_id": user.id,
                    "last_name": user.last_name,
                    "first_name": user.first_name,
                    "middle_name": user.middle_name,
                    "school": user.school,
                    "region": user.region,
                    "progress_percent": _percent(done_by_user.get(user.id, 0), len(items)),
                    "module_quizzes": [
                        {
                            "quiz_id": quiz["id"],
                            "title": quiz["title"],
                            "score": self._score(
                                by_user.get(user.id, {}).get(quiz["id"], []), max_scores
                            ),
                        }
                        for quiz in module_quizzes
                    ],
                    "final_quiz": self._final_state(
                        by_user.get(user.id, {}).get(final["id"], []) if final else [],
                        max_scores,
                    ),
                    "certificate": self._certificate_state(
                        course, user, issued, course_items, done_items
                    ),
                }
                for user in users
            ],
            "total": total,
        }

    @classmethod
    def _score(cls, attempts: list[QuizAttempt], max_scores: dict[int, int]) -> int | None:
        """Балл теста процентами. None — не сдавал: незавершённая попытка
        балла ещё не даёт."""
        counted = cls._counted(attempts)
        if counted is None:
            return None
        return score_percent(counted.score or 0, max_scores[counted.id])

    @classmethod
    def _final_state(cls, attempts: list[QuizAttempt], max_scores: dict[int, int]) -> dict:
        if any(attempt.finished_at is None for attempt in attempts):
            # Активная попытка главнее прочего — как и на экране теста
            return {"state": "in_progress", "score": None}
        counted = cls._counted(attempts)
        if counted is None:
            return {"state": "not_started", "score": None}
        return {
            "state": "passed" if counted.passed else "failed",
            "score": score_percent(counted.score or 0, max_scores[counted.id]),
        }

    @staticmethod
    def _counted(attempts: list[QuizAttempt]) -> QuizAttempt | None:
        """Зачётная попытка — та же, что показывает экран результата теста
        (`application/quizzes.py`): помеченная is_counted, а если пометки нет
        (история, оставшаяся от отзыва сертификата) — последняя завершённая."""
        finished = [attempt for attempt in attempts if attempt.finished_at is not None]
        if not finished:
            return None
        return next((attempt for attempt in finished if attempt.is_counted), finished[-1])

    @staticmethod
    def _certificate_state(
        course: Course,
        user: User,
        issued: set[int],
        course_items: dict[str, list],
        done_items: dict[int, set[tuple[str, int]]],
    ) -> str:
        if user.id in issued:
            return "issued"
        # «Условия выполнены, но документ не выдан» — тот же чек-лист, что
        # у учителя на экране завершения; второй копии правил быть не должно
        conditions = course_conditions(
            course, **course_items, done=done_items.get(user.id, set())
        )
        return "ready" if all_done(conditions) else "in_progress"
