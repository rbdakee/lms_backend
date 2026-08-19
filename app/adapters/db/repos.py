"""Репозитории — адаптер Postgres. Сценарии получают их готовыми объектами
и не знают про SQLAlchemy."""

import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    ColumnElement,
    Integer,
    Text,
    and_,
    cast,
    delete,
    func,
    literal,
    or_,
    select,
    tuple_,
    union,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import aliased

from app.adapters.db.models import (
    Answer,
    AuthCode,
    Category,
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
    Setting,
    Submission,
    Task,
    ThreadMessage,
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


# Пространства ключей советующей блокировки: номер и адрес — разные потолки,
# и совпасть их ключи между собой не должны.
LOCK_PHONE = 1
LOCK_IP = 2


def _lock_key(value: str) -> int:
    """Советующей блокировке нужно число, а считаем мы по строке: берём
    четыре байта sha256. Совпадение двух разных строк стоит лишнего ожидания,
    но не ошибки — блокировка только упорядочивает, а решает всё равно счёт.
    """
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:4], "big", signed=True)


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

    def teachers_count(self) -> int:
        """Учителя — все, у кого не стоит is_admin: заблокированный учителем
        быть не перестал (CONTRACT, сессия 6)."""
        return (
            self.db.scalar(
                select(func.count()).select_from(User).where(User.is_admin.is_(False))
            )
            or 0
        )


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

    def _advisory_lock(self, namespace: int, value: str) -> None:
        self.db.execute(
            select(
                func.pg_advisory_xact_lock(
                    cast(namespace, Integer), cast(_lock_key(value), Integer)
                )
            )
        )

    def lock_sending(self, phone: str, ip: str | None) -> None:
        """Занять до конца транзакции право отправить код этому номеру
        с этого адреса.

        Потолки SMS — это «прочитали, посчитали, записали», а между чтением
        и записью помещается такой же запрос: залп из сорока одновременных
        отправок читает один и тот же счётчик и уходит сорока SMS, за которые
        платит площадка. Уникальным индексом это не закрыть — живой код
        отличается от погашенного сравнением `expires_at` с now(), а предикат
        индекса обязан быть неизменяемым. Поэтому блокировка, и советующая,
        а не строчная: считаем мы не строку, а номер и адрес, и на девятом
        коде блокировать нечего — строки десятого ещё нет. Ждать здесь некому:
        отправок на номер десять в сутки.

        Порядок ключей всегда один — сначала номер, потом адрес: две
        блокировки, взятые в разном порядке, дают взаимную.
        """
        self._advisory_lock(LOCK_PHONE, phone)
        if ip is not None:
            self._advisory_lock(LOCK_IP, ip)

    def lock_attempts(self, code: AuthCode) -> int:
        """Занять строку кода до конца транзакции и вернуть счётчик попыток,
        какой он сейчас в базе.

        Строка занимается до сверки, а не после: без этого залп сверяет код
        столько раз, сколько в залпе запросов, и восемь одновременных
        подборов проходят там, где попыток три.
        """
        return self.db.scalar(
            select(AuthCode.attempts).where(AuthCode.id == code.id).with_for_update()
        )

    def bump_attempts(self, code: AuthCode) -> int:
        """Увеличить счётчик попыток и вернуть, сколько стало.

        Считает база, а не питон, по той же причине, что и `bump` у настроек:
        два `attempts += 1`, посчитанные чтением и записью, дают в базе
        единицу — и «три попытки» превращаются в «три попытки на залп».
        """
        return self.db.scalar(
            update(AuthCode)
            .where(AuthCode.id == code.id)
            .values(attempts=AuthCode.attempts + 1)
            .returning(AuthCode.attempts)
        )


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

    def revoke_all(self, user_id: int) -> int:
        """Все сессии человека разом — смена номера телефона в админке.
        Старая симка у него уже не в руках, и живая сессия на ней чужая
        (CONTRACT, сессия 7б)."""
        result = self.db.execute(
            update(Session)
            .where(Session.user_id == user_id, Session.revoked_at.is_(None))
            .values(revoked_at=now_utc())
        )
        return result.rowcount

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


class CourseVisibility:
    """Правило «курс существует для площадки» — отдельным методом, а не
    условием на месте: режим предпросмотра снимает его ровно с одного курса,
    подменяя репозиторий целиком (BACKEND_NOTES, раздел 12). Наследуют его
    все репозитории, которые о видимости курса спрашивают.
    """

    def visible_course(self) -> ColumnElement[bool]:
        return Course.status.in_(CATALOG_STATUSES)


class CourseRepo(CourseVisibility):
    def __init__(self, db: DbSession):
        self.db = db

    def by_id(self, course_id: int) -> Course | None:
        return self.db.get(Course, course_id)

    def visible_by_id(self, course_id: int) -> Course | None:
        """Версия курса, существующая для площадки: draft и hidden — как будто нет."""
        return self.db.scalar(
            select(Course).where(Course.id == course_id, self.visible_course())
        )

    def catalog(self) -> list[Course]:
        return list(
            self.db.scalars(select(Course).where(Course.status.in_(CATALOG_STATUSES)))
        )

    def published_count(self) -> int:
        """Версии курсов, видимые в каталоге: русская и казахская считаются
        порознь — это два курса в списке админа (CONTRACT, сессия 6)."""
        return (
            self.db.scalar(
                select(func.count())
                .select_from(Course)
                .where(Course.status.in_(CATALOG_STATUSES))
            )
            or 0
        )

    def group_versions(self, group_id: int) -> list[Course]:
        return list(
            self.db.scalars(
                select(Course)
                .where(Course.group_id == group_id, self.visible_course())
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
        чтобы один человек не влиял на среднюю трижды. Удалённый админом
        отзыв не идёт ни в среднюю, ни в счётчик на карточке: наружу он
        не приходит нигде, и число под звёздами обязано это повторять.
        """
        latest = (
            select(Course.group_id.label("group_id"), Review.rating.label("rating"))
            .select_from(Review)
            .join(Course, Course.id == Review.course_id)
            .where(Course.group_id.in_(group_ids), Review.deleted_at.is_(None))
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

    def lessons(self, course_id: int, *, include_hidden: bool = False) -> list[Lesson]:
        """Уроки курса по порядку. По умолчанию видимые: скрытого урока
        у учителя нет ни в программе, ни в процентах.

        `include_hidden` — для чек-листа условий сертификата: он отбирает
        скрытое по своему правилу, а не по одному только флагу, и решает это
        сам (`application/certificates.py`). То же у `quizzes` и `tasks`.
        """
        conds = [Module.course_id == course_id]
        if not include_hidden:
            conds.append(Lesson.is_hidden.is_(False))
        return list(
            self.db.scalars(
                select(Lesson)
                .join(Module, Module.id == Lesson.module_id)
                .where(*conds)
                .order_by(Lesson.order_index, Lesson.id)
            )
        )

    def quizzes(self, course_id: int, *, include_hidden: bool = False) -> list[Quiz]:
        conds = [Module.course_id == course_id]
        if not include_hidden:
            conds.append(Quiz.is_hidden.is_(False))
        return list(
            self.db.scalars(
                select(Quiz)
                .join(Module, Module.id == Quiz.module_id)
                .where(*conds)
                .order_by(Quiz.order_index, Quiz.id)
            )
        )

    def tasks(self, course_id: int, *, include_hidden: bool = False) -> list[Task]:
        conds = [Module.course_id == course_id]
        if not include_hidden:
            conds.append(Task.is_hidden.is_(False))
        return list(
            self.db.scalars(
                select(Task)
                .join(Module, Module.id == Task.module_id)
                .where(*conds)
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

    def lesson_numbers(self, course_ids: list[int]) -> dict[int, int]:
        """Сквозные номера видимых уроков курса — {lesson_id: number}.

        Порядок тот же, что у программы: модули по order_index, внутри модуля
        элементы по order_index. Тесты и задания в нумерации не участвуют
        (CONTRACT, сессия 6). Номер считает база одним окном на всю страницу:
        собирать программу курса ради подписи «Урок 6» — это лишний обход.
        """
        if not course_ids:
            return {}
        rows = self.db.execute(
            select(
                Lesson.id,
                func.row_number()
                .over(
                    partition_by=Module.course_id,
                    order_by=(Module.order_index, Module.id, Lesson.order_index, Lesson.id),
                )
                .label("number"),
            )
            .join(Module, Module.id == Lesson.module_id)
            .where(Module.course_id.in_(course_ids), Lesson.is_hidden.is_(False))
        )
        return dict(rows.all())


class LessonRepo(CourseVisibility):
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
                self.visible_course(),
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
                self.visible_course(),
            )
        ).first()
        return (row[0], row[1]) if row is not None else None

    def file_by_id(self, file_id: int) -> LessonFile | None:
        """Без проверок видимости: этой строкой пользуется раздача байтов,
        где право на файл доказывает подпись ссылки, а не сессия."""
        return self.db.get(LessonFile, file_id)


class QuizRepo(CourseVisibility):
    def __init__(self, db: DbSession):
        self.db = db

    def visible_with_course(self, quiz_id: int) -> tuple[Quiz, Course] | None:
        """Тест вместе с курсом: доступ проверяется по курсу. Скрытый тест
        и невидимый курс — как будто теста нет: спрятанный элемент исчезает
        у учителя целиком, а не только из программы."""
        row = self.db.execute(
            select(Quiz, Course)
            .join(Module, Module.id == Quiz.module_id)
            .join(Course, Course.id == Module.course_id)
            .where(Quiz.id == quiz_id, Quiz.is_hidden.is_(False), self.visible_course())
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


class AttemptRepo(CourseVisibility):
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
                self.visible_course(),
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

    def counted_for_quiz(self, course_id: int, quiz_id: int) -> list[QuizAttempt]:
        """Зачётные завершённые попытки одного теста у действующих участников
        курса — из них считается средний балл в отчёте. Отозванный доступ
        в среднее не идёт: его нет и в `granted` (CONTRACT, сессия 6)."""
        return list(
            self.db.scalars(
                select(QuizAttempt)
                .join(
                    Enrollment,
                    and_(
                        Enrollment.user_id == QuizAttempt.user_id,
                        Enrollment.course_id == course_id,
                        Enrollment.revoked_at.is_(None),
                    ),
                )
                .where(
                    QuizAttempt.quiz_id == quiz_id,
                    QuizAttempt.finished_at.is_not(None),
                    QuizAttempt.is_counted.is_(True),
                )
            )
        )

    def course_attempts(self, course_id: int, user_ids: list[int]) -> list[QuizAttempt]:
        """Попытки по всем тестам курса у перечисленных учителей — одним
        запросом на страницу отчёта, а не по запросу на клетку таблицы."""
        if not user_ids:
            return []
        return list(
            self.db.scalars(
                select(QuizAttempt)
                .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
                .join(Module, Module.id == Quiz.module_id)
                .where(Module.course_id == course_id, QuizAttempt.user_id.in_(user_ids))
                .order_by(QuizAttempt.started_at, QuizAttempt.id)
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


class TaskRepo(CourseVisibility):
    def __init__(self, db: DbSession):
        self.db = db

    def visible_with_course(self, task_id: int) -> tuple[Task, Course] | None:
        """Задание вместе с курсом: доступ проверяется по курсу. Скрытое
        задание и невидимый курс — как будто задания нет: спрятанный элемент
        исчезает у учителя целиком, а не только из программы."""
        row = self.db.execute(
            select(Task, Course)
            .join(Module, Module.id == Task.module_id)
            .join(Course, Course.id == Module.course_id)
            .where(Task.id == task_id, Task.is_hidden.is_(False), self.visible_course())
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

    def pending_head(
        self, limit: int
    ) -> tuple[list[tuple[Submission, User, Task, Course]], int]:
        """Свежие работы из очереди и полное число ждущих — плитка дашборда.

        Порядок свой, а не как в очереди проверки: на дашборде все три списка
        свежими сверху (DESIGN_BRIEF, 5.15), а очередь показывает наверху того,
        кто ждёт дольше всех. Номер сдачи здесь не нужен — дашборд его не рисует.
        """
        total = (
            self.db.scalar(
                select(func.count())
                .select_from(Submission)
                .where(Submission.status == SUBMISSION_PENDING)
            )
            or 0
        )
        rows = self.db.execute(
            select(Submission, User, Task, Course)
            .join(User, User.id == Submission.user_id)
            .join(Task, Task.id == Submission.task_id)
            .join(Module, Module.id == Task.module_id)
            .join(Course, Course.id == Module.course_id)
            .where(Submission.status == SUBMISSION_PENDING)
            .order_by(Submission.created_at.desc(), Submission.id.desc())
            .limit(limit)
        )
        return [
            (submission, teacher, task, course) for submission, teacher, task, course in rows
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

    def by_number(self, number: str) -> Certificate | None:
        """Публичная проверка ищет по номеру и находит в том числе отозванный:
        запись в реестре есть, просто документ недействителен."""
        return self.db.scalar(select(Certificate).where(Certificate.number == number))

    def by_id(self, certificate_id: int) -> Certificate | None:
        """Документ по id — вместе с отозванным.

        Печать отозванного запрещена, но отсеивает его сценарий, а не запрос:
        отфильтруй `revoked_at` здесь — и 404 получался бы по случайности,
        а не по решению, а чужой документ отвечал бы «не найден» вместо
        отказа в праве.
        """
        return self.db.get(Certificate, certificate_id)

    def list_for_user(self, user_id: int) -> list[Certificate]:
        """Свои сертификаты, свежие сверху. Отозванные в кабинет не попадают."""
        return list(
            self.db.scalars(
                select(Certificate)
                .where(Certificate.user_id == user_id, Certificate.revoked_at.is_(None))
                .order_by(Certificate.issued_at.desc(), Certificate.id.desc())
            )
        )

    def create(
        self,
        *,
        number: str,
        user_id: int,
        course_id: int,
        holder_name: str,
        course_title: str,
        hours: int,
        lang: str,
    ) -> Certificate | None:
        """Новый сертификат. None — вставку отбила база: либо номер уже занят,
        либо соседний запрос выдал сертификат первым (uq_certificate_active).
        Оба случая разбирает сценарий: первый — новым номером, второй — чужим
        документом, потому что двойной клик обязан дать один документ.
        """
        certificate = Certificate(
            number=number,
            user_id=user_id,
            course_id=course_id,
            holder_name=holder_name,
            course_title=course_title,
            hours=hours,
            lang=lang,
        )
        try:
            # SAVEPOINT: откатывать всю транзакцию запроса из-за проигранной
            # гонки незачем — дальше сценарий читает документ победителя
            with self.db.begin_nested():
                self.db.add(certificate)
                self.db.flush()
        except IntegrityError:
            return None
        return certificate

    def active_count(self, course_id: int | None = None) -> int:
        """Действующие сертификаты: без course_id — по всей платформе (справочное
        число дашборда), с ним — по версии курса (сводка отчёта). Отозванные
        не считаются ни там, ни там (CONTRACT, сессия 6)."""
        conds = [Certificate.revoked_at.is_(None)]
        if course_id is not None:
            conds.append(Certificate.course_id == course_id)
        return (
            self.db.scalar(select(func.count()).select_from(Certificate).where(*conds)) or 0
        )

    def active_user_ids(self, course_id: int, user_ids: list[int]) -> set[int]:
        """Кому из перечисленных сертификат уже выдан — одним запросом на всю
        страницу отчёта, а не по запросу на строку."""
        if not user_ids:
            return set()
        return set(
            self.db.scalars(
                select(Certificate.user_id).where(
                    Certificate.course_id == course_id,
                    Certificate.user_id.in_(user_ids),
                    Certificate.revoked_at.is_(None),
                )
            )
        )

    def unfinished_attempt(self, user_id: int, course_id: int) -> bool:
        """Идёт ли по курсу незавершённая попытка теста: её finish ещё может
        поменять зачёт, поэтому выдача ждёт (CONTRACT, сессия 6).

        Попытка скрытого теста ждать не заставляет: её finish в чек-листе
        уже ничего не поменяет, а брошенная попытка висит незавершённой
        вечно — человек остался бы без документа навсегда.
        """
        return (
            self.db.scalar(
                select(QuizAttempt.id)
                .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
                .join(Module, Module.id == Quiz.module_id)
                .where(
                    Module.course_id == course_id,
                    QuizAttempt.user_id == user_id,
                    Quiz.is_hidden.is_(False),
                    QuizAttempt.finished_at.is_(None),
                )
                .limit(1)
            )
            is not None
        )


class ProgressRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def done_keys(
        self, user_id: int, course_id: int, *, include_hidden: bool = False
    ) -> set[tuple[str, int]]:
        """Пройденное в курсе — ключами («lesson» | «quiz» | «task», id):
        отмеченные уроки, тесты со сданной зачётной попыткой, зачтённые задания.

        По умолчанию только видимое: программа и проценты считаются по нему,
        и скрытый элемент выпадает разом из done и из total. `include_hidden`
        нужен чек-листу условий сертификата: пройденное скрытие не отбирает,
        и решает это домен (`application/certificates.py`).
        """
        lesson_conds = [Module.course_id == course_id, LessonProgress.user_id == user_id]
        quiz_conds = [Module.course_id == course_id, QuizAttempt.user_id == user_id]
        task_conds = [Module.course_id == course_id, Submission.user_id == user_id]
        if not include_hidden:
            lesson_conds.append(Lesson.is_hidden.is_(False))
            quiz_conds.append(Quiz.is_hidden.is_(False))
            task_conds.append(Task.is_hidden.is_(False))
        lessons = self.db.scalars(
            select(LessonProgress.lesson_id)
            .join(Lesson, Lesson.id == LessonProgress.lesson_id)
            .join(Module, Module.id == Lesson.module_id)
            .where(*lesson_conds)
        )
        quizzes = self.db.scalars(
            select(QuizAttempt.quiz_id)
            .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .join(Module, Module.id == Quiz.module_id)
            .where(
                *quiz_conds,
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
            .where(*task_conds, Submission.status == SUBMISSION_ACCEPTED)
        )
        return (
            {("lesson", lesson_id) for lesson_id in lessons}
            | {("quiz", quiz_id) for quiz_id in quizzes}
            | {("task", task_id) for task_id in tasks}
        )

    def _course_done_rows(self, course_id: int):
        """Пройденное всеми действующими участниками курса — строки
        (user_id, kind, item_id, is_hidden).

        Правила ровно те же, что у `done_keys` одного учителя: отчёт админа
        и экран учителя обязаны показывать один и тот же процент, а два
        независимых подсчёта разошлись бы (CONTRACT, сессия 6). Отозванный
        доступ не считается — его нет и в `granted`.

        Скрытость приходит колонкой, а не фильтром: проценты и воронка
        считают по видимому, а чек-листу сертификата нужно и пройденное
        скрытое, и добирать его вторым запросом на страницу отчёта незачем.

        UNION, а не UNION ALL: два зачтённых ответа по одному заданию — это
        всё равно одно пройденное задание, как и в множестве `done_keys`.
        """
        lessons = (
            select(
                LessonProgress.user_id.label("user_id"),
                literal("lesson").label("kind"),
                LessonProgress.lesson_id.label("item_id"),
                Lesson.is_hidden.label("is_hidden"),
            )
            .select_from(LessonProgress)
            .join(Lesson, Lesson.id == LessonProgress.lesson_id)
            .join(Module, Module.id == Lesson.module_id)
            .join(
                Enrollment,
                and_(
                    Enrollment.user_id == LessonProgress.user_id,
                    Enrollment.course_id == Module.course_id,
                    Enrollment.revoked_at.is_(None),
                ),
            )
            .where(Module.course_id == course_id)
        )
        quizzes = (
            select(
                QuizAttempt.user_id.label("user_id"),
                literal("quiz").label("kind"),
                QuizAttempt.quiz_id.label("item_id"),
                Quiz.is_hidden.label("is_hidden"),
            )
            .select_from(QuizAttempt)
            .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .join(Module, Module.id == Quiz.module_id)
            .join(
                Enrollment,
                and_(
                    Enrollment.user_id == QuizAttempt.user_id,
                    Enrollment.course_id == Module.course_id,
                    Enrollment.revoked_at.is_(None),
                ),
            )
            .where(
                Module.course_id == course_id,
                QuizAttempt.finished_at.is_not(None),
                QuizAttempt.is_counted.is_(True),
                QuizAttempt.passed.is_(True),
            )
        )
        tasks = (
            select(
                Submission.user_id.label("user_id"),
                literal("task").label("kind"),
                Submission.task_id.label("item_id"),
                Task.is_hidden.label("is_hidden"),
            )
            .select_from(Submission)
            .join(Task, Task.id == Submission.task_id)
            .join(Module, Module.id == Task.module_id)
            .join(
                Enrollment,
                and_(
                    Enrollment.user_id == Submission.user_id,
                    Enrollment.course_id == Module.course_id,
                    Enrollment.revoked_at.is_(None),
                ),
            )
            .where(
                Module.course_id == course_id,
                Submission.status == SUBMISSION_ACCEPTED,
            )
        )
        return union(lessons, quizzes, tasks).subquery()

    def done_items_by_user(self, course_id: int) -> dict[int, set[tuple[str, int]]]:
        """Что именно прошёл каждый участник — {user_id: {(kind, item_id)}},
        вместе со скрытым: это `done_keys(include_hidden=True)` на весь курс.

        Тот же формат, что у `done_keys` одного учителя, но на весь курс
        одним запросом: условия сертификата в отчёте считаются на каждую
        строку таблицы, и поход в базу на строку — это N+1 на самом тяжёлом
        экране админки (BACKEND_NOTES, раздел 13).
        """
        done = self._course_done_rows(course_id)
        rows = self.db.execute(select(done.c.user_id, done.c.kind, done.c.item_id))
        by_user: dict[int, set[tuple[str, int]]] = {}
        for user_id, kind, item_id in rows.all():
            by_user.setdefault(user_id, set()).add((kind, item_id))
        return by_user

    def done_by_user(self, course_id: int) -> dict[int, int]:
        """Сколько элементов программы прошёл каждый участник — {user_id: count}.

        Считает база одним GROUP BY: в отчёте по курсу таких участников
        восемь сотен, и запрос на каждого — это N+1 на самом тяжёлом экране
        админки (BACKEND_NOTES, раздел 13). Кто не прошёл ничего, в ответ
        не попадает — это и есть «не начал».
        """
        done = self._course_done_rows(course_id)
        rows = self.db.execute(
            select(done.c.user_id, func.count())
            .where(done.c.is_hidden.is_(False))
            .group_by(done.c.user_id)
        )
        return dict(rows.all())

    def done_by_item(self, course_id: int) -> dict[tuple[str, int], int]:
        """Сколько участников прошли каждый элемент — {(kind, id): count}.
        Это и есть воронка отчёта, один GROUP BY на весь курс."""
        done = self._course_done_rows(course_id)
        rows = self.db.execute(
            select(done.c.kind, done.c.item_id, func.count())
            .where(done.c.is_hidden.is_(False))
            .group_by(done.c.kind, done.c.item_id)
        )
        return {(kind, item_id): count for kind, item_id, count in rows}

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
    """Отзывы. Удалённый админом отзыв не приходит наружу нигде и ни при
    каких фильтрах, поэтому `deleted_at IS NULL` стоит в каждом чтении:
    лента страницы курса, её `total`, разбивка по звёздам и лента админа
    обязаны считать по одним и тем же строкам."""

    def __init__(self, db: DbSession):
        self.db = db

    def by_id(self, review_id: int) -> Review | None:
        # Без фильтра удалённого: повторное удаление обязано найти строку,
        # чтобы ответить 204 и не двигать время первого
        return self.db.get(Review, review_id)

    def with_author_and_course(self, review_id: int) -> tuple[Review, User, Course] | None:
        """Отзыв вместе с автором и курсом — в этой форме его показывает
        лента админа, и ответ на отзыв возвращает её же элемент."""
        row = self.db.execute(
            select(Review, User, Course)
            .join(User, User.id == Review.user_id)
            .join(Course, Course.id == Review.course_id)
            .where(Review.id == review_id)
        ).first()
        return (row[0], row[1], row[2]) if row is not None else None

    def page(self, course_id: int, offset: int, limit: int) -> list[tuple[Review, User]]:
        rows = self.db.execute(
            select(Review, User)
            .join(User, User.id == Review.user_id)
            .where(Review.course_id == course_id, Review.deleted_at.is_(None))
            .order_by(Review.created_at.desc(), Review.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [(review, author) for review, author in rows]

    def count(self, course_id: int) -> int:
        return (
            self.db.scalar(
                select(func.count())
                .select_from(Review)
                .where(Review.course_id == course_id, Review.deleted_at.is_(None))
            )
            or 0
        )

    def breakdown(self, course_id: int) -> dict[int, int]:
        """Счётчики по звёздам {rating: count} — по последнему отзыву автора."""
        latest = (
            select(Review.rating.label("rating"))
            .where(Review.course_id == course_id, Review.deleted_at.is_(None))
            .distinct(Review.user_id)
            .order_by(Review.user_id, Review.created_at.desc(), Review.id.desc())
            .subquery()
        )
        rows = self.db.execute(select(latest.c.rating, func.count()).group_by(latest.c.rating))
        return dict(rows.all())

    def admin_page(
        self, *, course_id: int | None, rating: int | None, offset: int, limit: int
    ) -> tuple[list[tuple[Review, User, Course]], int]:
        """Лента отзывов по всей платформе, свежие сверху. Видимость курса
        здесь не проверяется: отзыв разбирают и по скрытому курсу."""
        conds = [Review.deleted_at.is_(None)]
        if course_id is not None:
            conds.append(Review.course_id == course_id)
        if rating is not None:
            conds.append(Review.rating == rating)
        total = self.db.scalar(select(func.count()).select_from(Review).where(*conds)) or 0
        rows = self.db.execute(
            select(Review, User, Course)
            .join(User, User.id == Review.user_id)
            .join(Course, Course.id == Review.course_id)
            .where(*conds)
            .order_by(Review.created_at.desc(), Review.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [(review, author, course) for review, author, course in rows], total

    def create(self, course_id: int, user_id: int, rating: int, text: str) -> Review:
        review = Review(course_id=course_id, user_id=user_id, rating=rating, text=text)
        self.db.add(review)
        self.db.flush()
        return review


def name_or_phone_filter(q: str):
    """Поиск по ФИО и телефону. Цифры запроса нормализуются к хранимому
    виду: «8 707 123…» находит +7707123…

    Один на очередь заявок и на список учителей: там и там админ ищет человека
    по тому, что у него записано на бумажке.
    """
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
            conds.append(name_or_phone_filter(q))

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


class EnrollmentRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def by_id(self, enrollment_id: int) -> Enrollment | None:
        return self.db.get(Enrollment, enrollment_id)

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

    def participants_count(self, course_id: int) -> int:
        """Доступы, не отозванные, — `granted` в сводке отчёта."""
        return (
            self.db.scalar(
                select(func.count())
                .select_from(Enrollment)
                .where(Enrollment.course_id == course_id, Enrollment.revoked_at.is_(None))
            )
            or 0
        )

    def completed_spans(self, course_id: int) -> list[tuple[datetime, datetime]]:
        """Пары (выдан доступ, завершён курс) у завершивших: из них считаются
        и `completed`, и среднее время прохождения. Дни считает Python той же
        функцией, что и «ждёт N дней», — второго календаря в SQL заводить
        незачем (CONTRACT, сессия 6)."""
        rows = self.db.execute(
            select(Enrollment.granted_at, Enrollment.completed_at).where(
                Enrollment.course_id == course_id,
                Enrollment.revoked_at.is_(None),
                Enrollment.completed_at.is_not(None),
            )
        )
        return [(granted_at, completed_at) for granted_at, completed_at in rows]

    def participants_page(
        self, course_id: int, *, q: str | None, offset: int, limit: int
    ) -> tuple[list[User], int]:
        """Страница таблицы участников. По алфавиту: в отчёте на восемь сотен
        строк человека ищут по фамилии, а не по дате выдачи доступа.

        Поиск только по ФИО — телефона в отчёте нет (CONTRACT, сессия 6).
        """
        conds = [Enrollment.course_id == course_id, Enrollment.revoked_at.is_(None)]
        if q is not None and q.strip():
            conds.append(
                func.concat_ws(" ", User.last_name, User.first_name, User.middle_name).ilike(
                    f"%{q.strip()}%"
                )
            )
        total = (
            self.db.scalar(
                select(func.count())
                .select_from(Enrollment)
                .join(User, User.id == Enrollment.user_id)
                .where(*conds)
            )
            or 0
        )
        users = list(
            self.db.scalars(
                select(User)
                .join(Enrollment, Enrollment.user_id == User.id)
                .where(*conds)
                .order_by(User.last_name, User.first_name, User.id)
                .offset(offset)
                .limit(limit)
            )
        )
        return users, total

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

    def page(self, user_id: int, offset: int, limit: int) -> list[Notification]:
        # Свежие сверху; непрочитанные наверх не поднимаются (CONTRACT, сессия 6)
        return list(
            self.db.scalars(
                select(Notification)
                .where(Notification.user_id == user_id)
                .order_by(Notification.created_at.desc(), Notification.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )

    def count(self, user_id: int) -> int:
        return (
            self.db.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.user_id == user_id)
            )
            or 0
        )

    def unread_count(self, user_id: int) -> int:
        return (
            self.db.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.user_id == user_id, Notification.read_at.is_(None))
            )
            or 0
        )

    def mark_read(self, user_id: int, ids: list[int] | None) -> None:
        """`ids=None` — все свои. Условие UPDATE и есть вся проверка: чужие
        и несуществующие id просто ничего не находят, а `read_at IS NULL`
        не даёт переписать время первого прочтения (CONTRACT, сессия 6)."""
        conds = [Notification.user_id == user_id, Notification.read_at.is_(None)]
        if ids is not None:
            if not ids:
                return
            conds.append(Notification.id.in_(ids))
        self.db.execute(update(Notification).where(*conds).values(read_at=now_utc()))


class ThreadMessageRepo:
    """Сообщения тредов под уроками. Удалённое админом не приходит наружу
    нигде: ни в вопросах урока, ни в очереди админа, ни в счётчике дашборда,
    — поэтому `deleted_at IS NULL` стоит в каждом чтении. Ответы под удалённым
    вопросом уходят из выдачи вместе с ним: тред собирается от корня, а корня
    в выдаче уже нет."""

    def __init__(self, db: DbSession):
        self.db = db

    def by_id(self, message_id: int) -> ThreadMessage | None:
        # Без фильтра удалённого: повторное удаление обязано найти строку,
        # чтобы ответить 204 и не двигать время первого
        return self.db.get(ThreadMessage, message_id)

    def roots_page(
        self, lesson_id: int, offset: int, limit: int
    ) -> tuple[list[tuple[ThreadMessage, User]], int]:
        """Вопросы урока, свежие сверху. Автор джойнится сразу: `author_name`
        и `author_is_admin` собираются в момент чтения."""
        conds = (
            ThreadMessage.lesson_id == lesson_id,
            ThreadMessage.parent_id.is_(None),
            ThreadMessage.deleted_at.is_(None),
        )
        total = (
            self.db.scalar(select(func.count()).select_from(ThreadMessage).where(*conds)) or 0
        )
        rows = self.db.execute(
            select(ThreadMessage, User)
            .join(User, User.id == ThreadMessage.user_id)
            .where(*conds)
            .order_by(ThreadMessage.created_at.desc(), ThreadMessage.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [(message, author) for message, author in rows], total

    def replies_for(self, root_ids: list[int]) -> dict[int, list[tuple[ThreadMessage, User]]]:
        """Ответы всей страницы одним запросом — иначе N+1 на каждый вопрос."""
        if not root_ids:
            return {}
        rows = self.db.execute(
            select(ThreadMessage, User)
            .join(User, User.id == ThreadMessage.user_id)
            .where(ThreadMessage.parent_id.in_(root_ids), ThreadMessage.deleted_at.is_(None))
            .order_by(ThreadMessage.created_at, ThreadMessage.id)
        )
        by_root: dict[int, list[tuple[ThreadMessage, User]]] = {}
        for message, author in rows:
            by_root.setdefault(message.parent_id, []).append((message, author))
        return by_root

    def create(
        self, *, lesson_id: int, course_id: int, user_id: int, text_: str, parent_id: int | None
    ) -> ThreadMessage:
        # course_id пишется рядом с lesson_id: админский экран фильтрует по курсу,
        # а ходить к нему через lesson → module → course на каждый запрос дорого
        message = ThreadMessage(
            lesson_id=lesson_id,
            course_id=course_id,
            user_id=user_id,
            text=text_,
            parent_id=parent_id,
        )
        self.db.add(message)
        self.db.flush()
        return message

    def admin_page(
        self,
        *,
        answered: bool | None,
        course_id: int | None,
        q: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[ThreadMessage, User, Course, Lesson]], int]:
        """Очередь вопросов по всей платформе: свежие сверху. Видимость курса
        здесь не проверяется — админ открывает вопрос и по скрытому курсу."""
        reply = aliased(ThreadMessage)
        # Ответ автора самому себе очередь не закрывает: «без ответа» здесь —
        # это «никто ещё не ответил», а не «в треде появилась вторая строка»
        has_reply = (
            select(reply.id)
            .where(
                reply.parent_id == ThreadMessage.id,
                reply.user_id != ThreadMessage.user_id,
                # Удалённый ответ вопрос не закрывает: в выдаче его нет,
                # и очередь показала бы вопрос отвеченным без ответа
                reply.deleted_at.is_(None),
            )
            .exists()
        )
        conds = [ThreadMessage.parent_id.is_(None), ThreadMessage.deleted_at.is_(None)]
        if answered is not None:
            conds.append(has_reply if answered else ~has_reply)
        if course_id is not None:
            conds.append(ThreadMessage.course_id == course_id)
        if q is not None and q.strip():
            needle = f"%{q.strip()}%"
            conds.append(
                or_(
                    ThreadMessage.text.ilike(needle),
                    func.concat_ws(" ", User.last_name, User.first_name, User.middle_name).ilike(
                        needle
                    ),
                )
            )

        total = (
            self.db.scalar(
                select(func.count())
                .select_from(ThreadMessage)
                .join(User, User.id == ThreadMessage.user_id)
                .where(*conds)
            )
            or 0
        )
        rows = self.db.execute(
            select(ThreadMessage, User, Course, Lesson)
            .join(User, User.id == ThreadMessage.user_id)
            .join(Course, Course.id == ThreadMessage.course_id)
            .join(Lesson, Lesson.id == ThreadMessage.lesson_id)
            .where(*conds)
            .order_by(ThreadMessage.created_at.desc(), ThreadMessage.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [
            (message, author, course, lesson) for message, author, course, lesson in rows
        ], total


# Редакторы содержания (сессия 7а). Курс, урок, тест и задание админ правит
# в любом статусе, поэтому CourseVisibility эти репозитории не наследуют:
# фильтр каталога к админским спискам не применяется вовсе.

# Модели элементов программы по тому же kind, что у item_key в domain/program.py.
ITEM_MODELS = {"lesson": Lesson, "quiz": Quiz, "task": Task}


class CourseAdminRepo:
    """Курсы глазами админа: черновики и скрытые версии — обычные строки.

    Программа читается своими методами, а не методами CourseRepo: тому
    скрытый урок, тест и задание не существуют, а редактору они нужны —
    их как раз и правят.
    """

    def __init__(self, db: DbSession):
        self.db = db

    def by_id(self, course_id: int) -> Course | None:
        return self.db.get(Course, course_id)

    def page(
        self,
        *,
        status: str | None,
        lang: str | None,
        q: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Course], int]:
        conds = []
        if status is not None:
            conds.append(Course.status == status)
        if lang is not None:
            conds.append(Course.lang == lang)
        if q is not None and q.strip():
            conds.append(Course.title.ilike(f"%{q.strip()}%"))

        total = (
            self.db.scalar(select(func.count()).select_from(Course).where(*conds)) or 0
        )
        rows = list(
            self.db.scalars(
                select(Course)
                .where(*conds)
                .order_by(Course.updated_at.desc(), Course.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        return rows, total

    def group_versions(self, group_ids: list[int]) -> dict[int, list[Course]]:
        """Версии по языковым группам — {group_id: [Course]}, включая черновики:
        переключатель РУС|ҚАЗ в редакторе ведёт и на неопубликованную версию."""
        if not group_ids:
            return {}
        versions: dict[int, list[Course]] = {}
        for course in self.db.scalars(
            select(Course).where(Course.group_id.in_(group_ids)).order_by(Course.id)
        ):
            versions.setdefault(course.group_id, []).append(course)
        return versions

    def version_in_group(self, group_id: int, lang: str) -> Course | None:
        return self.db.scalar(
            select(Course).where(Course.group_id == group_id, Course.lang == lang)
        )

    def next_group_id(self) -> int:
        """Номер новой языковой группы. Своей последовательности у group_id нет:
        это не ключ, а метка «тот же курс на другом языке»."""
        return (self.db.scalar(select(func.max(Course.group_id))) or 0) + 1

    def create(self, **fields) -> Course:
        """Набор полей у создания разный: черновик заводится четырьмя,
        языковая версия и дубликат — копией почти всех."""
        course = Course(**fields)
        self.db.add(course)
        self.db.flush()
        return course

    def create_version(self, **fields) -> Course | None:
        """Вторая языковая версия. None — уникальный индекс
        (group_id, lang) не пустил вторую: гонку выиграл соседний запрос,
        и сценарий отвечает 409 с его версией."""
        course = Course(**fields)
        try:
            # SAVEPOINT: проигранная гонка не должна откатывать всю транзакцию
            with self.db.begin_nested():
                self.db.add(course)
                self.db.flush()
        except IntegrityError:
            return None
        return course

    def lock(self, course_id: int) -> None:
        """Берёт строку курса на запись до конца транзакции.

        Так закрывается единственность итогового теста в курсе: частичным
        уникальным индексом её не выразить — `course_id` у теста нет вовсе,
        он в двух джойнах (quiz → module → course). Поэтому два одновременных
        `PATCH {"is_final": true}` сводятся на строке курса: второй ждёт
        первого и уже видит его тест.
        """
        self.db.execute(select(Course.id).where(Course.id == course_id).with_for_update())

    def touch(self, course_id: int) -> None:
        """Двигает `updated_at` курса.

        Зовётся из любого редактора: правка урока, теста или задания — это
        правка курса, и столбец «Изменён» в списке курсов без этого врал бы
        (CONTRACT, сессия 7а).

        Время берут часы базы, как и у created_at: два источника времени
        на одну колонку дают курс, изменённый раньше, чем создан.
        """
        self.db.execute(
            update(Course)
            .where(Course.id == course_id)
            .values(updated_at=func.now())
            .execution_options(synchronize_session="fetch")
        )

    def course_of_item(self, kind: str, item_id: int) -> Course | None:
        """Курс, которому принадлежит урок, тест или задание; `kind` — тот же,
        что у `item_key` в domain/program.py: lesson | quiz | task.

        Нужен редакторам урока, теста и задания: сохранив элемент, они зовут
        `touch` по найденному курсу.
        """
        model = ITEM_MODELS[kind]
        return self.db.scalar(
            select(Course)
            .join(Module, Module.course_id == Course.id)
            .join(model, model.module_id == Module.id)
            .where(model.id == item_id)
        )

    # -- программа целиком, вместе со скрытым ---------------------------

    def modules(self, course_id: int) -> list[Module]:
        return list(
            self.db.scalars(
                select(Module)
                .where(Module.course_id == course_id)
                .order_by(Module.order_index, Module.id)
            )
        )

    def module_by_id(self, module_id: int) -> Module | None:
        return self.db.get(Module, module_id)

    def create_module(self, course_id: int, title: str) -> Module:
        """Модуль встаёт последним — за самым большим order_index курса."""
        last = self.db.scalar(
            select(func.max(Module.order_index)).where(Module.course_id == course_id)
        )
        module = Module(course_id=course_id, title=title, order_index=(last or 0) + 1)
        self.db.add(module)
        self.db.flush()
        return module

    def next_order_index(self, module_id: int) -> int:
        """Место нового элемента в модуле — за последним.

        Считается по всем трём таблицам сразу: в дереве урок, тест и задание
        стоят вперемешку и упорядочены общим order_index, а свой максимум
        у каждой таблицы поставил бы новый урок в середину модуля.
        """
        last = max(
            self.db.scalar(
                select(func.max(model.order_index)).where(model.module_id == module_id)
            )
            or 0
            for model in ITEM_MODELS.values()
        )
        return last + 1

    def lessons(self, course_id: int) -> list[Lesson]:
        """Все уроки курса, скрытые тоже: их админ и правит."""
        return list(
            self.db.scalars(
                select(Lesson)
                .join(Module, Module.id == Lesson.module_id)
                .where(Module.course_id == course_id)
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
        """Нескрытые вопросы по тестам — {quiz_id: count}. Скрытые не считаются
        и здесь: в дереве стоит то же число, что видит учитель."""
        if not quiz_ids:
            return {}
        rows = self.db.execute(
            select(Question.quiz_id, func.count())
            .where(Question.quiz_id.in_(quiz_ids), Question.is_hidden.is_(False))
            .group_by(Question.quiz_id)
        )
        return dict(rows.all())

    # -- чужие данные: что держит элемент от удаления -------------------

    def progress_counts(self, lesson_ids: list[int]) -> dict[int, int]:
        """Сколько человек прошли урок — {lesson_id: count}."""
        if not lesson_ids:
            return {}
        rows = self.db.execute(
            select(LessonProgress.lesson_id, func.count())
            .where(LessonProgress.lesson_id.in_(lesson_ids))
            .group_by(LessonProgress.lesson_id)
        )
        return dict(rows.all())

    def attempt_counts(self, quiz_ids: list[int]) -> dict[int, int]:
        """Попытки по тестам — {quiz_id: count}, считаются все, включая
        незачётные: разбор покажет и их."""
        if not quiz_ids:
            return {}
        rows = self.db.execute(
            select(QuizAttempt.quiz_id, func.count())
            .where(QuizAttempt.quiz_id.in_(quiz_ids))
            .group_by(QuizAttempt.quiz_id)
        )
        return dict(rows.all())

    def submission_counts(self, task_ids: list[int]) -> dict[int, int]:
        """Сдачи по заданиям — {task_id: count}, включая доработки."""
        if not task_ids:
            return {}
        rows = self.db.execute(
            select(Submission.task_id, func.count())
            .where(Submission.task_id.in_(task_ids))
            .group_by(Submission.task_id)
        )
        return dict(rows.all())

    def usage_counts(self, course_id: int) -> dict[str, int]:
        """Всё, что держит курс от удаления. Считаются и отозванные доступы,
        и закрытые заявки: строка отчёта, ссылающаяся на несуществующий курс,
        дороже лишней кнопки в меню."""
        return {
            "enrollments": self.db.scalar(
                select(func.count())
                .select_from(Enrollment)
                .where(Enrollment.course_id == course_id)
            )
            or 0,
            "leads": self.db.scalar(
                select(func.count()).select_from(Lead).where(Lead.course_id == course_id)
            )
            or 0,
            "certificates": self.db.scalar(
                select(func.count())
                .select_from(Certificate)
                .where(Certificate.course_id == course_id)
            )
            or 0,
        }

    # -- счётчики списка курсов -----------------------------------------

    def modules_count(self, course_ids: list[int]) -> dict[int, int]:
        rows = self.db.execute(
            select(Module.course_id, func.count())
            .where(Module.course_id.in_(course_ids))
            .group_by(Module.course_id)
        )
        return dict(rows.all())

    def lessons_count(self, course_ids: list[int]) -> dict[int, int]:
        """Уроки курса — {course_id: count}. Скрытые входят в число: админ
        считает то, что в курсе есть, а не то, что видно на площадке."""
        rows = self.db.execute(
            select(Module.course_id, func.count())
            .select_from(Lesson)
            .join(Module, Module.id == Lesson.module_id)
            .where(Module.course_id.in_(course_ids))
            .group_by(Module.course_id)
        )
        return dict(rows.all())

    def open_leads_count(self, course_ids: list[int]) -> dict[int, int]:
        """Заявки в работе — {course_id: count}."""
        rows = self.db.execute(
            select(Lead.course_id, func.count())
            .where(Lead.course_id.in_(course_ids), Lead.status.in_(OPEN_LEAD_STATUSES))
            .group_by(Lead.course_id)
        )
        return dict(rows.all())

    def enrollment_counts(self, course_ids: list[int]) -> dict[int, tuple[int, int]]:
        """Действующие доступы и завершившие курс — {course_id: (students,
        completed)}. Одним запросом: столбцы стоят в списке рядом."""
        rows = self.db.execute(
            select(
                Enrollment.course_id,
                func.count().filter(Enrollment.revoked_at.is_(None)),
                func.count().filter(Enrollment.completed_at.is_not(None)),
            )
            .where(Enrollment.course_id.in_(course_ids))
            .group_by(Enrollment.course_id)
        )
        return {course_id: (students, completed) for course_id, students, completed in rows}

    # -- копирование и удаление -----------------------------------------

    def copy_program(self, source_id: int, target_id: int) -> None:
        """Программа одного курса в другой: модули, уроки со всем текстом
        и ссылками, тесты с настройками, вопросами и вариантами, задания
        с условием.

        Не копируется ничего чужого — ни доступов, ни прогресса — и не
        копируются файлы: ключ в хранилище один на файл, и две строки на один
        ключ означают, что удаление из одной версии ломает вторую.
        """
        modules: dict[int, int] = {}
        for module in self.modules(source_id):
            copy = Module(
                course_id=target_id, title=module.title, order_index=module.order_index
            )
            self.db.add(copy)
            self.db.flush()
            modules[module.id] = copy.id

        for lesson in self.lessons(source_id):
            self.db.add(
                Lesson(
                    module_id=modules[lesson.module_id],
                    title=lesson.title,
                    kind=lesson.kind,
                    # Своя копия JSON: общий словарь на две строки правится в обеих
                    body=dict(lesson.body) if lesson.body is not None else None,
                    video_url=lesson.video_url,
                    video_provider=lesson.video_provider,
                    duration_label=lesson.duration_label,
                    time_required_min=lesson.time_required_min,
                    order_index=lesson.order_index,
                    is_hidden=lesson.is_hidden,
                )
            )
        for quiz in self.quizzes(source_id):
            copy = Quiz(
                module_id=modules[quiz.module_id],
                title=quiz.title,
                is_final=quiz.is_final,
                pass_score=quiz.pass_score,
                time_limit_min=quiz.time_limit_min,
                shuffle=quiz.shuffle,
                show_review=quiz.show_review,
                retakable=quiz.retakable,
                time_required_min=quiz.time_required_min,
                order_index=quiz.order_index,
                is_hidden=quiz.is_hidden,
            )
            self.db.add(copy)
            self.db.flush()
            self._copy_questions(quiz.id, copy.id)
        for task in self.tasks(source_id):
            self.db.add(
                Task(
                    module_id=modules[task.module_id],
                    title=task.title,
                    statement=dict(task.statement),
                    # template_file не копируется: это ключ в хранилище
                    submit_format=task.submit_format,
                    allowed_ext=list(task.allowed_ext),
                    max_size_mb=task.max_size_mb,
                    time_required_min=task.time_required_min,
                    order_index=task.order_index,
                    is_hidden=task.is_hidden,
                )
            )

    def _copy_questions(self, source_quiz_id: int, target_quiz_id: int) -> None:
        """Вопросы вместе с вариантами: вопрос без вариантов — это тест,
        который не пройти."""
        questions = self.db.scalars(
            select(Question)
            .where(Question.quiz_id == source_quiz_id)
            .order_by(Question.order_index, Question.id)
        )
        for question in questions:
            copy = Question(
                quiz_id=target_quiz_id,
                type=question.type,
                text=question.text,
                explanation=question.explanation,
                points=question.points,
                order_index=question.order_index,
                is_hidden=question.is_hidden,
            )
            self.db.add(copy)
            self.db.flush()
            options = self.db.scalars(
                select(Option)
                .where(Option.question_id == question.id)
                .order_by(Option.order_index, Option.id)
            )
            for option in options:
                self.db.add(
                    Option(
                        question_id=copy.id,
                        text=option.text,
                        is_correct=option.is_correct,
                        order_index=option.order_index,
                    )
                )

    def delete_modules(self, module_ids: list[int]) -> None:
        """Модули со всем содержимым: уроками, тестами, вопросами, вариантами
        и заданиями. Материалы урока и варианты вопроса уносит каскад базы
        (`ondelete=CASCADE` у lesson_file и option), а вопросы учителей под
        уроками — как и в LessonAdminRepo.delete, руками: каскада у их
        внешнего ключа нет, и без этого удаление модуля упирается в него.
        """
        if not module_ids:
            return
        quiz_ids = list(
            self.db.scalars(select(Quiz.id).where(Quiz.module_id.in_(module_ids)))
        )
        if quiz_ids:
            self.db.execute(delete(Question).where(Question.quiz_id.in_(quiz_ids)))
        self.db.execute(delete(Quiz).where(Quiz.module_id.in_(module_ids)))
        lesson_ids = list(
            self.db.scalars(select(Lesson.id).where(Lesson.module_id.in_(module_ids)))
        )
        if lesson_ids:
            self.db.execute(
                delete(ThreadMessage).where(ThreadMessage.lesson_id.in_(lesson_ids))
            )
        self.db.execute(delete(Lesson).where(Lesson.module_id.in_(module_ids)))
        self.db.execute(delete(Task).where(Task.module_id.in_(module_ids)))
        self.db.execute(delete(Module).where(Module.id.in_(module_ids)))

    def delete_course(self, course_id: int) -> None:
        self.delete_modules([module.id for module in self.modules(course_id)])
        # Режим предпросмотра держит курс ссылкой из строки сессии: не сняв
        # её, удаление упёрлось бы во внешний ключ
        self.db.execute(
            update(Session)
            .where(Session.preview_course_id == course_id)
            .values(preview_course_id=None)
        )
        self.db.execute(delete(Course).where(Course.id == course_id))


class LessonAdminRepo:
    """Урок глазами админа: скрытый урок и урок невидимого курса — обычные
    строки, их как раз и правят.

    Видимость здесь не проверяется вовсе, поэтому это не LessonRepo: тому
    урок черновика не существует, а редактор без него пуст.
    """

    def __init__(self, db: DbSession):
        self.db = db

    def with_course_and_module(self, lesson_id: int) -> tuple[Lesson, Module, Course] | None:
        """Урок вместе с модулем и курсом: и то и другое — хлебные крошки
        шапки редактора, и брать их тремя запросами незачем."""
        row = self.db.execute(
            select(Lesson, Module, Course)
            .join(Module, Module.id == Lesson.module_id)
            .join(Course, Course.id == Module.course_id)
            .where(Lesson.id == lesson_id)
        ).first()
        return (row[0], row[1], row[2]) if row is not None else None

    def create(self, **fields) -> Lesson:
        """Заготовка: ни ссылки, ни текста — они появятся в редакторе.
        Единственное место, где урок сохраняется пустым."""
        lesson = Lesson(**fields)
        self.db.add(lesson)
        self.db.flush()
        return lesson

    def delete(self, lesson_id: int) -> None:
        """Урок со всем, что на него ссылается. Материалы уносит каскад базы
        (`ondelete=CASCADE` у lesson_file), вопросы под уроком удаляем сами:
        каскада у их внешнего ключа нет, а осиротевший вопрос очередь админа
        всё равно не покажет — она джойнит урок. Прогресс сюда не доходит:
        урок, который кто-то прошёл, не удаляется вовсе.
        """
        self.db.execute(delete(ThreadMessage).where(ThreadMessage.lesson_id == lesson_id))
        self.db.execute(delete(Lesson).where(Lesson.id == lesson_id))

    # -- материалы урока -------------------------------------------------

    def files(self, lesson_id: int) -> list[LessonFile]:
        return list(
            self.db.scalars(
                select(LessonFile)
                .where(LessonFile.lesson_id == lesson_id)
                .order_by(LessonFile.order_index, LessonFile.id)
            )
        )

    def file_by_id(self, file_id: int) -> LessonFile | None:
        return self.db.get(LessonFile, file_id)

    def add_file(self, **fields) -> LessonFile:
        """Материал встаёт последним в уроке."""
        last = self.db.scalar(
            select(func.max(LessonFile.order_index)).where(
                LessonFile.lesson_id == fields["lesson_id"]
            )
        )
        file = LessonFile(**fields, order_index=(last or 0) + 1)
        self.db.add(file)
        self.db.flush()
        return file

    def delete_file(self, file_id: int) -> None:
        """Отвязывает материал от урока. Байты в хранилище остаются: порт
        storage умеет писать, читать и мерить, но не удалять."""
        self.db.execute(delete(LessonFile).where(LessonFile.id == file_id))


class QuizAdminRepo:
    """Тест глазами админа: скрытый тест, тест черновика и скрытые вопросы —
    обычные строки, их как раз и правят.

    Видимость здесь не проверяется вовсе, поэтому это не QuizRepo: тому
    и скрытый тест, и скрытый вопрос не существуют, а редактору без них
    нечего показать — спрятанное нечем достать обратно.
    """

    def __init__(self, db: DbSession):
        self.db = db

    def with_course_and_module(self, quiz_id: int) -> tuple[Quiz, Module, Course] | None:
        """Тест вместе с модулем и курсом: и то и другое — хлебные крошки
        шапки редактора, и брать их тремя запросами незачем."""
        row = self.db.execute(
            select(Quiz, Module, Course)
            .join(Module, Module.id == Quiz.module_id)
            .join(Course, Course.id == Module.course_id)
            .where(Quiz.id == quiz_id)
        ).first()
        return (row[0], row[1], row[2]) if row is not None else None

    def create(self, **fields) -> Quiz:
        """Заготовка: название, проходной балл и требуемое время. Вопросов
        у неё нет, и дерево программы покажет её как «черновик»."""
        quiz = Quiz(**fields)
        self.db.add(quiz)
        self.db.flush()
        return quiz

    def delete(self, quiz_id: int) -> None:
        """Тест с вопросами и вариантами. Варианты уносит каскад базы
        (`ondelete=CASCADE` у option), вопросы удаляем сами: каскада у их
        внешнего ключа нет. Попытки сюда не доходят: тест, который кто-то
        проходил, не удаляется вовсе.
        """
        self.db.execute(delete(Question).where(Question.quiz_id == quiz_id))
        self.db.execute(delete(Quiz).where(Quiz.id == quiz_id))

    def final_in_course(self, course_id: int, *, exclude_id: int | None = None) -> Quiz | None:
        """Итоговый тест курса, если он есть. Скрытый считается тоже: спрятанный
        итоговый остаётся итоговым, и второй рядом с ним — это уже два."""
        conds = [Module.course_id == course_id, Quiz.is_final.is_(True)]
        if exclude_id is not None:
            conds.append(Quiz.id != exclude_id)
        return self.db.scalar(
            select(Quiz).join(Module, Module.id == Quiz.module_id).where(*conds)
        )

    # -- вопросы и варианты ----------------------------------------------

    def questions(self, quiz_id: int) -> list[Question]:
        """Все вопросы теста, скрытые тоже: их админ и достаёт обратно."""
        return list(
            self.db.scalars(
                select(Question)
                .where(Question.quiz_id == quiz_id)
                .order_by(Question.order_index, Question.id)
            )
        )

    def question_by_id(self, question_id: int) -> Question | None:
        return self.db.get(Question, question_id)

    def options(self, question_ids: list[int]) -> dict[int, list[Option]]:
        """Варианты по вопросам — {question_id: [Option]}, своим порядком:
        перемешивается порядок вопросов, не ответов."""
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

    def next_question_order(self, quiz_id: int) -> int:
        """Место нового вопроса — за последним, включая скрытые: у скрытого
        порядок остаётся за ним, и новый вопрос не должен встать на его место."""
        last = self.db.scalar(
            select(func.max(Question.order_index)).where(Question.quiz_id == quiz_id)
        )
        return (last or 0) + 1

    def create_question(self, **fields) -> Question:
        question = Question(**fields)
        self.db.add(question)
        self.db.flush()
        return question

    def replace_options(self, question_id: int, options: list[dict]) -> None:
        """Варианты приходят полным списком и заменяют прежние: у вопроса их
        два-три, а отдельные ручки на вариант — это ещё три эндпоинта ради
        экономии килобайта.

        Замена безопасна только потому, что вопрос с попытками не правится
        вовсе: иначе она стёрла бы варианты, по которым посчитан чужой балл.
        """
        self.db.execute(delete(Option).where(Option.question_id == question_id))
        for index, option in enumerate(options):
            self.db.add(
                Option(
                    question_id=question_id,
                    text=option["text"],
                    is_correct=option["is_correct"],
                    order_index=index,
                )
            )
        self.db.flush()

    def delete_question(self, question_id: int) -> None:
        """Вопрос с вариантами: их уносит каскад базы (`ondelete=CASCADE`
        у option). Ответы сюда не доходят — вопрос с попытками не удаляется."""
        self.db.execute(delete(Question).where(Question.id == question_id))

    # -- чужие данные: что запирает вопрос от правки ---------------------

    def attempted_question_ids(self, quiz_id: int) -> set[int]:
        """Вопросы теста, попавшие в состав хотя бы одной попытки.

        Считается по question_order, а не по ответам: вопрос показан и тогда,
        когда человек его пропустил, — разбор всё равно покажет то, чего он
        не видел (BACKEND_NOTES, раздел 10).
        """
        return set(
            self.db.scalars(
                select(func.unnest(QuizAttempt.question_order)).where(
                    QuizAttempt.quiz_id == quiz_id
                )
            )
        )

    def question_attempts_count(self, question_id: int) -> int:
        """Сколько попыток показывали этот вопрос — число уходит в details
        ошибки: «был в 34 попытках» объясняет отказ, а голое «нельзя» — нет."""
        return (
            self.db.scalar(
                select(func.count())
                .select_from(QuizAttempt)
                .where(QuizAttempt.question_order.any(question_id))
            )
            or 0
        )


class TaskAdminRepo:
    """Задание глазами админа: скрытое задание и задание невидимого курса —
    обычные строки, их как раз и правят.

    Видимость здесь не проверяется вовсе, поэтому это не TaskRepo: тому
    задание черновика не существует, а редактор без него пуст.
    """

    def __init__(self, db: DbSession):
        self.db = db

    def with_course_and_module(self, task_id: int) -> tuple[Task, Module, Course] | None:
        """Задание вместе с модулем и курсом: и то и другое — хлебные крошки
        шапки редактора, и брать их тремя запросами незачем."""
        row = self.db.execute(
            select(Task, Module, Course)
            .join(Module, Module.id == Task.module_id)
            .join(Course, Course.id == Module.course_id)
            .where(Task.id == task_id)
        ).first()
        return (row[0], row[1], row[2]) if row is not None else None

    def create(self, **fields) -> Task:
        """Заготовка: название и требуемое время. Условие пустое — колонка
        обязательная, а текст появится в редакторе."""
        task = Task(**fields)
        self.db.add(task)
        self.db.flush()
        return task

    def delete(self, task_id: int) -> None:
        """Задание со всем, что в нём. Сдачи сюда не доходят: задание, на которое
        сдавали, не удаляется вовсе, а файл-шаблон остаётся в хранилище —
        удалять порт storage не умеет."""
        self.db.execute(delete(Task).where(Task.id == task_id))


class TeacherAdminRepo:
    """Учителя глазами админа: список с фильтрами и всё, что показывает карточка.

    Отдельно от UserRepo потому, что тот отвечает на вопросы входа — «кто
    записан на этот номер». Здесь вопрос другой: кого показать в таблице
    и что у человека в четырёх вкладках карточки.

    Каждый метод отвечает сразу на весь список — карточка не должна ходить
    в базу за прогрессом каждого курса и за баллом каждой попытки.
    """

    def __init__(self, db: DbSession):
        self.db = db

    # -- список ----------------------------------------------------------

    def page(
        self,
        *,
        region: str | None,
        school: str | None,
        course_id: int | None,
        q: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[User], int]:
        """Страница списка учителей, свежие сверху.

        Админов в списке нет: «учителя» здесь — те же люди, что в счётчике
        дашборда (`UserRepo.teachers_count`), и два разных числа на двух
        экранах читаются как ошибка. Карточка при этом открывается и у админа:
        свой номер он меняет на том же экране.
        """
        conds = [User.is_admin.is_(False)]
        if region:
            conds.append(User.region == region)
        if school:
            conds.append(User.school == school)
        if q is not None and q.strip():
            conds.append(name_or_phone_filter(q))
        if course_id is not None:
            # Учителя этой версии курса — по действующему доступу: человек
            # с отозванным доступом курс уже не проходит
            conds.append(
                User.id.in_(
                    select(Enrollment.user_id).where(
                        Enrollment.course_id == course_id,
                        Enrollment.revoked_at.is_(None),
                    )
                )
            )
        total = self.db.scalar(select(func.count()).select_from(User).where(*conds)) or 0
        users = list(
            self.db.scalars(
                select(User)
                .where(*conds)
                .order_by(User.created_at.desc(), User.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        return users, total

    def enrollment_counts(self, user_ids: list[int]) -> dict[int, tuple[int, int]]:
        """Действующие доступы и завершённые курсы — {user_id: (courses,
        completed)}. Одним запросом: столбцы стоят в таблице рядом."""
        if not user_ids:
            return {}
        rows = self.db.execute(
            select(
                Enrollment.user_id,
                func.count().filter(Enrollment.revoked_at.is_(None)),
                func.count().filter(Enrollment.completed_at.is_not(None)),
            )
            .where(Enrollment.user_id.in_(user_ids))
            .group_by(Enrollment.user_id)
        )
        return {user_id: (courses, completed) for user_id, courses, completed in rows}

    def certificate_counts(self, user_ids: list[int]) -> dict[int, int]:
        """Действующие сертификаты — {user_id: count}. Отозванный документ
        в счёт не идёт: у человека его на руках нет."""
        if not user_ids:
            return {}
        rows = self.db.execute(
            select(Certificate.user_id, func.count())
            .where(Certificate.user_id.in_(user_ids), Certificate.revoked_at.is_(None))
            .group_by(Certificate.user_id)
        )
        return dict(rows.all())

    # -- карточка --------------------------------------------------------

    def enrollments(self, user_id: int) -> list[tuple[Enrollment, Course]]:
        """Все доступы человека вместе с курсами, включая отозванные: прогресс
        и результаты при закрытии доступа не удаляются, и админ обязан их
        видеть (CONTRACT, сессия 7б)."""
        rows = self.db.execute(
            select(Enrollment, Course)
            .join(Course, Course.id == Enrollment.course_id)
            .where(Enrollment.user_id == user_id)
            .order_by(Enrollment.granted_at.desc(), Enrollment.id.desc())
        )
        return [(enrollment, course) for enrollment, course in rows]

    def admins_among(self, user_ids: list[int]) -> set[int]:
        """Кто из перечисленных — админ. Карточке нужен признак «доступ выдал
        админ», а не имя выдавшего: лишним ФИО в ответе никто не пользуется."""
        if not user_ids:
            return set()
        return set(
            self.db.scalars(
                select(User.id).where(User.id.in_(user_ids), User.is_admin.is_(True))
            )
        )

    def item_counts(self, course_ids: list[int]) -> dict[int, tuple[int, int]]:
        """Сколько в курсе видимых уроков и сколько элементов программы всего —
        {course_id: (lessons, items)}.

        Одним запросом на все курсы карточки: собирать программу каждого курса
        ради двух чисел — это запрос на строку вкладки «Курсы».
        """
        if not course_ids:
            return {}
        items = self._program_items(course_ids)
        rows = self.db.execute(
            select(items.c.course_id, items.c.kind, func.count()).group_by(
                items.c.course_id, items.c.kind
            )
        )
        return self._by_course(rows.all())

    def done_counts(self, user_id: int, course_ids: list[int]) -> dict[int, tuple[int, int]]:
        """Сколько видимых уроков и элементов программы человек прошёл в каждом
        курсе — {course_id: (lessons, items)}.

        Правила пройденности те же, что у `ProgressRepo.done_keys`: отмеченный
        урок, тест со сданной зачётной попыткой, зачтённое задание. Иначе
        карточка админа и кабинет учителя показали бы разный процент.
        """
        if not course_ids:
            return {}
        done = self._done_items(user_id, course_ids)
        rows = self.db.execute(
            select(done.c.course_id, done.c.kind, func.count()).group_by(
                done.c.course_id, done.c.kind
            )
        )
        return self._by_course(rows.all())

    @staticmethod
    def _by_course(rows: list[tuple[int, str, int]]) -> dict[int, tuple[int, int]]:
        """Строки (course_id, kind, count) в пару «уроков, элементов всего»."""
        counts: dict[int, tuple[int, int]] = {}
        for course_id, kind, count in rows:
            lessons, items = counts.get(course_id, (0, 0))
            counts[course_id] = (lessons + (count if kind == "lesson" else 0), items + count)
        return counts

    def _program_items(self, course_ids: list[int]):
        """Видимые элементы программы курсов — строки (course_id, kind, item_id).

        UNION ALL: id урока, теста и задания свои, одна и та же строка дважды
        не придёт, и снимать повторы незачем.
        """
        lessons = (
            select(
                Module.course_id.label("course_id"),
                literal("lesson").label("kind"),
                Lesson.id.label("item_id"),
            )
            .select_from(Lesson)
            .join(Module, Module.id == Lesson.module_id)
            .where(Module.course_id.in_(course_ids), Lesson.is_hidden.is_(False))
        )
        quizzes = (
            select(
                Module.course_id.label("course_id"),
                literal("quiz").label("kind"),
                Quiz.id.label("item_id"),
            )
            .select_from(Quiz)
            .join(Module, Module.id == Quiz.module_id)
            .where(Module.course_id.in_(course_ids), Quiz.is_hidden.is_(False))
        )
        tasks = (
            select(
                Module.course_id.label("course_id"),
                literal("task").label("kind"),
                Task.id.label("item_id"),
            )
            .select_from(Task)
            .join(Module, Module.id == Task.module_id)
            .where(Module.course_id.in_(course_ids), Task.is_hidden.is_(False))
        )
        return union_all(lessons, quizzes, tasks).subquery()

    def _done_items(self, user_id: int, course_ids: list[int]):
        """Пройденное человеком в этих курсах — строки (course_id, kind, item_id).

        UNION, а не UNION ALL: две зачтённые сдачи одного задания — это всё
        равно одно пройденное задание, как и в множестве `done_keys`.
        """
        lessons = (
            select(
                Module.course_id.label("course_id"),
                literal("lesson").label("kind"),
                LessonProgress.lesson_id.label("item_id"),
            )
            .select_from(LessonProgress)
            .join(Lesson, Lesson.id == LessonProgress.lesson_id)
            .join(Module, Module.id == Lesson.module_id)
            .where(
                Module.course_id.in_(course_ids),
                LessonProgress.user_id == user_id,
                Lesson.is_hidden.is_(False),
            )
        )
        quizzes = (
            select(
                Module.course_id.label("course_id"),
                literal("quiz").label("kind"),
                QuizAttempt.quiz_id.label("item_id"),
            )
            .select_from(QuizAttempt)
            .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .join(Module, Module.id == Quiz.module_id)
            .where(
                Module.course_id.in_(course_ids),
                QuizAttempt.user_id == user_id,
                Quiz.is_hidden.is_(False),
                # Незавершённая и незачётная попытки тест не проходят
                QuizAttempt.finished_at.is_not(None),
                QuizAttempt.is_counted.is_(True),
                QuizAttempt.passed.is_(True),
            )
        )
        tasks = (
            select(
                Module.course_id.label("course_id"),
                literal("task").label("kind"),
                Submission.task_id.label("item_id"),
            )
            .select_from(Submission)
            .join(Task, Task.id == Submission.task_id)
            .join(Module, Module.id == Task.module_id)
            .where(
                Module.course_id.in_(course_ids),
                Submission.user_id == user_id,
                Task.is_hidden.is_(False),
                Submission.status == SUBMISSION_ACCEPTED,
            )
        )
        return union(lessons, quizzes, tasks).subquery()

    def attempts(self, user_id: int) -> list[tuple[QuizAttempt, Quiz, Course]]:
        """Все попытки человека вместе с тестом и курсом, старые сверху —
        в этом порядке вкладка «Тесты» их и нумерует."""
        rows = self.db.execute(
            select(QuizAttempt, Quiz, Course)
            .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .join(Module, Module.id == Quiz.module_id)
            .join(Course, Course.id == Module.course_id)
            .where(QuizAttempt.user_id == user_id)
            .order_by(QuizAttempt.started_at, QuizAttempt.id)
        )
        return [(attempt, quiz, course) for attempt, quiz, course in rows]

    def submissions(self, user_id: int) -> list[tuple[Submission, Task, int]]:
        """Сдачи человека вместе с заданием и курсом задания, свежие сверху."""
        rows = self.db.execute(
            select(Submission, Task, Module.course_id)
            .join(Task, Task.id == Submission.task_id)
            .join(Module, Module.id == Task.module_id)
            .where(Submission.user_id == user_id)
            .order_by(Submission.created_at.desc(), Submission.id.desc())
        )
        return [(submission, task, course_id) for submission, task, course_id in rows]

    def certificates(self, user_id: int) -> list[Certificate]:
        """Документы человека, свежие сверху. Отозванные приходят вместе
        с остальными — с отметкой revoked_at."""
        return list(
            self.db.scalars(
                select(Certificate)
                .where(Certificate.user_id == user_id)
                .order_by(Certificate.issued_at.desc(), Certificate.id.desc())
            )
        )


# Настройки площадки (сессия 7б). Категории переехали сюда из констант
# в domain/dictionaries.py: бриф (5.25) обещает их правку в админке.


class CategoryRepo:
    """Категории курсов — справочник, который правит админ.

    Курс ссылается на категорию по `category_id`, и связь эта без FK:
    строку категории удаляют только когда на ней не висит ни одного курса,
    и проверяет это сценарий (`CategoryInUseError`).
    """

    def __init__(self, db: DbSession):
        self.db = db

    def all(self) -> list[Category]:
        # Порядок задаёт админ; id — вторым ключом, чтобы список не прыгал
        # при одинаковом order_index
        return list(
            self.db.scalars(select(Category).order_by(Category.order_index, Category.id))
        )

    def by_id(self, category_id: int) -> Category | None:
        return self.db.get(Category, category_id)

    def by_title(self, title: str) -> Category | None:
        return self.db.scalar(select(Category).where(Category.title == title))

    def create(self, title: str) -> Category:
        # Новая категория встаёт в конец списка: ручки на правку порядка
        # в контракте нет, и придумывать ей место посередине не из чего
        last = self.db.scalar(select(func.max(Category.order_index))) or 0
        category = Category(title=title, order_index=last + 1)
        self.db.add(category)
        self.db.flush()
        return category

    def delete(self, category: Category) -> None:
        self.db.delete(category)

    def courses_count(self, category_ids: list[int]) -> dict[int, int]:
        """Сколько курсов в каждой категории — все версии, включая черновики:
        по этому числу решают, можно ли категорию удалять."""
        if not category_ids:
            return {}
        rows = self.db.execute(
            select(Course.category_id, func.count())
            .where(Course.category_id.in_(category_ids))
            .group_by(Course.category_id)
        )
        return dict(rows.all())


class SettingRepo:
    """Настройки площадки — таблица ключ-значение.

    Колонок под настройки не заводим: их десяток, они разнородные и меняются
    вместе с экраном (CONTRACT, GET /admin/settings). Значение строки —
    словарь: `platform`, `contacts`, `branding`, `telegram`.
    """

    def __init__(self, db: DbSession):
        self.db = db

    def get(self, key: str) -> dict:
        """Значение по ключу; строки нет — пустой словарь. Отличать «не
        настраивали» от «пусто» некому: экран рисует поля всегда."""
        value = self.db.scalar(select(Setting.value).where(Setting.key == key))
        # Копия, а не сама строка из базы: правку JSONB на месте SQLAlchemy
        # не замечает, и такое изменение молча не сохранилось бы
        return dict(value) if value else {}

    def put(self, key: str, value: dict) -> None:
        """Записать значение целиком. Upsert, а не «выбрать и обновить»:
        первая же настройка приходит на несуществующую строку.

        Годится там, где строку всегда пишут целиком — иначе `merge`: этот
        затирает и те ключи, которых в `value` нет.
        """
        stmt = pg_insert(Setting).values(key=key, value=value)
        self.db.execute(
            stmt.on_conflict_do_update(
                index_elements=[Setting.key], set_={"value": stmt.excluded.value}
            )
        )

    def merge(self, key: str, value: dict) -> None:
        """Слить присланные ключи в значение строки, не трогая соседние.

        Сливает сам Postgres (`||` у jsonb), а не «прочитать в питоне,
        изменить, записать»: между чтением и записью влезает соседний
        запрос, и «Отвязать» отменялось переключателем уведомлений с той же
        вкладки — чат считался отвязанным, а `chat_id` оставался в базе.
        Здесь каждый писатель трогает только свои ключи, и затереть чужие
        нечем.
        """
        stmt = pg_insert(Setting).values(key=key, value=value)
        self.db.execute(
            stmt.on_conflict_do_update(
                index_elements=[Setting.key],
                set_={"value": Setting.value.op("||", return_type=JSONB)(stmt.excluded.value)},
            )
        )

    def clear_if(self, key: str, field: str, value: str) -> bool:
        """Обнулить значение строки, если её поле равно ожидаемому. True —
        обнулили именно мы.

        Сравнение делает база, а не питон: между «прочитали» и «записали»
        успевает пройти второй такой же запрос, и одноразовый код привязки
        срабатывал дважды — второй чат перезаписывал `chat_id` первого.
        Здесь второму достаётся ноль изменённых строк.
        """
        result = self.db.execute(
            update(Setting)
            .where(Setting.key == key, Setting.value[field].astext == value)
            .values(value={})
        )
        return result.rowcount == 1

    def bump(self, key: str, field: str) -> int:
        """Увеличить счётчик в значении строки и вернуть, сколько стало.
        Строки нет — считать нечего, вернётся 0.

        Считает Postgres, а не питон, по той же причине, что и `merge`:
        два одновременных промаха, посчитанных чтением и записью, дают
        в базе один.
        """
        counted = func.coalesce(Setting.value[field].as_integer(), 0) + 1
        result = self.db.execute(
            update(Setting)
            .where(Setting.key == key)
            .values(
                value=Setting.value.op("||", return_type=JSONB)(
                    func.jsonb_build_object(literal(field, Text), counted)
                )
            )
            .returning(Setting.value[field].as_integer())
        )
        return result.scalar() or 0

    def unset(self, key: str, fields: tuple[str, ...]) -> None:
        """Убрать перечисленные ключи из значения строки, не трогая соседние.

        Обратная сторона `merge` и по той же причине: «Отвязать» и
        переключатели уведомлений живут на одной вкладке экрана, а вычитание
        (`-` у jsonb) делает сам Postgres.

        Строки нет — убирать нечего: непривязанный бот отвязывается тем же
        204, что и привязанный.
        """
        value: ColumnElement = Setting.value
        for field in fields:
            value = value.op("-", return_type=JSONB)(literal(field, Text))
        self.db.execute(update(Setting).where(Setting.key == key).values(value=value))
