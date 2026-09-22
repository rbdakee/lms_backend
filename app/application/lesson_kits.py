"""Комплект к уроку глазами учителя: заказать и следить, как он собирается.

Генерации здесь нет: ручка заводит комплект, материалы и задания на них,
а собирает их очередь (`lesson_ai_queue.py`). Учитель опрашивает комплект,
пока у всех материалов не закончится ожидание (TEACHER_AI_BRIEF, раздел 7).

Комплект виден только своему учителю: чужой — 404, а не 403, чтобы по
номерам нельзя было перебрать, чьи комплекты существуют.
"""

from app.adapters.db.lesson_ai_repos import LessonKitRepo
from app.adapters.db.models import LessonKit, User
from app.domain.errors import FieldError, NotFoundError, ProgramNotPublishedError
from app.domain.lesson_ai import (
    ANSWER_SOURCES,
    DEFAULT_DIFFICULTY,
    DEFAULT_ENGAGEMENT,
    JOB_ERROR_MESSAGES,
    MATERIAL_KINDS,
)


class LessonKitsService:
    def __init__(self, kits: LessonKitRepo):
        self.kits = kits

    # -- POST /lesson_ai/kits ----------------------------------------------

    def create(
        self,
        user: User,
        *,
        objective_id: int,
        topic: str,
        kinds: list[str],
        class_size: int | None,
        notes: str,
        engagement_level: str = DEFAULT_ENGAGEMENT,
        difficulty_level: str = DEFAULT_DIFFICULTY,
    ) -> dict:
        topic = topic.strip()
        if not topic:
            raise FieldError("topic", "Тема урока не может быть пустой")
        # Ключи собираются из ответов к заданиям, а не генерируются: без
        # заданий, домашней или проверочной им не из чего взяться (раздел 4.2)
        if "answer_key" in kinds and not set(kinds) & set(ANSWER_SOURCES):
            raise FieldError(
                "kinds",
                "Ключи с ответами собираются из заданий, домашнего задания"
                " или проверочной — отметьте хотя бы одно из них",
            )
        card = self.kits.objective_card(objective_id)
        if card is None:
            raise NotFoundError("Цель обучения не найдена")
        objective, program, _subject = card
        # Генерация видит только опубликованное: загруженная программа ещё
        # не вычитана владельцем (раздел 3.1)
        if program.status != "published":
            raise ProgramNotPublishedError()
        kit = self.kits.create_kit(
            user_id=user.id,
            objective_id=objective.id,
            topic=topic,
            # Класс и язык — из цели: язык документов равен языку обучения,
            # а цель взята из редакции приказа на этом языке (раздел 1)
            grade=objective.grade,
            lang=program.lang,
            class_size=class_size,
            notes=notes.strip(),
            engagement_level=engagement_level,
            difficulty_level=difficulty_level,
        )
        # Порядок видов — из брифа, а не из запроса: план урока идёт первым,
        # остальные материалы получат его на вход (раздел 4.2)
        for kind in sorted(set(kinds), key=MATERIAL_KINDS.index):
            self.kits.add_material(kit.id, kind)
        return self._out(kit)

    # -- GET /lesson_ai/kits/{id} ------------------------------------------

    def kit(self, user: User, kit_id: int) -> dict:
        kit = self.kits.kit(kit_id)
        if kit is None or kit.user_id != user.id:
            raise NotFoundError("Комплект не найден")
        return self._out(kit)

    def _out(self, kit: LessonKit) -> dict:
        objective, _program, subject = self.kits.objective_card(kit.objective_id)
        errors = self.kits.last_errors(kit.id)
        return {
            "id": kit.id,
            "objective": {
                "id": objective.id,
                "code": objective.code,
                "text": objective.text,
                "marker": objective.marker,
            },
            "subject_code": subject.code,
            "grade": kit.grade,
            "lang": kit.lang,
            "topic": kit.topic,
            "class_size": kit.class_size,
            "notes": kit.notes,
            "engagement_level": kit.engagement_level,
            "difficulty_level": kit.difficulty_level,
            "status": kit.status,
            "created_at": kit.created_at,
            "updated_at": kit.updated_at,
            "materials": [
                {
                    "id": material.id,
                    "kind": material.kind,
                    "status": material.status,
                    # Отказ показывается только у упавшего материала: у ждущего
                    # повтора он ещё не приговор
                    "error": _error(errors.get(material.kind))
                    if material.status == "failed"
                    else None,
                    "content": material.content,
                    "updated_at": material.updated_at,
                }
                for material in self.kits.materials(kit.id)
            ],
        }


def _error(code: str | None) -> dict | None:
    if code is None:
        return None
    message = JOB_ERROR_MESSAGES.get(code, JOB_ERROR_MESSAGES["internal_error"])
    return {"code": code, "message": message}
