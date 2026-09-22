"""Что знает генерация об уроке — двумя разными частями.

`LessonBrief` — всё, что уходит модели. Поля кода цели обучения в нём нет
вовсе, и это граница, а не договорённость: промпт собирается только из брифа,
значит код в него попасть не может даже по ошибке в шаблоне промпта
(TEACHER_AI_BRIEF, раздел 4.1, правило 2).

`Prepared.objectives` и `Prepared.section` — то, что сервер кладёт в документ
сам, дословно из справочника. Модель их не видит в этом виде и не пишет.

С L5 в брифе урока есть ещё два уровня заказа и готовый план урока: план
идёт первым, остальные виды получают его на вход и обязаны ему соответствовать
(TEACHER_AI_BRIEF, раздел 4.2). План уходит модели без блоков сервера — в них
цель обучения с кодом.
"""

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session as DbSession

from app.adapters.db.lesson_ai_repos import LessonKitRepo, objective_sections
from app.adapters.db.models import LessonKit
from app.domain.lesson_ai import (
    DEFAULT_DIFFICULTY,
    DEFAULT_ENGAGEMENT,
    JobError,
    strip_objective_codes,
)
from app.lesson_ai.schemas.lesson_plan import SERVER_BLOCKS as PLAN_SERVER_BLOCKS

LANG_NAMES = {"ru": "русский", "kz": "казахский"}

# Что значат уровни — словами брифа (раздел 4.3): модели нужен смысл, а не слаг
ENGAGEMENT_NAMES = {
    "calm": (
        "спокойно — дети за партами, задания письменные; игровое тихое:"
        " найди пару, раскрась по ответу, расшифруй слово"
    ),
    "active": ("с соревнованием — по рядам или командам, очки на доске, награда в конце"),
    "full_game": (
        "весь урок — игра-квест: движение по классу, станции, роли, сюжет от начала до конца"
    ),
}
DIFFICULTY_NAMES = {
    "basic": "база — догнать программу: минимум, много повторения, мелкие шаги",
    "standard": "по программе — как в учебнике",
    "advanced": "с запасом — усложнённые задания, олимпиадные вкрапления",
}


@dataclass(frozen=True)
class LessonBrief:
    """Урок глазами модели. Язык — язык обучения, а не интерфейса: учитель
    с русским интерфейсом ведёт казахский класс (раздел 1)."""

    lang: str
    grade: int
    subject: str
    section: str
    subsection: str
    objective: str
    topic: str
    class_size: int | None
    notes: str
    engagement_level: str = DEFAULT_ENGAGEMENT
    difficulty_level: str = DEFAULT_DIFFICULTY
    # Готовый план урока — блоки модели в порядке формы, коды вырезаны.
    # None — план не заказан, упал или это и есть генерация плана
    plan: dict | None = None
    # Строительные блоки из каталога игр — только у игры
    references: str = ""
    # Код предмета из справочника (`math`): по нему каталог игр отбирает
    # механики. Модели не показывается — ей хватает названия
    subject_code: str = ""


@dataclass(frozen=True)
class Prepared:
    brief: LessonBrief
    # Блоки сервера: раздел программы и цели обучения с кодом — дословно
    section: dict
    objectives: list[dict]


def prepare(db: DbSession, kit: LessonKit, *, with_plan: bool = False) -> Prepared:
    """Собрать урок из комплекта и справочника.

    Публикация проверяется здесь ещё раз, а не только при заказе: между
    заказом и генерацией программу могли снять с публикации, и тогда
    генерация по ней закрыта так же, как для нового заказа (раздел 3.1).
    Повтор тут не поможет — отказ окончательный.
    """
    objective, program, subject = LessonKitRepo(db).objective_card(kit.objective_id)
    if program.status != "published":
        raise JobError("program_not_published", retryable=False)
    section, subsection = objective_sections(db, objective)
    brief = LessonBrief(
        lang=kit.lang,
        grade=kit.grade,
        subject=subject.title,
        section=section,
        subsection=subsection,
        # Коды вырезаются из всего, что пишет человек или приказ: учитель мог
        # вписать код в тему, а в тексте цели он мог остаться от склейки ячеек
        objective=strip_objective_codes(objective.text),
        topic=strip_objective_codes(kit.topic),
        class_size=kit.class_size,
        notes=strip_objective_codes(kit.notes),
        engagement_level=kit.engagement_level,
        difficulty_level=kit.difficulty_level,
        plan=_plan(db, kit.id) if with_plan else None,
        subject_code=subject.code,
    )
    return Prepared(
        brief=brief,
        section={"title": section, "subsection": subsection},
        objectives=[{"code": objective.code, "text": objective.text, "marker": objective.marker}],
    )


def _plan(db: DbSession, kit_id: int) -> dict | None:
    """План урока для соседних видов: блоки модели, без цели обучения
    с кодом, и коды вырезаны отовсюду — учитель мог вписать их в план руками."""
    found = LessonKitRepo(db).ready_blocks(kit_id, ("lesson_plan",))
    if not found:
        return None
    _kind, blocks = found[0]
    return {
        name: strip_codes_deep(value) for name, value in blocks if name not in PLAN_SERVER_BLOCKS
    }


def strip_codes_deep(value):
    """Коды целей — из всех строк на любой глубине."""
    if isinstance(value, str):
        return strip_objective_codes(value)
    if isinstance(value, list):
        return [strip_codes_deep(item) for item in value]
    if isinstance(value, dict):
        return {key: strip_codes_deep(item) for key, item in value.items()}
    return value


def describe(brief: LessonBrief, *, levels: bool = True) -> str:
    """Урок строками для сообщения модели. Необязательное — только если есть:
    пустая строка «Пожелания учителя:» модель читает как пожелание.

    `levels=False` — у презентации: уровни доходят до неё через план урока,
    а не напрямую (раздел 4.3), иначе слайды спорили бы с ходом урока.
    """
    lines = [
        f"Предмет: {brief.subject}",
        f"Класс: {brief.grade}",
        f"Язык обучения: {LANG_NAMES[brief.lang]}",
        f"Раздел программы: {brief.section}",
    ]
    if brief.subsection:
        lines.append(f"Подраздел: {brief.subsection}")
    lines += [
        f"Цель обучения по программе: {brief.objective}",
        f"Тема урока: {brief.topic}",
    ]
    if brief.class_size:
        lines.append(f"Детей в классе: {brief.class_size}")
    if brief.notes:
        lines.append(f"Пожелания учителя: {brief.notes}")
    if levels:
        lines += [
            f"Как проводим урок: {ENGAGEMENT_NAMES[brief.engagement_level]}",
            f"Уровень класса: {DIFFICULTY_NAMES[brief.difficulty_level]}",
        ]
    if brief.plan:
        lines.append(
            "\nПлан урока (ҚМЖ), которому материал обязан соответствовать — ход урока,"
            " этапы, задания и ресурсы (JSON):\n"
            + json.dumps(brief.plan, ensure_ascii=False, indent=1)
        )
    return "\n".join(lines)
