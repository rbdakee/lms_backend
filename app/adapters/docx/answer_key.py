"""Ключи с ответами в Word — отдельный файл, который остаётся у учителя.

Ответы собирает сервер из поля `answer` каждой задачи в заданиях, домашнем
и проверочной (TEACHER_AI_BRIEF, раздел 4.2); здесь они только печатаются:
по материалу, внутри — по карточкам и вариантам, с номером задания, как его
видит ребёнок на своём листе.
"""

from io import BytesIO

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from app.adapters.docx import common
from app.adapters.docx.common import TEXT_W, DocHeader

LABELS = {
    "ru": {
        "title": "Ключи с ответами",
        "section": "Раздел",
        "grade": "Класс",
        "topic": "Тема урока",
        "learning_objectives": "Цели обучения",
        "tasks": "Задания на урок",
        "homework": "Домашнее задание",
        "assessment": "Проверочная работа",
        "number": "№",
        "task": "Задание",
        "answer": "Ответ",
    },
    "kz": {
        "title": "Жауаптар кілті",
        "section": "Бөлім",
        "grade": "Сынып",
        "topic": "Сабақтың тақырыбы",
        "learning_objectives": "Оқу мақсаттары",
        "tasks": "Сабаққа арналған тапсырмалар",
        "homework": "Үй тапсырмасы",
        "assessment": "Формативті бағалау",
        "number": "№",
        "task": "Тапсырма",
        "answer": "Жауап",
    },
}

# Подпись блока-источника, если без неё группы не различить: у заданий
# карточки трёх уровней могут называться одинаково. Блоки, которых здесь
# нет, печатаются одним заголовком группы.
BLOCK_LABELS = {
    "ru": {
        "cards_support": "Уровень А",
        "cards_core": "Уровень В",
        "cards_challenge": "Уровень С",
    },
    "kz": {
        "cards_support": "А деңгейі",
        "cards_core": "В деңгейі",
        "cards_challenge": "С деңгейі",
    },
}

SOURCES = ("tasks", "homework", "assessment")
LABEL_W = 5.0
ANSWER_COLS = (1.0, 9.5, 7.0)


def render_answer_key(content: dict, header: DocHeader) -> bytes:
    labels = LABELS[header.lang]
    blocks = BLOCK_LABELS[header.lang]
    doc = Document()
    common.page(doc)
    common.base_style(doc, header.lang)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    common.run(title, labels["title"], bold=True, size=Pt(14))

    section = content.get("section") or {}
    head = common.table(doc, 2, (LABEL_W, TEXT_W - LABEL_W))
    rows = [
        ("section", [section.get("title", ""), section.get("subsection", "")]),
        ("grade", [str(header.grade)]),
        ("topic", [header.topic]),
        (
            "learning_objectives",
            [
                f"{item['code']}{item.get('marker', '')} {item['text']}"
                for item in content.get("learning_objectives") or []
            ],
        ),
    ]
    for kind, values in rows:
        label, value = head.add_row().cells
        common.label(label, labels[kind])
        common.lines(value, [line for line in values if line])
    common.widths(head, (LABEL_W, TEXT_W - LABEL_W))

    for source in SOURCES:
        groups = content.get(source) or []
        if not groups:
            continue
        heading = doc.add_paragraph()
        heading.paragraph_format.space_before = Pt(14)
        common.run(heading, labels[source], bold=True, size=Pt(13))
        for group in groups:
            name = " — ".join(
                part for part in (blocks.get(group.get("block", "")), group.get("title")) if part
            )
            caption = doc.add_paragraph()
            caption.paragraph_format.space_before = Pt(8)
            common.run(caption, name, bold=True)
            table = common.table(doc, 3, ANSWER_COLS)
            first = table.add_row().cells
            for cell, key in zip(first, ("number", "task", "answer"), strict=True):
                common.label(cell, labels[key], center=True)
            for item in group.get("answers") or []:
                number, task, answer = table.add_row().cells
                common.lines(number, [str(item.get("number", ""))])
                common.lines(task, [item.get("task", "")])
                common.lines(answer, [item.get("answer", "")])
            common.widths(table, ANSWER_COLS)

    out = BytesIO()
    doc.save(out)
    return out.getvalue()
