"""Схемы «Жоспара» — отдельным файлом, а не в `schemas.py`, по той же причине,
что у справочника и ассистента: большой общий файл — место встречи параллельных
сессий.

Формы ответов зафиксированы примерами в `CONTRACT.md`, раздел «Жоспар:
комплект к уроку»: фронт работает от них и не гадает.
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

MaterialKind = Literal[
    "lesson_plan", "presentation", "tasks", "homework", "assessment", "game", "answer_key"
]


EngagementLevel = Literal["calm", "active", "full_game"]
DifficultyLevel = Literal["basic", "standard", "advanced"]


class LessonKitIn(BaseModel):
    """Заказ комплекта. Язык и класс не принимаются: их знают цели обучения.

    `objective_ids` — цели урока в порядке учителя, без потолка по числу.
    Все из одной программы и одного класса, иначе 422 `objectives_mixed`;
    повторы схлопываются.

    `kinds` — что подготовить, любые из семи видов. `answer_key` не генерируется,
    а собирается из ответов к `tasks`, `homework` и `assessment` — без них
    он не принимается.

    Два уровня (раздел 4.3 брифа): насколько игровым идёт урок и какой у класса
    уровень. Не прислал — середина, `active` и `standard`.
    """

    objective_ids: list[int] = Field(min_length=1)
    topic: str = Field(min_length=1, max_length=200)
    kinds: list[MaterialKind] = Field(min_length=1)
    class_size: int | None = Field(default=None, ge=1, le=60)
    notes: str = Field(default="", max_length=1000)
    engagement_level: EngagementLevel = "active"
    difficulty_level: DifficultyLevel = "standard"


class LessonObjectiveOut(BaseModel):
    """Цель обучения из справочника — дословно, как в приказе."""

    id: int
    code: str
    text: str
    marker: str


class LessonMaterialErrorOut(BaseModel):
    code: Literal[
        "ai_failed",
        "model_output_invalid",
        "interrupted",
        "program_not_published",
        "no_sources",
        "internal_error",
    ]
    message: str


class LessonMaterialOut(BaseModel):
    """Материал комплекта. `content` пуст, пока материал не готов; форма
    у каждого вида своя — все семь в `CONTRACT.md`.

    `error` заполнен только у упавшего материала (`status: "failed"`).
    """

    id: int
    kind: MaterialKind
    status: Literal["queued", "generating", "ready", "failed"]
    error: LessonMaterialErrorOut | None
    content: dict[str, Any] | None
    updated_at: datetime


class LessonKitOut(BaseModel):
    """Комплект целиком. Им отвечают и создание, и опрос: экран ожидания
    перерисовывается одним и тем же ответом."""

    id: int
    objectives: list[LessonObjectiveOut]
    subject_code: str
    grade: int
    lang: Literal["ru", "kz"]
    topic: str
    class_size: int | None
    notes: str
    engagement_level: EngagementLevel
    difficulty_level: DifficultyLevel
    status: Literal["generating", "ready", "partial", "failed"]
    created_at: datetime
    updated_at: datetime
    materials: list[LessonMaterialOut]


class LessonKitListMaterialOut(BaseModel):
    id: int
    kind: MaterialKind
    status: Literal["queued", "generating", "ready", "failed"]


class LessonKitListItemOut(BaseModel):
    """Карточка комплекта в «Моих уроках»: комплект без `content`, `notes`
    и `class_size`, материалы — только `id`, `kind`, `status`."""

    id: int
    objectives: list[LessonObjectiveOut]
    subject_code: str
    grade: int
    lang: Literal["ru", "kz"]
    topic: str
    engagement_level: EngagementLevel
    difficulty_level: DifficultyLevel
    status: Literal["generating", "ready", "partial", "failed"]
    created_at: datetime
    updated_at: datetime
    materials: list[LessonKitListMaterialOut]


class LessonKitsPageOut(BaseModel):
    items: list[LessonKitListItemOut]
    total: int
    page: int
    per_page: int


# -- Блоки материала и их история (сессия L4) --------------------------------


class LessonBlockOut(BaseModel):
    """Блок материала — поле верхнего уровня его содержимого.

    `editable: false` — блок сервера (раздел программы, цель обучения
    с кодом): ни перегенерации, ни правки, ни отката у него нет.
    `content` — той же формы, что ключ `kind` в `material.content`.
    """

    kind: str
    order_index: int
    editable: bool
    content: Any
    updated_at: datetime


class LessonBlocksOut(BaseModel):
    material_id: int
    kind: MaterialKind
    items: list[LessonBlockOut]


class LessonBlockRegenerateIn(BaseModel):
    """Просьба учителя своими словами — «сделай подвижнее». Пусто — просто
    переписать блок заново."""

    instruction: str = Field(default="", max_length=500)


class LessonBlockEditIn(BaseModel):
    """Ручная правка: блок целиком, той же формы, что в `content` материала.
    Проверяется той же схемой, что ответ модели."""

    content: Any


class LessonBlockVersionOut(BaseModel):
    id: int
    source: Literal["generated", "regenerated", "edited", "restored"]
    instruction: str | None
    content: Any
    created_at: datetime
    current: bool


class LessonBlockVersionsOut(BaseModel):
    """История блока, свежие сверху; первая строка — текущее состояние."""

    block: str
    items: list[LessonBlockVersionOut]


# -- Чат сбора заказа (сессия L6) --------------------------------------------


class LessonChatObjectiveOut(BaseModel):
    """Цель обучения строкой справочника — дословно, с кодом, разделом
    и подразделом. В этой форме её отдают инструменты чата (карточки
    в `tool_result`), карточка заказа и список «выбрать самому»."""

    id: int
    code: str
    text: str
    marker: str
    grade: int
    lang: Literal["ru", "kz"]
    subject_code: str
    section: str
    subsection: str


class LessonChatOrderOut(BaseModel):
    """Заказ, каким его записал сервер. Предмет и класс — из цели.

    `missing` — кого из пятерых ещё нет; пусто и `ready: true` — карточку
    с галочками и кнопкой «Готовить» можно показывать. Уровни и виды —
    с учётом сказанного в разговоре, иначе середина и список по умолчанию.
    """

    subject_code: str | None
    grade: int | None
    lang: Literal["ru", "kz"] | None
    objectives: list[LessonChatObjectiveOut]
    # Учитель подтвердил, что целей больше трёх и столько нужно
    many_objectives_confirmed: bool
    topic: str | None
    engagement_level: EngagementLevel
    difficulty_level: DifficultyLevel
    kinds: list[MaterialKind]
    class_size: int | None
    notes: str
    missing: list[Literal["subject", "grade", "lang", "objective", "topic"]]
    ready: bool


class LessonChatEventOut(BaseModel):
    """Событие ленты. `type: "kit_status"` — комплект собирается / готов /
    собран не целиком / не собрался (`status`, `failed_kinds` у неполного);
    `type: "block_changed"` — помощник изменил материал из чата: `action`,
    `material_id`, `kind`, `block`, `index` (пункт блока с единицы, `null` —
    блок переписан целиком) и `slide` (сквозной номер слайда презентации,
    только у слайдов). По нему экран подсвечивает материал справа."""

    type: Literal["kit_status", "block_changed"]
    kit_id: int | None = None
    status: Literal["generating", "ready", "partial", "failed"] | None = None
    failed_kinds: list[MaterialKind] | None = None
    action: Literal["rewritten", "item_added", "item_replaced", "item_deleted"] | None = None
    material_id: int | None = None
    kind: MaterialKind | None = None
    block: str | None = None
    index: int | None = None
    slide: int | None = None


class LessonChatMessageOut(BaseModel):
    """Строка ленты. `role: "tool"` — вызов инструмента: поиск по программе
    (в `tool_result.items` найденные цели формы `LessonChatObjectiveOut`, отказ —
    в `tool_result.error`) или правка материала. `role: "event"` — событие
    (`event`), `content` у него — запасная подпись словами."""

    id: int
    role: Literal["user", "assistant", "tool", "event"]
    content: str
    tool_name: str | None
    tool_args: dict[str, Any] | None
    tool_result: dict[str, Any] | None
    event: LessonChatEventOut | None
    created_at: datetime


class LessonChatKitOut(BaseModel):
    """Комплект чата без содержимого — для правой панели: статусы материалов
    и параметры урока. Содержимое — ручки `/lesson_ai/kits/{id}` и блоков."""

    id: int
    status: Literal["generating", "ready", "partial", "failed"]
    topic: str
    grade: int
    lang: Literal["ru", "kz"]
    engagement_level: EngagementLevel
    difficulty_level: DifficultyLevel
    materials: list[LessonKitListMaterialOut]


class LessonChatOut(BaseModel):
    """`mode: "order"` — заказ собирается (`status: "collecting"`), `"edit"` —
    комплект заказан (`status: "confirmed"`), чат правит материалы; `kit` — он же."""

    id: int
    status: Literal["collecting", "confirmed"]
    mode: Literal["order", "edit"]
    kit_id: int | None
    kit: LessonChatKitOut | None
    order: LessonChatOrderOut
    items: list[LessonChatMessageOut]
    created_at: datetime
    updated_at: datetime


class LessonChatListItemOut(BaseModel):
    """Строка истории разговоров. Предмет и класс — из цели заказа, как
    в `LessonChatOrderOut`; `first_message` — первая реплика учителя,
    не длиннее 120 символов, `null` — учитель ещё ничего не написал."""

    id: int
    status: Literal["collecting", "confirmed"]
    mode: Literal["order", "edit"]
    kit_id: int | None
    kit_status: Literal["generating", "ready", "partial", "failed"] | None
    title: str | None
    topic: str | None
    subject_code: str | None
    grade: int | None
    first_message: str | None
    created_at: datetime
    updated_at: datetime


class LessonChatsPageOut(BaseModel):
    items: list[LessonChatListItemOut]
    total: int
    page: int
    per_page: int


class LessonChatFeedOut(BaseModel):
    """`GET /lesson_ai/chats/{id}/messages?after_id=` — хвост ленты и комплект:
    опрос, пока комплект собирается."""

    status: Literal["collecting", "confirmed"]
    mode: Literal["order", "edit"]
    kit: LessonChatKitOut | None
    items: list[LessonChatMessageOut]


class LessonChatSayIn(BaseModel):
    text: str


class LessonChatTurnOut(BaseModel):
    """Событие `done` хода: чат целиком — лента и заказ."""

    chat: LessonChatOut
    stopped_at_cap: bool


class LessonChatOrderPatchIn(BaseModel):
    """Кнопки под сообщениями: «Да, она», «Русский» / «Қазақша», правка темы.
    Прислано `null` — поле стёрто; не прислано — не тронуто."""

    objective_ids: list[int] | None = None
    many_objectives_confirmed: bool | None = None
    lang: Literal["ru", "kz"] | None = None
    topic: str | None = None


class LessonChatConfirmIn(BaseModel):
    """Кнопка «Готовить» — карточка заказа целиком.

    Оба уровня **обязательны** (раздел 4.3 брифа): у них есть середина,
    но экран обязан показать вопрос и прислать ответ, пусть и нетронутый.
    `class_size` и `notes` не прислали — берутся из разговора.
    """

    kinds: list[MaterialKind] = Field(min_length=1)
    engagement_level: EngagementLevel
    difficulty_level: DifficultyLevel
    class_size: int | None = Field(default=None, ge=1, le=60)
    notes: str | None = Field(default=None, max_length=1000)


class LessonChatConfirmOut(BaseModel):
    chat: LessonChatOut
    kit: LessonKitOut


class LessonObjectivesOut(BaseModel):
    items: list[LessonChatObjectiveOut]


class LessonSubjectOut(BaseModel):
    code: str
    title_ru: str
    title_kz: str
    grade_from: int
    grade_to: int
    langs: list[Literal["ru", "kz"]]


class LessonSubjectsOut(BaseModel):
    items: list[LessonSubjectOut]


# -- Доступ к продукту (сессия L7) ---------------------------------------------


class LessonAccessStatusOut(BaseModel):
    """`GET /lesson_ai/access` — есть ли у учителя доступ. Срок — только
    у действующего; `null` у действующего значит «бессрочно»."""

    has_access: bool
    expires_on: date | None


class LessonAccessPersonOut(BaseModel):
    id: int
    name: str


class LessonAccessOut(BaseModel):
    """Доступ учителя глазами админа. `status`: `active` — действует,
    `expired` — срок прошёл, `revoked` — снят админом."""

    status: Literal["active", "expired", "revoked"]
    expires_on: date | None
    paid: bool
    paid_note: str | None
    granted_at: datetime
    granted_by: LessonAccessPersonOut | None
    revoked_at: datetime | None


class LessonAccessCardOut(BaseModel):
    """Доступ одного учителя; `access: null` — не выдавался ни разу."""

    user_id: int
    access: LessonAccessOut | None


class LessonAccessGrantIn(BaseModel):
    """Выдать, продлить или выдать заново после снятия.

    `expires_on` — по этот день включительно, по казахстанскому времени;
    `null` — без срока. `paid` — отметка «оплата получена», `note` — комментарий
    к ней; без оплаты комментарий не сохраняется, как у доступа к курсу.
    """

    expires_on: date | None = None
    paid: bool = False
    note: str | None = Field(default=None, max_length=500)


class LessonAccessTeacherOut(BaseModel):
    """Человек в строке списка доступов — те же поля и имена, что в списке
    учителей админки: экран рисует их тем же компонентом."""

    id: int
    last_name: str
    first_name: str
    middle_name: str
    phone: str
    school: str
    region: str
    city: str
    subject: str
    is_blocked: bool


class LessonAccessUsageOut(BaseModel):
    """Расход учителя за всё время. `cost_usd: null` — стоимость неизвестна:
    модели нет в прайсе или расхода ещё не было; токены при этом честные."""

    kits: int
    tokens_in: int
    tokens_out: int
    cost_usd: float | None


class LessonAccessItemOut(BaseModel):
    teacher: LessonAccessTeacherOut
    access: LessonAccessOut
    usage: LessonAccessUsageOut


class LessonAccessPageOut(BaseModel):
    items: list[LessonAccessItemOut]
    total: int
    page: int
    per_page: int
