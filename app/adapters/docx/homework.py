"""Домашнее задание в Word: лист, который ребёнок уносит домой.

Чистая функция над готовыми данными, как ҚМЖ: на входе содержимое материала
и шапка из комплекта, на выходе байты .docx. Ни базы, ни FastAPI.

Ответов в файле нет: лист уходит ребёнку, а ответы — в ключах, отдельным
файлом (TEACHER_AI_BRIEF, раздел 4.2). Печать чёрно-белая, поэтому игровая
подсказка — рамка, а не цвет.

Язык документа — язык обучения, подписи тоже (раздел 1). Цель обучения
с кодом печатается сверху дословно из справочника: блок `learning_objectives`
модель не пишет.
"""

from io import BytesIO

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from app.adapters.docx import common
from app.adapters.docx.common import TEXT_W, DocHeader

LABELS = {
    "ru": {
        "title": "Домашнее задание",
        "name": "Фамилия, имя",
        "grade": "Класс",
        "topic": "Тема",
        "section": "Раздел",
        "learning_objectives": "Цель обучения",
        "minutes": "Время: около {minutes} мин",
        "answer": "Ответ",
        "parents": "Для родителей",
    },
    "kz": {
        "title": "Үй тапсырмасы",
        "name": "Аты-жөні",
        "grade": "Сынып",
        "topic": "Тақырып",
        "section": "Бөлім",
        "learning_objectives": "Оқу мақсаты",
        "minutes": "Уақыты: шамамен {minutes} мин",
        "answer": "Жауабы",
        "parents": "Ата-аналарға",
    },
}

# Место для ответа от руки: строка подчёркиваний держится на любой печати,
# а пустая ячейка таблицы на листе ребёнка выглядит как ошибка вёрстки
ANSWER_LINE = "_" * 60
NAME_LINE = "_" * 45
# Пропуск в условии, куда ребёнок пишет ответ: так его обозначает промпт
BLANK = "___"


def render_homework(content: dict, header: DocHeader) -> bytes:
    labels = LABELS[header.lang]
    doc = Document()
    common.page(doc)
    common.base_style(doc, header.lang)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    common.run(title, labels["title"], bold=True, size=Pt(14))

    _head(doc, content, header, labels)

    minutes = content.get("minutes")
    if minutes:
        common.run(doc.add_paragraph(), labels["minutes"].format(minutes=minutes), italic=True)

    intro = doc.add_paragraph()
    intro.paragraph_format.space_before = Pt(8)
    common.run(intro, content.get("intro") or "")

    legend = content.get("play_legend") or []
    if legend:
        doc.add_paragraph()
        box = common.table(doc, 1, (TEXT_W,))
        common.lines(box.add_row().cells[0], list(legend))
        common.widths(box, (TEXT_W,))

    for name in ("required", "optional"):
        _group(doc, content.get(name) or {}, labels)

    note = content.get("parent_note")
    if note:
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(18)
        common.run(paragraph, f"{labels['parents']}: ", bold=True, italic=True, size=Pt(11))
        common.run(paragraph, note, italic=True, size=Pt(11))

    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def _head(doc, content: dict, header: DocHeader, labels: dict) -> None:
    """Шапка листа: кто сдаёт, класс и тема, раздел и цель обучения.

    «Фамилия, имя» — пустым полем: листы из одной пачки перепутаются,
    а имя ребёнка в материал не входит ни при каких условиях.
    """
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(8)
    common.run(paragraph, f"{labels['name']}: ", bold=True)
    common.run(paragraph, NAME_LINE)

    paragraph = doc.add_paragraph()
    common.run(paragraph, f"{labels['grade']}: ", bold=True)
    common.run(paragraph, f"{header.grade}    ")
    common.run(paragraph, f"{labels['topic']}: ", bold=True)
    common.run(paragraph, header.topic)

    section = content.get("section") or {}
    parts = [part for part in (section.get("title"), section.get("subsection")) if part]
    if parts:
        paragraph = doc.add_paragraph()
        common.run(paragraph, f"{labels['section']}: ", bold=True, size=Pt(10))
        common.run(paragraph, ". ".join(parts), size=Pt(10))
    # Код с пометкой из приказа (`**`) и текст — дословно из справочника
    for item in content.get("learning_objectives") or []:
        paragraph = doc.add_paragraph()
        common.run(paragraph, f"{labels['learning_objectives']}: ", bold=True, size=Pt(10))
        common.run(
            paragraph,
            f"{item.get('code', '')}{item.get('marker') or ''} {item.get('text', '')}".strip(),
            size=Pt(10),
        )


def _group(doc, group: dict, labels: dict) -> None:
    """Группа задач: заголовок и задания с номером внутри группы — так же,
    как их нумеруют ключи с ответами."""
    tasks = group.get("tasks") or []
    if not tasks:
        return
    heading = doc.add_paragraph()
    heading.paragraph_format.space_before = Pt(12)
    heading.paragraph_format.space_after = Pt(4)
    common.run(heading, group.get("title") or "", bold=True)
    for number, task in enumerate(tasks, start=1):
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(6)
        common.run(paragraph, f"{number}. ", bold=True)
        common.run(paragraph, task.get("text") or "")
        # Столбик примеров несёт пропуски в самом условии — строка ответа
        # под ним лишняя; задаче и вопросу место для ответа нужно
        if BLANK not in (task.get("text") or ""):
            answer = doc.add_paragraph()
            answer.paragraph_format.space_before = Pt(6)
            common.run(answer, f"{labels['answer']}: {ANSWER_LINE}")
