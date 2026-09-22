"""Проверочная работа в Word: листы ученика двух вариантов и таблица
критериев для учителя.

Чистая функция над готовыми данными, как ҚМЖ: на входе содержимое материала
и шапка из комплекта, на выходе байты .docx. Ни базы, ни FastAPI.

Каждый вариант — с новой страницы и со своей шапкой: учитель печатает
страницу варианта нужным числом копий, и лист ученика не должен зависеть
от соседнего. Таблица критериев — последней страницей, её детям не раздают.
Ответов в файле нет — они в ключах, отдельным файлом (TEACHER_AI_BRIEF,
раздел 4.2).

Критерии, дескрипторы и баллы лежат у первого варианта (схема `assessment`):
второй параллельный, и таблица одна на оба.
"""

from io import BytesIO

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

from app.adapters.docx import common
from app.adapters.docx.common import TEXT_W, DocHeader

LABELS = {
    "ru": {
        "title": "Проверочная работа",
        "name": "Фамилия, имя",
        "grade": "Класс",
        "topic": "Тема",
        "section": "Раздел",
        "learning_objectives": "Цель обучения",
        "answer": "Ответ",
        "self_assessment": "Оцени себя: поставь знак в нужной клетке",
        "done": "Получилось",
        "help": "Нужна помощь",
        "feedback": "Отзыв учителя",
        "teacher_title": "Критерии оценивания и дескрипторы (для учителя)",
        "criterion": "Критерий оценивания",
        "thinking_level": "Уровень мыслительных навыков",
        "task": "Задание",
        "descriptor": "Дескриптор. Обучающийся",
        "points": "Балл",
        "total": "Итого",
        "parallel": (
            "Задания варианта 2 параллельны заданиям варианта 1 с теми же номерами:"
            " критерии, дескрипторы и баллы те же."
        ),
        "levels": {
            "knowledge": "Знание и понимание",
            "application": "Применение",
            "higher_order": "Навыки высокого порядка",
        },
    },
    "kz": {
        "title": "Формативті бағалау",
        "name": "Аты-жөні",
        "grade": "Сынып",
        "topic": "Тақырып",
        "section": "Бөлім",
        "learning_objectives": "Оқу мақсаты",
        "answer": "Жауабы",
        "self_assessment": "Өзіңді бағала: керекті тор көзге белгі қой",
        "done": "Шықты",
        "help": "Көмек керек",
        "feedback": "Мұғалімнің кері байланысы",
        "teacher_title": "Бағалау критерийлері мен дескрипторлар (мұғалімге)",
        "criterion": "Бағалау критерийі",
        "thinking_level": "Ойлау дағдыларының деңгейі",
        "task": "Тапсырма",
        "descriptor": "Дескриптор. Білім алушы",
        "points": "Балл",
        "total": "Барлығы",
        "parallel": (
            "2-нұсқаның тапсырмалары 1-нұсқаның нөмірі бірдей тапсырмаларына параллель:"
            " критерийлер, дескрипторлар және балдар бірдей."
        ),
        "levels": {
            "knowledge": "Білу және түсіну",
            "application": "Қолдану",
            "higher_order": "Жоғары деңгей дағдылары",
        },
    },
}

# Место для ответа от руки: строка подчёркиваний держится на любой печати
ANSWER_LINE = "_" * 60
NAME_LINE = "_" * 45
# Пропуск в условии, куда ребёнок пишет ответ: так его обозначает промпт
BLANK = "___"

SELF_COLS = (TEXT_W - 5.0, 2.5, 2.5)
CRITERIA_COLS = (4.0, 3.0, 1.8, 6.7, 2.0)


def render_assessment(content: dict, header: DocHeader) -> bytes:
    labels = LABELS[header.lang]
    doc = Document()
    common.page(doc)
    common.base_style(doc, header.lang)

    variants = [
        content.get(name) or {}
        for name in ("variant_1", "variant_2")
        if (content.get(name) or {}).get("tasks")
    ]
    for number, variant in enumerate(variants):
        if number:
            doc.add_page_break()
        _student_sheet(doc, content, variant, header, labels)

    if variants:
        doc.add_page_break()
    _criteria(doc, content, labels)

    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def _student_sheet(doc, content: dict, variant: dict, header: DocHeader, labels: dict) -> None:
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    common.run(title, labels["title"], bold=True, size=Pt(14))
    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    common.run(subtitle, variant.get("title") or "", bold=True)

    _head(doc, content, header, labels)

    instructions = content.get("instructions")
    if instructions:
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(8)
        common.run(paragraph, instructions, italic=True)

    # Номер задания — внутри варианта, так же, как его нумеруют ключи
    for number, task in enumerate(variant.get("tasks") or [], start=1):
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(10)
        common.run(paragraph, f"{number}. ", bold=True)
        common.run(paragraph, task.get("text") or "")
        # Столбик примеров несёт пропуски в самом условии — строка ответа
        # под ним лишняя; задаче и вопросу место для ответа нужно
        if BLANK not in (task.get("text") or ""):
            answer = doc.add_paragraph()
            answer.paragraph_format.space_before = Pt(6)
            common.run(answer, f"{labels['answer']}: {ANSWER_LINE}")

    statements = content.get("self_assessment") or []
    if statements:
        heading = doc.add_paragraph()
        heading.paragraph_format.space_before = Pt(14)
        heading.paragraph_format.space_after = Pt(4)
        common.run(heading, labels["self_assessment"], bold=True)
        # Клетки пустые, а не значки: на чёрно-белой печати смайлики
        # сливаются, а галочку в пустой клетке ребёнок ставит сам
        table = common.table(doc, 3, SELF_COLS)
        head = table.add_row().cells
        common.lines(head[0], [""])
        common.label(head[1], labels["done"], center=True)
        common.label(head[2], labels["help"], center=True)
        for statement in statements:
            common.lines(table.add_row().cells[0], [statement])
        common.widths(table, SELF_COLS)

    feedback = doc.add_paragraph()
    feedback.paragraph_format.space_before = Pt(14)
    common.run(feedback, f"{labels['feedback']}: ", bold=True)
    common.run(feedback, ANSWER_LINE[:45])
    common.run(doc.add_paragraph(), ANSWER_LINE + "_" * 12)


def _head(doc, content: dict, header: DocHeader, labels: dict) -> None:
    """Шапка листа ученика: имя — пустым полем, класс, тема, раздел и цель
    обучения. Имя ребёнка в материал не входит ни при каких условиях."""
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
    _objectives(doc, content, labels)


def _objectives(doc, content: dict, labels: dict) -> None:
    """Цель обучения — на листе ученика и над таблицей учителя: завуч сверяет
    с ней и задания, и критерии. Код с пометкой из приказа (`**`) и текст —
    дословно из справочника."""
    for item in content.get("learning_objectives") or []:
        paragraph = doc.add_paragraph()
        common.run(paragraph, f"{labels['learning_objectives']}: ", bold=True, size=Pt(10))
        common.run(
            paragraph,
            f"{item.get('code', '')}{item.get('marker') or ''} {item.get('text', '')}".strip(),
            size=Pt(10),
        )


def _criteria(doc, content: dict, labels: dict) -> None:
    """Таблица для учителя по первому варианту: строка на дескриптор,
    ячейки критерия, уровня и задания склеены по вертикали на все
    дескрипторы задания — так эту таблицу рисуют в школе."""
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_after = Pt(6)
    common.run(title, labels["teacher_title"], bold=True)

    _objectives(doc, content, labels)
    doc.add_paragraph()

    table = common.table(doc, len(CRITERIA_COLS), CRITERIA_COLS)
    head = table.add_row().cells
    keys = ("criterion", "thinking_level", "task", "descriptor", "points")
    for cell, key in zip(head, keys, strict=True):
        common.label(cell, labels[key], center=True)
    common.repeat_header(table.rows[0])

    total = 0
    tasks = (content.get("variant_1") or {}).get("tasks") or []
    for number, task in enumerate(tasks, start=1):
        descriptors = task.get("descriptors") or [{}]
        first = last = None
        for descriptor in descriptors:
            cells = table.add_row().cells
            first = first or cells
            last = cells
            common.lines(cells[3], [descriptor.get("text") or ""])
            points = descriptor.get("points")
            if isinstance(points, int):
                total += points
            common.label(cells[4], "" if points is None else str(points), center=True)
        # Критерий, уровень и номер задания — одна ячейка на все его дескрипторы
        for column, value in (
            (0, task.get("criterion") or ""),
            (1, labels["levels"].get(task.get("thinking_level"), "")),
            (2, str(number)),
        ):
            merged = first[column].merge(last[column]) if first is not last else first[column]
            merged.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
            merged.paragraphs[0].text = ""
            if column == 2:
                common.label(merged, value, center=True)
            else:
                common.lines(merged, [value])

    common.widths(table, CRITERIA_COLS)

    # Строка итога — после ширин: склеенная по горизонтали ячейка получила бы
    # ширину одного столбца, а не четырёх
    cells = table.add_row().cells
    merged = cells[0].merge(cells[3])
    merged.width = Cm(sum(CRITERIA_COLS[:4]))
    common.label(merged, labels["total"])
    cells[4].width = Cm(CRITERIA_COLS[4])
    common.label(cells[4], str(total), center=True)

    if (content.get("variant_2") or {}).get("tasks"):
        note = doc.add_paragraph()
        note.paragraph_format.space_before = Pt(8)
        common.run(note, labels["parallel"], italic=True, size=Pt(10))
