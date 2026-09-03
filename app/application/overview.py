"""Дашборд админа: три счётчика «требует действия», три коротких списка
под ними и справочные числа внизу экрана.

Это рабочий стол, а не аналитика: всё считается в момент запроса и ничего
не кэшируется (BACKEND_NOTES, раздел 13). Графиков, периодов и «активных
за месяц» здесь нет и не будет.
"""

from datetime import datetime

from app.adapters.db.models import (
    Course,
    Lead,
    Lesson,
    Submission,
    Task,
    ThreadMessage,
    User,
)
from app.adapters.db.repos import (
    CertificateRepo,
    CourseRepo,
    LeadRepo,
    SubmissionRepo,
    ThreadMessageRepo,
    UserRepo,
    now_utc,
)
from app.domain.kz_time import waiting_days

# Списки под плитками — по 5 элементов, одинаково у всех трёх
# (CONTRACT, сессия 6). Дальше человек уходит в саму очередь.
LIST_LIMIT = 5

# Плитка «новых заявок» считает именно новые: с contacted и paid админ уже
# работает, и красный счётчик про них молчит (DESIGN_BRIEF, 5.15).
NEW_LEAD_STATUS = "new"


def _teacher_out(teacher: User) -> dict:
    """ФИО тремя полями, как в заявках и очереди работ: собирает его фронт."""
    return {
        "id": teacher.id,
        "last_name": teacher.last_name,
        "first_name": teacher.first_name,
        "middle_name": teacher.middle_name,
    }


class OverviewService:
    def __init__(
        self,
        users: UserRepo,
        courses: CourseRepo,
        leads: LeadRepo,
        submissions: SubmissionRepo,
        messages: ThreadMessageRepo,
        certificates: CertificateRepo,
    ):
        self.users = users
        self.courses = courses
        self.leads = leads
        self.submissions = submissions
        self.messages = messages
        self.certificates = certificates

    # -- GET /admin/overview ---------------------------------------------

    def overview(self, platform: str | None) -> dict:
        """Один ответ на весь экран: два запроса, которыми шапка админки
        тянула `total` у заявок и работ, этим и закрываются.

        Площадка — общий фильтр всего экрана, а не вторая колонка у каждого
        показателя (PLATFORMS_BRIEF, решение 9): он действует и на счётчики,
        и на списки под ними. Параметра нет — обе площадки, как и было.
        """
        leads, leads_count = self.leads.admin_page(
            statuses=[NEW_LEAD_STATUS],
            course_ids=None,
            q=None,
            platform=platform,
            offset=0,
            limit=LIST_LIMIT,
        )
        submissions, submissions_count = self.submissions.pending_head(LIST_LIMIT, platform)
        questions, questions_count = self.messages.admin_page(
            answered=False,
            course_id=None,
            q=None,
            platform=platform,
            offset=0,
            limit=LIST_LIMIT,
        )
        # Подпись «Урок 6 · …» — сквозной номер видимых уроков курса
        numbers = self.courses.lesson_numbers(
            sorted({course.id for _, _, course, _ in questions})
        )
        now = now_utc()
        return {
            "leads_count": leads_count,
            "submissions_count": submissions_count,
            "questions_count": questions_count,
            "leads": [
                self._lead_out(lead, teacher, course, now)
                for lead, teacher, course in leads
            ],
            "submissions": [
                self._submission_out(submission, teacher, task, course, now)
                for submission, teacher, task, course in submissions
            ],
            "questions": [
                self._question_out(message, author, course, lesson, numbers)
                for message, author, course, lesson in questions
            ],
            "totals": {
                # Единственное число экрана, которое фильтр не трогает:
                # аккаунт один на обе площадки (PLATFORMS_BRIEF, решение 11),
                # и «учителей площадки» в базе не существует
                "teachers": self.users.teachers_count(),
                "courses_published": self.courses.published_count(platform),
                "certificates": self.certificates.active_count(platform=platform),
            },
        }

    # -- сборка ответа ----------------------------------------------------

    @staticmethod
    def _lead_out(lead: Lead, teacher: User, course: Course, now: datetime) -> dict:
        return {
            "id": lead.id,
            # Те же строки, что в очереди заявок, — и метка та же: без неё
            # админ видел бы одну и ту же заявку с площадкой на одном экране
            # и без неё на соседнем
            "platform": lead.platform,
            "created_at": lead.created_at,
            # В списке только новые заявки — каждая ждёт с самого создания
            "waiting_days": waiting_days(lead.created_at, now),
            "price_snapshot": lead.price_snapshot,
            "teacher": _teacher_out(teacher),
            "course": {"id": course.id, "title": course.title},
        }

    @staticmethod
    def _submission_out(
        submission: Submission,
        teacher: User,
        task: Task,
        course: Course,
        now: datetime,
    ) -> dict:
        return {
            "id": submission.id,
            "platform": submission.platform,
            "created_at": submission.created_at,
            "waiting_days": waiting_days(submission.created_at, now),
            "teacher": _teacher_out(teacher),
            "course": {"id": course.id, "title": course.title},
            "task": {"id": task.id, "title": task.title},
        }

    @staticmethod
    def _question_out(
        message: ThreadMessage,
        author: User,
        course: Course,
        lesson: Lesson,
        numbers: dict[int, int],
    ) -> dict:
        return {
            "id": message.id,
            "platform": message.platform,
            "text": message.text,
            "created_at": message.created_at,
            "teacher": _teacher_out(author),
            "course": {"id": course.id, "title": course.title},
            "lesson": {
                "id": lesson.id,
                # Скрытый урок в нумерации не участвует — номера у него нет
                "number": numbers.get(lesson.id, 0),
                "title": lesson.title,
            },
        }
