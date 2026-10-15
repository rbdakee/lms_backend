"""Все таблицы из BACKEND_NOTES, раздел 2 — сразу, включая те, что понадобятся
в поздних сессиях: дописывать модель по кусочку дороже.

Имена таблиц и колонок — как в эскизе: они пересекают границу фронт ↔ бэкенд
и пишутся через `_` без преобразований.

Сверх эскиза здесь две вещи, обе — механика входа:
- таблица auth_code: SMS-коды с лимитами не переживут рестарт в памяти,
  а отдельный кэш-сервис в системе запрещён;
- у session колонки token_hash (в базе не хранится сам токен — утечка дампа
  не должна отдавать чужие сессии) и ip.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.iin import IIN_PLACEHOLDER
from app.domain.lesson_ai import (
    BANK_KINDS,
    CHAT_STATUSES,
    DEFAULT_DIFFICULTY,
    DEFAULT_ENGAGEMENT,
    DIFFICULTY_LEVELS,
    ENGAGEMENT_LEVELS,
    JOB_STATUSES,
    KIT_STATUSES,
    MATERIAL_KINDS,
    MATERIAL_STATUSES,
    USAGE_SOURCES,
    VERSION_SOURCES,
)
from app.domain.platform import PLATFORMS


def now_utc() -> datetime:
    return datetime.now(UTC)


# Единые имена индексов и ограничений — чтобы миграции были воспроизводимы.
naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


# Условие CHECK у колонки platform — одно на все таблицы: список кодов
# площадок живёт в domain/platform.py, а база сверяет его сама.
PLATFORM_CHECK = "platform IN (" + ", ".join(f"'{code}'" for code in PLATFORMS) + ")"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=naming_convention)
    # В базе всё время — timestamptz в UTC (BACKEND_NOTES, раздел 7).
    type_annotation_map = {datetime: DateTime(timezone=True)}


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    phone: Mapped[str] = mapped_column(Text, unique=True)  # нормализованный +7XXXXXXXXXX
    first_name: Mapped[str] = mapped_column(Text, default="", server_default="")
    last_name: Mapped[str] = mapped_column(Text, default="", server_default="")
    middle_name: Mapped[str] = mapped_column(Text, default="", server_default="")
    # ИИН строкой, а не числом: ведущий ноль значащий (CERTIFICATES_BRIEF, 1).
    # Заглушка у всех, кто зарегистрировался до 04.09.2026, — это метка
    # «не заполнен», и онбординг мимо неё не пускает.
    iin: Mapped[str] = mapped_column(
        Text, default=IIN_PLACEHOLDER, server_default=IIN_PLACEHOLDER
    )
    photo_url: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, default="", server_default="")
    school: Mapped[str] = mapped_column(Text, default="", server_default="")
    position: Mapped[str] = mapped_column(Text, default="", server_default="")
    region: Mapped[str] = mapped_column(Text, default="", server_default="")
    city: Mapped[str] = mapped_column(Text, default="", server_default="")
    subject: Mapped[str] = mapped_column(Text, default="", server_default="")
    experience: Mapped[int | None] = mapped_column(Integer)  # стаж, лет
    lang: Mapped[str] = mapped_column(Text, default="ru", server_default="ru")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Согласие на обработку персональных данных — галочка на экране входа.
    consented_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("lang IN ('ru', 'kz')", name="lang"),
        # Один человек — один аккаунт: вторым ИИН второй аккаунт не завести
        # (CERTIFICATES_BRIEF, 1). Заглушка под уникальность не попадает —
        # она стоит у всех, кто регистрировался раньше, и один индекс
        # запретил бы им сохранять профиль.
        Index(
            "uq_user_iin",
            "iin",
            unique=True,
            postgresql_where=text(f"iin <> '{IIN_PLACEHOLDER}'"),
        ),
    )


class Course(Base):
    __tablename__ = "course"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Русская и казахская версии — две строки с общим group_id (раздел 3).
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    lang: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    short: Mapped[str] = mapped_column(Text, default="", server_default="")
    full: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Обложка — загруженный файл: ключ объекта в приватном хранилище и имя,
    # которое видит админ. Раздаёт байты GET /courses/{id}/cover.
    cover_key: Mapped[str | None] = mapped_column(Text)
    cover_name: Mapped[str | None] = mapped_column(Text)
    # Обложка старым способом — внешний адрес картинки, вписанный руками
    # до сессии 7в. Только на чтение: задать его через API больше нельзя,
    # но у заведённых раньше курсов он остаётся и продолжает работать.
    cover: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[int] = mapped_column(Integer)  # справочник в domain/dictionaries.py
    hours: Mapped[int] = mapped_column(Integer)
    duration_text: Mapped[str | None] = mapped_column(Text)
    # Цены здесь нет: она своя у каждой площадки и лежит в course_platform
    # (PLATFORMS_BRIEF, решение 3). Колонку убрала ревизия e6a41d0c7b58.
    status: Mapped[str] = mapped_column(Text, default="draft", server_default="draft")
    starts_at: Mapped[datetime | None] = mapped_column(Date)
    # Строгий порядок прохождения: элемент открывается после предыдущего.
    strict_order: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Условия сертификата — настройка курса (DESIGN_BRIEF, редактор курса).
    # Ни один флаг не включён — сертификат выдаётся сразу после выдачи доступа.
    # Проходной балл сюда не переехал: он остаётся у теста (quiz.pass_score),
    # иначе экран результата и чек-лист сертификата покажут разные числа.
    cert_require_lessons: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    cert_require_tasks: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    cert_require_module_quizzes: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    cert_require_final_quiz: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Столбец «Изменён» в списке курсов. Двигают его и правки самого курса,
    # и правки программы: для методиста курс — это дерево целиком.
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("group_id", "lang"),
        CheckConstraint("lang IN ('ru', 'kz')", name="lang"),
        CheckConstraint(
            "status IN ('draft', 'planned', 'open', 'closed', 'hidden')", name="status"
        ),
    )


class CoursePlatform(Base):
    """Публикация курса на площадке и его цена там.

    Строка есть — курс выложен на этой площадке по этой цене; галочка
    в редакторе курса эту строку и создаёт. Снятая галочка убирает курс
    из каталога площадки, но доступа не отбирает: тот, кто уже учится,
    доучивается и получает сертификат (PLATFORMS_BRIEF, решение 12).

    Пустая цена — «Цена по запросу», как и раньше у курса: эквайринга нет,
    цена здесь просто число.
    """

    __tablename__ = "course_platform"

    course_id: Mapped[int] = mapped_column(ForeignKey("course.id"), primary_key=True)
    platform: Mapped[str] = mapped_column(Text, primary_key=True)
    price: Mapped[int | None] = mapped_column(Integer)  # тенге; эквайринга нет, цена — число

    __table_args__ = (CheckConstraint(PLATFORM_CHECK, name="platform"),)


class Module(Base):
    __tablename__ = "module"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Lesson(Base):
    __tablename__ = "lesson"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("module.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)  # video | text
    # none_as_null: «содержимого нет» в базе должно быть одним значением.
    # По умолчанию None ложится JSON-ом null, и проверка kind_content отличает
    # его от SQL NULL — заготовка урока перестала бы сохраняться.
    body: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    video_url: Mapped[str | None] = mapped_column(Text)
    video_provider: Mapped[str] = mapped_column(Text, default="youtube", server_default="youtube")
    duration_label: Mapped[str | None] = mapped_column(Text)
    time_required_min: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    order_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Урок с чьим-то прогрессом не удаляется, а скрывается (раздел 10).
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    __table_args__ = (
        CheckConstraint("kind IN ('video', 'text')", name="kind"),
        # kind=video: video_url обязателен; kind=text: обязателен body.
        # Третья ветка — заготовка из окна «Добавить в программу»: содержимого
        # у неё нет вовсе, и появится оно в редакторе (CONTRACT, сессия 7а).
        CheckConstraint(
            "(kind = 'video' AND video_url IS NOT NULL) OR (kind = 'text' AND body IS NOT NULL)"
            " OR (video_url IS NULL AND body IS NULL)",
            name="kind_content",
        ),
    )


class LessonFile(Base):
    __tablename__ = "lesson_file"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("lesson.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime: Mapped[str] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Quiz(Base):
    __tablename__ = "quiz"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("module.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    is_final: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    pass_score: Mapped[int] = mapped_column(Integer)  # проходной, в процентах
    time_limit_min: Mapped[int | None] = mapped_column(Integer)
    shuffle: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    show_review: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # По умолчанию одна попытка (раздел 4).
    retakable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    time_required_min: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    order_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Тест с попытками не удаляется, а скрывается (раздел 10).
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Question(Base):
    __tablename__ = "question"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    quiz_id: Mapped[int] = mapped_column(ForeignKey("quiz.id"), index=True)
    type: Mapped[str] = mapped_column(Text)  # single | multi | bool
    text: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)
    points: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    order_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Вопрос с попытками не редактируется — создаётся новый, старый скрывается.
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    __table_args__ = (CheckConstraint("type IN ('single', 'multi', 'bool')", name="type"),)


class Option(Base):
    __tablename__ = "option"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    order_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Task(Base):
    __tablename__ = "task"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("module.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    statement: Mapped[dict] = mapped_column(JSONB)
    template_file: Mapped[str | None] = mapped_column(Text)
    # Имя, которое видит учитель. Из ключа его не вывести: ключи POST /files
    # случайные нарочно, имя от человека в них не попадает (files.py).
    template_file_name: Mapped[str | None] = mapped_column(Text)
    submit_format: Mapped[str] = mapped_column(Text, default="both", server_default="both")
    allowed_ext: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    max_size_mb: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    time_required_min: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    order_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Задание со сдачами не удаляется, а скрывается (раздел 10).
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    __table_args__ = (
        CheckConstraint("submit_format IN ('text', 'file', 'both')", name="submit_format"),
    )


class Lead(Base):
    __tablename__ = "lead"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id"), index=True)
    # Цена на момент заявки: цену курса потом поменяют, заявка помнит свою.
    price_snapshot: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, default="new", server_default="new")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    reminded_at: Mapped[datetime | None] = mapped_column()
    # Откуда пришла заявка. По курсу это не вычисляется: курс бывает общим,
    # а заявка — по одной на площадку (PLATFORMS_BRIEF, решение 15).
    platform: Mapped[str] = mapped_column(Text)

    __table_args__ = (CheckConstraint(PLATFORM_CHECK, name="platform"),)


class Enrollment(Base):
    __tablename__ = "enrollment"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id"), index=True)
    # Кто выдал доступ: админов несколько, actor обязателен.
    granted_by: Mapped[int] = mapped_column(ForeignKey("user.id"))
    granted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    paid_note: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()
    # Доступ выдаётся на площадку: тот же курс на второй — второй доступ,
    # со своим прогрессом и своим сертификатом (PLATFORMS_BRIEF, решение 2).
    platform: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("user_id", "course_id", "platform"),
        CheckConstraint(PLATFORM_CHECK, name="platform"),
    )


class LessonProgress(Base):
    __tablename__ = "lesson_progress"

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lesson.id"), primary_key=True)
    # Урок общего курса проходится на каждой площадке заново — платформа
    # в ключе, а не рядом с ним.
    platform: Mapped[str] = mapped_column(Text, primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (CheckConstraint(PLATFORM_CHECK, name="platform"),)


class QuizAttempt(Base):
    __tablename__ = "quiz_attempt"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    quiz_id: Mapped[int] = mapped_column(ForeignKey("quiz.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column()
    score: Mapped[int | None] = mapped_column(Integer)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    # Порядок вопросов фиксируется на старте, иначе разбор покажет не то (раздел 4).
    question_order: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), default=list, server_default="{}"
    )
    is_counted: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # «Разрешить пересдачу» снимает зачёт с попытки, а не удаляет её: админу
    # нужна история, и по правилу проекта у действия админа хранится actor_id.
    uncounted_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    uncounted_reason: Mapped[str | None] = mapped_column(Text)
    uncounted_at: Mapped[datetime | None] = mapped_column()
    # Попытка принадлежит площадке: у общего курса на каждой своя
    # единственная попытка (PLATFORMS_BRIEF, решение 2).
    platform: Mapped[str] = mapped_column(Text)

    # Одна зачётная попытка — гарантия базы, а не проверка в коде: двойной
    # клик по «Начать тест» упирается в этот индекс.
    __table_args__ = (
        CheckConstraint(PLATFORM_CHECK, name="platform"),
        Index(
            "uq_quiz_attempt_counted",
            "user_id",
            "quiz_id",
            "platform",
            unique=True,
            postgresql_where=text("is_counted"),
        ),
        # Активная попытка тоже одна и тоже гарантией базы: у пересдаваемого
        # теста попытка стартует незачётной, зачётный индекс молчит — без
        # этого гонка двойного старта рождала бы попытку-фантом.
        Index(
            "uq_quiz_attempt_active",
            "user_id",
            "quiz_id",
            "platform",
            unique=True,
            postgresql_where=text("finished_at IS NULL"),
        ),
    )


class Answer(Base):
    __tablename__ = "answer"

    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("quiz_attempt.id", ondelete="CASCADE"), primary_key=True
    )
    question_id: Mapped[int] = mapped_column(ForeignKey("question.id"), primary_key=True)
    option_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), default=list, server_default="{}"
    )


# Условие индекса собрано заранее: внутри класса имя text перекрыто колонкой.
_SUBMISSION_PENDING_ONLY = text("status = 'pending'")


class Submission(Base):
    __tablename__ = "submission"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("task.id"), index=True)
    text: Mapped[str | None] = mapped_column(Text)
    # [{"name": ..., "url": ..., "size_bytes": ..., "mime": ...}]
    files: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    comment: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    platform: Mapped[str] = mapped_column(Text)

    # Одна работа «на проверке» — гарантия базы: проверка в коде не закрывает
    # гонку двух одновременных отправок.
    __table_args__ = (
        CheckConstraint(PLATFORM_CHECK, name="platform"),
        Index(
            "uq_submission_pending",
            "user_id",
            "task_id",
            "platform",
            unique=True,
            postgresql_where=_SUBMISSION_PENDING_ONLY,
        ),
    )


class Certificate(Base):
    __tablename__ = "certificate"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Пусто, пока документ не выписан: до выдачи эта строка — заявка,
    # а номер придумывает выдача (CERTIFICATES_BRIEF, 3).
    number: Mapped[str | None] = mapped_column(Text, unique=True)
    # Номер академии: он не наш, вычислить его нельзя — админ берёт его
    # из своей нумерации и вводит руками. Пусто у документов до 04.09.2026
    # и у всех заявок.
    registration_number: Mapped[str] = mapped_column(Text, default="", server_default="")
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id"), index=True)
    # Снимок на момент выдачи: курс переименуют — сертификат остаётся прежним.
    holder_name: Mapped[str] = mapped_column(Text)
    course_title: Mapped[str] = mapped_column(Text)
    hours: Mapped[int] = mapped_column(Integer)
    # Язык тоже снимок: сертификат одноязычный, по языку версии курса (раздел 3).
    lang: Mapped[str] = mapped_column(Text, default="ru", server_default="ru")
    # Время заявки есть у всех трёх состояний строки, время выдачи — только
    # у выданной. server_default у issued_at нет нарочно: с ним заявка
    # получала бы дату выдачи прямо на вставке.
    requested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    issued_at: Mapped[datetime | None] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column()
    # Кто выдал и кто отозвал: у действия админа хранится actor_id
    # (backend/CLAUDE.md), как у enrollment.granted_by и review.reply_by.
    # Наружу не отдаются — экрана, который их спрашивает, нет.
    issued_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    revoked_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    # Сертификат свой на каждой площадке; номер при этом уникален на всю
    # базу — нумерация общая, иначе проверка по номеру неоднозначна.
    platform: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("lang IN ('ru', 'kz')", name="lang"),
        CheckConstraint(PLATFORM_CHECK, name="platform"),
        # Номер и дата выдачи появляются одним движением — выдачей. Строка
        # с номером, но без даты (или наоборот) означает недоведённую выдачу,
        # и не пустить её в базу дешевле, чем потом разбирать, что случилось.
        CheckConstraint("(number IS NULL) = (issued_at IS NULL)", name="issued"),
        # Двойной клик по «Запросить сертификат» не должен заводить две
        # строки: единственность держит база, а не проверка в сценарии.
        # Она же не даёт попросить документ дважды (CERTIFICATES_BRIEF, 5).
        Index(
            "uq_certificate_active",
            "user_id",
            "course_id",
            "platform",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class Session(Base):
    __tablename__ = "session"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Хэш токена: сам токен живёт только в куке у пользователя.
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    user_agent: Mapped[str] = mapped_column(Text, default="", server_default="")
    ip: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column()
    # «Предпросмотр как учитель» — флаг сессии, а не кука: гарантия «ничего
    # не записывается» серверная (раздел 12). Курс, а не весь кабинет: админ
    # пришёл смотреть конкретный курс.
    preview_course_id: Mapped[int | None] = mapped_column(ForeignKey("course.id"))


class Notification(Base):
    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    # type + params, а не готовый текст: язык уведомления — язык читателя (раздел 11).
    type: Mapped[str] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    read_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Площадка решает, на какой домен ведёт ссылка из колокольчика.
    platform: Mapped[str] = mapped_column(Text)

    # Колокольчик всегда читается одним запросом: свои, свежие сверху.
    __table_args__ = (
        CheckConstraint(PLATFORM_CHECK, name="platform"),
        Index("ix_notification_user_created", "user_id", "created_at"),
    )


class ThreadMessage(Base):
    __tablename__ = "thread_message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # parent_id пустой — корневое сообщение, заполнен — ответ в треде.
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("thread_message.id"))
    lesson_id: Mapped[int | None] = mapped_column(ForeignKey("lesson.id"), index=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("course.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Админ может удалить любое сообщение (DESIGN_BRIEF, 5.24); мягко,
    # по той же причине, что и отзыв.
    deleted_at: Mapped[datetime | None] = mapped_column()
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    # Откуда задан вопрос: урок общего курса открыт на обеих площадках,
    # а обсуждения у них свои.
    platform: Mapped[str] = mapped_column(Text)

    # Ответы треда собираются по parent_id — без индекса это seq scan на каждый
    # открытый урок.
    __table_args__ = (
        CheckConstraint(PLATFORM_CHECK, name="platform"),
        Index("ix_thread_message_parent", "parent_id"),
    )


class Review(Base):
    __tablename__ = "review"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column()
    # Ответ админа лежит у самого отзыва: он один, и вторых уровней у него
    # не бывает — в отличие от вопросов под уроком.
    reply_text: Mapped[str | None] = mapped_column(Text)
    reply_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    reply_at: Mapped[datetime | None] = mapped_column()
    # Удаление мягкое: у действий админа хранится actor_id (backend/CLAUDE.md).
    deleted_at: Mapped[datetime | None] = mapped_column()
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    # Отзыв виден в каталоге своей площадки, рейтинг считается по ней же
    # (PLATFORMS_BRIEF, решение 3).
    platform: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating"),
        CheckConstraint(PLATFORM_CHECK, name="platform"),
    )


class Category(Base):
    """Категории курсов. До сессии 7б жили константами в domain/dictionaries.py —
    редактора для них не было, а бриф (5.25) его обещает."""

    __tablename__ = "category"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text, unique=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")


class AuthCode(Base):
    __tablename__ = "auth_code"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    phone: Mapped[str] = mapped_column(Text, index=True)
    # В базе — только хэш: код одноразовый и короткоживущий, но дамп базы
    # не должен позволять войти за любого, кто сейчас логинится.
    code_hash: Mapped[str] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Исчерпал попытки — ввод для номера заблокирован до этого времени.
    blocked_until: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[datetime | None] = mapped_column()


# MCP: работа админа из внешнего ИИ (`MCP_BRIEF`). Пять таблиц. Таблицы чата
# ассистента (`assistant_*`) из кода убраны, но остаются в базе вместе
# с историей: миграций, которые их дропают, нет.
#
# Площадки здесь нет ни у одной таблицы: админка одна на обе площадки,
# площадка приходит аргументом инструмента.


class McpClient(Base):
    """Клиент OAuth, зарегистрированный динамически (RFC 7591).

    Публичный: секрета нет, защита кода — PKCE. Имя человек читает на экране
    согласия, поэтому оно показывается как есть и подтверждения не заменяет:
    регистрироваться может кто угодно.
    """

    __tablename__ = "mcp_client"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_id: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    redirect_uris: Mapped[list[str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class McpConnection(Base):
    """Подключение админа: строка и для OAuth, и для личного токена.

    В базе лежат только хеши токенов (утечка дампа не отдаёт доступ).
    Токен доступа личного токена не истекает (`access_expires_at` пусто) и
    живёт до отзыва; у OAuth он короткий, а жизнь подключения держит токен
    обновления. Отзыв — `revoked_at`: строка остаётся, журнал вызовов на неё
    ссылается.
    """

    __tablename__ = "mcp_connection"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    kind: Mapped[str] = mapped_column(Text)
    # Имя клиента для OAuth, имя токена, которое ввёл админ, — для личного
    name: Mapped[str] = mapped_column(Text)
    client_id: Mapped[str | None] = mapped_column(Text)
    # Допуск к персональным данным: подключение либо видит их все, либо нет
    pd_allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    access_hash: Mapped[str] = mapped_column(Text, unique=True)
    access_expires_at: Mapped[datetime | None] = mapped_column()
    refresh_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    refresh_expires_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (CheckConstraint("kind IN ('oauth', 'token')", name="kind"),)


class McpAuthCode(Base):
    """Одноразовый код авторизации: живёт минуты, обменивается один раз."""

    __tablename__ = "mcp_auth_code"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code_hash: Mapped[str] = mapped_column(Text, unique=True)
    client_id: Mapped[str] = mapped_column(Text)
    admin_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    redirect_uri: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(Text)
    pd_allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[datetime | None] = mapped_column()


class McpCallLog(Base):
    """Журнал вызовов: кто, какой инструмент, с какими аргументами, чем кончилось.

    Аргументы приходят уже через `mask_args` — персональные поля заменены
    пометкой. Результат не хранится вовсе. Человека-актёра отдельно не пишем:
    он виден через подключение.
    """

    __tablename__ = "mcp_call_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("mcp_connection.id"), index=True)
    tool: Mapped[str] = mapped_column(Text)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    ok: Mapped[bool] = mapped_column(Boolean)
    # Код ошибки реестра (`pd_not_allowed`, `not_found`…), без текста: тексты
    # могут нести то, что не нужно хранить
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# Справочник школьной учебной программы (TEACHER_AI_BRIEF, раздел 3). Пять
# таблиц: предмет, программа, разделы, цели обучения и подозрения автопроверки.
#
# Площадки здесь нет ни у одной таблицы, и это не забывчивость: справочник —
# не наш контент, а государственная программа. Приказ Министра просвещения
# один на всю страну, и обеим площадкам он один и тот же (раздел 8).


class CurriculumSubject(Base):
    """Предмет справочника.

    `title` — служебное русское название для админки: учителю предмет виден
    названием программы своей языковой редакции, а не этой строкой.
    """

    __tablename__ = "curriculum_subject"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Слаг латиницей ('math'): он уходит в пути и в промпт, русское имя — нет.
    code: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class CurriculumProgram(Base):
    """Программа: предмет, языковая редакция приказа и сам приказ.

    Русская и казахская редакции — две строки, а не перевод одной: в приказе
    это два официальных корпуса, и брать надо оба (раздел 3).

    Смена программы заводит новую строку, старая остаётся: по ней уже
    сгенерированы документы, и задним числом они не должны начать ссылаться
    на несуществующую цель.
    """

    __tablename__ = "curriculum_program"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("curriculum_subject.id"))
    lang: Mapped[str] = mapped_column(Text)
    grade_from: Mapped[int] = mapped_column(Integer)
    grade_to: Mapped[int] = mapped_column(Integer)
    # Номер приказа строкой: '399' — обозначение, а не количество.
    order_number: Mapped[str] = mapped_column(Text)
    order_date: Mapped[date] = mapped_column(Date)
    # Сырая строка из шапки документа рядом с разобранной датой: формат
    # официальных документов плавает, и по ней на сверке видно, что именно
    # разобрал импорт.
    order_date_text: Mapped[str] = mapped_column(Text)
    # Откуда взято: uuid документа в adilet и адрес страницы. Без них
    # повторный импорт не доказать, а вычитку не повторить.
    source_document_id: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    revision_date: Mapped[date | None] = mapped_column(Date)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="loaded", server_default="loaded")
    imported_at: Mapped[datetime] = mapped_column(server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        CheckConstraint("lang IN ('ru', 'kz')", name="lang"),
        # Загружен и опубликован — два состояния справочника: генерация видит
        # только опубликованный (раздел 3.1).
        CheckConstraint("status IN ('loaded', 'published')", name="status"),
        # Одна действующая программа на предмет и язык: повторный импорт того же
        # приказа не должен молча удвоить справочник.
        UniqueConstraint("subject_id", "lang", "order_number"),
    )


class CurriculumSection(Base):
    """Раздел и подраздел программы в одной таблице: уровня всего два,
    и вторая таблица удвоила бы и модель, и импорт. Раздел — строка
    с пустым `parent_id`, подраздел ссылается на свой раздел.
    """

    __tablename__ = "curriculum_section"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    program_id: Mapped[int] = mapped_column(
        ForeignKey("curriculum_program.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("curriculum_section.id", ondelete="CASCADE")
    )
    # '1' у раздела, '1.1' у подраздела — как в приказе.
    code: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    __table_args__ = (UniqueConstraint("program_id", "code"),)


class LearningObjective(Base):
    """Цель обучения: её выбирает учитель, по её коду завуч сверяет план.

    `code` — строка, а не число: `1.1.2.3` числом не бывает, а ведущие нули
    значащие (раздел 3).
    """

    __tablename__ = "learning_objective"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    program_id: Mapped[int] = mapped_column(
        ForeignKey("curriculum_program.id", ondelete="CASCADE"), index=True
    )
    # Ссылка на подраздел, а не на раздел: в приказе цели разложены
    # по подразделам, раздел достаётся по parent_id подраздела.
    section_id: Mapped[int] = mapped_column(ForeignKey("curriculum_section.id"), index=True)
    code: Mapped[str] = mapped_column(Text)
    grade: Mapped[int] = mapped_column(Integer)
    # Последний номер кода отдельной колонкой: разрывы в нумерации внутри
    # подраздела ищет автопроверка, и разбирать строку на каждую сверку дорого.
    index: Mapped[int] = mapped_column(Integer)
    # Пометка из документа вроде '**'. Она стоит у цели в приказе и должна
    # доехать до документа учителя, а не потеряться при разборе.
    marker: Mapped[str] = mapped_column(Text, default="", server_default="")
    text: Mapped[str] = mapped_column(Text)
    # Одна цель изучается в нескольких четвертях — отсюда массив, а не число.
    quarters: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), default=list, server_default="{}"
    )
    # В тексте вместо математического знака остался [img]: картинку из приказа
    # разобрать не удалось. Такая цель показывается человеку на сверке первой.
    has_image: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Текст правили руками на сверке; NULL — формулировка как в приказе.
    # Метка не для истории: повторный импорт сносит программу целиком, и по
    # числу помеченных строк сервер говорит, сколько ручной работы пропадёт.
    edited_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        CheckConstraint("grade BETWEEN 1 AND 12", name="grade"),
        UniqueConstraint("program_id", "code"),
    )


class CurriculumIssue(Base):
    """Подозрение, найденное автопроверкой импорта: машина ловит то, что видно
    без знания программы, а смысл вычитывает владелец лично (раздел 3.1).

    Именно подозрение, а не отказ: разрыв в нумерации бывает законным — цель
    могли исключить приказом об изменениях. Решение за человеком, поэтому
    строка гасится `resolved_at`, а не удаляется: иначе повторный импорт
    показал бы то же самое во второй раз.
    """

    __tablename__ = "curriculum_issue"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    program_id: Mapped[int] = mapped_column(
        ForeignKey("curriculum_program.id", ondelete="CASCADE"), index=True
    )
    # Машинный код проверки: экран сверки группирует список по нему.
    kind: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    # Код целью, а не ссылкой: подозрение бывает ровно о той цели, которой
    # в этой редакции нет — её потеряли при разборе, и ссылаться не на что.
    objective_code: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column()
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))

    __table_args__ = (
        CheckConstraint("severity IN ('error', 'warning')", name="severity"),
    )


# «Жоспар» — помощник учителя (TEACHER_AI_BRIEF, разделы 4, 7 и 8). Комплект
# к уроку, его материалы, блоки материала с историей правок и очередь
# генерации. К курсам отношения не имеет: общий с ними только `user`.
#
# Площадки здесь нет ни у одной таблицы: продукт один на обе площадки,
# и учитель в нём один и тот же человек (раздел 8).


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


class LessonKit(Base):
    """Комплект к одному уроку: заказ учителя и сводный статус его материалов.

    `grade` и `lang` берутся из целей обучения, а не из запроса: язык документов
    равен языку обучения, и цели из редакции приказа на этом языке знают его
    сами (раздел 1). Цели одной программы и одного класса проверяет сервер.
    Хранятся рядом с целями ради списка уроков учителя.
    """

    __tablename__ = "lesson_kit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    # Цели урока — в `lesson_kit_objective` по порядку учителя.
    topic: Mapped[str] = mapped_column(Text)
    grade: Mapped[int] = mapped_column(Integer)
    lang: Mapped[str] = mapped_column(Text)
    # Необязательное из заказа: сколько детей и на чём сделать акцент.
    class_size: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Два уровня заказа (раздел 4.3): насколько игровым идёт урок и какой
    # у класса уровень. Середина по умолчанию — вопрос не блокирующий.
    engagement_level: Mapped[str] = mapped_column(
        Text, default=DEFAULT_ENGAGEMENT, server_default=DEFAULT_ENGAGEMENT
    )
    difficulty_level: Mapped[str] = mapped_column(
        Text, default=DEFAULT_DIFFICULTY, server_default=DEFAULT_DIFFICULTY
    )
    # Сводка по материалам, а не отдельное состояние: пишется только
    # пересчётом после каждого закрытого задания (`kit_status` в домене).
    status: Mapped[str] = mapped_column(Text, default="generating", server_default="generating")
    # Комплект пересобран под цели учителя: генерация личная, с его темой и
    # планом, и в банк не ложится (даже без пожеланий)
    personal: Mapped[bool] = mapped_column(default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("lang IN ('ru', 'kz')", name="lang"),
        CheckConstraint(_in("status", KIT_STATUSES), name="status"),
        CheckConstraint("class_size BETWEEN 1 AND 60", name="class_size"),
        CheckConstraint(_in("engagement_level", ENGAGEMENT_LEVELS), name="engagement_level"),
        CheckConstraint(_in("difficulty_level", DIFFICULTY_LEVELS), name="difficulty_level"),
        # Свои уроки учитель видит списком, свежие сверху
        Index("ix_lesson_kit_user_id_id", "user_id", "id"),
    )


class LessonKitObjective(Base):
    """Цель урока: комплект ведёт одна или несколько целей, порядок задаёт учитель.

    Внешний ключ на цель без каскада нарочно: по ней уже собран документ,
    и повторный импорт программы не должен выдернуть её из-под комплекта (раздел 3).
    """

    __tablename__ = "lesson_kit_objective"

    kit_id: Mapped[int] = mapped_column(
        ForeignKey("lesson_kit.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    objective_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("learning_objective.id"), index=True
    )

    __table_args__ = (
        UniqueConstraint("kit_id", "objective_id"),
        CheckConstraint("position >= 1", name="position"),
    )


class LessonMaterial(Base):
    """Один материал комплекта: ҚМЖ, презентация, задания и так далее.

    `content` — документ целиком в текущем виде: ответ модели по схеме
    вида вместе с блоками, которые подставил сервер (цель обучения).
    Блоки (`lesson_material_block`) — его зеркало по полям верхнего уровня
    для правки по одному; правка обязана писать оба места одной транзакцией.
    """

    __tablename__ = "lesson_material"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kit_id: Mapped[int] = mapped_column(ForeignKey("lesson_kit.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="queued", server_default="queued")
    content: Mapped[dict | None] = mapped_column(JSONB)
    # Готовый файл — .docx или .pptx; появится с выгрузкой (сессии L4 и L5).
    file_path: Mapped[str | None] = mapped_column(Text)
    # Слаги механик из каталога игр, отданных модели как строительные блоки
    # (`app/lesson_ai/games/catalog`). Пишет сервер: он знает, что отдал, а модель
    # могла бы ответить неправду. Массив строк, а не внешний ключ — таблицы
    # механик нет, каталог — файлы. Нужен, чтобы потом посчитать, из каких
    # механик вырастают игры, которые нравятся методистам.
    mechanics: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    # Вариант банка, который достался учителю: выдан готовым или сгенерирован
    # этим материалом и положен в банк (сессия L7). По нему банк не выдаёт
    # учителю второй раз то же самое — у одной цели бывает несколько уроков.
    # Пусто — материал личный: по пожеланиям учителя или собран сервером.
    bank_id: Mapped[int | None] = mapped_column(
        ForeignKey("lesson_material_bank.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in("kind", MATERIAL_KINDS), name="kind"),
        CheckConstraint(_in("status", MATERIAL_STATUSES), name="status"),
        # Один материал каждого вида на комплект: перегенерация переписывает
        # его же, а не заводит второй
        UniqueConstraint("kit_id", "kind"),
    )


class LessonMaterialBlock(Base):
    """Блок материала — поле верхнего уровня его схемы: «середина урока»,
    «критерии оценивания». Единица правки: переписывается один блок,
    соседние остаются как были (раздел 4.1, правило 3)."""

    __tablename__ = "lesson_material_block"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("lesson_material.id", ondelete="CASCADE")
    )
    # Имя поля схемы: `stage_middle`, `learning_objectives`.
    kind: Mapped[str] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer)
    # Любой JSON: блок бывает строкой, списком и объектом.
    content: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (UniqueConstraint("material_id", "kind"),)


class LessonMaterialVersion(Base):
    """История блока: каждое его состояние целиком, включая первое.

    Первая версия пишется при генерации, поэтому откат «к тому, что
    сгенерировалось» — обычный откат, а не особый случай.
    """

    __tablename__ = "lesson_material_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Своего индекса по одному block_id нет: составной ниже покрывает и его
    block_id: Mapped[int] = mapped_column(
        ForeignKey("lesson_material_block.id", ondelete="CASCADE")
    )
    content: Mapped[Any] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(Text)
    # Кто попросил: владелец комплекта. Модель — не «кто», она в `source`.
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    # Просьба учителя своими словами — «сделай середину подвижнее»;
    # у первой генерации и ручной правки её нет.
    instruction: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in("source", VERSION_SOURCES), name="source"),
        Index("ix_lesson_material_version_block_id_id", "block_id", "id"),
    )


class LessonAiJob(Base):
    """Задание на генерацию одного материала — очередь без Celery и Redis,
    своей таблицей (раздел 7).

    Задание на каждый материал отдельно: один сбойный вид не рушит комплект.
    Все метки времени ставит база (`now()`), и сравниваются они с её же
    часами: часы контейнера Postgres и хоста в проекте уже расходились.
    """

    __tablename__ = "lesson_ai_job"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kit_id: Mapped[int] = mapped_column(ForeignKey("lesson_kit.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="queued", server_default="queued")
    # Сколько раз задание брали в работу — вместе с текущим разом.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Машинный код последнего отказа (`ai_failed`, `model_output_invalid`…).
    # Только код: в тексте исключения бывают тема урока и пожелания учителя.
    error: Mapped[str | None] = mapped_column(Text)
    # Не раньше этого момента: пауза между попытками.
    run_after: Mapped[datetime] = mapped_column(server_default=func.now())
    # Аренда взятого задания. Процесс умер посреди генерации — по истечении
    # аренды задание подберёт любой воркер как следующую попытку.
    lease_until: Mapped[datetime | None] = mapped_column()
    # Израсходованное всеми попытками, и неудачными тоже: кривой ответ модели
    # стоит тех же денег. Учёт по учителю — сессия L7 (`lesson_ai_usage`).
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Какой моделью сделано: настройка меняется, а материал остаётся.
    model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        CheckConstraint(_in("kind", MATERIAL_KINDS), name="kind"),
        CheckConstraint(_in("status", JOB_STATUSES), name="status"),
        Index("ix_lesson_ai_job_kit_id", "kit_id"),
        # Что брать следующим: только живые задания, и их немного —
        # закрытые копятся, но в индекс не попадают
        Index(
            "ix_lesson_ai_job_pending",
            "run_after",
            "id",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        # Одно живое задание на материал: двойной клик «перегенерировать»
        # не должен запускать две генерации одного и того же
        Index(
            "uq_lesson_ai_job_active",
            "kit_id",
            "kind",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )


class LessonAiChatGroup(Base):
    """Личная группа чатов сайдбара — например, по предмету. Удаление группы
    не удаляет чаты: они возвращаются в «без группы» (`ON DELETE SET NULL`)."""

    __tablename__ = "lesson_ai_chat_group"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    name: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 60", name="name"),
        Index("ix_lesson_ai_chat_group_user_id_position", "user_id", "position"),
    )


class LessonAiChat(Base):
    """Чат сбора заказа — вход в «Жоспар» (TEACHER_AI_BRIEF, раздел 1).

    Заказ лежит колонками, а не выводится из переписки на каждый запрос:
    чат может сказать что угодно, а генерацию запускает сервер по тому, что
    записано здесь. Пустая колонка значит «ещё не собрано».

    Цель — ссылкой на строку справочника: в комплект уходит она, а не пересказ
    модели. Предмета и класса здесь нет — их знает цель. После подтверждения
    чат закрыт и указывает на заведённый комплект.
    """

    __tablename__ = "lesson_ai_chat"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    status: Mapped[str] = mapped_column(Text, default="collecting", server_default="collecting")
    # Цели — в `lesson_ai_chat_objective` по порядку учителя.
    lang: Mapped[str | None] = mapped_column(Text)
    # Учитель подтвердил, что целей больше трёх и столько и нужно: помощник
    # переспрашивает один раз (решение владельца 15.10.2026), флаг — чтобы не повторять
    many_objectives_confirmed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    topic: Mapped[str | None] = mapped_column(Text)
    # Что учитель сказал в разговоре о двух уровнях, списке материалов
    # и классе. Пусто — не говорил; карточка заказа тогда предлагает середину
    # и список по умолчанию, а окончательные значения приходят с подтверждением.
    engagement_level: Mapped[str | None] = mapped_column(Text)
    difficulty_level: Mapped[str | None] = mapped_column(Text)
    kinds: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    class_size: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    kit_id: Mapped[int | None] = mapped_column(
        ForeignKey("lesson_kit.id", ondelete="SET NULL"), unique=True
    )
    # Сайдбар (решение владельца 15.10.2026): закрепление, группа, порядок
    # внутри своей секции и архив. `position` пуст у чата, которого учитель
    # ещё не двигал: такие идут выше расставленных, по свежести.
    pinned_at: Mapped[datetime | None] = mapped_column()
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("lesson_ai_chat_group.id", ondelete="SET NULL")
    )
    position: Mapped[int | None] = mapped_column(Integer)
    archived_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in("status", CHAT_STATUSES), name="status"),
        Index("ix_lesson_ai_chat_user_id_archived_at", "user_id", "archived_at"),
        Index("ix_lesson_ai_chat_group_id", "group_id"),
        CheckConstraint("lang IN ('ru', 'kz')", name="lang"),
        CheckConstraint(_in("engagement_level", ENGAGEMENT_LEVELS), name="engagement_level"),
        CheckConstraint(_in("difficulty_level", DIFFICULTY_LEVELS), name="difficulty_level"),
        CheckConstraint("class_size BETWEEN 1 AND 60", name="class_size"),
        Index("ix_lesson_ai_chat_user_id_id", "user_id", "id"),
    )


class LessonAiChatObjective(Base):
    """Цели заказа чата по порядку учителя. Без каскада на цель, как у комплекта:
    повторный импорт программы не должен выдернуть цель из-под заказа."""

    __tablename__ = "lesson_ai_chat_objective"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("lesson_ai_chat.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    objective_id: Mapped[int] = mapped_column(ForeignKey("learning_objective.id"))

    __table_args__ = (
        UniqueConstraint("chat_id", "objective_id"),
        CheckConstraint("position >= 1", name="position"),
    )


class LessonAiChatMessage(Base):
    """Лента чата сбора заказа — той же формы, что у ассистента: роль `tool` —
    карточка вызова, и экран показывает найденные цели из её `tool_result`,
    то есть строками справочника, а не словами модели."""

    __tablename__ = "lesson_ai_chat_message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("lesson_ai_chat.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, default="", server_default="")
    tool_name: Mapped[str | None] = mapped_column(Text)
    tool_args: Mapped[dict | None] = mapped_column(JSONB)
    tool_result: Mapped[dict | None] = mapped_column(JSONB)
    # Только у роли `event`: что произошло с комплектом (собирается, готов,
    # блок изменён из чата). Экран подсвечивает материал справа по этому
    # объекту, а `content` — запасная подпись словами.
    event: Mapped[dict | None] = mapped_column(JSONB)
    # Только у ответа помощника: быстрые ответы-кнопки под сообщением,
    # [{label, text}]; клик отправляет `text` как реплику учителя
    suggestions: Mapped[list[dict] | None] = mapped_column(JSONB)
    # Связь «просьба вызвать — результат»: хвост истории уходит модели как есть
    tool_call_id: Mapped[str | None] = mapped_column(Text)
    # Расход хода — на ответе помощника; учёт по учителю — сессия L7
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'tool', 'event')", name="role"),
        Index("ix_lesson_ai_chat_message_chat_id_id", "chat_id", "id"),
    )


class LessonAiAccess(Base):
    """Доступ к «Жоспару» — отдельный от доступа к курсам (TEACHER_AI_BRIEF,
    «Что решено», п. 4): купивший курс помощника не получает, пользующийся
    помощником курс не получает. Выдаёт админ руками, оплата — вне платформы.

    Строка одна на учителя: повторная выдача после снятия или продление
    переписывают её, а не заводят вторую (как у `enrollment`).
    """

    __tablename__ = "lesson_ai_access"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), unique=True)
    # Кто выдал: админов несколько, actor обязателен (backend/CLAUDE.md).
    granted_by: Mapped[int] = mapped_column(ForeignKey("user.id"))
    granted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # По этот день включительно, по Алматы. Пусто — бессрочно. Поле ждёт
    # решения владельца о сроке: скажет «бессрочно» — уходит миграцией.
    expires_on: Mapped[date | None] = mapped_column(Date)
    # Отметка «оплата получена», как у доступа к курсу: не NULL (хотя бы "") —
    # оплата была. Суммы нет: платёж проходит вне платформы.
    paid_note: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column()
    revoked_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))


class LessonAiUsage(Base):
    """Расход «Жоспара» по учителю: строка на каждое обращение к модели,
    за которое заплатили, — и за неудачное тоже, кривой ответ стоит тех же денег.

    Потолка расхода нет (решение владельца 19.09.2026): таблица — учёт, а не
    вентиль. Суточный лимит на учителя считается в комплектах и живёт в памяти
    процесса (раздел 7).
    """

    __tablename__ = "lesson_ai_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    source: Mapped[str] = mapped_column(Text)
    # К чему относится расход — для разбивки «во что обошёлся урок». Комплект
    # и чат удалять нечем, но ссылка не должна держать их строки навсегда.
    kit_id: Mapped[int | None] = mapped_column(ForeignKey("lesson_kit.id", ondelete="SET NULL"))
    chat_id: Mapped[int | None] = mapped_column(
        ForeignKey("lesson_ai_chat.id", ondelete="SET NULL")
    )
    # Вид материала — у генерации и перегенерации блока; у чата пусто.
    kind: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Долларов по прайсу модели на момент записи; пусто — модели нет в прайсе
    # (`MODEL_PRICES_USD_PER_M`).
    cost_usd: Mapped[Any | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in("source", USAGE_SOURCES), name="source"),
        Index("ix_lesson_ai_usage_user_id_created_at", "user_id", "created_at"),
        Index("ix_lesson_ai_usage_created_at", "created_at"),
    )


class LessonMaterialBank(Base):
    """Банк готовых материалов (TEACHER_AI_BRIEF, раздел 7): сгенерировали один
    раз — выдаём из базы, учитель правит под свой класс.

    Ключ — цель + вид + язык + оба уровня; у видов, которые строятся по плану
    урока, ещё и вариант плана (`plan_bank_id`): презентация, не совпавшая
    с ходом урока, хуже отсутствующей, поэтому банк выдаёт план и его соседей
    только одной цепочкой. Пусто — материал делали без плана.

    Материал здесь **обезличенный**: его писала модель, которая не видела
    ни темы учителя, ни числа детей, ни пожеланий, а ФИО и школа в содержимое
    не входят вовсе — шапку документа сервер собирает при скачивании. Ссылки
    на комплект, учителя или исходный материал у строки нет нарочно.

    Вариантов на ключ может быть несколько: у одной цели бывает несколько уроков,
    и учителю не выдаётся второй раз то, что у него уже есть.
    """

    __tablename__ = "lesson_material_bank"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Ключ набора целей: id через запятую по возрастанию («12,45»). Порядок
    # в ключ не входит — банк выдаёт на совпадение набора, а порядок целей
    # в документе при выдаче ставится по комплекту учителя. Сами цели с порядком
    # автора варианта — в `lesson_material_bank_objective`.
    objective_key: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(Text)
    engagement_level: Mapped[str] = mapped_column(Text)
    difficulty_level: Mapped[str] = mapped_column(Text)
    plan_bank_id: Mapped[int | None] = mapped_column(ForeignKey("lesson_material_bank.id"))
    # Документ целиком — блоки модели и блоки сервера, как `lesson_material.content`.
    content: Mapped[dict] = mapped_column(JSONB)
    # Порядок блоков: JSONB раскладывает ключи по-своему (короткие первыми),
    # а выданный материал обязан получить блоки в порядке формы
    block_order: Mapped[list[str]] = mapped_column(ARRAY(Text))
    mechanics: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in("kind", BANK_KINDS), name="kind"),
        CheckConstraint("lang IN ('ru', 'kz')", name="lang"),
        CheckConstraint(_in("engagement_level", ENGAGEMENT_LEVELS), name="engagement_level"),
        CheckConstraint(_in("difficulty_level", DIFFICULTY_LEVELS), name="difficulty_level"),
        Index(
            "ix_lesson_material_bank_key",
            "objective_key",
            "kind",
            "lang",
            "engagement_level",
            "difficulty_level",
            "id",
        ),
    )


class LessonMaterialBankObjective(Base):
    """Цели варианта банка. Без каскада на цель: повторный импорт программы
    не должен выдёргивать цель из-под готовых материалов."""

    __tablename__ = "lesson_material_bank_objective"

    bank_id: Mapped[int] = mapped_column(
        ForeignKey("lesson_material_bank.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    objective_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("learning_objective.id"), index=True
    )
