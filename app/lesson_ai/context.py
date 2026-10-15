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

С L7 генерация бывает обезличенной (`bank=True`, комплект без пожеланий):
её результат ложится в банк готовых материалов и выдаётся другим учителям
(раздел 7). Такая модель не видит ни темы, ни числа детей — обезличенность
держится тем, что личного в брифе нет, а не чисткой готового текста.
"""

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session as DbSession

from app.adapters.db.lesson_ai_repos import LessonKitRepo, objective_sections
from app.adapters.db.lesson_bank_repo import LessonBankRepo, document
from app.adapters.db.models import LessonKit
from app.application.lesson_ai_queue import BankSlot
from app.domain.lesson_ai import (
    DEFAULT_DIFFICULTY,
    DEFAULT_ENGAGEMENT,
    JobError,
    bankable,
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
    # Тексты всех целей урока по порядку учителя, коды вырезаны
    objectives: tuple[str, ...]
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
    # Куда в банке ляжет результат. None — генерация личная, в банк не идёт
    bank: BankSlot | None = None


def prepare(
    db: DbSession, kit: LessonKit, *, with_plan: bool = False, bank: bool = False
) -> Prepared:
    """Собрать урок из комплекта и справочника.

    Публикация проверяется здесь ещё раз, а не только при заказе: между
    заказом и генерацией программу могли снять с публикации, и тогда
    генерация по ней закрыта так же, как для нового заказа (раздел 3.1).
    Повтор тут не поможет — отказ окончательный.

    `bank=True` — генерация всего материала очередью: у комплекта без
    пожеланий она обезличенная и ложится в банк. Перегенерация блока
    и ключи с ответами зовут без него — они личные и в банк не идут.
    """
    cards = LessonKitRepo(db).kit_objectives([kit.id])[kit.id]
    _first, program, subject = cards[0]
    objective_ids = tuple(card[0].id for card in cards)
    if program.status != "published":
        raise JobError("program_not_published", retryable=False)
    # Цели одной программы, но разделов у них может быть несколько: в документ
    # идут все, без повторов, в порядке целей
    sections = [objective_sections(db, card[0]) for card in cards]
    section = "; ".join(dict.fromkeys(title for title, _ in sections))
    subsection = "; ".join(dict.fromkeys(sub for _, sub in sections if sub))
    impersonal = bank and bankable(kit.notes)
    if impersonal:
        slot, plan = _bank_slot(db, kit, objective_ids, with_plan=with_plan)
    else:
        slot, plan = None, (_plan(db, kit.id) if with_plan else None)
    brief = LessonBrief(
        lang=kit.lang,
        grade=kit.grade,
        subject=subject.title,
        section=section,
        subsection=subsection,
        # Коды вырезаются из всего, что пишет человек или приказ: учитель мог
        # вписать код в тему, а в тексте цели он мог остаться от склейки ячеек
        objectives=tuple(strip_objective_codes(card[0].text) for card in cards),
        # Тема и число детей — личное учителя: в банковую генерацию не идут,
        # их учитель правит поверх выданного (раздел 7)
        topic="" if impersonal else strip_objective_codes(kit.topic),
        class_size=None if impersonal else kit.class_size,
        notes=strip_objective_codes(kit.notes),
        engagement_level=kit.engagement_level,
        difficulty_level=kit.difficulty_level,
        plan=plan,
        subject_code=subject.code,
    )
    return Prepared(
        brief=brief,
        section={"title": section, "subsection": subsection},
        objectives=[
            {"code": card[0].code, "text": card[0].text, "marker": card[0].marker}
            for card in cards
        ],
        bank=slot,
    )


def _bank_slot(
    db: DbSession, kit: LessonKit, objective_ids: tuple[int, ...], *, with_plan: bool
) -> tuple[BankSlot | None, dict | None]:
    """Слот банка и план урока для обезличенной генерации.

    План соседи берут из строки банка, а не из материала учителя: учитель
    мог уже поправить свой план, и через соседа личное утекло бы в банк.
    План учителя готов, но не из банка (сделан до банка или вариант удалён) —
    построенный по нему материал в банк не кладётся: ключа плана у него нет.
    Плана в комплекте нет или он не готов — это законный вариант «без плана».

    Вид в слоте пустой: общий сбор урока не знает, какой вид его заказал, —
    вписывает генератор вида.
    """
    plan_bank_id = None
    plan = None
    if with_plan:
        bank = LessonBankRepo(db)
        material = bank.kit_plan(kit.id)
        if material is not None and material.status == "ready":
            variant = bank.variant(material.bank_id) if material.bank_id else None
            if variant is None:
                return None, _plan(db, kit.id)
            plan_bank_id = variant.id
            plan = _model_blocks(document(variant).items())
    slot = BankSlot(
        objective_ids=objective_ids,
        kind="",
        lang=kit.lang,
        engagement_level=kit.engagement_level,
        difficulty_level=kit.difficulty_level,
        plan_bank_id=plan_bank_id,
    )
    return slot, plan


def _plan(db: DbSession, kit_id: int) -> dict | None:
    """План урока для соседних видов из материала учителя — текущий,
    с его правками."""
    found = LessonKitRepo(db).ready_blocks(kit_id, ("lesson_plan",))
    if not found:
        return None
    _kind, blocks = found[0]
    return _model_blocks(blocks)


def _model_blocks(blocks) -> dict:
    """Блоки модели без цели обучения с кодом, и коды вырезаны отовсюду —
    учитель мог вписать их в план руками."""
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
    if len(brief.objectives) == 1:
        lines.append(f"Цель обучения по программе: {brief.objectives[0]}")
    else:
        # Урок идёт по нескольким целям сразу: материал обязан покрыть каждую
        lines.append("Цели обучения по программе (урок идёт по всем сразу, материал должен"
                     " покрыть каждую):")
        lines += [f"{number}. {text}" for number, text in enumerate(brief.objectives, start=1)]
    # Темы нет у обезличенной генерации для банка: материал пишется по цели
    if brief.topic:
        lines.append(f"Тема урока: {brief.topic}")
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
