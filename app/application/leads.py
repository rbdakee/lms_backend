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
from app.application.admin_notify import AdminNotifier
from app.application.ports import NotificationCard
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
        telegram: AdminNotifier,
        commit: Callable[[], None],
        preview_course_id: int | None,
        admin_base_url: str,
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
        # Кнопка «Открыть в админке» в карточке уведомления
        self.admin_base_url = admin_base_url

    # -- заявка учителя -------------------------------------------------

    def create_lead(self, user: User, course_id: int, platform: str) -> dict:
        # Правило каталожное, без поблажки на доступ: заявку подаёт тот, у кого
        # доступа ещё нет, и на курс соседней площадки её подать нельзя
        course = self.courses.published_by_id(course_id, platform)
        if course is None:
            raise NotFoundError("Курс не найден")
        # Снимок берётся с цены той площадки, откуда пришла заявка: на второй
        # тот же курс стоит своих денег (PLATFORMS_BRIEF, решение 3)
        price = self.courses.platforms.prices([course.id], platform).get(course.id)
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
                    price_snapshot=price,
                    status="new",
                    created_at=now_utc(),
                    platform=platform,
                )
            )
        if self.enrollments.active_for(user.id, course.id, platform) is not None:
            raise AlreadyEnrolledError()
        if course.status == "closed":
            raise EnrollmentClosedError()

        lead = self.leads.open_for(user.id, course.id, platform)
        if lead is not None:
            # Повторная кнопка — напоминание админу, а не дубль заявки
            lead.reminded_at = now_utc()
            return self._lead_out(lead)

        lead = self.leads.create(user.id, course.id, price, platform)
        self.commit()
        try:
            # Без ФИО и телефона: подробности админ откроет по кнопке в
            # админке — карточка уходит в Telegram, а не за дверь с входом
            lines = [f"Курс: {course.title}"]
            if lead.price_snapshot is not None:
                lines.append(f"Цена: {_fmt_tenge(lead.price_snapshot)}")
            self.telegram.notify_admins(
                NotificationCard(
                    title="Новая заявка",
                    lines=lines,
                    link_text="Открыть в админке",
                    link_url=f"{self.admin_base_url}/leads/{lead.id}",
                ),
                kind="lead",
                # Площадка заявки, а не запроса: строку в текст ставит
                # сам notifier — см. AdminNotifier.notify_admins
                platform=lead.platform,
            )
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
        statuses: list[str] | None,
        course_ids: list[int] | None,
        q: str | None,
        platform: str | None,
        offset: int,
        limit: int,
    ) -> dict:
        rows, total = self.leads.admin_page(
            statuses=statuses,
            course_ids=course_ids,
            q=q,
            platform=platform,
            offset=offset,
            limit=limit,
        )
        # Запрос на площадку, а не на заявку: цена у каждой своя, а страница
        # бывает и на сто строк — читать её построчно значит сто запросов
        course_ids_on_page = [course.id for _, _, course in rows]
        prices = {
            lead_platform: self.courses.platforms.prices(
                course_ids_on_page, lead_platform
            )
            for lead_platform in {lead.platform for lead, _, _ in rows}
        }
        return {
            "items": [
                self._admin_lead_out(
                    lead, teacher, course, prices[lead.platform].get(course.id)
                )
                for lead, teacher, course in rows
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
        if status is not None and status != lead.status:
            if lead.status == "granted":
                # Доступ уже выдан — enrollment существует, откатывать
                # заявку в другой статус нельзя, курс у учителя уже есть
                raise ValidationAppError("Доступ уже выдан, статус заявки закрыт")
            lead.status = status
        if "note" in fields:
            lead.note = fields["note"]
        teacher = self.users.by_id(lead.user_id)
        course = self.courses.by_id(lead.course_id)
        price = self.courses.platforms.prices([course.id], lead.platform).get(course.id)
        return self._admin_lead_out(lead, teacher, course, price)

    @staticmethod
    def _admin_lead_out(
        lead: Lead, teacher: User, course: Course, price: int | None
    ) -> dict:
        """Цена приходит готовой: у списка она берётся одним запросом
        на площадку, у карточки — одним на строку."""
        closed = lead.status in ("granted", "declined")
        return {
            "id": lead.id,
            # Метка площадки: админка одна на обе, и в строке видно, откуда
            # заявка. Кодом, а не именем — имя админка возьмёт из справочника
            # GET /admin/settings
            "platform": lead.platform,
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
                # Админ смотрит на заявку, и цена рядом с ней — цена того
                # каталога, откуда она пришла, а не цена второй площадки
                "price": price,
            },
        }

    # -- выдача доступа -------------------------------------------------

    def grant(
        self,
        admin: User,
        user_id: int,
        course_id: int,
        paid: bool,
        note: str | None,
        platform: str,
    ) -> dict:
        # Площадка приходит из тела запроса, а не из `Origin`: админка одна
        # на обе, и её источник платформы не несёт (решение владельца).
        teacher = self.users.by_id(user_id)
        if teacher is None:
            raise NotFoundError("Учитель не найден")
        course = self.courses.visible_by_id(course_id)
        if course is None:
            raise NotFoundError("Курс не найден")

        existing = self.enrollments.by_user_course(user_id, course_id, platform)
        if existing is not None and existing.revoked_at is None:
            raise AlreadyEnrolledError()

        # Непустой paid_note (хотя бы "") и есть отметка «оплата получена»
        paid_note = (note or "") if paid else None
        if existing is not None:
            # Повторная выдача после отзыва доступа: строка одна на тройку
            # user+course+platform (unique), поэтому снимаем revoked_at,
            # а не плодим новую
            existing.revoked_at = None
            existing.granted_by = admin.id
            existing.granted_at = now_utc()
            existing.paid_note = paid_note
            enrollment = existing
        else:
            enrollment = self.enrollments.create(
                user_id, course_id, platform, granted_by=admin.id, paid_note=paid_note
            )

        lead = self.leads.open_for(user_id, course_id, platform)
        if lead is not None:
            lead.status = "granted"
            if not paid and note:
                # Оплаты нет — комментарий остаётся в заявке, а не в enrollment
                lead.note = f"{lead.note}\n{note}" if lead.note else note

        # Учителю — колокольчик; текст не хранится, соберётся на его языке.
        # Площадка — у созданного доступа: ссылка ведёт на тот сайт, где курс
        # и открылся
        self.notifications.create(
            user_id,
            "access_granted",
            {"course_id": course_id, "course_title": course.title},
            enrollment.platform,
        )
        return {
            "id": enrollment.id,
            "user_id": user_id,
            "course_id": course_id,
            "platform": enrollment.platform,
            "granted_at": enrollment.granted_at,
            "paid": enrollment.paid_note is not None,
            "note": enrollment.paid_note,
        }


def _fmt_tenge(amount: int) -> str:
    """`60000` → «60 000 ₸» — разряды пробелом, как на карточке курса."""
    return f"{amount:,}".replace(",", " ") + " ₸"
