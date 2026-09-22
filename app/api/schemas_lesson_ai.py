"""Схемы «Жоспара» — отдельным файлом, а не в `schemas.py`, по той же причине,
что у справочника и ассистента: большой общий файл — место встречи параллельных
сессий.

Формы ответов зафиксированы примерами в `CONTRACT.md`, раздел «Жоспар:
комплект к уроку»: фронт работает от них и не гадает.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

MaterialKind = Literal[
    "lesson_plan", "presentation", "tasks", "homework", "assessment", "game", "answer_key"
]


class LessonKitIn(BaseModel):
    """Заказ комплекта. Язык и класс не принимаются: их знает цель обучения.

    `kinds` — что подготовить. Пока генератор умеет один вид, ҚМЖ; остальные
    шесть добавит сессия L5, расширив этот список.
    """

    objective_id: int
    topic: str = Field(min_length=1, max_length=200)
    kinds: list[Literal["lesson_plan"]] = Field(min_length=1)
    class_size: int | None = Field(default=None, ge=1, le=60)
    notes: str = Field(default="", max_length=1000)


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
        "internal_error",
    ]
    message: str


class LessonMaterialOut(BaseModel):
    """Материал комплекта. `content` пуст, пока материал не готов; форма
    у каждого вида своя — у `lesson_plan` она в `CONTRACT.md`.

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
    objective: LessonObjectiveOut
    subject_code: str
    grade: int
    lang: Literal["ru", "kz"]
    topic: str
    class_size: int | None
    notes: str
    status: Literal["generating", "ready", "partial", "failed"]
    created_at: datetime
    updated_at: datetime
    materials: list[LessonMaterialOut]


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
