"""Файл материала «Жоспара» по его виду: чем рендерить, какой тип, как назвать.

Рендеры — чистые функции над содержимым материала и шапкой комплекта
(`app/adapters/docx/*`, `app/adapters/pptx/*`). Здесь только выбор по виду:
роутер не должен знать, что план — это Word, а презентация — PowerPoint.
"""

from collections.abc import Callable

from app.adapters.docx.answer_key import render_answer_key
from app.adapters.docx.assessment import render_assessment
from app.adapters.docx.common import DocHeader
from app.adapters.docx.game import render_game
from app.adapters.docx.homework import render_homework
from app.adapters.docx.lesson_plan import render_lesson_plan
from app.adapters.docx.tasks import render_tasks
from app.adapters.pptx.presentation import render_presentation

MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

RENDERERS: dict[str, Callable[[dict, DocHeader], bytes]] = {
    "lesson_plan": render_lesson_plan,
    "presentation": render_presentation,
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
