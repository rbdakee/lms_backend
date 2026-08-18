"""Репозитории — адаптер Postgres. Сценарии получают их готовыми объектами
и не знают про SQLAlchemy."""

import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.models import (
    Answer,
    AuthCode,
    Certificate,
    Course,
    Enrollment,
    Lead,
    Lesson,
    LessonFile,
    LessonProgress,
    Module,
    Notification,
    Option,
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

# Оценка задания бинарная: зачтено или на доработку (BACKEND_NOTES, раздел 5),
# до вердикта работа ждёт в очереди. Других статусов у сдачи не бывает.
SUBMISSION_PENDING = "pending"
SUBMISSION_ACCEPTED = "accepted"
SUBMISSION_REWORK = "rework"


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


class QuizRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def visible_with_course(self, quiz_id: int) -> tuple[Quiz, Course] | None:
        """Тест вместе с курсом: доступ проверяется по курсу. Своего is_hidden
        у теста нет — прячет его только невидимый курс."""
        row = self.db.execute(
            select(Quiz, Course)
            .join(Module, Module.id == Quiz.module_id)
            .join(Course, Course.id == Module.course_id)
            .where(Quiz.id == quiz_id, Course.status.in_(CATALOG_STATUSES))
        ).first()
        return (row[0], row[1]) if row is not None else None

    def visible_questions(self, quiz_id: int) -> list[Question]:
        """Вопросы, из которых собирается попытка. Скрытые не показываются
        и не считаются — ни в questions_count, ни в max_score."""
        return list(
            self.db.scalars(
                select(Question)
                .where(Question.quiz_id == quiz_id, Question.is_hidden.is_(False))
                .order_by(Question.order_index, Question.id)
            )
        )

    def questions_by_ids(self, question_ids: list[int]) -> dict[int, Question]:
        """Вопросы снимка попытки — включая те, что успели скрыть: балл
        и разбор идут по составу попытки, а не по нынешнему тесту."""
        if not question_ids:
            return {}
        rows = self.db.scalars(select(Question).where(Question.id.in_(question_ids)))
        return {question.id: question for question in rows}

    def options(self, question_ids: list[int]) -> dict[int, list[Option]]:
        """Варианты по вопросам — {question_id: [Option]}. Внутри вопроса они
        всегда идут своим порядком: перемешивается порядок вопросов, не ответов."""
        if not question_ids:
            return {}
        rows = self.db.scalars(
            select(Option)
            .where(Option.question_id.in_(question_ids))
            .order_by(Option.question_id, Option.order_index, Option.id)
        )
        by_question: dict[int, list[Option]] = {}
        for option in rows:
            by_question.setdefault(option.question_id, []).append(option)
        return by_question


class AttemptRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def active_for(self, user_id: int, quiz_id: int) -> QuizAttempt | None:
        """Незавершённая попытка теста. Она одна: новую не начать, пока эта идёт."""
        return self.db.scalar(
            select(QuizAttempt).where(
                QuizAttempt.user_id == user_id,
                QuizAttempt.quiz_id == quiz_id,
                QuizAttempt.finished_at.is_(None),
            )
        )

    def finished_for(self, user_id: int, quiz_id: int) -> list[QuizAttempt]:
        """История попыток, старые сверху — в этом порядке её рисует экран."""
        return list(
            self.db.scalars(
                select(QuizAttempt)
                .where(
                    QuizAttempt.user_id == user_id,
                    QuizAttempt.quiz_id == quiz_id,
                    QuizAttempt.finished_at.is_not(None),
                )
                .order_by(QuizAttempt.started_at, QuizAttempt.id)
            )
        )

    def own_with_quiz(
        self, attempt_id: int, user_id: int
    ) -> tuple[QuizAttempt, Quiz, Course] | None:
        """Своя попытка вместе с тестом и курсом. Чужая не находится вовсе:
        попытки личные, и их id не публикуются — отсюда 404, а не 403."""
        row = self.db.execute(
            select(QuizAttempt, Quiz, Course)
            .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .join(Module, Module.id == Quiz.module_id)
            .join(Course, Course.id == Module.course_id)
            .where(
                QuizAttempt.id == attempt_id,
                QuizAttempt.user_id == user_id,
                Course.status.in_(CATALOG_STATUSES),
            )
        ).first()
        return (row[0], row[1], row[2]) if row is not None else None

    def create(
        self, user_id: int, quiz_id: int, question_order: list[int], *, is_counted: bool
    ) -> QuizAttempt | None:
        """Новая попытка. None — один из частичных уникальных индексов не
        пустил вторую: зачётную у непересдаваемого или активную у любого.
        Это и есть защита от двойного клика, и сценарий в этом случае
        отвечает уже существующей попыткой.

        Точка отсчёта ставится здесь, часами приложения: таймер потом считается
        теми же часами, а не серверными — смешивать их значит подарить или
        отнять у человека несколько секунд.
        """
        attempt = QuizAttempt(
            user_id=user_id,
            quiz_id=quiz_id,
            started_at=now_utc(),
            question_order=question_order,
            is_counted=is_counted,
        )
        try:
            # SAVEPOINT: откатывать всю транзакцию запроса из-за проигранной
            # гонки незачем — дальше сценарий читает попытку победителя
            with self.db.begin_nested():
                self.db.add(attempt)
                self.db.flush()
        except IntegrityError:
            return None
        return attempt

    def counted_for(self, user_id: int, quiz_id: int) -> QuizAttempt | None:
        return self.db.scalar(
            select(QuizAttempt).where(
                QuizAttempt.user_id == user_id,
                QuizAttempt.quiz_id == quiz_id,
                QuizAttempt.is_counted.is_(True),
            )
        )

    def switch_counted(self, attempt: QuizAttempt) -> None:
        """Зачётной становится последняя завершённая попытка.

        Порядок обязателен: сперва снять зачёт со старой, потом поставить
        на эту, и всё в одной транзакции — иначе частичный уникальный индекс
        не пустит вторую зачётную даже на миг.
        """
        self.db.execute(
            update(QuizAttempt)
            .where(
                QuizAttempt.user_id == attempt.user_id,
                QuizAttempt.quiz_id == attempt.quiz_id,
                QuizAttempt.id != attempt.id,
                QuizAttempt.is_counted.is_(True),
            )
            .values(is_counted=False)
        )
        attempt.is_counted = True
        self.db.flush()

    def answers(self, attempt_id: int) -> list[Answer]:
        return list(
            self.db.scalars(
                select(Answer)
                .where(Answer.attempt_id == attempt_id)
                .order_by(Answer.question_id)
            )
        )

    def save_answer(self, attempt_id: int, question_id: int, option_ids: list[int]) -> None:
        """Ответ на вопрос — upsert: человек передумал, а не ответил дважды."""
        self.db.execute(
            pg_insert(Answer)
            .values(attempt_id=attempt_id, question_id=question_id, option_ids=option_ids)
            .on_conflict_do_update(
                index_elements=[Answer.attempt_id, Answer.question_id],
                set_={"option_ids": option_ids},
            )
        )

    def max_scores(self, attempts: list[QuizAttempt]) -> dict[int, int]:
        """Максимум по каждой попытке — сумма баллов её снимка вопросов.
        Одним запросом на всю историю: у теста их бывает много."""
        question_ids = {qid for attempt in attempts for qid in attempt.question_order}
        if not question_ids:
            return {attempt.id: 0 for attempt in attempts}
        points = dict(
            self.db.execute(
                select(Question.id, Question.points).where(Question.id.in_(question_ids))
            ).all()
        )
        return {
            attempt.id: sum(points.get(qid, 0) for qid in attempt.question_order)
            for attempt in attempts
        }


class TaskRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def visible_with_course(self, task_id: int) -> tuple[Task, Course] | None:
        """Задание вместе с курсом: доступ проверяется по курсу. Своего
        is_hidden у задания нет — прячет его только невидимый курс."""
        row = self.db.execute(
            select(Task, Course)
            .join(Module, Module.id == Task.module_id)
            .join(Course, Course.id == Module.course_id)
            .where(Task.id == task_id, Course.status.in_(CATALOG_STATUSES))
        ).first()
        return (row[0], row[1]) if row is not None else None

    def by_id(self, task_id: int) -> Task | None:
        """Без проверок видимости: этой строкой пользуется раздача байтов
        шаблона, где право на файл доказывает подпись ссылки, а не сессия."""
        return self.db.get(Task, task_id)


class SubmissionRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def by_id(self, submission_id: int) -> Submission | None:
        return self.db.get(Submission, submission_id)

    def last_for(self, user_id: int, task_id: int) -> Submission | None:
        """Последняя сдача пары: её статус — и есть статус задания на экране."""
        return self.db.scalar(
            select(Submission)
            .where(Submission.user_id == user_id, Submission.task_id == task_id)
            .order_by(Submission.created_at.desc(), Submission.id.desc())
            .limit(1)
        )

    def history_for(self, user_id: int, task_id: int) -> list[Submission]:
        """История сдач, свежие сверху — в этом порядке её рисует экран."""
        return list(
            self.db.scalars(
                select(Submission)
                .where(Submission.user_id == user_id, Submission.task_id == task_id)
                .order_by(Submission.created_at.desc(), Submission.id.desc())
            )
        )

    def earlier_than(self, submission: Submission) -> list[Submission]:
        """Прежние сдачи той же пары, свежие сверху: history карточки проверки
        показывает, что человек присылал до этой работы, — саму работу нет."""
        return list(
            self.db.scalars(
                select(Submission)
                .where(
                    Submission.user_id == submission.user_id,
                    Submission.task_id == submission.task_id,
                    tuple_(Submission.created_at, Submission.id)
                    < tuple_(submission.created_at, submission.id),
                )
                .order_by(Submission.created_at.desc(), Submission.id.desc())
            )
        )

    def create(
        self, user_id: int, task_id: int, text: str | None, files: list
    ) -> Submission | None:
        """Каждая отправка — новая строка: история сдач не переписывается,
        и вердикт остаётся при той работе, к которой он был написан.

        None — частичный индекс не пустил вторую pending-строку: гонка двух
        одновременных отправок, сценарий отвечает «работа уже на проверке»."""
        submission = Submission(user_id=user_id, task_id=task_id, text=text, files=files)
        try:
            # SAVEPOINT: проигранная гонка не должна откатывать всю транзакцию
            with self.db.begin_nested():
                self.db.add(submission)
                self.db.flush()
        except IntegrityError:
            return None
        return submission

    def mark_reviewed(
        self,
        submission: Submission,
        *,
        status: str,
        comment: str | None,
        reviewed_by: int,
        reviewed_at: datetime,
    ) -> bool:
        """Вердикт — условным UPDATE: переход только из pending. False — работу
        уже проверил другой админ, и его решение не затирается: reviewed_by
        должен указывать на того, кто решение принял на самом деле."""
        result = self.db.execute(
            update(Submission)
            .where(Submission.id == submission.id, Submission.status == SUBMISSION_PENDING)
            .values(
                status=status,
                comment=comment,
                reviewed_by=reviewed_by,
                reviewed_at=reviewed_at,
            )
        )
        if result.rowcount == 0:
            return False
        self.db.refresh(submission)
        return True

    def attempt_number(self, submission: Submission) -> int:
        """Номер сдачи в паре учитель+задание: 1 — первая работа, больше —
        доработка. Порядок тот же, что в очереди, — по времени создания."""
        return (
            self.db.scalar(
                select(func.count())
                .select_from(Submission)
                .where(
                    Submission.user_id == submission.user_id,
                    Submission.task_id == submission.task_id,
                    tuple_(Submission.created_at, Submission.id)
                    <= tuple_(submission.created_at, submission.id),
                )
            )
            or 0
        )

    def by_id_with_context(
        self, submission_id: int
    ) -> tuple[Submission, User, Task, Course] | None:
        """Работа со всем, что рисует карточка проверки. Видимость курса здесь
        не проверяется: админ открывает работу и по скрытому курсу."""
        row = self.db.execute(
            select(Submission, User, Task, Course)
            .join(User, User.id == Submission.user_id)
            .join(Task, Task.id == Submission.task_id)
            .join(Module, Module.id == Task.module_id)
            .join(Course, Course.id == Module.course_id)
            .where(Submission.id == submission_id)
        ).first()
        return (row[0], row[1], row[2], row[3]) if row is not None else None

    def admin_page(
        self,
        *,
        status: str | None,
        course_id: int | None,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[Submission, User, Task, Course, int]], int]:
        """Очередь проверки: старые сверху — наверху тот, кто ждёт дольше всех."""
        conds = []
        if status is not None:
            conds.append(Submission.status == status)
        if course_id is not None:
            conds.append(Module.course_id == course_id)

        total = (
            self.db.scalar(
                select(func.count())
                .select_from(Submission)
                .join(Task, Task.id == Submission.task_id)
                .join(Module, Module.id == Task.module_id)
                .where(*conds)
            )
            or 0
        )
        # Номер сдачи считает база одним окном на всю страницу: считать его
        # запросом на каждую строку — это N+1 на самом ходовом экране админки
        numbered = select(
            Submission.id.label("submission_id"),
            func.row_number()
            .over(
                partition_by=(Submission.user_id, Submission.task_id),
                order_by=(Submission.created_at, Submission.id),
            )
            .label("attempt_number"),
        ).subquery()
        rows = self.db.execute(
            select(Submission, User, Task, Course, numbered.c.attempt_number)
            .join(User, User.id == Submission.user_id)
            .join(Task, Task.id == Submission.task_id)
            .join(Module, Module.id == Task.module_id)
            .join(Course, Course.id == Module.course_id)
            .join(numbered, numbered.c.submission_id == Submission.id)
            .where(*conds)
            .order_by(Submission.created_at, Submission.id)
            .offset(offset)
            .limit(limit)
        )
        return [
            (submission, teacher, task, course, number)
            for submission, teacher, task, course, number in rows
        ], total


class CertificateRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def active_for(self, user_id: int, course_id: int) -> Certificate | None:
        """Действующий сертификат по курсу: он закрывает новые попытки тестов.
        Отозванный не считается — результат снова можно менять."""
        return self.db.scalar(
            select(Certificate).where(
                Certificate.user_id == user_id,
                Certificate.course_id == course_id,
                Certificate.revoked_at.is_(None),
            )
        )


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
