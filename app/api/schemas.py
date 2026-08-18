from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.adapters.db.models import User
from app.domain.profile import onboarding_done


class RequestCodeIn(BaseModel):
    phone: str = Field(max_length=20)
    consent: bool


class RequestCodeOut(BaseModel):
    retry_after_sec: int


class VerifyCodeIn(BaseModel):
    phone: str = Field(max_length=20)
    code: str = Field(max_length=10)


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

    @classmethod
    def from_user(cls, user: User) -> "UserOut":
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
    experience: int | None = Field(None, ge=0, le=70)
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


class ReviewOut(BaseModel):
    id: int
    author_name: str
    school: str
    city: str
    rating: int
    text: str
    created_at: datetime
    # Ответа админа в модели данных пока нет — поле всегда null (см. CONTRACT.md)
    reply: None = None


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
