"""Какие виды материала умеет генерировать «Жоспар» — и чем.

Очередь спрашивает генератор по виду задания отсюда. Вид с моделью — описание
`KindSpec`: схема ответа, промпт и блоки сервера; очередь и генератор общие.
Ключи с ответами модели не заказываются — их собирает сервер
(`AnswerKeyAssembler`, TEACHER_AI_BRIEF, раздел 4.2).
"""

from app.application.lesson_ai_queue import MaterialGenerator
from app.application.ports import AiPort
from app.lesson_ai.answer_key import AnswerKeyAssembler
from app.lesson_ai.games.select import references
from app.lesson_ai.generate import KindSpec, SchemaGenerator
from app.lesson_ai.methods.select import references as method_references
from app.lesson_ai.prompts import assessment as assessment_prompt
from app.lesson_ai.prompts import game as game_prompt
from app.lesson_ai.prompts import homework as homework_prompt
from app.lesson_ai.prompts import lesson_plan as lesson_plan_prompt
from app.lesson_ai.prompts import presentation as presentation_prompt
from app.lesson_ai.prompts import tasks as tasks_prompt
from app.lesson_ai.schemas import SERVER_BLOCKS
from app.lesson_ai.schemas.assessment import AssessmentDraft
from app.lesson_ai.schemas.game import GameDraft
from app.lesson_ai.schemas.homework import HomeworkDraft
from app.lesson_ai.schemas.lesson_plan import SERVER_BLOCKS as PLAN_SERVER_BLOCKS
from app.lesson_ai.schemas.lesson_plan import LessonPlanDraft
from app.lesson_ai.schemas.presentation import PresentationDraft
from app.lesson_ai.schemas.tasks import TasksDraft

LESSON_PLAN = KindSpec(
    kind="lesson_plan",
    draft=LessonPlanDraft,
    system=lesson_plan_prompt.SYSTEM,
    user_message=lesson_plan_prompt.user_message,
    server_blocks=PLAN_SERVER_BLOCKS,
    # План идёт первым: остальные получают его на вход (раздел 4.2)
    with_plan=False,
)

PRESENTATION = KindSpec(
    kind="presentation",
    draft=PresentationDraft,
    system=presentation_prompt.SYSTEM,
    user_message=presentation_prompt.user_message,
    server_blocks=SERVER_BLOCKS,
    # Уровни доходят до слайдов через план, а не напрямую (раздел 4.3)
    levels=False,
)

TASKS = KindSpec(
    kind="tasks",
    draft=TasksDraft,
    system=tasks_prompt.SYSTEM,
    user_message=tasks_prompt.user_message,
    server_blocks=SERVER_BLOCKS,
    # Методрекомендации НАО — в сообщение, а не в системный промпт
    references=method_references("tasks"),
)

HOMEWORK = KindSpec(
    kind="homework",
    draft=HomeworkDraft,
    system=homework_prompt.SYSTEM,
    user_message=homework_prompt.user_message,
    server_blocks=SERVER_BLOCKS,
    references=method_references("homework"),
)

ASSESSMENT = KindSpec(
    kind="assessment",
    draft=AssessmentDraft,
    system=assessment_prompt.SYSTEM,
    user_message=assessment_prompt.user_message,
    server_blocks=SERVER_BLOCKS,
    references=method_references("assessment"),
)

GAME = KindSpec(
    kind="game",
    draft=GameDraft,
    system=game_prompt.SYSTEM,
    user_message=game_prompt.user_message,
    server_blocks=SERVER_BLOCKS,
    # Строительные блоки из каталога механик — в сообщение, не в систему
    references=references,
)

SPECS = (LESSON_PLAN, PRESENTATION, TASKS, HOMEWORK, ASSESSMENT, GAME)


def generators(ai: AiPort, model: str) -> dict[str, MaterialGenerator]:
    found: dict[str, MaterialGenerator] = {
        spec.kind: SchemaGenerator(ai, model, spec) for spec in SPECS
    }
    found["answer_key"] = AnswerKeyAssembler()
    return found
