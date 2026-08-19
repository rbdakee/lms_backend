"""Заявки и выдача доступа: путь от кнопки «Записаться» до enrollment.

Оплата проходит вне платформы, поэтому заявка — не заказ, а просьба
связаться: админ сам звонит, отмечает оплату и выдаёт доступ руками.
"""

import logging
from collections.abc import Callable

from app.adapters.db.models import Course, Lead, User
from app.adapters.db.repos import (
    CourseRepo,
    EnrollmentRepo,
    LeadRepo,
    NotificationRepo,
    UserRepo,
    now_utc,
)
from app.application.ports import TelegramPort
from app.domain.errors import (
    AlreadyEnrolledError,
    EnrollmentClosedError,
    NotFoundError,
    ValidationAppError,
)
from app.domain.kz_time import waiting_days

log = logging.getLogger("leads")


class LeadsService:
    def __init__(
        self,
        users: UserRepo,
        courses: CourseRepo,
        leads: LeadRepo,
        enrollments: EnrollmentRepo,
        notifications: NotificationRepo,
        telegram: TelegramPort,
        commit: Callable[[], None],
        preview_course_id: int | None,
    ):
        self.users = users
        self.courses = courses
        self.leads = leads
        self.enrollments = enrollments
        self.notifications = notifications
        self.telegram = telegram
        # Коммит нужен сценарию точечно: заявка обязана быть в базе до того,
        # как уйдёт в Telegram, — бот недоступен, а заявка всё равно в админке.
        self.commit = commit
        # Включённый режим предпросмотра, см. create_lead
        self.preview_course_id = preview_course_id

    # -- заявка учителя -------------------------------------------------

    def create_lead(self, user: User, course_id: int) -> dict:
        course = self.courses.visible_by_id(course_id)
        if course is None:
            raise NotFoundError("Курс не найден")
        if self.preview_course_id is not None:
            # Ранний выход: ни заявки в очереди админа, ни сообщения в бот
            # (BACKEND_NOTES, раздел 12). Глушится заявка на любой курс, а не
            # только на предпросматриваемый: заявку подаёт учитель, и другого
            # пути её создать у админа нет — настоящую работу это не съедает,
            # а мусорная заявка от самого админа пережила бы выход из режима.
            # Объект создан в памяти и в сессию SQLAlchemy не добавлен
            return self._lead_out(
                Lead(
                    id=0,
                    user_id=user.id,
                    course_id=course.id,
                    price_snapshot=course.price,
                    status="new",
                    created_at=now_utc(),
                )
            )
        if self.enrollments.active_for(user.id, course.id) is not None:
            raise AlreadyEnrolledError()
        if course.status == "closed":
            raise EnrollmentClosedError()

        lead = self.leads.open_for(user.id, course.id)
        if lead is not None:
            # Повторная кнопка — напоминание админу, а не дубль заявки
            lead.reminded_at = now_utc()
            return self._lead_out(lead)

        lead = self.leads.create(user.id, course.id, course.price)
        self.commit()
        try:
            # Без ФИО и телефона: подробности админ откроет в админке
            self.telegram.notify_admins(f"Новая заявка №{lead.id} на курс „{course.title}“")
        except Exception:
            log.exception("Telegram-уведомление о заявке %s не ушло", lead.id)
        return self._lead_out(lead)

    @staticmethod
    def _lead_out(lead: Lead) -> dict:
        return {
            "id": lead.id,
            "status": lead.status,
            "created_at": lead.created_at,
            "waiting_days": waiting_days(lead.created_at, now_utc()),
        }

    # -- очередь заявок в админке --------------------------------------

    def admin_list(
        self,
        *,
        status: str | None,
        course_id: int | None,
        q: str | None,
        offset: int,
        limit: int,
    ) -> dict:
        rows, total = self.leads.admin_page(
            status=status, course_id=course_id, q=q, offset=offset, limit=limit
        )
        return {
            "items": [
                self._admin_lead_out(lead, teacher, course) for lead, teacher, course in rows
            ],
            "total": total,
        }

    def admin_patch(self, lead_id: int, fields: dict) -> dict:
        lead = self.leads.by_id(lead_id)
        if lead is None:
            raise NotFoundError("Заявка не найдена")
        status = fields.get("status")
        if status == "granted":
            # Выдача — отдельный сценарий: enrollment, уведомление, отметка оплаты
            raise ValidationAppError("Доступ выдаётся через POST /admin/enrollments")
        if status is not None:
            lead.status = status
        if "note" in fields:
            lead.note = fields["note"]
        teacher = self.users.by_id(lead.user_id)
        course = self.courses.by_id(lead.course_id)
        return self._admin_lead_out(lead, teacher, course)

    @staticmethod
    def _admin_lead_out(lead: Lead, teacher: User, course: Course) -> dict:
        closed = lead.status in ("granted", "declined")
        return {
            "id": lead.id,
            "status": lead.status,
            "price_snapshot": lead.price_snapshot,
            "note": lead.note,
            "created_at": lead.created_at,
            # Закрытая заявка не «ждёт»: счётчик остановлен на нуле
            "waiting_days": 0 if closed else waiting_days(lead.created_at, now_utc()),
            "reminded_at": lead.reminded_at,
            "teacher": {
                "id": teacher.id,
                "last_name": teacher.last_name,
                "first_name": teacher.first_name,
                "middle_name": teacher.middle_name,
                "phone": teacher.phone,
                "school": teacher.school,
                "region": teacher.region,
                "subject": teacher.subject,
            },
            "course": {
                "id": course.id,
                "lang": course.lang,
                "title": course.title,
                "price": course.price,
            },
        }

    # -- выдача доступа -------------------------------------------------

    def grant(
        self, admin: User, user_id: int, course_id: int, paid: bool, note: str | None
    ) -> dict:
        teacher = self.users.by_id(user_id)
        if teacher is None:
            raise NotFoundError("Учитель не найден")
        course = self.courses.visible_by_id(course_id)
        if course is None:
            raise NotFoundError("Курс не найден")

        existing = self.enrollments.by_user_course(user_id, course_id)
        if existing is not None and existing.revoked_at is None:
            raise AlreadyEnrolledError()

        # Непустой paid_note (хотя бы "") и есть отметка «оплата получена»
        paid_note = (note or "") if paid else None
        if existing is not None:
            # Повторная выдача после отзыва доступа: строка одна на пару
            # user+course (unique), поэтому снимаем revoked_at, а не плодим новую
            existing.revoked_at = None
            existing.granted_by = admin.id
            existing.granted_at = now_utc()
            existing.paid_note = paid_note
            enrollment = existing
        else:
            enrollment = self.enrollments.create(
                user_id, course_id, granted_by=admin.id, paid_note=paid_note
            )

        lead = self.leads.open_for(user_id, course_id)
        if lead is not None:
            lead.status = "granted"
            if not paid and note:
                # Оплаты нет — комментарий остаётся в заявке, а не в enrollment
                lead.note = f"{lead.note}\n{note}" if lead.note else note

        # Учителю — колокольчик; текст не хранится, соберётся на его языке
        self.notifications.create(
            user_id,
            "access_granted",
            {"course_id": course_id, "course_title": course.title},
        )
        return {
            "id": enrollment.id,
            "user_id": user_id,
            "course_id": course_id,
            "granted_at": enrollment.granted_at,
            "paid": enrollment.paid_note is not None,
            "note": enrollment.paid_note,
        }
