"""Отчёт по версии курса — единственный экран с показателями.

Всё считается в момент запроса и нигде не хранится: один курс, никаких
периодов, сравнений и накопленной истории (BACKEND_NOTES, раздел 13).
Пояснительных выводов сервер не даёт — числа, а текст к ним пишет экран.

В отчёте есть ФИО, школа и регион — это персональные данные, поэтому
эндпоинт только для админа.
"""

from app.adapters.db.models import Course, QuizAttempt
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

    def report(
        self,
        course_id: int,
        *,
        platform: str | None,
        q: str | None,
        offset: int,
        limit: int,
    ) -> dict:
        """Строка отчёта — это доступ, а не человек: курс, выложенный на обеих
        площадках, на каждой изучается заново (PLATFORMS_BRIEF, решение 2),
        и у человека с двумя доступами две строки со своими процентами.
        Без фильтра приходят обе, с фильтром — только его площадка
        (решение оркестратора, сессия 2).
        """
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
        done_by_access = self.progress.done_by_access(course.id, platform)
        # Условия сертификата считаются на каждую строку таблицы, а участников
        # на странице до сотни: состав курса и пройденное грузим один раз.
        # Скрытое приходит вместе со всем — чек-лист оставит из него то,
        # что человек успел пройти, и делает это без похода в базу на строку
        course_items = {
            "lessons": self.courses.lessons(course.id, include_hidden=True),
            "tasks": self.courses.tasks(course.id, include_hidden=True),
            "quizzes": self.courses.quizzes(course.id, include_hidden=True),
        }
        done_items = self.progress.done_items_by_access(course.id, platform)
        return {
            "course": {"id": course.id, "lang": course.lang, "title": course.title},
            # Отчёт считается на лету: момент запроса и есть его дата
            "generated_at": now_utc(),
            "summary": self._summary(course, items, done_by_access, platform),
            "funnel": self._funnel(course, items, platform),
            "participants": self._participants(
                course,
                items,
                done_by_access,
                course_items=course_items,
                done_items=done_items,
                platform=platform,
                q=q,
                offset=offset,
                limit=limit,
            ),
        }

    # -- сводка ------------------------------------------------------------

    def _summary(
        self,
        course: Course,
        items: list[dict],
        done_by_access: dict[tuple[int, str], int],
        platform: str | None,
    ) -> dict:
        """Все числа сводки — по доступам: и `granted`, и начавшие, и среднее.
        Человек с доступом на обеих площадках считается дважды ровно потому,
        что учится дважды, и иначе средний процент делился бы не на то число.
        """
        granted = self.enrollments.participants_count(course.id, platform)
        spans = self.enrollments.completed_spans(course.id, platform)
        days = [waiting_days(granted_at, completed_at) for granted_at, completed_at in spans]
        # Не начавшие входят в среднее нулями: они такие же участники курса,
        # и без них средний прогресс выглядел бы лучше, чем есть
        progress_sum = sum(
            _percent(done_count, len(items)) for done_count in done_by_access.values()
        )
        return {
            "granted": granted,
            # Начал — доступ, по которому пройден хоть один элемент программы;
            # в done_by_access другие и не попадают
            "started": len(done_by_access),
            "completed": len(spans),
            "avg_progress_percent": round(progress_sum / granted) if granted else None,
            "avg_final_score": self._avg_final_score(course, items, platform),
            "certificates": self.certificates.active_count(course.id, platform),
            "avg_days_to_complete": round(sum(days) / len(days)) if days else None,
        }

    def _avg_final_score(
        self, course: Course, items: list[dict], platform: str | None
    ) -> int | None:
        """Средний балл итогового теста — процентами, как на экране результата.

        Процент считает Python той же функцией, что и сам тест: максимум
        у попытки свой (снимок вопросов), и в SQL этот расчёт пришлось бы
        писать заново.
        """
        final = _final_quiz(items)
        if final is None:
            return None
        attempts = self.attempts.counted_for_quiz(course.id, final["id"], platform)
        if not attempts:
            return None
        max_scores = self.attempts.max_scores(attempts)
        percents = [
            score_percent(attempt.score or 0, max_scores[attempt.id]) for attempt in attempts
        ]
        return round(sum(percents) / len(percents))

    # -- воронка -----------------------------------------------------------

    def _funnel(self, course: Course, items: list[dict], platform: str | None) -> list[dict]:
        """Все видимые элементы программы в сквозном порядке. Не выборка:
        решение, какие показать, принимает экран (CONTRACT, сессия 6).

        `reached` считает доступы, а не людей: воронка стоит под `granted`,
        и оба числа обязаны мерить одно и то же.
        """
        reached = self.progress.done_by_item(course.id, platform)
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
        done_by_access: dict[tuple[int, str], int],
        *,
        course_items: dict[str, list],
        done_items: dict[tuple[int, str], set[tuple[str, int]]],
        platform: str | None,
        q: str | None,
        offset: int,
        limit: int,
    ) -> dict:
        """Строка — доступ: у человека, купившего общий курс дважды, их две,
        и всё в них своё. Поэтому и попытки, и сертификаты, и пройденное
        раскладываются по паре (учитель, площадка), а не по одному user_id.
        """
        accesses, total = self.enrollments.participants_page(
            course.id, platform=platform, q=q, offset=offset, limit=limit
        )
        # Список для IN: у человека с двумя доступами id на странице повторится
        user_ids = sorted({user.id for user, _ in accesses})
        # Попытки и выданные сертификаты — по запросу на страницу, а не на строку
        attempts = self.attempts.course_attempts(course.id, user_ids)
        max_scores = self.attempts.max_scores(attempts)
        by_access: dict[tuple[int, str], dict[int, list[QuizAttempt]]] = {}
        for attempt in attempts:
            by_access.setdefault((attempt.user_id, attempt.platform), {}).setdefault(
                attempt.quiz_id, []
            ).append(attempt)
        issued = self.certificates.active_access_ids(course.id, user_ids)
        module_quizzes = [
            item for item in items if item["kind"] == "quiz" and not item["is_final"]
        ]
        final = _final_quiz(items)
        return {
            "items": [
                {
                    "user_id": user.id,
                    # Метка площадки: строка — доступ, и без неё две строки
                    # одного человека были бы неотличимы
                    "platform": access_platform,
                    "last_name": user.last_name,
                    "first_name": user.first_name,
                    "middle_name": user.middle_name,
                    "school": user.school,
                    "region": user.region,
                    "progress_percent": _percent(
                        done_by_access.get((user.id, access_platform), 0), len(items)
                    ),
                    "module_quizzes": [
                        {
                            "quiz_id": quiz["id"],
                            "title": quiz["title"],
                            "score": self._score(
                                by_access.get((user.id, access_platform), {}).get(
                                    quiz["id"], []
                                ),
                                max_scores,
                            ),
                        }
                        for quiz in module_quizzes
                    ],
                    "final_quiz": self._final_state(
                        by_access.get((user.id, access_platform), {}).get(final["id"], [])
                        if final
                        else [],
                        max_scores,
                    ),
                    "certificate": self._certificate_state(
                        course,
                        (user.id, access_platform),
                        issued,
                        course_items,
                        done_items,
                    ),
                }
                for user, access_platform in accesses
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
        access: tuple[int, str],
        issued: set[tuple[int, str]],
        course_items: dict[str, list],
        done_items: dict[tuple[int, str], set[tuple[str, int]]],
    ) -> str:
        """Состояние документа считается по доступу: сертификат свой на каждой
        площадке (PLATFORMS_BRIEF, решение 2), и выданный на первой не делает
        строку второй «выданной»."""
        if access in issued:
            return "issued"
        # «Условия выполнены, но документ не выдан» — тот же чек-лист, что
        # у учителя на экране завершения; второй копии правил быть не должно
        conditions = course_conditions(
            course, **course_items, done=done_items.get(access, set())
        )
        return "ready" if all_done(conditions) else "in_progress"
