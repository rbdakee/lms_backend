"""ҚМЖ в Word: документ, который учитель сдаёт завучу (TEACHER_AI_BRIEF, раздел 5).

Чистая функция над готовыми данными, как сертификат в PDF: на входе содержимое
материала и шапка из комплекта, на выходе байты .docx. Ни базы, ни FastAPI.
Порта нет: python-docx — библиотека, а не внешняя система.

Шаблон — этот код, а не файл .docx с плейсхолдерами. Своего шаблона у академии
пока нет (журнал L4), а таблица хода урока растёт по числу шагов и склеивает
ячейку этапа по вертикали — в коде это три строки, в шаблоне — цикл
по строкам таблицы на языке шаблонизатора.

Язык документа — язык обучения, подписи формы тоже: казахский ҚМЖ казахский
целиком (раздел 1). Цели обучения печатаются как есть, из справочника: блок
`learning_objectives` модель не пишет.
"""

from io import BytesIO

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from app.adapters.docx import common
from app.adapters.docx.common import FONT, TEXT_W, WORD_LANG
from app.adapters.docx.common import DocHeader as PlanHeader
from app.lesson_ai.schemas.lesson_plan import STAGE_MINUTES

# Имена, которые импортируют снаружи: шрифт и язык — тесты казахского документа
__all__ = ["FONT", "LABELS", "WORD_LANG", "PlanHeader", "render_lesson_plan"]

LABELS = {
    "ru": {
        "title": "Краткосрочный план урока",
        "section": "Раздел",
        "teacher": "ФИО педагога",
        "date": "Дата",
        "grade": "Класс",
        "present": "Количество присутствующих",
        "topic": "Тема урока",
        "learning_objectives": (
            "Цели обучения, которые достигаются на данном уроке (ссылка на учебную программу)"
        ),
        "lesson_goals": "Цели урока",
        "assessment_criteria": "Критерии оценивания",
        "language_goals": "Языковые цели",
        "vocabulary": "Предметная лексика и терминология",
        "phrases": "Полезные выражения для диалогов и письма",
        "values": "Привитие ценностей",
        "cross_curricular_links": "Межпредметные связи",
        "prior_knowledge": "Предварительные знания",
        "course": "Ход урока",
        "stage": "Этап урока / время",
        "teacher_actions": "Действия педагога",
        "students_actions": "Действия учеников",
        "resources": "Ресурсы",
        "stage_beginning": "Начало урока",
        "stage_middle": "Середина урока",
        "stage_end": "Конец урока",
        "minutes": "мин",
        "differentiation": "Дифференциация",
        "assessment": "Оценивание",
        "health_and_safety": "Здоровье и соблюдение техники безопасности",
        "reflection": "Рефлексия по уроку",
    },
    "kz": {
        "title": "Қысқа мерзімді жоспар",
        "section": "Бөлім",
        "teacher": "Педагогтің аты-жөні",
        "date": "Күні",
        "grade": "Сынып",
        "present": "Қатысушылар саны",
        "topic": "Сабақтың тақырыбы",
        "learning_objectives": (
            "Осы сабақта қол жеткізілетін оқу мақсаттары (оқу бағдарламасына сілтеме)"
        ),
        "lesson_goals": "Сабақтың мақсаты",
        "assessment_criteria": "Бағалау критерийлері",
        "language_goals": "Тілдік мақсаттар",
        "vocabulary": "Пәнге қатысты сөздік қор және терминология",
        "phrases": "Диалог пен жазылым үшін пайдалы тіркестер",
        "values": "Құндылықтарды дарыту",
        "cross_curricular_links": "Пәнаралық байланыстар",
        "prior_knowledge": "Алдыңғы білім",
        "course": "Сабақтың барысы",
        "stage": "Сабақтың кезеңі / уақыты",
        "teacher_actions": "Педагогтің әрекеті",
        "students_actions": "Оқушылардың әрекеті",
        "resources": "Ресурстар",
        "stage_beginning": "Сабақтың басы",
        "stage_middle": "Сабақтың ортасы",
        "stage_end": "Сабақтың соңы",
        "minutes": "мин",
        "differentiation": "Саралау",
        "assessment": "Бағалау",
        "health_and_safety": "Денсаулық және қауіпсіздік техникасының сақталуы",
        "reflection": "Сабақ бойынша рефлексия",
    },
}

LABEL_W = 5.5
STAGE_COLS = (3.0, 5.5, 5.0, 4.0)

# Блоки шапки, которые пишет модель, — после целей обучения, в порядке формы
TEXT_ROWS = ("values", "cross_curricular_links", "prior_knowledge")
LIST_ROWS = ("lesson_goals", "assessment_criteria")


def render_lesson_plan(content: dict, header: PlanHeader) -> bytes:
    labels = LABELS[header.lang]
    doc = Document()
    common.page(doc)
    common.base_style(doc, header.lang)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    common.run(title, labels["title"], bold=True, size=Pt(14))

    _head_table(doc, content, header, labels)

    course = doc.add_paragraph()
    course.alignment = WD_ALIGN_PARAGRAPH.CENTER
    course.paragraph_format.space_before = Pt(12)
    common.run(course, labels["course"], bold=True)
    _stages_table(doc, content, labels)

    _closing_table(doc, content, labels)

    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def _head_table(doc, content: dict, header: PlanHeader, labels: dict) -> None:
    section = content.get("section") or {}
    rows: list[tuple[str, list[str]]] = [
        ("section", [section.get("title", ""), section.get("subsection", "")]),
        ("teacher", [header.teacher_name]),
        ("date", []),
        ("grade", [str(header.grade)]),
        ("present", []),
        ("topic", [header.topic]),
        # Код с пометкой из приказа (`**`) и текст — дословно из справочника
        (
            "learning_objectives",
            [
                f"{item['code']}{item.get('marker', '')} {item['text']}"
                for item in content.get("learning_objectives") or []
            ],
        ),
    ]
    rows += [(kind, common.bullets(content.get(kind))) for kind in LIST_ROWS]
    table = common.table(doc, 2, (LABEL_W, TEXT_W - LABEL_W))
    for kind, lines in rows:
        label, value = table.add_row().cells
        common.label(label, labels[kind])
        common.lines(value, [line for line in lines if line])

    label, value = table.add_row().cells
    common.label(label, labels["language_goals"])
    goals = content.get("language_goals") or {}
    value.paragraphs[0].text = ""
    first = True
    for part in ("vocabulary", "phrases"):
        paragraph = value.paragraphs[0] if first else value.add_paragraph()
        first = False
        common.run(paragraph, f"{labels[part]}:", italic=True)
        for line in common.bullets(goals.get(part)):
            common.run(value.add_paragraph(), line)

    for kind in TEXT_ROWS:
        label, value = table.add_row().cells
        common.label(label, labels[kind])
        common.lines(value, [content.get(kind) or ""])
    common.widths(table, (LABEL_W, TEXT_W - LABEL_W))


def _stages_table(doc, content: dict, labels: dict) -> None:
    table = common.table(doc, 4, STAGE_COLS)
    head = table.add_row().cells
    for cell, key in zip(
        head, ("stage", "teacher_actions", "students_actions", "resources"), strict=True
    ):
        common.label(cell, labels[key], center=True)
    common.repeat_header(table.rows[0])

    for stage, (start, end) in STAGE_MINUTES.items():
        steps = content.get(stage) or []
        if not steps:
            continue
        first = None
        for step in steps:
            cells = table.add_row().cells
            first = first or cells[0]
            common.lines(cells[1], [step.get("teacher", "")])
            common.lines(cells[2], [step.get("students", "")])
            common.lines(cells[3], list(step.get("resources") or []))
        # Этап — одна ячейка на все его шаги, по вертикали: так ҚМЖ
        # выглядит на бумаге, и время этапа не повторяется у каждого шага
        merged = first.merge(cells[0]) if len(steps) > 1 else first
        merged.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
        paragraph = merged.paragraphs[0]
        paragraph.text = ""
        common.run(paragraph, labels[stage], bold=True)
        common.run(merged.add_paragraph(), f"{start}–{end} {labels['minutes']}")
    common.widths(table, STAGE_COLS)


def _closing_table(doc, content: dict, labels: dict) -> None:
    doc.add_paragraph()
    kinds = ("differentiation", "assessment", "health_and_safety")
    width = TEXT_W / len(kinds)
    table = common.table(doc, len(kinds), (width,) * len(kinds))
    head = table.add_row().cells
    for cell, kind in zip(head, kinds, strict=True):
        common.label(cell, labels[kind], center=True)
    body = table.add_row().cells
    for cell, kind in zip(body, kinds, strict=True):
        common.lines(cell, [content.get(kind) or ""])
    common.widths(table, (width,) * len(kinds))

    doc.add_paragraph()
    table = common.table(doc, 2, (LABEL_W, TEXT_W - LABEL_W))
    label, value = table.add_row().cells
    common.label(label, labels["reflection"])
    common.lines(value, common.bullets(content.get("reflection")))
    common.widths(table, (LABEL_W, TEXT_W - LABEL_W))
