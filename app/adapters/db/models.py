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
from datetime import UTC, datetime

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
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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

    __table_args__ = (CheckConstraint("lang IN ('ru', 'kz')", name="lang"),)


class Course(Base):
    __tablename__ = "course"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Русская и казахская версии — две строки с общим group_id (раздел 3).
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    lang: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    short: Mapped[str] = mapped_column(Text, default="", server_default="")
    full: Mapped[str] = mapped_column(Text, default="", server_default="")
    cover: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[int] = mapped_column(Integer)  # справочник в domain/dictionaries.py
    hours: Mapped[int] = mapped_column(Integer)
    duration_text: Mapped[str | None] = mapped_column(Text)
    price: Mapped[int | None] = mapped_column(Integer)  # тенге; эквайринга нет, цена — число
    status: Mapped[str] = mapped_column(Text, default="draft", server_default="draft")
    starts_at: Mapped[datetime | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("group_id", "lang"),
        CheckConstraint("lang IN ('ru', 'kz')", name="lang"),
        CheckConstraint(
            "status IN ('draft', 'planned', 'open', 'closed', 'hidden')", name="status"
        ),
    )


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
    body: Mapped[dict | None] = mapped_column(JSONB)
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
        CheckConstraint(
            "(kind = 'video' AND video_url IS NOT NULL) OR (kind = 'text' AND body IS NOT NULL)",
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
    submit_format: Mapped[str] = mapped_column(Text, default="both", server_default="both")
    allowed_ext: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    max_size_mb: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    time_required_min: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    order_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

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

    __table_args__ = (UniqueConstraint("user_id", "course_id"),)


class LessonProgress(Base):
    __tablename__ = "lesson_progress"

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lesson.id"), primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(server_default=func.now())


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

    # Одна зачётная попытка — гарантия базы, а не проверка в коде: двойной
    # клик по «Начать тест» упирается в этот индекс.
    __table_args__ = (
        Index(
            "uq_quiz_attempt_counted",
            "user_id",
            "quiz_id",
            unique=True,
            postgresql_where=text("is_counted"),
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


class Certificate(Base):
    __tablename__ = "certificate"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    number: Mapped[str] = mapped_column(Text, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id"), index=True)
    # Снимок на момент выдачи: курс переименуют — сертификат остаётся прежним.
    holder_name: Mapped[str] = mapped_column(Text)
    course_title: Mapped[str] = mapped_column(Text)
    hours: Mapped[int] = mapped_column(Integer)
    issued_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column()


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


class Notification(Base):
    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    # type + params, а не готовый текст: язык уведомления — язык читателя (раздел 11).
    type: Mapped[str] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    read_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


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


class Review(Base):
    __tablename__ = "review"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (CheckConstraint("rating BETWEEN 1 AND 5", name="rating"),)


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
