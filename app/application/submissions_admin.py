"""Очередь проверки работ в админке: список, карточка и вердикт.

От учительского сценария отделено сознательно: здесь другие права (админ,
а не enrollment), другой состав ответа (ФИО учителя, условие задания целиком)
и другая видимость — админ открывает работу и по скрытому курсу, куда
учителя уже не пускают.
"""

from app.adapters.db.models import Course, Submission, Task, User
from app.adapters.db.repos import (
    SUBMISSION_PENDING,
    SUBMISSION_REWORK,
    NotificationRepo,
    SubmissionRepo,
    now_utc,
)
from app.application.ports import StoragePort
from app.application.tasks import files_out, submission_out, template_file_out
from app.config import Settings
from app.domain.errors import AlreadyReviewedError, FieldError, NotFoundError
from app.domain.kz_time import waiting_days


class SubmissionsAdminService:
    def __init__(
        self,
        submissions: SubmissionRepo,
        notifications: NotificationRepo,
        storage: StoragePort,
        cfg: Settings,
    ):
        self.submissions = submissions
        self.notifications = notifications
        self.storage = storage
        self.cfg = cfg

    # -- GET /admin/submissions -----------------------------------------

    def admin_list(
        self,
        *,
        status: str | None,
        course_id: int | None,
        platform: str | None,
        offset: int,
        limit: int,
    ) -> dict:
        rows, total = self.submissions.admin_page(
            status=status,
            course_id=course_id,
            platform=platform,
            offset=offset,
            limit=limit,
        )
        return {
            "items": [
                self._item_out(submission, teacher, task, course, number)
                for submission, teacher, task, course, number in rows
            ],
            "total": total,
        }

    # -- GET /admin/submissions/{id} ------------------------------------

    def card(self, submission_id: int) -> dict:
        return self._card_out(*self._found(submission_id))

    # -- POST /admin/submissions/{id}/review ----------------------------

    def review(
        self, admin: User, submission_id: int, verdict: str, comment: str | None
    ) -> dict:
        found = self._found(submission_id)
        submission, _, task, course = found
        if submission.status != SUBMISSION_PENDING:
            raise AlreadyReviewedError()
        comment = (comment or "").strip() or None
        if verdict == SUBMISSION_REWORK and comment is None:
            # На экране это единственная подсказка учителю, что доделать
            raise FieldError("comment", "При отправке на доработку комментарий обязателен")

        # Условный UPDATE, а не присваивание: два админа в двух вкладках могут
        # пройти проверку выше одновременно, а решение должно остаться одно —
        # и reviewed_by указывать на того, кто его принял на самом деле
        marked = self.submissions.mark_reviewed(
            submission,
            status=verdict,
            comment=comment,
            reviewed_by=admin.id,
            reviewed_at=now_utc(),
        )
        if not marked:
            raise AlreadyReviewedError()
        # Текст уведомления не храним — соберётся на языке читателя (раздел 11).
        # Площадка — у самой работы: админка одна на обе, и её `Origin`
        # площадки не несёт
        self.notifications.create(
            submission.user_id,
            "submission_reviewed",
            {
                "task_id": task.id,
                "task_title": task.title,
                "course_id": course.id,
                "verdict": verdict,
            },
            submission.platform,
        )
        return self._card_out(*found)

    # -- сборка ответа ---------------------------------------------------

    def _found(self, submission_id: int) -> tuple[Submission, User, Task, Course]:
        found = self.submissions.by_id_with_context(submission_id)
        if found is None:
            raise NotFoundError("Работа не найдена")
        return found

    @staticmethod
    def _item_out(
        submission: Submission, teacher: User, task: Task, course: Course, number: int
    ) -> dict:
        return {
            "id": submission.id,
            # Метка площадки — и в очереди, и в карточке: карточка отдаёт
            # ту же строку, и после вердикта метка на экране не должна
            # пропадать. Кодом, а не именем: имя админка возьмёт
            # из справочника GET /admin/settings
            "platform": submission.platform,
            "status": submission.status,
            "attempt_number": number,
            "created_at": submission.created_at,
            # Проверенная работа не «ждёт»: счётчик остановлен на нуле
            "waiting_days": (
                waiting_days(submission.created_at, now_utc())
                if submission.status == SUBMISSION_PENDING
                else 0
            ),
            "teacher": {
                "id": teacher.id,
                "last_name": teacher.last_name,
                "first_name": teacher.first_name,
                "middle_name": teacher.middle_name,
                "photo_url": teacher.photo_url,
            },
            "task": {"id": task.id, "title": task.title},
            "course": {"id": course.id, "lang": course.lang, "title": course.title},
        }

    def _card_out(
        self, submission: Submission, teacher: User, task: Task, course: Course
    ) -> dict:
        number = self.submissions.attempt_number(submission)
        base_url = self.cfg.public_base_url
        return {
            **self._item_out(submission, teacher, task, course, number),
            # Карточке нужно условие целиком: слева задание, справа работа
            "task": {
                "id": task.id,
                "title": task.title,
                "statement": task.statement,
                "template_file": template_file_out(self.storage, task),
                "submit_format": task.submit_format,
                "allowed_ext": task.allowed_ext,
                "max_size_mb": task.max_size_mb,
            },
            "text": submission.text,
            "files": files_out(submission, base_url),
            "comment": submission.comment,
            "reviewed_at": submission.reviewed_at,
            # Что человек присылал до этой работы и что ему на это писали
            "history": [
                submission_out(earlier, base_url)
                for earlier in self.submissions.earlier_than(submission)
            ],
        }
