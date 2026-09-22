"""Ключи с ответами — вид, который собирает сервер, а не модель.

Модели этот вид не заказывается вовсе (TEACHER_AI_BRIEF, раздел 4.2): у каждой
задачи в `tasks`, `homework` и `assessment` есть поле `answer`, заполненное тогда
же, когда задача придумана. Ключи — сводка этих полей. Заодно это на один вызов
модели дешевле.

Задание на ключи ждёт в очереди, пока живы задания на их источники
(`WAITS_FOR`), и собирается из того, что готово: упавшая проверочная
не оставляет учителя без ключей к заданиям.
"""

from sqlalchemy.orm import Session as DbSession

from app.adapters.db.lesson_ai_repos import LessonKitRepo
from app.adapters.db.models import LessonKit
from app.application.lesson_ai_queue import Generated
from app.domain.lesson_ai import ANSWER_SOURCES, JobError, assemble_answer_key
from app.lesson_ai.context import prepare
from app.lesson_ai.schemas import SERVER_BLOCKS


class AnswerKeyAssembler:
    kind = "answer_key"
    # Модели нет — расхода нет
    model = None

    def prepare(self, db: DbSession, kit: LessonKit) -> dict:
        # Публикация проверяется так же, как у видов с моделью: по снятой
        # программе не собирается ничего
        prepared = prepare(db, kit)
        key = assemble_answer_key(LessonKitRepo(db).ready_blocks(kit.id, ANSWER_SOURCES))
        if not key:
            raise JobError("no_sources", retryable=False)
        server = {"section": prepared.section, "learning_objectives": prepared.objectives}
        return {name: server[name] for name in SERVER_BLOCKS} | key

    def generate(self, prepared: dict) -> Generated:
        return Generated(content=prepared)
