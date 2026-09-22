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

from dataclasses import dataclass
from io import BytesIO

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from docx.table import _Cell

from app.lesson_ai.schemas.lesson_plan import STAGE_MINUTES

# Times New Roman — шрифт школьной документации, и в Word, и в LibreOffice
# в нём есть все девять казахских букв (ә ғ қ ң ө ұ ү һ і). Имя шрифта
# ставится во все четыре слота rFonts: Word выбирает слот по диапазону
# символа, и неназначенный слот подменял бы кириллицу шрифтом темы.
FONT = "Times New Roman"
FONT_SIZE = Pt(12)

# Язык текста для проверки орфографии в Word: без него казахский документ
# подчёркивается красным целиком как «русский с ошибками».
WORD_LANG = {"ru": "ru-RU", "kz": "kk-KZ"}

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

# Ширина текста на A4 книжном при полях 2 см слева и 1,5 см справа.
TEXT_W = 21.0 - 2.0 - 1.5
LABEL_W = 5.5
STAGE_COLS = (3.0, 5.5, 5.0, 4.0)

# Блоки шапки, которые пишет модель, — после целей обучения, в порядке формы
TEXT_ROWS = ("values", "cross_curricular_links", "prior_knowledge")
LIST_ROWS = ("lesson_goals", "assessment_criteria")


@dataclass(frozen=True)
class PlanHeader:
    """Шапка ҚМЖ — поля комплекта и учителя, а не содержимого материала:
    ФИО в содержимое не входит, иначе уехало бы в банк готовых материалов.

    Даты урока и числа присутствующих в заказе нет — в документе эти
    строки пустые, учитель впишет их от руки.
    """

    lang: str
    teacher_name: str
    grade: int
    topic: str


def render_lesson_plan(content: dict, header: PlanHeader) -> bytes:
    labels = LABELS[header.lang]
    doc = Document()
    _page(doc)
    _base_style(doc, header.lang)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(title, labels["title"], bold=True, size=Pt(14))

    _head_table(doc, content, header, labels)

    course = doc.add_paragraph()
    course.alignment = WD_ALIGN_PARAGRAPH.CENTER
    course.paragraph_format.space_before = Pt(12)
    _run(course, labels["course"], bold=True)
    _stages_table(doc, content, labels)

    _closing_table(doc, content, labels)

    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def _page(doc) -> None:
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21.0), Cm(29.7)
    section.left_margin, section.right_margin = Cm(2.0), Cm(1.5)
    section.top_margin, section.bottom_margin = Cm(1.5), Cm(1.5)


def _base_style(doc, lang: str) -> None:
    style = doc.styles["Normal"]
    style.font.name = FONT
    style.font.size = FONT_SIZE
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.get_or_add_rFonts()
    for slot in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        fonts.set(qn(slot), FONT)
    word_lang = OxmlElement("w:lang")
    word_lang.set(qn("w:val"), WORD_LANG[lang])
    rpr.append(word_lang)
    style.paragraph_format.space_after = Pt(0)
    style.paragraph_format.space_before = Pt(0)


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
    rows += [(kind, _bullets(content.get(kind))) for kind in LIST_ROWS]
    table = _table(doc, 2, (LABEL_W, TEXT_W - LABEL_W))
    for kind, lines in rows:
        label, value = table.add_row().cells
        _label(label, labels[kind])
        _lines(value, [line for line in lines if line])

    label, value = table.add_row().cells
    _label(label, labels["language_goals"])
    goals = content.get("language_goals") or {}
    value.paragraphs[0].text = ""
    first = True
    for part in ("vocabulary", "phrases"):
        paragraph = value.paragraphs[0] if first else value.add_paragraph()
        first = False
        _run(paragraph, f"{labels[part]}:", italic=True)
        for line in _bullets(goals.get(part)):
            _run(value.add_paragraph(), line)

    for kind in TEXT_ROWS:
        label, value = table.add_row().cells
        _label(label, labels[kind])
        _lines(value, [content.get(kind) or ""])
    _widths(table, (LABEL_W, TEXT_W - LABEL_W))


def _stages_table(doc, content: dict, labels: dict) -> None:
    table = _table(doc, 4, STAGE_COLS)
    head = table.add_row().cells
    for cell, key in zip(
        head, ("stage", "teacher_actions", "students_actions", "resources"), strict=True
    ):
        _label(cell, labels[key], center=True)
    _repeat_header(table.rows[0])

    for stage, (start, end) in STAGE_MINUTES.items():
        steps = content.get(stage) or []
        if not steps:
            continue
        first = None
        for step in steps:
            cells = table.add_row().cells
            first = first or cells[0]
            _lines(cells[1], [step.get("teacher", "")])
            _lines(cells[2], [step.get("students", "")])
            _lines(cells[3], list(step.get("resources") or []))
        # Этап — одна ячейка на все его шаги, по вертикали: так ҚМЖ
        # выглядит на бумаге, и время этапа не повторяется у каждого шага
        merged = first.merge(cells[0]) if len(steps) > 1 else first
        merged.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
        paragraph = merged.paragraphs[0]
        paragraph.text = ""
        _run(paragraph, labels[stage], bold=True)
        _run(merged.add_paragraph(), f"{start}–{end} {labels['minutes']}")
    _widths(table, STAGE_COLS)


def _closing_table(doc, content: dict, labels: dict) -> None:
    doc.add_paragraph()
    kinds = ("differentiation", "assessment", "health_and_safety")
    width = TEXT_W / len(kinds)
    table = _table(doc, len(kinds), (width,) * len(kinds))
    head = table.add_row().cells
    for cell, kind in zip(head, kinds, strict=True):
        _label(cell, labels[kind], center=True)
    body = table.add_row().cells
    for cell, kind in zip(body, kinds, strict=True):
        _lines(cell, [content.get(kind) or ""])
    _widths(table, (width,) * len(kinds))

    doc.add_paragraph()
    table = _table(doc, 2, (LABEL_W, TEXT_W - LABEL_W))
    label, value = table.add_row().cells
    _label(label, labels["reflection"])
    _lines(value, _bullets(content.get("reflection")))
    _widths(table, (LABEL_W, TEXT_W - LABEL_W))


def _table(doc, cols: int, widths: tuple[float, ...]):
    table = doc.add_table(rows=0, cols=cols)
    table.autofit = False
    _borders(table)
    for column, width in zip(table.columns, widths, strict=True):
        column.width = Cm(width)
    return table


def _borders(table) -> None:
    """Рамки — прямо в таблице, а не стилем «Table Grid»: стиль живёт
    в шаблоне документа, и открывший файл не в Word (Pages, просмотр
    в почте) видит таблицу без единой линии."""
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        line = OxmlElement(f"w:{edge}")
        line.set(qn("w:val"), "single")
        line.set(qn("w:sz"), "4")
        line.set(qn("w:space"), "0")
        line.set(qn("w:color"), "000000")
        borders.append(line)
    # Порядок детей tblPr задан схемой OOXML, и Word на нарушенном порядке
    # предлагает «восстановить повреждённый файл»: рамки встают перед
    # раскладкой и полями ячеек, а не в конец
    for tag in ("w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook"):
        after = tbl_pr.find(qn(tag))
        if after is not None:
            after.addprevious(borders)
            return
    tbl_pr.append(borders)


def _widths(table, widths: tuple[float, ...]) -> None:
    """Ширина — ещё и каждой ячейке: Word верстает по ширине ячеек, а сетку
    столбцов читает только LibreOffice."""
    for row in table.rows:
        for cell, width in zip(row.cells, widths, strict=True):
            cell.width = Cm(width)


def _repeat_header(row) -> None:
    """Шапка таблицы хода урока повторяется на каждой странице: таблица
    почти всегда переезжает на вторую."""
    tr_pr = row._tr.get_or_add_trPr()
    flag = OxmlElement("w:tblHeader")
    flag.set(qn("w:val"), "true")
    tr_pr.append(flag)


def _label(cell: _Cell, text: str, *, center: bool = False) -> None:
    paragraph = cell.paragraphs[0]
    if center:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(paragraph, text, bold=True)


def _lines(cell: _Cell, lines: list[str]) -> None:
    """Строки — отдельными абзацами ячейки. Пустой список — пустая ячейка:
    её заполнит учитель от руки."""
    for number, line in enumerate(lines):
        paragraph = cell.paragraphs[0] if number == 0 else cell.add_paragraph()
        _run(paragraph, line)


def _bullets(items) -> list[str]:
    return [f"• {item}" for item in items or []]


def _run(paragraph, text: str, *, bold=False, italic=False, size=None) -> None:
    run = paragraph.add_run(text)
    run.bold = bold
    run.italic = italic
    if size is not None:
        run.font.size = size
