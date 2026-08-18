"""Репозитории — адаптер Postgres. Сценарии получают их готовыми объектами
и не знают про SQLAlchemy."""

import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.models import (
    AuthCode,
    Course,
    Enrollment,
    Lead,
    Lesson,
    LessonFile,
    LessonProgress,
    Module,
    Notification,
    Question,
    Quiz,
    QuizAttempt,
    Review,
    Session,
    Submission,
    Task,
    User,
)

# Черновик и скрытый курс для площадки не существуют: ни в каталоге,
# ни по прямой ссылке (предпросмотр админом — отдельный режим, сессия 6).
CATALOG_STATUSES = ("planned", "open", "closed")

# Заявка «в работе»: new | contacted | paid. granted и declined — закрытые.
OPEN_LEAD_STATUSES = ("new", "contacted", "paid")

# Оценка задания бинарная: зачтено или на доработку (BACKEND_NOTES, раздел 5).
SUBMISSION_ACCEPTED = "accepted"


def now_utc() -> datetime:
    return datetime.now(UTC)


class UserRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def by_phone(self, phone: str) -> User | None:
        return self.db.scalar(select(User).where(User.phone == phone))

    def by_id(self, user_id: int) -> User | None:
        return self.db.get(User, user_id)

    def create(self, phone: str) -> User:
        # Согласие проставляется при создании: без галочки код не отправляется
        user = User(phone=phone, consented_at=now_utc())
        self.db.add(user)
        self.db.flush()
        return user


class AuthCodeRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def active_for_phone(self, phone: str) -> AuthCode | None:
        """Последний неиспользованный код номера; действует всегда последний."""
        return self.db.scalar(
            select(AuthCode)
            .where(AuthCode.phone == phone, AuthCode.used_at.is_(None))
            .order_by(AuthCode.created_at.desc())
            .limit(1)
        )

    def blocked_until(self, phone: str) -> datetime | None:
        until = self.db.scalar(
            select(func.max(AuthCode.blocked_until)).where(AuthCode.phone == phone)
        )
        if until is not None and until > now_utc():
            return until
        return None

    def last_sent_at(self, phone: str) -> datetime | None:
        return self.db.scalar(
            select(func.max(AuthCode.created_at)).where(AuthCode.phone == phone)
        )

    def oldest_in_day(self, *, phone: str | None = None, ip: str | None = None) -> datetime | None:
        """Начало суточного окна — чтобы честно сказать, когда лимит отпустит."""
        q = select(func.min(AuthCode.created_at)).where(
            AuthCode.created_at > now_utc() - timedelta(hours=24)
        )
        q = q.where(AuthCode.phone == phone) if phone else q.where(AuthCode.ip == ip)
        return self.db.scalar(q)

    def count_in_day(self, *, phone: str | None = None, ip: str | None = None) -> int:
        q = select(func.count()).select_from(AuthCode).where(
            AuthCode.created_at > now_utc() - timedelta(hours=24)
        )
        q = q.where(AuthCode.phone == phone) if phone else q.where(AuthCode.ip == ip)
        return self.db.scalar(q) or 0

    def expire_active(self, phone: str) -> None:
        # Новый код гасит предыдущие: действителен всегда последний отправленный
        self.db.execute(
            update(AuthCode)
            .where(AuthCode.phone == phone, AuthCode.used_at.is_(None))
            .values(expires_at=now_utc())
        )

    def create(self, phone: str, code_hash: str, ip: str | None, ttl_min: int) -> AuthCode:
        code = AuthCode(
            phone=phone,
            code_hash=code_hash,
            ip=ip,
            expires_at=now_utc() + timedelta(minutes=ttl_min),
        )
        self.db.add(code)
        self.db.flush()
        return code


class SessionRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def create(self, user_id: int, token_hash: str, user_agent: str, ip: str | None) -> Session:
        session = Session(user_id=user_id, token_hash=token_hash, user_agent=user_agent, ip=ip)
        self.db.add(session)
        self.db.flush()
        return session

    def by_token_hash(self, token_hash: str) -> Session | None:
        return self.db.scalar(
            select(Session).where(
                Session.token_hash == token_hash, Session.revoked_at.is_(None)
            )
        )

    def by_id_for_user(self, session_id: uuid.UUID, user_id: int) -> Session | None:
        return self.db.scalar(
            select(Session).where(
                Session.id == session_id,
                Session.user_id == user_id,
                Session.revoked_at.is_(None),
            )
        )

    def list_for_user(self, user_id: int) -> list[Session]:
        return list(
            self.db.scalars(
                select(Session)
                .where(Session.user_id == user_id, Session.revoked_at.is_(None))
                .order_by(Session.last_seen_at.desc())
            )
        )

    def revoke(self, session: Session) -> None:
        session.revoked_at = now_utc()

    def revoke_others(self, user_id: int, keep_id: uuid.UUID) -> int:
        result = self.db.execute(
            update(Session)
            .where(
                Session.user_id == user_id,
                Session.id != keep_id,
                Session.revoked_at.is_(None),
            )
            .values(revoked_at=now_utc())
        )
        return result.rowcount


class CourseRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def by_id(self, course_id: int) -> Course | None:
        return self.db.get(Course, course_id)

    def visible_by_id(self, course_id: int) -> Course | None:
        """Версия курса, существующая для площадки: draft и hidden — как будто нет."""
        course = self.db.get(Course, course_id)
        if course is None or course.status not in CATALOG_STATUSES:
            return None
        return course

    def catalog(self) -> list[Course]:
        return list(
            self.db.scalars(select(Course).where(Course.status.in_(CATALOG_STATUSES)))
        )

    def group_versions(self, group_id: int) -> list[Course]:
        return list(
            self.db.scalars(
                select(Course)
                .where(Course.group_id == group_id, Course.status.in_(CATALOG_STATUSES))
                .order_by(Course.id)
            )
        )

    def lessons_count(self, course_ids: list[int]) -> dict[int, int]:
        """Число нескрытых уроков по курсам — {course_id: count}."""
        rows = self.db.execute(
            select(Module.course_id, func.count())
            .select_from(Lesson)
            .join(Module, Module.id == Lesson.module_id)
            .where(Module.course_id.in_(course_ids), Lesson.is_hidden.is_(False))
            .group_by(Module.course_id)
        )
        return dict(rows.all())

    def students_count(self, course_ids: list[int]) -> dict[int, int]:
        """Действующие enrollment по курсам — {course_id: count}."""
        rows = self.db.execute(
            select(Enrollment.course_id, func.count())
            .where(Enrollment.course_id.in_(course_ids), Enrollment.revoked_at.is_(None))
            .group_by(Enrollment.course_id)
        )
        return dict(rows.all())

    def group_ratings(self, group_ids: list[int]) -> dict[int, tuple[float, int]]:
        """Средняя и число оценок по языковой группе — {group_id: (avg, count)}.

        В расчёт идёт последний отзыв каждого автора (BACKEND_NOTES, раздел 13),
        чтобы один человек не влиял на среднюю трижды.
        """
        latest = (
            select(Course.group_id.label("group_id"), Review.rating.label("rating"))
            .select_from(Review)
            .join(Course, Course.id == Review.course_id)
            .where(Course.group_id.in_(group_ids))
            .distinct(Course.group_id, Review.user_id)
            .order_by(
                Course.group_id, Review.user_id, Review.created_at.desc(), Review.id.desc()
            )
            .subquery()
        )
        rows = self.db.execute(
            select(latest.c.group_id, func.avg(latest.c.rating), func.count()).group_by(
                latest.c.group_id
            )
        )
        return {gid: (round(float(avg), 1), count) for gid, avg, count in rows}

    def modules(self, course_id: int) -> list[Module]:
        return list(
            self.db.scalars(
                select(Module)
                .where(Module.course_id == course_id)
                .order_by(Module.order_index, Module.id)
            )
        )

    def lessons(self, course_id: int) -> list[Lesson]:
        return list(
            self.db.scalars(
                select(Lesson)
                .join(Module, Module.id == Lesson.module_id)
                .where(Module.course_id == course_id, Lesson.is_hidden.is_(False))
                .order_by(Lesson.order_index, Lesson.id)
            )
        )

    def quizzes(self, course_id: int) -> list[Quiz]:
        return list(
            self.db.scalars(
                select(Quiz)
                .join(Module, Module.id == Quiz.module_id)
                .where(Module.course_id == course_id)
                .order_by(Quiz.order_index, Quiz.id)
            )
        )

    def tasks(self, course_id: int) -> list[Task]:
        return list(
            self.db.scalars(
                select(Task)
                .join(Module, Module.id == Task.module_id)
                .where(Module.course_id == course_id)
                .order_by(Task.order_index, Task.id)
            )
        )

    def questions_count(self, quiz_ids: list[int]) -> dict[int, int]:
        """Число нескрытых вопросов по тестам — {quiz_id: count}."""
        if not quiz_ids:
            return {}
        rows = self.db.execute(
            select(Question.quiz_id, func.count())
            .where(Question.quiz_id.in_(quiz_ids), Question.is_hidden.is_(False))
            .group_by(Question.quiz_id)
        )
        return dict(rows.all())


class LessonRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def visible_with_course(self, lesson_id: int) -> tuple[Lesson, Course] | None:
        """Урок вместе с курсом, которому он принадлежит: доступ проверяется
        по курсу. Скрытый урок и невидимый курс — как будто урока нет."""
        row = self.db.execute(
            select(Lesson, Course)
            .join(Module, Module.id == Lesson.module_id)
            .join(Course, Course.id == Module.course_id)
            .where(
                Lesson.id == lesson_id,
                Lesson.is_hidden.is_(False),
                Course.status.in_(CATALOG_STATUSES),
            )
        ).first()
        return (row[0], row[1]) if row is not None else None

    def files(self, lesson_id: int) -> list[LessonFile]:
        return list(
            self.db.scalars(
                select(LessonFile)
                .where(LessonFile.lesson_id == lesson_id)
                .order_by(LessonFile.order_index, LessonFile.id)
            )
        )

    def file_with_course(self, file_id: int) -> tuple[LessonFile, Course] | None:
        """Материал урока вместе с курсом: доступ к файлу — это доступ к курсу.
        Скрытый урок и невидимый курс прячут и свои материалы."""
        row = self.db.execute(
            select(LessonFile, Course)
            .join(Lesson, Lesson.id == LessonFile.lesson_id)
            .join(Module, Module.id == Lesson.module_id)
            .join(Course, Course.id == Module.course_id)
            .where(
                LessonFile.id == file_id,
                Lesson.is_hidden.is_(False),
                Course.status.in_(CATALOG_STATUSES),
            )
        ).first()
        return (row[0], row[1]) if row is not None else None

    def file_by_id(self, file_id: int) -> LessonFile | None:
        """Без проверок видимости: этой строкой пользуется раздача байтов,
        где право на файл доказывает подпись ссылки, а не сессия."""
        return self.db.get(LessonFile, file_id)


class ProgressRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def done_keys(self, user_id: int, course_id: int) -> set[tuple[str, int]]:
        """Пройденное в курсе — ключами («lesson» | «quiz» | «task», id):
        отмеченные уроки, тесты со сданной зачётной попыткой, зачтённые задания.
        Скрытый урок выпадает и из done, и из total — проценты не ломаются.
        """
        lessons = self.db.scalars(
            select(LessonProgress.lesson_id)
            .join(Lesson, Lesson.id == LessonProgress.lesson_id)
            .join(Module, Module.id == Lesson.module_id)
            .where(
                Module.course_id == course_id,
                LessonProgress.user_id == user_id,
                Lesson.is_hidden.is_(False),
            )
        )
        quizzes = self.db.scalars(
            select(QuizAttempt.quiz_id)
            .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .join(Module, Module.id == Quiz.module_id)
            .where(
                Module.course_id == course_id,
                QuizAttempt.user_id == user_id,
                # Незавершённая и незачётная попытки тест не проходят
                QuizAttempt.finished_at.is_not(None),
                QuizAttempt.is_counted.is_(True),
                QuizAttempt.passed.is_(True),
            )
        )
        tasks = self.db.scalars(
            select(Submission.task_id)
            .join(Task, Task.id == Submission.task_id)
            .join(Module, Module.id == Task.module_id)
            .where(
                Module.course_id == course_id,
                Submission.user_id == user_id,
                Submission.status == SUBMISSION_ACCEPTED,
            )
        )
        return (
            {("lesson", lesson_id) for lesson_id in lessons}
            | {("quiz", quiz_id) for quiz_id in quizzes}
            | {("task", task_id) for task_id in tasks}
        )

    def is_lesson_done(self, user_id: int, lesson_id: int) -> bool:
        return (
            self.db.get(LessonProgress, {"user_id": user_id, "lesson_id": lesson_id})
            is not None
        )

    def mark_lesson_done(self, user_id: int, lesson_id: int) -> None:
        """Отметка «урок пройден». Идемпотентна на уровне базы: повторный вызов
        и двойной клик не сдвигают completed_at первой отметки."""
        self.db.execute(
            pg_insert(LessonProgress)
            .values(user_id=user_id, lesson_id=lesson_id)
            .on_conflict_do_nothing()
        )


class ReviewRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def page(self, course_id: int, offset: int, limit: int) -> list[tuple[Review, User]]:
        rows = self.db.execute(
            select(Review, User)
            .join(User, User.id == Review.user_id)
            .where(Review.course_id == course_id)
            .order_by(Review.created_at.desc(), Review.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [(review, author) for review, author in rows]

    def count(self, course_id: int) -> int:
        return (
            self.db.scalar(
                select(func.count()).select_from(Review).where(Review.course_id == course_id)
            )
            or 0
        )

    def breakdown(self, course_id: int) -> dict[int, int]:
        """Счётчики по звёздам {rating: count} — по последнему отзыву автора."""
        latest = (
            select(Review.rating.label("rating"))
            .where(Review.course_id == course_id)
            .distinct(Review.user_id)
            .order_by(Review.user_id, Review.created_at.desc(), Review.id.desc())
            .subquery()
        )
        rows = self.db.execute(select(latest.c.rating, func.count()).group_by(latest.c.rating))
        return dict(rows.all())

    def create(self, course_id: int, user_id: int, rating: int, text: str) -> Review:
        review = Review(course_id=course_id, user_id=user_id, rating=rating, text=text)
        self.db.add(review)
        self.db.flush()
        return review


class LeadRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def by_id(self, lead_id: int) -> Lead | None:
        return self.db.get(Lead, lead_id)

    def open_for(self, user_id: int, course_id: int) -> Lead | None:
        return self.db.scalar(
            select(Lead)
            .where(
                Lead.user_id == user_id,
                Lead.course_id == course_id,
                Lead.status.in_(OPEN_LEAD_STATUSES),
            )
            .order_by(Lead.id.desc())
            .limit(1)
        )

    def open_for_user(self, user_id: int) -> list[tuple[Lead, Course]]:
        rows = self.db.execute(
            select(Lead, Course)
            .join(Course, Course.id == Lead.course_id)
            .where(Lead.user_id == user_id, Lead.status.in_(OPEN_LEAD_STATUSES))
            .order_by(Lead.created_at.desc(), Lead.id.desc())
        )
        return [(lead, course) for lead, course in rows]

    def create(self, user_id: int, course_id: int, price_snapshot: int | None) -> Lead:
        lead = Lead(user_id=user_id, course_id=course_id, price_snapshot=price_snapshot)
        self.db.add(lead)
        self.db.flush()
        return lead

    def admin_page(
        self,
        *,
        status: str | None,
        course_id: int | None,
        q: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[Lead, User, Course]], int]:
        conds = []
        if status == "open":
            # Псевдостатус «в работе»: всё, что ещё ждёт решения админа
            conds.append(Lead.status.in_(OPEN_LEAD_STATUSES))
        elif status is not None:
            conds.append(Lead.status == status)
        if course_id is not None:
            conds.append(Lead.course_id == course_id)
        if q is not None and q.strip():
            conds.append(self._q_filter(q))

        total = (
            self.db.scalar(
                select(func.count())
                .select_from(Lead)
                .join(User, User.id == Lead.user_id)
                .where(*conds)
            )
            or 0
        )
        rows = self.db.execute(
            select(Lead, User, Course)
            .join(User, User.id == Lead.user_id)
            .join(Course, Course.id == Lead.course_id)
            .where(*conds)
            .order_by(Lead.created_at.desc(), Lead.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [(lead, teacher, course) for lead, teacher, course in rows], total

    @staticmethod
    def _q_filter(q: str):
        """Поиск по ФИО и телефону. Цифры запроса нормализуются к хранимому
        виду: «8 707 123…» находит +7707123…"""
        conds = [
            func.concat_ws(" ", User.last_name, User.first_name, User.middle_name).ilike(
                f"%{q.strip()}%"
            )
        ]
        digits = re.sub(r"\D", "", q)
        if digits:
            if digits.startswith("8"):
                digits = "7" + digits[1:]
            conds.append(User.phone.like(f"%{digits}%"))
        return or_(*conds)


class EnrollmentRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def active_for(self, user_id: int, course_id: int) -> Enrollment | None:
        return self.db.scalar(
            select(Enrollment).where(
                Enrollment.user_id == user_id,
                Enrollment.course_id == course_id,
                Enrollment.revoked_at.is_(None),
            )
        )

    def by_user_course(self, user_id: int, course_id: int) -> Enrollment | None:
        # Включая отозванный: unique (user_id, course_id) — строка всегда одна
        return self.db.scalar(
            select(Enrollment).where(
                Enrollment.user_id == user_id, Enrollment.course_id == course_id
            )
        )

    def active_for_user(self, user_id: int) -> list[tuple[Enrollment, Course]]:
        rows = self.db.execute(
            select(Enrollment, Course)
            .join(Course, Course.id == Enrollment.course_id)
            .where(Enrollment.user_id == user_id, Enrollment.revoked_at.is_(None))
            .order_by(Enrollment.granted_at.desc(), Enrollment.id.desc())
        )
        return [(enrollment, course) for enrollment, course in rows]

    def create(
        self, user_id: int, course_id: int, *, granted_by: int, paid_note: str | None
    ) -> Enrollment:
        enrollment = Enrollment(
            user_id=user_id, course_id=course_id, granted_by=granted_by, paid_note=paid_note
        )
        self.db.add(enrollment)
        self.db.flush()
        return enrollment


class NotificationRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def create(self, user_id: int, type_: str, params: dict) -> Notification:
        notification = Notification(user_id=user_id, type=type_, params=params)
        self.db.add(notification)
        self.db.flush()
        return notification
