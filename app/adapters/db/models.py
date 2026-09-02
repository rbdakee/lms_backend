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
    price: Mapped[int | None] = mapped_column(Integer)  # тенге; эквайринга нет, цена — число
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
    в редакторе курса это строку и создаёт. Цена бывает пустой ровно так же,
    как пустой бывает `course.price`.

    `course.price` пока остаётся на месте и продолжает читаться: чтение
    переключает сессия 2, она же убирает колонку. Сломанного каталога между
    сессиями быть не должно — он в бою.
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
    number: Mapped[str] = mapped_column(Text, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id"), index=True)
    # Снимок на момент выдачи: курс переименуют — сертификат остаётся прежним.
    holder_name: Mapped[str] = mapped_column(Text)
    course_title: Mapped[str] = mapped_column(Text)
    hours: Mapped[int] = mapped_column(Integer)
    # Язык тоже снимок: сертификат одноязычный, по языку версии курса (раздел 3).
    lang: Mapped[str] = mapped_column(Text, default="ru", server_default="ru")
    issued_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column()
    # Сертификат свой на каждой площадке; номер при этом уникален на всю
    # базу — нумерация общая, иначе проверка по номеру неоднозначна.
    platform: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("lang IN ('ru', 'kz')", name="lang"),
        CheckConstraint(PLATFORM_CHECK, name="platform"),
        # Двойной клик по «Получить сертификат» не должен выдавать два
        # документа: единственность держит база, а не проверка в сценарии.
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
