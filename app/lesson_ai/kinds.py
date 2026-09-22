"""Какие виды материала умеет генерировать «Жоспар» — и чем.

Очередь спрашивает генератор по виду задания отсюда. L5 добавляет шесть
остальных видов описаниями `KindSpec` в этот файл, не трогая ни очередь,
ни генератор.
"""

from app.application.lesson_ai_queue import MaterialGenerator
from app.application.ports import AiPort
from app.lesson_ai.generate import KindSpec, SchemaGenerator
from app.lesson_ai.prompts import lesson_plan as lesson_plan_prompt
from app.lesson_ai.schemas.lesson_plan import SERVER_BLOCKS, LessonPlanDraft

LESSON_PLAN = KindSpec(
    kind="lesson_plan",
    draft=LessonPlanDraft,
    system=lesson_plan_prompt.SYSTEM,
    user_message=lesson_plan_prompt.user_message,
    server_blocks=SERVER_BLOCKS,
)

SPECS = (LESSON_PLAN,)


def generators(ai: AiPort, model: str) -> dict[str, MaterialGenerator]:
    return {spec.kind: SchemaGenerator(ai, model, spec) for spec in SPECS}
