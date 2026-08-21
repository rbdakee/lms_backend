from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, ValidationInfo

from app.adapters.db.models import User
from app.domain.errors import FieldError
from app.domain.profile import onboarding_done


def _ranged_int(low: int, high: int | None, message: str) -> Any:
    """Целое поле формы с границами — и с русским текстом, когда в них
    не попали.

    Само ограничение остаётся у `Field`: оно уходит в OpenAPI-схему, и фронт
    читает границы оттуда. Проверка стоит перед ним и поднимает обычную
    `FieldError` — ту же, что кидают самописные проверки сценариев
    (`_checked_title` и подобные), с тем же именем поля и тем же видом ответа.
    Иначе под полем в админке появляется английское «Input should be greater
    than or equal to 1» — единственное английское место среди русских ошибок.

    Ошибка поднимается исключением, а не возвратом: pydantic пропускает
    наружу всё, кроме ValueError, и обработчик AppError отвечает 422 в общем
    формате. Значит, из нескольких неверных чисел одного запроса до экрана
    доедет первое — так же ведут себя и остальные проверки сценариев.
    """

    def check(value: Any, info: ValidationInfo) -> Any:
        # Не число — не наше дело: тип проверит сам pydantic
        if isinstance(value, int | float) and (
            value < low or (high is not None and value > high)
        ):
            raise FieldError(info.field_name, message)
        return value

    constraint = Field(ge=low) if high is None else Field(ge=low, le=high)
    return Annotated[int, constraint, BeforeValidator(check)]


# Числовые поля, которые человек вводит руками. Одинаковые по смыслу границы
# обязаны и звучать одинаково, поэтому текст живёт рядом с самой границей.
Hours = _ranged_int(1, 999, "Объём курса — от 1 до 999 часов")
Price = _ranged_int(0, None, "Цена не может быть отрицательной")
PassScore = _ranged_int(1, 100, "Проходной балл — от 1 до 100")
QuestionPoints = _ranged_int(1, 100, "Баллы за вопрос — от 1 до 100")
TimeLimitMin = _ranged_int(1, 600, "Таймер теста — от 1 до 600 минут")
TimeRequiredMin = _ranged_int(0, 600, "Требуемое время — от 0 до 600 минут")
MaxSizeMb = _ranged_int(1, None, "Размер файла — от 1 МБ")
Experience = _ranged_int(0, 70, "Стаж — от 0 до 70 лет")


class RequestCodeIn(BaseModel):
    phone: str = Field(max_length=20)
    consent: bool


class RequestCodeOut(BaseModel):
    retry_after_sec: int


class VerifyCodeIn(BaseModel):
    phone: str = Field(max_length=20)
    code: str = Field(max_length=10)


class PreviewOut(BaseModel):
    """Режим «Предпросмотр как учитель»: null — режим выключен. Клиентское
    приложение узнаёт о режиме только отсюда (CONTRACT, сессия 6)."""

    course_id: int


class UserOut(BaseModel):
    id: int
    phone: str
    first_name: str
    last_name: str
    middle_name: str
    photo_url: str | None
    email: str
    school: str
    position: str
    region: str
    city: str
    subject: str
    experience: int | None
    lang: str
    is_admin: bool
    onboarding_done: bool
    created_at: datetime
    preview: PreviewOut | None = None

    @classmethod
    def from_user(cls, user: User, preview_course_id: int | None = None) -> "UserOut":
        return cls(
            id=user.id,
            phone=user.phone,
            first_name=user.first_name,
            last_name=user.last_name,
            middle_name=user.middle_name,
            photo_url=user.photo_url,
            email=user.email,
            school=user.school,
            position=user.position,
            region=user.region,
            city=user.city,
            subject=user.subject,
            experience=user.experience,
            lang=user.lang,
            is_admin=user.is_admin,
            onboarding_done=onboarding_done(user.first_name, user.last_name),
            created_at=user.created_at,
            preview=(
                PreviewOut(course_id=preview_course_id)
                if preview_course_id is not None
                else None
            ),
        )


class UserPatch(BaseModel):
    """Все поля необязательные: PATCH меняет только присланное."""

    model_config = {"extra": "forbid"}

    first_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    middle_name: str | None = Field(None, max_length=100)
    email: str | None = Field(None, max_length=320)
    school: str | None = Field(None, max_length=300)
    position: str | None = Field(None, max_length=200)
    region: str | None = Field(None, max_length=100)
    city: str | None = Field(None, max_length=100)
    subject: str | None = Field(None, max_length=100)
    experience: Experience | None = None
    lang: Literal["ru", "kz"] | None = None


class SessionOut(BaseModel):
    id: str
    created_at: datetime
    last_seen_at: datetime
    user_agent: str
    is_current: bool


class SessionListOut(BaseModel):
    items: list[SessionOut]


class LogoutOthersOut(BaseModel):
    revoked_count: int


class CategoryOut(BaseModel):
    id: int
    title: str


class DictionariesOut(BaseModel):
    regions: list[str]
    subjects: list[str]
    categories: list[CategoryOut]


# -- каталог и страница курса ------------------------------------------


class CourseCardOut(BaseModel):
    """Карточка версии курса — и в каталоге, и шапка страницы курса."""

    id: int
    lang: str
    title: str
    category_id: int
    cover: str | None
    hours: int
    duration_text: str | None
    # null — «Цена по запросу»
    price: int | None
    status: str
    starts_at: date | None
    created_at: datetime
    lessons_count: int
    students_count: int


class CatalogGroupOut(BaseModel):
    group_id: int
    langs: list[str]
    # Рейтинг всей языковой группы; нет отзывов — null и 0
    rating: float | None
    reviews_count: int
    versions: list[CourseCardOut]


class CatalogOut(BaseModel):
    items: list[CatalogGroupOut]


class VersionChipOut(BaseModel):
    id: int
    lang: str
    title: str
    status: str


class ProgramLessonOut(BaseModel):
    kind: Literal["video", "text"]
    id: int
    title: str
    time_required_min: int
    duration_label: str | None


class ProgramQuizOut(BaseModel):
    kind: Literal["quiz"]
    id: int
    title: str
    time_required_min: int
    questions_count: int
    time_limit_min: int | None
    pass_score: int
    is_final: bool


class ProgramTaskOut(BaseModel):
    kind: Literal["task"]
    id: int
    title: str
    time_required_min: int
    submit_format: str


ProgramItemOut = Annotated[
    ProgramLessonOut | ProgramQuizOut | ProgramTaskOut, Field(discriminator="kind")
]


class ProgramModuleOut(BaseModel):
    id: int
    title: str
    items: list[ProgramItemOut]


class NextLessonOut(BaseModel):
    id: int
    title: str
    # Следующим может быть тест или задание: id у трёх видов свои, и без kind
    # кнопка «Продолжить» не знает, какой экран открывать
    kind: Literal["video", "text", "quiz", "task"]


class AccessNoneOut(BaseModel):
    state: Literal["none"]


class AccessRequestedOut(BaseModel):
    state: Literal["requested"]
    waiting_days: int


class AccessGrantedOut(BaseModel):
    state: Literal["granted"]
    done_count: int
    total_count: int
    progress_percent: int
    # Все уроки пройдены — null
    next_lesson: NextLessonOut | None


AccessOut = Annotated[
    AccessNoneOut | AccessRequestedOut | AccessGrantedOut, Field(discriminator="state")
]


class CoursePageOut(CourseCardOut):
    short: str
    full: str
    # Чипы переключения языка: только версии, видимые в каталоге
    versions: list[VersionChipOut]
    rating: float | None
    reviews_count: int
    # Строгий порядок прохождения; статусы элементов — в GET /courses/{id}/program
    strict_order: bool
    program: list[ProgramModuleOut]
    access: AccessOut


# -- программа со статусами (экран урока) ------------------------------

# Три состояния значка в сайдбаре
ProgramStatus = Literal["available", "locked", "done"]


class ProgramLessonStatusOut(ProgramLessonOut):
    status: ProgramStatus


class ProgramQuizStatusOut(ProgramQuizOut):
    status: ProgramStatus


class ProgramTaskStatusOut(ProgramTaskOut):
    status: ProgramStatus


ProgramStatusItemOut = Annotated[
    ProgramLessonStatusOut | ProgramQuizStatusOut | ProgramTaskStatusOut,
    Field(discriminator="kind"),
]


class ProgramStatusModuleOut(BaseModel):
    id: int
    title: str
    # Модуль без видимых элементов остаётся в ответе с пустым items
    items: list[ProgramStatusItemOut]


class ProgramOut(BaseModel):
    program: list[ProgramStatusModuleOut]


# -- урок --------------------------------------------------------------


class LessonFileOut(BaseModel):
    id: int
    name: str
    size_bytes: int
    mime: str


class LessonOut(BaseModel):
    id: int
    module_id: int
    title: str
    kind: Literal["video", "text"]
    # Содержимое как в базе, без обработки: форма фиксируется в сессии 7
    body: dict | None
    duration_label: str | None
    time_required_min: int
    is_completed: bool
    # Ссылки на файл здесь нет — за ней идут в GET /files/{id}
    files: list[LessonFileOut]


class LessonCompleteOut(BaseModel):
    is_completed: bool
    done_count: int
    total_count: int
    progress_percent: int
    next_lesson: NextLessonOut | None


class PlaybackOut(BaseModel):
    provider: str
    url: str
    # У youtube ссылка не протухает; со своим хостингом здесь будет срок
    expires_at: datetime | None


# -- тесты и попытки ---------------------------------------------------


QuestionType = Literal["single", "multi", "bool"]


class QuizOptionOut(BaseModel):
    """Вариант внутри идущей попытки: is_correct здесь нет и быть не может."""

    id: int
    text: str


class QuizQuestionOut(BaseModel):
    id: int
    type: QuestionType
    text: str
    points: int
    # Варианты идут своим порядком: перемешивается порядок вопросов, не ответов
    options: list[QuizOptionOut]


class QuizAnswerOut(BaseModel):
    question_id: int
    # Пустой список — ответ снят
    option_ids: list[int]


class QuizAttemptOut(BaseModel):
    id: int
    quiz_id: int
    started_at: datetime
    # null — у теста нет лимита времени, попытка не истекает
    remaining_sec: int | None
    questions: list[QuizQuestionOut]
    answers: list[QuizAnswerOut]


class QuizResultOut(BaseModel):
    """Результат попытки — ответ finish и он же `result` завершённого теста."""

    id: int
    score: int
    max_score: int
    score_percent: int
    pass_score: int
    passed: bool
    is_counted: bool
    timed_out: bool
    minutes_spent: int
    review_available: bool


class QuizAttemptHistoryOut(BaseModel):
    id: int
    started_at: datetime
    finished_at: datetime
    minutes_spent: int
    score: int
    max_score: int
    score_percent: int
    passed: bool
    timed_out: bool
    is_counted: bool


class QuizStateNotStartedOut(BaseModel):
    status: Literal["not_started"]
    # false — только когда по курсу уже выдан сертификат
    can_start: bool


class QuizStateInProgressOut(BaseModel):
    status: Literal["in_progress"]
    attempt: QuizAttemptOut


class QuizStateFinishedOut(BaseModel):
    status: Literal["finished"]
    result: QuizResultOut
    can_retake: bool
    review_available: bool


QuizStateOut = Annotated[
    QuizStateNotStartedOut | QuizStateInProgressOut | QuizStateFinishedOut,
    Field(discriminator="status"),
]


class QuizOut(BaseModel):
    id: int
    module_id: int
    title: str
    is_final: bool
    pass_score: int
    time_limit_min: int | None
    retakable: bool
    show_review: bool
    time_required_min: int
    # Числа видимых вопросов; сами вопросы уходят только внутри попытки
    questions_count: int
    max_score: int
    state: QuizStateOut
    # Завершённые попытки, старые сверху; незавершённая — в state
    attempts: list[QuizAttemptHistoryOut]


class AnswerIn(BaseModel):
    model_config = {"extra": "forbid"}

    question_id: int
    option_ids: list[int]


class QuizReviewOptionOut(BaseModel):
    id: int
    text: str
    # is_correct — как надо было, is_chosen — как ответил человек
    is_correct: bool
    is_chosen: bool


class QuizReviewQuestionOut(BaseModel):
    id: int
    type: QuestionType
    text: str
    points: int
    earned_points: int
    # null — пояснения у вопроса нет, блок не рисуется
    explanation: str | None
    options: list[QuizReviewOptionOut]


class QuizReviewOut(BaseModel):
    result: QuizResultOut
    questions: list[QuizReviewQuestionOut]


# -- задания и сдачи ---------------------------------------------------


class TemplateFileOut(BaseModel):
    """Файл-шаблон задания. Ссылки здесь нет — за ней идут отдельно,
    в GET /tasks/{id}/template_file."""

    name: str
    size_bytes: int
    mime: str


class SubmissionFileOut(BaseModel):
    name: str
    size_bytes: int
    mime: str
    # Постоянная ссылка, а не ключ хранилища: байты отдаются с проверкой сессии
    url: str


class SubmissionOut(BaseModel):
    """Элемент истории сдач — и ответ на отправку работы. reviewed_by здесь
    нет: имя проверяющего учителю не отдаётся."""

    id: int
    status: Literal["pending", "accepted", "rework"]
    created_at: datetime
    text: str | None
    files: list[SubmissionFileOut]
    comment: str | None
    reviewed_at: datetime | None


class TaskOut(BaseModel):
    id: int
    module_id: int
    title: str
    # Условие как в базе, без обработки: форма фиксируется в сессии 7
    statement: dict
    template_file: TemplateFileOut | None
    submit_format: Literal["text", "file", "both"]
    # Пустой список — принимается любое расширение
    allowed_ext: list[str]
    max_size_mb: int
    time_required_min: int
    # Статус последней сдачи; none — ещё не сдавал
    status: Literal["none", "pending", "accepted", "rework"]
    can_submit: bool
    # История сдач, свежие сверху
    submissions: list[SubmissionOut]


class SubmissionFileIn(BaseModel):
    model_config = {"extra": "forbid"}

    # key из ответа POST /files; наружу он больше не возвращается
    key: str = Field(max_length=500)
    name: str = Field(max_length=300)


class SubmissionIn(BaseModel):
    model_config = {"extra": "forbid"}

    text: str | None = Field(None, max_length=20000)
    files: list[SubmissionFileIn] = Field(default_factory=list)


# -- файлы -------------------------------------------------------------


class UploadedFileOut(BaseModel):
    # key, а не id: строки в базе за загруженным файлом ещё нет — она появится
    # там, куда этот key передадут дальше
    key: str
    name: str
    size_bytes: int
    mime: str


class FileLinkOut(BaseModel):
    url: str
    expires_at: datetime


# -- отзывы ------------------------------------------------------------


class ReviewIn(BaseModel):
    model_config = {"extra": "forbid"}

    rating: int = Field(ge=1, le=5)
    text: str = Field("", max_length=2000)


class ReviewReplyOut(BaseModel):
    """Ответ администратора на отзыв. Имени отвечающего здесь нет —
    на экране он подписан просто «Администратор»."""

    text: str
    created_at: datetime


class ReviewOut(BaseModel):
    id: int
    author_name: str
    school: str
    city: str
    rating: int
    text: str
    created_at: datetime
    # null — админ на отзыв не отвечал
    reply: ReviewReplyOut | None = None


class ReviewsPageOut(BaseModel):
    items: list[ReviewOut]
    total: int
    page: int
    per_page: int
    # Средняя и разбивка по звёздам — по последнему отзыву каждого автора
    rating: float | None
    breakdown: dict[str, int]


# -- заявки и мои курсы ------------------------------------------------


class LeadOut(BaseModel):
    id: int
    status: str
    created_at: datetime
    waiting_days: int


class MyCourseOut(BaseModel):
    id: int
    lang: str
    title: str
    category_id: int
    cover: str | None
    hours: int
    price: int | None
    status: str
    done_count: int
    total_count: int
    progress_percent: int
    next_lesson: NextLessonOut | None
    completed_at: datetime | None


class MyLeadCourseOut(BaseModel):
    id: int
    title: str
    cover: str | None
    # Снимок цены на момент заявки, а не текущая цена курса
    price: int | None


class MyLeadOut(BaseModel):
    id: int
    status: str
    created_at: datetime
    waiting_days: int
    course: MyLeadCourseOut


class MyCoursesOut(BaseModel):
    items: list[MyCourseOut]
    leads: list[MyLeadOut]


# -- админка: заявки и выдача доступа ----------------------------------


class LeadTeacherOut(BaseModel):
    id: int
    last_name: str
    first_name: str
    middle_name: str
    phone: str
    school: str
    region: str
    subject: str


class AdminLeadCourseOut(BaseModel):
    id: int
    lang: str
    title: str
    price: int | None


class AdminLeadOut(BaseModel):
    id: int
    status: str
    price_snapshot: int | None
    note: str | None
    created_at: datetime
    # У granted и declined всегда 0 — заявка больше не ждёт
    waiting_days: int
    reminded_at: datetime | None
    teacher: LeadTeacherOut
    course: AdminLeadCourseOut


class AdminLeadsPageOut(BaseModel):
    items: list[AdminLeadOut]
    total: int
    page: int
    per_page: int


class LeadPatchIn(BaseModel):
    model_config = {"extra": "forbid"}

    # granted принимается схемой, но отклоняется сценарием с понятным текстом:
    # выдача доступа идёт через POST /admin/enrollments
    status: Literal["new", "contacted", "paid", "granted", "declined"] | None = None
    note: str | None = Field(None, max_length=2000)


class PreviewEnterIn(BaseModel):
    model_config = {"extra": "forbid"}

    course_id: int


class EnrollmentIn(BaseModel):
    model_config = {"extra": "forbid"}

    user_id: int
    course_id: int
    paid: bool
    note: str | None = Field(None, max_length=2000)


class EnrollmentOut(BaseModel):
    id: int
    user_id: int
    course_id: int
    granted_at: datetime
    paid: bool
    # Комментарий к оплате (paid_note); при paid=false — null, комментарий ушёл в заявку
    note: str | None


# -- админка: очередь проверки работ -----------------------------------


class SubmissionTeacherOut(BaseModel):
    """Учитель в очереди: телефона и школы здесь нет — они на карточке
    учителя, а очередь их не показывает."""

    id: int
    last_name: str
    first_name: str
    middle_name: str
    photo_url: str | None


class SubmissionCourseOut(BaseModel):
    id: int
    lang: str
    title: str


class SubmissionTaskOut(BaseModel):
    id: int
    title: str


class AdminSubmissionOut(BaseModel):
    id: int
    status: Literal["pending", "accepted", "rework"]
    # 1 — первая работа, больше — доработка после rework
    attempt_number: int
    created_at: datetime
    # У проверенных всегда 0 — работа больше не ждёт
    waiting_days: int
    teacher: SubmissionTeacherOut
    task: SubmissionTaskOut
    course: SubmissionCourseOut


class AdminSubmissionsPageOut(BaseModel):
    items: list[AdminSubmissionOut]
    total: int
    page: int
    per_page: int


class AdminSubmissionTaskOut(SubmissionTaskOut):
    """Задание в карточке проверки: слева условие, справа работа."""

    statement: dict
    template_file: TemplateFileOut | None
    submit_format: Literal["text", "file", "both"]
    allowed_ext: list[str]
    max_size_mb: int


class AdminSubmissionCardOut(AdminSubmissionOut):
    task: AdminSubmissionTaskOut
    text: str | None
    files: list[SubmissionFileOut]
    comment: str | None
    reviewed_at: datetime | None
    # Прежние сдачи той же пары, свежие сверху; самой работы здесь нет
    history: list[SubmissionOut]


class SubmissionReviewIn(BaseModel):
    model_config = {"extra": "forbid"}

    verdict: Literal["accepted", "rework"]
    # При rework обязателен — проверяет сценарий, текст отказа один на форму
    comment: str | None = Field(None, max_length=4000)


# -- сертификаты -------------------------------------------------------


ConditionStatus = Literal["not_started", "in_progress", "done"]


class ConditionOut(BaseModel):
    """Строка чек-листа. `done_count` — null, пока доступа к курсу нет:
    до выдачи это просто список требований, живой прогресс появляется после."""

    code: Literal["lessons", "tasks", "module_quizzes"]
    label: str
    status: ConditionStatus
    done_count: int | None
    # Считается по видимым элементам программы: скрытый урок в условие не входит
    total_count: int


class FinalQuizConditionOut(ConditionOut):
    """У итогового теста в строке есть проходной балл; у тестов модулей его нет —
    он свой у каждого теста (CONTRACT, сессия 6)."""

    code: Literal["final_quiz"]
    # null — условие включено, а самого итогового теста в программе ещё нет
    pass_score: int | None


CompletionConditionOut = Annotated[
    ConditionOut | FinalQuizConditionOut, Field(discriminator="code")
]


class BlockerOut(BaseModel):
    """Почему кнопка выдачи неактивна, хотя условия выполнены."""

    code: Literal["attempt_in_progress"]
    message: str


class CertificateBriefOut(BaseModel):
    """Уже выданный сертификат в чек-листе: ссылка на документ, не сам документ."""

    id: int
    number: str
    issued_at: datetime


class CompletionOut(BaseModel):
    conditions: list[CompletionConditionOut]
    can_issue: bool
    # null — помех нет
    blocker: BlockerOut | None
    certificate: CertificateBriefOut | None


class MyCertificateOut(BaseModel):
    """Элемент списка `/certificates` — он же всё, что печатает `/certificates/{id}`:
    отдельного эндпоинта за одним сертификатом нет."""

    id: int
    number: str
    course_id: int
    course_title: str
    holder_name: str
    hours: int
    lang: str
    issued_at: datetime


class MyCertificatesOut(BaseModel):
    items: list[MyCertificateOut]


class CertificateOut(MyCertificateOut):
    # Отозванный в кабинет не попадает, поэтому revoked_at есть только в ответе выдачи
    revoked_at: datetime | None


class VerifyOut(BaseModel):
    """Публичная проверка: только то, что напечатано на бумаге — ни user_id,
    ни course_id, ни id сертификата, ни ссылок в кабинет."""

    status: Literal["valid", "revoked"]
    number: str
    holder_name: str
    course_title: str
    hours: int
    issued_at: datetime
    revoked_at: datetime | None


# -- уведомления -------------------------------------------------------


class NotificationOut(BaseModel):
    id: int
    # retake_allowed добавлен в сессии 7б — вместе с кнопкой «Разрешить пересдачу»
    type: Literal[
        "access_granted",
        "submission_reviewed",
        "answer_posted",
        "certificate_issued",
        "retake_allowed",
    ]
    # Собран в момент чтения по языку учителя — в базе текста нет
    text: str
    # Отдаются рядом с текстом: из них фронт строит адрес перехода
    params: dict
    read_at: datetime | None
    created_at: datetime


class NotificationsPageOut(BaseModel):
    items: list[NotificationOut]
    # Сверх страницы: счётчик нужен шапке на каждом экране
    unread_count: int
    total: int
    page: int
    per_page: int


class NotificationsReadIn(BaseModel):
    """Ровно одно из двух полей: клик по уведомлению или «отметить все».
    Что именно прислано — проверяет сценарий, ему же принадлежит текст отказа."""

    model_config = {"extra": "forbid"}

    # Потолок на список: экран отмечает то, что показал, а страница у него
    # не длиннее сотни — миллион id в одном IN (...) сюда попасть не должен
    ids: Annotated[list[int], Field(max_length=100)] | None = None
    all: bool | None = None


# -- вопросы под уроком ------------------------------------------------


class ThreadReplyOut(BaseModel):
    id: int
    text: str
    # «Фамилия Имя» собирает сервер; инициалы для аватара — забота фронта
    author_name: str
    # Синяя подпись и бейдж «Администратор»; берётся у автора при чтении
    author_is_admin: bool
    created_at: datetime


class ThreadQuestionOut(ThreadReplyOut):
    # Пустой список и есть признак «ждёт ответа»: отдельного статуса нет
    replies: list[ThreadReplyOut]


class QuestionsPageOut(BaseModel):
    items: list[ThreadQuestionOut]
    total: int
    page: int
    per_page: int


class ThreadMessageIn(BaseModel):
    model_config = {"extra": "forbid"}

    text: str = Field(max_length=2000)
    # null — новый вопрос; id корня этого же урока — ответ в треде
    parent_id: int | None = None


# -- админка: очередь вопросов -----------------------------------------


class QuestionTeacherOut(BaseModel):
    id: int
    last_name: str
    first_name: str
    middle_name: str


class QuestionCourseOut(BaseModel):
    id: int
    title: str


class QuestionLessonOut(BaseModel):
    id: int
    # Сквозной номер урока среди видимых уроков курса: подпись «Урок 6 · …»
    number: int
    title: str


class AdminQuestionOut(BaseModel):
    id: int
    text: str
    created_at: datetime
    teacher: QuestionTeacherOut
    course: QuestionCourseOut
    lesson: QuestionLessonOut
    replies: list[ThreadReplyOut]


class AdminQuestionsPageOut(BaseModel):
    items: list[AdminQuestionOut]
    total: int
    page: int
    per_page: int


# -- админка: дашборд и отчёт по курсу ---------------------------------


class OverviewTeacherOut(BaseModel):
    """Учитель в списках дашборда: ни телефона, ни школы — за ними карточка
    заявки. ФИО тремя полями, собирает его фронт."""

    id: int
    last_name: str
    first_name: str
    middle_name: str


class OverviewCourseOut(BaseModel):
    id: int
    title: str


class OverviewLeadOut(BaseModel):
    id: int
    created_at: datetime
    waiting_days: int
    price_snapshot: int | None
    teacher: OverviewTeacherOut
    course: OverviewCourseOut


class OverviewSubmissionOut(BaseModel):
    id: int
    created_at: datetime
    waiting_days: int
    teacher: OverviewTeacherOut
    course: OverviewCourseOut
    task: SubmissionTaskOut


class OverviewQuestionOut(BaseModel):
    id: int
    text: str
    created_at: datetime
    teacher: OverviewTeacherOut
    course: OverviewCourseOut
    lesson: QuestionLessonOut


class OverviewTotalsOut(BaseModel):
    """Строка справочных чисел внизу экрана. Учителя — все, у кого не стоит
    is_admin; курсы — версии, видимые в каталоге; сертификаты — действующие."""

    teachers: int
    courses_published: int
    certificates: int


class AdminOverviewOut(BaseModel):
    # Полные счётчики плиток: их же показывают бейджи меню
    leads_count: int
    submissions_count: int
    questions_count: int
    # Списки — по 5 элементов, свежие сверху
    leads: list[OverviewLeadOut]
    submissions: list[OverviewSubmissionOut]
    questions: list[OverviewQuestionOut]
    totals: OverviewTotalsOut


class ReportCourseOut(BaseModel):
    id: int
    lang: str
    title: str


class ReportSummaryOut(BaseModel):
    """Средние — округлённые до целого; null, если считать не по кому."""

    granted: int
    started: int
    completed: int
    avg_progress_percent: int | None
    avg_final_score: int | None
    certificates: int
    avg_days_to_complete: int | None


class ReportFunnelItemOut(BaseModel):
    # Все видимые элементы программы в сквозном порядке; какие показать —
    # решает экран
    kind: Literal["video", "text", "quiz", "task"]
    id: int
    number: int
    title: str
    # Сколько учителей этот элемент прошли
    reached: int


class ReportQuizScoreOut(BaseModel):
    quiz_id: int
    title: str
    # null — не сдавал
    score: int | None


class ReportFinalQuizOut(BaseModel):
    state: Literal["not_started", "in_progress", "passed", "failed"]
    # Процент зачётной попытки
    score: int | None


class ReportParticipantOut(BaseModel):
    user_id: int
    last_name: str
    first_name: str
    middle_name: str
    school: str
    region: str
    progress_percent: int
    # Баллы тестов модулей в порядке программы
    module_quizzes: list[ReportQuizScoreOut]
    final_quiz: ReportFinalQuizOut
    # issued — выдан, ready — условия выполнены, in_progress — ещё нет
    certificate: Literal["issued", "ready", "in_progress"]


class ReportParticipantsPageOut(BaseModel):
    items: list[ReportParticipantOut]
    total: int
    page: int
    per_page: int


class AdminReportOut(BaseModel):
    course: ReportCourseOut
    # Момент запроса: отчёт считается на лету и нигде не хранится
    generated_at: datetime
    summary: ReportSummaryOut
    funnel: list[ReportFunnelItemOut]
    # Участников на экране восемь сотен — они приходят страницами
    participants: ReportParticipantsPageOut


# -- админка: список курсов и редактор курса ---------------------------


class AdminCourseVersionOut(BaseModel):
    """Соседняя языковая версия той же группы: переключатель РУС|ҚАЗ ведёт
    и на черновик, поэтому статус приходит вместе с языком."""

    id: int
    lang: str
    status: str


class AdminCourseOut(BaseModel):
    """Строка списка курсов — версия, а не группа: редактируют версию."""

    id: int
    group_id: int
    lang: str
    title: str
    cover: str | None
    category_id: int
    hours: int
    # null — «Цена по запросу»
    price: int | None
    status: str
    starts_at: date | None
    modules_count: int
    # Скрытые уроки тоже: админ считает то, что в курсе есть
    lessons_count: int
    # Заявки в работе: new | contacted | paid
    open_leads_count: int
    students_count: int
    completed_count: int
    # Двигает любая правка курса или его программы
    updated_at: datetime
    versions: list[AdminCourseVersionOut]


class AdminCoursesPageOut(BaseModel):
    items: list[AdminCourseOut]
    total: int
    page: int
    per_page: int


class AdminProgramLessonOut(ProgramLessonOut):
    # Скрыт от учителей; is_ready — есть ли содержимое; has_data — есть ли
    # чужой прогресс, попытки или сдачи: по нему меню показывает «Скрыть»
    # вместо «Удалить», не дожидаясь ответа сервера
    is_hidden: bool
    is_ready: bool
    has_data: bool


class AdminProgramQuizOut(ProgramQuizOut):
    # Скрытый тест исчезает у учителя целиком, а админу приходит с is_hidden:
    # true — иначе спрятанное нечем достать обратно
    is_hidden: bool
    is_ready: bool
    has_data: bool


class AdminProgramTaskOut(ProgramTaskOut):
    is_hidden: bool
    is_ready: bool
    has_data: bool


AdminProgramItemOut = Annotated[
    AdminProgramLessonOut | AdminProgramQuizOut | AdminProgramTaskOut,
    Field(discriminator="kind"),
]


class AdminProgramModuleOut(BaseModel):
    id: int
    title: str
    items: list[AdminProgramItemOut]


class ReadinessCheckOut(BaseModel):
    """Пункт чек-листа «Публикация»: `code` — для ветвления, `text` — готовая
    строка по-русски, `items` — названия, которых не хватает."""

    code: str
    ok: bool
    # Держит ли невыполненный пункт кнопку «Открыть набор». Не у всех держит:
    # обложка — предупреждение, курс без картинки открыть можно
    blocking: bool
    text: str
    items: list[str]


class ReadinessOut(BaseModel):
    # Две кнопки вкладки «Публикация» с разными требованиями: набор открывают
    # курсу, у которого закрыты все blocking-пункты, а запланированный
    # публикуют пустым — ему нужна только дата старта
    can_open: bool
    can_plan: bool
    items: list[ReadinessCheckOut]


class AdminCourseCardOut(BaseModel):
    """Редактор курса целиком: четыре вкладки экрана живут этим ответом."""

    id: int
    group_id: int
    lang: str
    title: str
    short: str
    full: str
    cover: str | None
    category_id: int
    hours: int
    duration_text: str | None
    price: int | None
    status: str
    starts_at: date | None
    strict_order: bool
    cert_require_lessons: bool
    cert_require_tasks: bool
    cert_require_module_quizzes: bool
    cert_require_final_quiz: bool
    created_at: datetime
    updated_at: datetime
    # Есть ли хоть один действующий доступ: по нему экран показывает
    # предупреждение о правках курса, где уже учатся
    has_students: bool
    # Сумма time_required_min по видимым элементам
    program_minutes: int
    versions: list[AdminCourseVersionOut]
    program: list[AdminProgramModuleOut]
    readiness: ReadinessOut


class AdminCourseIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Четыре поля, без которых курс нечем показать даже в списке. status
    # не принимается: только что созданный курс — черновик
    title: str = Field(max_length=200)
    lang: Literal["ru", "kz"]
    category_id: int
    hours: Hours


class CourseCoverIn(BaseModel):
    """Загруженная обложка курса — как картинки настроек: ключ объекта и имя
    файла из ответа POST /files.

    Имя хранится рядом с ключом: ключи загрузки случайные нарочно, и админу
    досталось бы «9f3c1a7e.jpg» вместо «Обложка курса.jpg».
    """

    model_config = {"extra": "forbid"}

    key: str = Field(max_length=500)
    name: str = Field(max_length=255)


class AdminCoursePatchIn(BaseModel):
    model_config = {"extra": "forbid"}

    title: str | None = Field(None, max_length=200)
    short: str | None = Field(None, max_length=500)
    full: str | None = Field(None, max_length=20000)
    # Загруженная картинка; null — снять обложку. Строкой-адресом обложка
    # больше не задаётся, хотя в ответе `cover` остаётся адресом: сервер
    # собирает его сам (CONTRACT, PATCH /admin/courses/{id})
    cover: CourseCoverIn | None = None
    category_id: int | None = None
    hours: Hours | None = None
    duration_text: str | None = Field(None, max_length=100)
    # null — «Цена по запросу»
    price: Price | None = None
    # Публикация — это тот же PATCH со сменой статуса, отдельной ручки нет
    status: Literal["draft", "planned", "open", "closed", "hidden"] | None = None
    # Дата без времени; у запланированного курса обязательна
    starts_at: date | None = None
    strict_order: bool | None = None
    cert_require_lessons: bool | None = None
    cert_require_tasks: bool | None = None
    cert_require_module_quizzes: bool | None = None
    cert_require_final_quiz: bool | None = None


class CourseVersionIn(BaseModel):
    model_config = {"extra": "forbid"}

    lang: Literal["ru", "kz"]
    # Чекбокс «Скопировать структуру программы как заготовку»
    copy_program: bool = False


class ModuleIn(BaseModel):
    model_config = {"extra": "forbid"}

    title: str = Field(max_length=200)


class ProgramOrderItemIn(BaseModel):
    model_config = {"extra": "forbid"}

    # video и text оба означают урок, но приходят как есть: фронт шлёт обратно
    # то, что получил
    kind: Literal["video", "text", "quiz", "task"]
    id: int


class ProgramOrderModuleIn(BaseModel):
    model_config = {"extra": "forbid"}

    id: int
    items: list[ProgramOrderItemIn]


class ProgramOrderIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Дерево приходит целиком: неполное отбивается, иначе перетаскивание
    # потеряет элемент, о котором экран не знал
    modules: list[ProgramOrderModuleIn]


class AdminProgramOut(BaseModel):
    program: list[AdminProgramModuleOut]
    program_minutes: int


# -- админка: редактор урока -------------------------------------------


class AdminLessonCourseOut(BaseModel):
    """Хлебная крошка шапки редактора: lang рисует метку языка курса —
    переключателя языка в уроке нет."""

    id: int
    lang: str
    title: str


class AdminLessonModuleOut(BaseModel):
    id: int
    title: str


class AdminLessonOut(BaseModel):
    """Редактор урока. От учительского GET /lessons/{id} отличается тремя
    вещами: приходит video_url, приходит скрытый урок и урок невидимого курса,
    а вместо is_completed — админские признаки."""

    id: int
    module_id: int
    course: AdminLessonCourseOut
    module: AdminLessonModuleOut
    title: str
    kind: Literal["video", "text"]
    # Разметка после чистки сервером; null — заметки под видео нет
    body: dict | None
    # Учителю ссылку не отдают никогда — только подписанный playback;
    # null — заготовка, ссылку ещё не вписали
    video_url: str | None
    duration_label: str | None
    time_required_min: int
    is_hidden: bool
    is_ready: bool
    has_data: bool
    # Материалы по order_index; ключа хранилища здесь нет, как и везде
    files: list[LessonFileOut]


class LessonBodyIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Объект с единственным ключом html: форма зафиксирована в CONTRACT.md.
    # Лишние теги вырезаются молча — отбивать сохранение из-за вставки
    # из Word бессмысленно
    html: str = Field(max_length=100000)


class AdminLessonIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Заготовка: название, вид и требуемое время. Содержимого нет — оно
    # появится в редакторе, куда фронт уводит сразу после создания
    title: str = Field(max_length=200)
    kind: Literal["video", "text"]
    time_required_min: TimeRequiredMin


class AdminLessonPatchIn(BaseModel):
    model_config = {"extra": "forbid"}

    title: str | None = Field(None, max_length=200)
    kind: Literal["video", "text"] | None = None
    # null — стереть заметку под видео; у текстового урока пустой html — 422
    body: LessonBodyIn | None = None
    # Принимаются все обычные написания ссылки, сохраняется нормализованное
    video_url: str | None = Field(None, max_length=500)
    # Длительность вводится руками: по чужой ссылке она ненадёжна
    duration_label: str | None = Field(None, max_length=20)
    time_required_min: TimeRequiredMin | None = None
    is_hidden: bool | None = None


class LessonFileIn(BaseModel):
    model_config = {"extra": "forbid"}

    # key из ответа POST /files и имя, которое увидит учитель. Размер сервер
    # берёт из хранилища, mime — из имени
    key: str = Field(max_length=500)
    name: str = Field(max_length=255)


# -- админка: редактор теста -------------------------------------------


class AdminQuizCourseOut(BaseModel):
    """Хлебная крошка шапки редактора: lang рисует метку языка курса —
    переключателя языка в тесте нет."""

    id: int
    lang: str
    title: str


class AdminQuizModuleOut(BaseModel):
    id: int
    title: str


class AdminQuizOptionOut(BaseModel):
    id: int
    text: str
    # Верный ответ уходит наружу только здесь и в разборе своей попытки
    is_correct: bool


class AdminQuizQuestionOut(BaseModel):
    id: int
    type: QuestionType
    text: str
    # null — пояснения нет, блок на экране не рисуется
    explanation: str | None
    points: int
    # Скрытый вопрос приходит как обычная строка: админ должен видеть,
    # что он спрятал, — в max_score такой вопрос не считается
    is_hidden: bool
    # Попадал ли этот вопрос в состав хотя бы одной попытки: по нему экран
    # рисует замок, не дожидаясь 409
    has_attempts: bool
    options: list[AdminQuizOptionOut]


class AdminQuizOut(BaseModel):
    """Редактор теста. От учительского GET /quizzes/{id} отличается тем, что
    здесь приходят правильные ответы и пояснения, приходит скрытый тест
    и тест невидимого курса, а вместо состояния попытки — админские признаки."""

    id: int
    module_id: int
    course: AdminQuizCourseOut
    module: AdminQuizModuleOut
    title: str
    is_final: bool
    pass_score: int
    # null — таймера нет, попытка не истекает
    time_limit_min: int | None
    shuffle: bool
    show_review: bool
    retakable: bool
    time_required_min: int
    is_hidden: bool
    # Были ли по тесту попытки вообще: по ним запрещено удаление
    has_attempts: bool
    # Сумма баллов по нескрытым вопросам — то же число, что видит учитель
    max_score: int
    questions: list[AdminQuizQuestionOut]


class AdminQuizIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Заготовка: название, время и то, без чего строки в базе не существует.
    # Вопросы появятся в редакторе, куда фронт уводит сразу после создания
    title: str = Field(max_length=200)
    time_required_min: TimeRequiredMin
    # Итоговый в курсе один: заготовка с is_final отбивается так же, как PATCH
    is_final: bool = False
    pass_score: PassScore


class AdminQuizPatchIn(BaseModel):
    model_config = {"extra": "forbid"}

    title: str | None = Field(None, max_length=200)
    is_final: bool | None = None
    pass_score: PassScore | None = None
    # null — таймера нет: переключатель «Таймер» и есть выбор между null и числом
    time_limit_min: TimeLimitMin | None = None
    shuffle: bool | None = None
    show_review: bool | None = None
    retakable: bool | None = None
    time_required_min: TimeRequiredMin | None = None
    is_hidden: bool | None = None


class QuizOptionIn(BaseModel):
    model_config = {"extra": "forbid"}

    text: str = Field(max_length=500)
    is_correct: bool = False


class QuizQuestionIn(BaseModel):
    model_config = {"extra": "forbid"}

    type: QuestionType
    text: str = Field(max_length=2000)
    explanation: str | None = Field(None, max_length=2000)
    points: QuestionPoints = 1
    # Варианты приходят вместе с вопросом: варианта без вопроса не бывает,
    # а двумя запросами в базе оставались бы вопросы без ответов
    options: list[QuizOptionIn]


class QuizQuestionPatchIn(BaseModel):
    model_config = {"extra": "forbid"}

    type: QuestionType | None = None
    text: str | None = Field(None, max_length=2000)
    # null — стереть пояснение
    explanation: str | None = Field(None, max_length=2000)
    points: QuestionPoints | None = None
    is_hidden: bool | None = None
    # Полным списком, заменяют прежние: частичной правки одного варианта нет
    options: list[QuizOptionIn] | None = None


# -- админка: редактор задания -----------------------------------------


class AdminTaskCourseOut(BaseModel):
    id: int
    lang: str
    title: str


class AdminTaskModuleOut(BaseModel):
    id: int
    title: str


class AdminTaskOut(BaseModel):
    """Редактор задания. От учительского GET /tasks/{id} отличается тем же,
    чем редактор урока: приходит задание невидимого курса, а вместо истории
    сдач — админские признаки."""

    id: int
    module_id: int
    course: AdminTaskCourseOut
    module: AdminTaskModuleOut
    title: str
    # Условие после чистки сервером; у заготовки — пустой html
    statement: dict
    # Так же, как учителю: имя, размер и тип, без ключа хранилища
    template_file: TemplateFileOut | None
    submit_format: Literal["text", "file", "both"]
    # Пустой список — принимается любое расширение
    allowed_ext: list[str]
    max_size_mb: int
    time_required_min: int
    is_hidden: bool
    is_ready: bool
    has_data: bool


class AdminTaskIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Заготовка: название и требуемое время. Условие появится в редакторе
    title: str = Field(max_length=200)
    time_required_min: TimeRequiredMin


class TaskStatementIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Объект с единственным ключом html, как у урока: форма зафиксирована
    # в CONTRACT.md, а лишние теги вырезаются молча
    html: str = Field(max_length=100000)


class TaskTemplateIn(BaseModel):
    model_config = {"extra": "forbid"}

    # key из ответа POST /files. Имя приходит с экрана, но в базе у задания
    # одна колонка — ключ хранилища, и наружу имя берётся из него же
    key: str = Field(max_length=500)
    name: str = Field(max_length=255)


class AdminTaskPatchIn(BaseModel):
    model_config = {"extra": "forbid"}

    title: str | None = Field(None, max_length=200)
    # Колонка обязательная, поэтому null здесь — «не прислано», а не «стереть»:
    # пустое условие присылают объектом с пустым html
    statement: TaskStatementIn | None = None
    # null — убрать шаблон у задания
    template_file: TaskTemplateIn | None = None
    submit_format: Literal["text", "file", "both"] | None = None
    # Расширения без точки и в нижнем регистре; пустой список — ограничения нет
    allowed_ext: list[str] | None = Field(None, max_length=20)
    # Верхнюю границу проверяет сценарий: потолок у загрузки свой, и текст
    # ошибки называет именно его
    max_size_mb: MaxSizeMb | None = None
    time_required_min: TimeRequiredMin | None = None
    is_hidden: bool | None = None


# -- админка: учителя и карточка учителя -------------------------------


class AdminTeacherOut(BaseModel):
    """Строка списка учителей. Столбцов «последний вход» и «активность» здесь
    нет: чтобы они были правдой, пришлось бы писать в базу на каждое движение
    учителя (DESIGN_BRIEF, 5.22)."""

    id: int
    last_name: str
    first_name: str
    middle_name: str
    phone: str
    school: str
    region: str
    city: str
    subject: str
    # Действующие доступы, завершённые курсы и документы на руках:
    # отозванный сертификат в счёт не идёт
    courses_count: int
    completed_count: int
    certificates_count: int
    is_blocked: bool
    created_at: datetime


class AdminTeachersPageOut(BaseModel):
    items: list[AdminTeacherOut]
    total: int
    page: int
    per_page: int


class AdminTeacherCourseOut(BaseModel):
    id: int
    lang: str
    title: str


class AdminTeacherEnrollmentOut(BaseModel):
    """Строка вкладки «Курсы» — по строке на доступ, включая отозванные:
    прогресс и результаты при закрытии доступа не удаляются."""

    enrollment_id: int
    course: AdminTeacherCourseOut
    granted_at: datetime
    granted_by_admin: bool
    # Комментарий выдачи: сумма, способ, дата — для истории. null — оплату
    # не отмечали
    paid_note: str | None
    # Заполнены — доступ закрыт / курс завершён
    revoked_at: datetime | None
    completed_at: datetime | None
    # Счётчик именно уроков; процент считается по всем видимым элементам
    # программы — так же, как в кабинете учителя и в отчёте по курсу
    lessons_done: int
    lessons_total: int
    progress_percent: int


class AdminTeacherAttemptOut(BaseModel):
    id: int
    started_at: datetime
    # Пусто — попытка идёт прямо сейчас, балла у неё ещё нет
    finished_at: datetime | None
    score: int | None
    passed: bool | None
    is_counted: bool
    # Заполнены у попытки, с которой сняли зачёт при выдаче пересдачи
    uncounted_reason: str | None
    uncounted_at: datetime | None


class AdminTeacherQuizOut(BaseModel):
    """Строка вкладки «Тесты»: тест и все попытки человека по нему.

    `retake_blocker` объясняет отказ заранее, чтобы экран не показывал живую
    кнопку, которая ответит 409; коды те же, что у ошибок пересдачи.
    """

    quiz_id: int
    title: str
    course_id: int
    course_title: str
    retakable: bool
    pass_score: int
    can_allow_retake: bool
    retake_blocker: (
        Literal["quiz_retakable", "no_attempt", "attempt_in_progress", "certificate_issued"]
        | None
    )
    attempts: list[AdminTeacherAttemptOut]


class AdminTeacherSubmissionOut(BaseModel):
    id: int
    task_id: int
    task_title: str
    course_id: int
    status: Literal["pending", "accepted", "rework"]
    created_at: datetime
    # null — работа ещё в очереди
    reviewed_at: datetime | None


class AdminTeacherCertificateOut(BaseModel):
    id: int
    number: str
    course_id: int
    # Снимок на момент выдачи, а не текущее название курса
    course_title: str
    hours: int
    issued_at: datetime
    # Заполнено — документ отозван
    revoked_at: datetime | None


class AdminTeacherCardOut(BaseModel):
    """Карточка учителя: профиль и четыре вкладки одним ответом. Пагинации
    здесь нет — у одного человека курсов, тестов, работ и документов заведомо
    немного, и экран её не рисует."""

    id: int
    last_name: str
    first_name: str
    middle_name: str
    phone: str
    email: str
    school: str
    position: str
    region: str
    city: str
    subject: str
    # null — стаж не указан
    experience: int | None
    lang: str
    is_admin: bool
    is_blocked: bool
    created_at: datetime
    courses: list[AdminTeacherEnrollmentOut]
    quizzes: list[AdminTeacherQuizOut]
    submissions: list[AdminTeacherSubmissionOut]
    certificates: list[AdminTeacherCertificateOut]


class AdminTeacherPatchIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Два действия карточки: «Заблокировать» и «Изменить номер телефона».
    # Номер нормализуется к +7XXXXXXXXXX, как при входе, и отзывает все
    # сессии этого человека
    is_blocked: bool | None = None
    phone: str | None = Field(None, max_length=20)


class TeacherRetakeIn(BaseModel):
    model_config = {"extra": "forbid"}

    quiz_id: int
    # Причина обязательна: она остаётся в истории попытки, и по ней потом
    # разбирают, почему зачёт снят. Пустую строку отбивает сценарий
    reason: str = Field(max_length=2000)


# -- админка: отзывы и модерация ---------------------------------------


class AdminReviewCourseOut(BaseModel):
    id: int
    lang: str
    title: str


class AdminReviewTeacherOut(BaseModel):
    """Автор отзыва: ФИО тремя полями, как в заявках и очереди работ,
    плюс школа и город — по ним админ узнаёт человека в ленте."""

    id: int
    last_name: str
    first_name: str
    middle_name: str
    school: str
    city: str


class AdminReviewOut(BaseModel):
    """Строка ленты отзывов. Премодерации нет — отзыв виден на странице курса
    сразу, поэтому «неопубликованных» здесь не бывает, а удалённые в ленту
    не попадают вовсе."""

    id: int
    rating: int
    text: str
    created_at: datetime
    # Правки отзыва учителем в продукте нет — поле остаётся null
    updated_at: datetime | None
    course: AdminReviewCourseOut
    teacher: AdminReviewTeacherOut
    # null — админ на отзыв не отвечал
    reply: ReviewReplyOut | None


class AdminReviewsPageOut(BaseModel):
    items: list[AdminReviewOut]
    total: int
    page: int
    per_page: int


class ReviewReplyIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Повторный вызов меняет ответ — на экране это «Изменить ответ»,
    # отдельной ручки правки нет. Пустую строку отбивает сценарий
    text: str = Field(max_length=2000)


# -- админка: категории курсов -----------------------------------------


class AdminAdminOut(BaseModel):
    """Строка списка администраторов в настройках. ФИО приходит пустым, пока
    человек не заполнил профиль сам: добавляют по одному телефону."""

    id: int
    last_name: str
    first_name: str
    middle_name: str
    phone: str
    # Это вы: в списке из нескольких номеров себя видно сразу
    is_current: bool
    created_at: datetime


class AdminAdminsOut(BaseModel):
    # Без пагинации: админов единицы
    items: list[AdminAdminOut]


class AdminAdminIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Единственное поле: ФИО человек пишет себе сам в профиле
    phone: str = Field(max_length=20)


class AdminCategoryOut(BaseModel):
    id: int
    title: str
    order_index: int
    # Все версии курса, включая черновики: по этому числу решают,
    # можно ли категорию удалить
    courses_count: int


class AdminCategoriesOut(BaseModel):
    # Без пагинации: категорий единицы
    items: list[AdminCategoryOut]


class AdminCategoryIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Одно поле и на создание, и на переименование: order_index ставится
    # при создании, а courses_count считается, а не присылается
    title: str = Field(max_length=200)


# -- настройки площадки ------------------------------------------------


class SettingsImageOut(BaseModel):
    """Картинка настроек: адрес публичной раздачи и имя файла. Ключ хранилища
    наружу не уходит — как и у материалов урока."""

    url: str
    name: str


class SettingsContactsOut(BaseModel):
    """Контакты администратора: их подставляют в кнопку «Связаться
    с администратором» и в подвал. Не заполняли — приходят пустые строки,
    а не null: экран рисует поля всегда.

    `phone` — для звонков, `whatsapp` — номер, а не ссылка: ссылку wa.me
    фронт собирает сам."""

    name: str
    phone: str
    whatsapp: str
    hours: str


class AdminCertificateImagesOut(BaseModel):
    # Слоты хранилища cert_logo | cert_sign | cert_stamp; null — не ставили
    logo: SettingsImageOut | None
    sign: SettingsImageOut | None
    stamp: SettingsImageOut | None


class AdminSettingsTelegramOut(BaseModel):
    """Привязка бота. chat_id наружу не уходит — от него остаётся признак
    connected и название чата, по которому админ узнаёт, куда идут заявки."""

    connected: bool
    chat_title: str | None
    connected_at: datetime | None
    notify_leads: bool
    notify_submissions: bool


class AdminSettingsOut(BaseModel):
    """Настройки площадки одним ответом — все четыре вкладки экрана."""

    platform_name: str
    org_name: str
    # Слот logo; null — логотип не ставили
    logo: SettingsImageOut | None
    contacts: SettingsContactsOut
    certificate_images: AdminCertificateImagesOut
    telegram: AdminSettingsTelegramOut


class SettingsImageIn(BaseModel):
    model_config = {"extra": "forbid"}

    # key и name из ответа POST /files. Имя хранится рядом с ключом: ключи
    # загрузки случайные нарочно, и админу досталось бы «9f3c1a7e.png»
    key: str = Field(max_length=500)
    name: str = Field(max_length=255)


class SettingsContactsIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Пустая строка стирает поле; null — то же, что поле не прислали.
    # whatsapp — номер, как и phone: ссылку собирает фронт
    name: str | None = Field(None, max_length=200)
    phone: str | None = Field(None, max_length=50)
    whatsapp: str | None = Field(None, max_length=50)
    hours: str | None = Field(None, max_length=200)


class AdminCertificateImagesIn(BaseModel):
    model_config = {"extra": "forbid"}

    # null убирает картинку; поля, которого в запросе нет, правка не касается
    logo: SettingsImageIn | None = None
    sign: SettingsImageIn | None = None
    stamp: SettingsImageIn | None = None


class AdminSettingsTelegramIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Только флаги: chat_id этим PATCH не пишется никогда, привязку меняют
    # своими ручками. Поля chat_id здесь нет вовсе, и extra: forbid отбивает
    # попытку его прислать
    notify_leads: bool | None = None
    notify_submissions: bool | None = None


class AdminSettingsPatchIn(BaseModel):
    model_config = {"extra": "forbid"}

    platform_name: str | None = Field(None, max_length=200)
    org_name: str | None = Field(None, max_length=300)
    contacts: SettingsContactsIn | None = None
    # null убирает логотип
    logo: SettingsImageIn | None = None
    certificate_images: AdminCertificateImagesIn | None = None
    telegram: AdminSettingsTelegramIn | None = None


class PublicSettingsOut(BaseModel):
    """Публичный ответ без входа: только то, что лендинг и страница курса
    показывают всем. Ни привязки бота, ни картинок сертификата, ни ключей
    хранилища здесь нет — лишнее поле утекает наружу вместе с ответом."""

    platform_name: str
    org_name: str
    # Адрес GET /branding/logo; null — логотип не ставили
    logo_url: str | None
    contacts: SettingsContactsOut


class TelegramBindCodeOut(BaseModel):
    """Код привязки и адрес бота. Админ отправляет боту `/start <код>`,
    и chat_id записывает сервер, приняв сообщение: руками его не вписать
    (CONTRACT, «Telegram-бот: привязка»)."""

    code: str
    bot_username: str
    # Готовая ссылка «Открыть бота»: код в ней уже подставлен
    deep_link: str
    expires_at: datetime
