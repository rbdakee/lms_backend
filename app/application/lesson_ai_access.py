"""Доступ к «Жоспару»: выдаёт и снимает админ, проверяет каждая ручка продукта.

Доступ отдельный от курсов (TEACHER_AI_BRIEF, «Что решено», п. 4): здесь нет
ни одного обращения к `enrollment`, а проверки курсов не смотрят сюда. Купивший
курс помощника не получает, пользующийся помощником курс не получает — это
держит тест, а не соглашение.

Оплата проходит вне платформы; как у доступа к курсу, у выдачи есть только
отметка «оплата получена» с комментарием.
"""

from datetime import date, datetime

from app.adapters.db.lesson_ai_repos import LessonAccessRepo
from app.adapters.db.models import LessonAiAccess, User, now_utc
from app.domain.errors import FieldError, LessonAiNoAccessError, NotFoundError
from app.domain.kz_time import ALMATY
from app.domain.lesson_ai import access_active

NOTE_MAX = 500


def today_kz(now: datetime) -> date:
    return now.astimezone(ALMATY).date()


class LessonAccessService:
    def __init__(self, access: LessonAccessRepo):
        self.access = access

    # -- учитель ------------------------------------------------------------

    def status(self, user: User) -> dict:
        """GET /lesson_ai/access — есть ли доступ. Экран по нему решает,
        показывать ли вход в продукт или «как получить доступ»."""
        row = self.access.by_user(user.id)
        active = row is not None and self._active(row)
        return {
            "has_access": active,
            # Срок показывается только у действующего: у снятого он ничего
            # не значит, а у истёкшего экран и так пишет «нет доступа»
            "expires_on": row.expires_on if active else None,
        }

    def check(self, user: User) -> None:
        """Зависимость каждой ручки продукта: нет действующего доступа — 403.

        Админ здесь не исключение: доступ проверяется по строке, а не по роли.
        Пускать ли в продукт админскую куку вообще — открытый вопрос владельцу
        (L6, «требует решения» № 1), и эта проверка его не решает.
        """
        row = self.access.by_user(user.id)
        if row is None or not self._active(row):
            raise LessonAiNoAccessError()

    # -- админ --------------------------------------------------------------

    def admin_list(self, *, status: str | None, q: str | None, offset: int, limit: int) -> dict:
        """Учителя, которым доступ выдавался хоть раз, с расходом каждого.

        Действует и истёк — по той же дате Алматы, что и проверка на входе
        в продукт: иначе фильтр «действует» показывал бы человека, которого
        продукт уже не пускает.
        """
        rows, total = self.access.page(
            status=status, q=q, today=today_kz(now_utc()), offset=offset, limit=limit
        )
        items = [
            {
                "teacher": {
                    "id": teacher.id,
                    "last_name": teacher.last_name,
                    "first_name": teacher.first_name,
                    "middle_name": teacher.middle_name,
                    "phone": teacher.phone,
                    "school": teacher.school,
                    "region": teacher.region,
                    "city": teacher.city,
                    "subject": teacher.subject,
                    "is_blocked": teacher.is_blocked,
                },
                "access": self._out(row),
                "usage": {
                    "kits": kits,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    # Числом, как средний балл в каталоге: Decimal ушёл бы
                    # в JSON строкой
                    "cost_usd": float(cost) if cost is not None else None,
                },
            }
            for row, teacher, kits, tokens_in, tokens_out, cost in rows
        ]
        return {"items": items, "total": total}

    def card(self, user_id: int) -> dict:
        """Доступ учителя для карточки в админке; `access: null` — не выдавался."""
        self._teacher(user_id)
        row = self.access.by_user(user_id)
        return {"user_id": user_id, "access": self._out(row) if row is not None else None}

    def grant(
        self,
        admin: User,
        user_id: int,
        *,
        expires_on: date | None,
        paid: bool,
        note: str | None,
    ) -> dict:
        """Выдать, продлить или выдать заново после снятия — одним действием.

        Строка одна на учителя и переписывается целиком: кто выдал и когда —
        последняя выдача, как у доступа к курсу.
        """
        self._teacher(user_id)
        note = (note or "").strip()
        if len(note) > NOTE_MAX:
            raise FieldError("note", f"Не длиннее {NOTE_MAX} символов")
        if expires_on is not None and expires_on < today_kz(now_utc()):
            raise FieldError("expires_on", "Дата окончания уже прошла")
        # Непустой paid_note (хотя бы "") и есть отметка «оплата получена»
        paid_note = note if paid else None
        row = self.access.lock_by_user(user_id)
        if row is None:
            row = self.access.create(
                user_id=user_id,
                granted_by=admin.id,
                granted_at=now_utc(),
                expires_on=expires_on,
                paid_note=paid_note,
            )
        else:
            row.granted_by = admin.id
            row.granted_at = now_utc()
            row.expires_on = expires_on
            row.paid_note = paid_note
            row.revoked_at = None
            row.revoked_by = None
        return {"user_id": user_id, "access": self._out(row)}

    def revoke(self, admin: User, user_id: int) -> None:
        """Снять доступ. Ничего не удаляется: комплекты, материалы и расход
        учителя остаются, пропадает только вход. Повторное снятие ничего
        не меняет — время остаётся временем первого."""
        self._teacher(user_id)
        row = self.access.lock_by_user(user_id)
        if row is None:
            raise NotFoundError("Доступ к помощнику этому учителю не выдавался")
        if row.revoked_at is not None:
            return
        row.revoked_at = now_utc()
        row.revoked_by = admin.id

    # -- сборка ответа ------------------------------------------------------

    def _teacher(self, user_id: int) -> User:
        teacher = self.access.db.get(User, user_id)
        if teacher is None:
            raise NotFoundError("Учитель не найден")
        return teacher

    @staticmethod
    def _active(row: LessonAiAccess) -> bool:
        return access_active(
            expires_on=row.expires_on, revoked_at=row.revoked_at, today=today_kz(now_utc())
        )

    def _out(self, row: LessonAiAccess) -> dict:
        if row.revoked_at is not None:
            status = "revoked"
        elif self._active(row):
            status = "active"
        else:
            status = "expired"
        granted_by = self.access.db.get(User, row.granted_by)
        return {
            "status": status,
            "expires_on": row.expires_on,
            "paid": row.paid_note is not None,
            "paid_note": row.paid_note,
            "granted_at": row.granted_at,
            "granted_by": _person(granted_by),
            "revoked_at": row.revoked_at,
        }


def _person(user: User | None) -> dict | None:
    if user is None:
        return None
    name = " ".join(part for part in (user.last_name, user.first_name) if part)
    return {"id": user.id, "name": name}
