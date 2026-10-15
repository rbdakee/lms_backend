"""Файл материала «Жоспара» по его виду: чем рендерить, какой тип, как назвать.

Рендеры — чистые функции над содержимым материала и шапкой комплекта
(`app/adapters/docx/*`, `app/adapters/pptx/*`). Здесь только выбор по виду:
роутер не должен знать, что план — это Word, а презентация — PowerPoint.
"""

import logging
from collections.abc import Callable

from app.adapters.docx.answer_key import render_answer_key
from app.adapters.docx.assessment import render_assessment
from app.adapters.docx.common import DocHeader
from app.adapters.docx.game import render_game
from app.adapters.docx.homework import render_homework
from app.adapters.docx.lesson_plan import render_lesson_plan
from app.adapters.docx.tasks import render_tasks
from app.adapters.pptx import deck
from app.adapters.pptx.presentation import render_presentation

log = logging.getLogger(__name__)

MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

def render_presentation_file(content: dict, header: DocHeader) -> bytes:
    """Презентация: слайды-сценарии собираются из библиотеки шаблонов
    (`deck.render_deck`), материалы прежней формы — старым путём. Форма
    определяется по содержимому, а не по дате: старые комплекты живут
    в базе вечно. Сценарий без библиотеки возможен только при откате
    деплоя — тогда он собирается старым путём с ошибкой в логе: гасить
    выгрузку готового материала дороже, чем отдать упрощённый слайд."""
    from app.lesson_ai import pptx_templates

    if not deck.is_scenario(content):
        return render_presentation(content, header)
    library = pptx_templates.root()
    if not pptx_templates.available(library):
        log.error("презентация-сценарий без библиотеки шаблонов (%s) — сборка по-старому", library)
        return render_presentation(content, header)
    result = deck.render_deck(content, header, library)
    for warning in result.warnings:
        log.warning("презентация: %s", warning)
    return result.data


RENDERERS: dict[str, Callable[[dict, DocHeader], bytes]] = {
    "lesson_plan": render_lesson_plan,
    "presentation": render_presentation_file,
    "tasks": render_tasks,
    "homework": render_homework,
    "assessment": render_assessment,
    "game": render_game,
    "answer_key": render_answer_key,
}

# Имя файла — на языке обучения: учитель складывает материалы в одну папку,
# и по номеру их не различить. В русском план — «Краткосрочный план» (КСП),
# в казахском — «ҚМЖ» (решение владельца 15.10.2026).
TITLES = {
    "ru": {
        "lesson_plan": "Краткосрочный план",
        "presentation": "Презентация",
        "tasks": "Задания",
        "homework": "Домашнее задание",
        "assessment": "Проверочная работа",
        "game": "Игра",
        "answer_key": "Ключи с ответами",
    },
    "kz": {
        "lesson_plan": "ҚМЖ",
        "presentation": "Презентация",
        "tasks": "Тапсырмалар",
        "homework": "Үй тапсырмасы",
        "assessment": "Формативті бағалау",
        "game": "Ойын",
        "answer_key": "Жауаптар кілті",
    },
}


def render(kind: str, content: dict, header: DocHeader) -> bytes:
    return RENDERERS[kind](content, header)
