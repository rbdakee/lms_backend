"""Что знает генерация об уроке — двумя разными частями.

`LessonBrief` — всё, что уходит модели. Поля кода цели обучения в нём нет
вовсе, и это граница, а не договорённость: промпт собирается только из брифа,
значит код в него попасть не может даже по ошибке в шаблоне промпта
(TEACHER_AI_BRIEF, раздел 4.1, правило 2).

`Prepared.objectives` и `Prepared.section` — то, что сервер кладёт в документ
сам, дословно из справочника. Модель их не видит в этом виде и не пишет.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session as DbSession

from app.adapters.db.lesson_ai_repos import LessonKitRepo, objective_sections
from app.adapters.db.models import LessonKit
from app.domain.lesson_ai import JobError, strip_objective_codes

LANG_NAMES = {"ru": "русский", "kz": "казахский"}


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


@dataclass(frozen=True)
class Prepared:
    brief: LessonBrief
    # Блоки сервера: раздел программы и цели обучения с кодом — дословно
    section: dict
    objectives: list[dict]


def prepare(db: DbSession, kit: LessonKit) -> Prepared:
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
    )
    return Prepared(
        brief=brief,
        section={"title": section, "subsection": subsection},
        objectives=[{"code": objective.code, "text": objective.text, "marker": objective.marker}],
    )


def describe(brief: LessonBrief) -> str:
    """Урок строками для сообщения модели. Необязательное — только если есть:
    пустая строка «Пожелания учителя:» модель читает как пожелание."""
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
    return "\n".join(lines)
