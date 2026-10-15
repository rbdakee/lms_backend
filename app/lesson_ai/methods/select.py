"""Отбор методических рекомендаций под заказ: пакет по предмету и виду.

Как и каталог игр, рекомендации кладутся в **сообщение пользователя**,
а не в системный промпт: системный промпт один на все заказы и берётся
провайдером из кэша, переменный текст в нём ломал бы кэш на каждом вызове.

Пакет подбирается по `subject_code` урока и виду материала. Нет пакета
под этот предмет — материал генерируется без рекомендаций, как раньше:
каталог дополняет промпт, а не заменяет его.
"""

from collections.abc import Callable

from app.lesson_ai.context import LessonBrief
from app.lesson_ai.methods.method import MethodPack, catalog

# Заголовок встаёт перед текстом рекомендаций в сообщении модели
FRAME = (
    "Методические рекомендации НАО им. Алтынсарина к этому предмету."
    " Это методическая основа заданий и оценивания: критерии, дескрипторы"
    " и правила из них соблюдай; где рекомендации молчат — действуй по"
    " общим правилам из своих инструкций."
)

# Какие разделы пакета показывать модели по видам материала: оценивание
# живёт дескрипторами, задания и домашка — примерами и правилами
SECTIONS: dict[str, tuple[str, ...]] = {
    "assessment": (
        "assessment_structure", "formative", "summative", "generation_notes",
    ),
    "tasks": ("generation_notes", "task_examples"),
    "homework": ("generation_notes", "task_examples"),
}

SECTION_TITLES = {
    "assessment_structure": "Структура оценивания, критерии и дескрипторы",
    "formative": "Формативное оценивание",
    "summative": "Суммативное оценивание",
    "generation_notes": "Правила для этого материала",
    "task_examples": "Примеры заданий (ориентир по форме, не для копирования)",
}


def references(kind: str) -> Callable[[LessonBrief, int], tuple[str, tuple[str, ...]]]:
    """Хук для `KindSpec.references`: текст рекомендаций под вид материала.

    Второй элемент кортежа — слаги для учёта механик игр; у методики
    своего учёта нет, поэтому возвращается пустой кортеж.
    """

    def pick(brief: LessonBrief, seed: int) -> tuple[str, tuple[str, ...]]:
        del seed  # отбор детерминирован: один заказ — один и тот же текст
        packs = [
            pack for pack in catalog()
            if brief.subject_code in pack.subjects and kind in pack.kinds
        ]
        if not packs:
            return "", ()
        return "\n\n".join(_pack(pack, kind) for pack in packs), ()

    return pick


def _pack(pack: MethodPack, kind: str) -> str:
    parts = [pack.summary]
    for section in SECTIONS.get(kind, ("generation_notes",)):
        items = getattr(pack, section)
        if not items:
            continue
        parts.append(
            SECTION_TITLES[section] + ":\n"
            + "\n".join(f"- {item}" for item in items)
        )
    return "\n\n".join(parts)
