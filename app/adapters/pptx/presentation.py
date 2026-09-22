"""Презентация в PowerPoint — слайды, которые учитель правит под свой класс.

Настоящий `.pptx`, а не PDF и не «слайды в Word»: учитель почти всегда
правит слайд, а PDF не правится (TEACHER_AI_BRIEF, раздел 5). Чистая функция
над готовыми данными, как ҚМЖ в Word: на входе содержимое материала и шапка
комплекта, на выходе байты. Ни базы, ни FastAPI, порта нет — python-pptx
библиотека, а не внешняя система.

Оформление нарочно простое: белый фон, чёрный крупный текст, одна тонкая
полоса. Слайд читают с доски в конце класса, а проектор в школе бывает
тусклым — цветной фон и мелкий шрифт там не читаются. Шаблона академии нет;
пришлют — меняется этот файл.

Картинок нет: у слайда есть «что показать», оно уходит в заметки
докладчика вместе с тем, что учитель говорит.
"""

from io import BytesIO

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_AUTO_SIZE, PP_ALIGN
from pptx.util import Emu, Pt

from app.adapters.docx.common import DocHeader

# Arial есть на любой школьной машине и рисует все девять казахских букв
# (ә ғ қ ң ө ұ ү һ і); Times New Roman на доске читается хуже.
FONT = "Arial"
LANG = {"ru": "ru-RU", "kz": "kk-KZ"}
BLACK = RGBColor(0x1A, 0x1A, 0x1A)
GREY = RGBColor(0x6B, 0x6B, 0x6B)
ACCENT = RGBColor(0x2F, 0x5D, 0x8A)

# 16:9 — формат современных проекторов и интерактивных панелей
WIDTH, HEIGHT = Emu(12192000), Emu(6858000)
MARGIN = Emu(609600)

LABELS = {
    "ru": {
        "grade": "класс",
        "goals": "Сегодня на уроке",
        "stage_beginning": "Начало урока",
        "stage_middle": "Середина урока",
        "stage_end": "Конец урока",
        "objective": "Цель обучения",
        "show": "Что показать",
    },
    "kz": {
        "grade": "сынып",
        "goals": "Бүгін сабақта",
        "stage_beginning": "Сабақтың басы",
        "stage_middle": "Сабақтың ортасы",
        "stage_end": "Сабақтың соңы",
        "objective": "Оқу мақсаты",
        "show": "Не көрсету керек",
    },
}

STAGES = (
    ("slides_beginning", "stage_beginning"),
    ("slides_middle", "stage_middle"),
    ("slides_end", "stage_end"),
)


def render_presentation(content: dict, header: DocHeader) -> bytes:
    labels = LABELS[header.lang]
    deck = Presentation()
    deck.slide_width, deck.slide_height = WIDTH, HEIGHT
    blank = deck.slide_layouts[6]

    # Титульный — из комплекта, а не от модели: тема и класс у всех материалов
    # одни. Цель обучения с кодом — в заметках, она для учителя, не для детей
    slide = deck.slides.add_slide(blank)
    _bar(slide)
    _text(
        slide,
        header.topic,
        top=Emu(2286000),
        height=Emu(1600000),
        size=44,
        bold=True,
        lang=header.lang,
        align=PP_ALIGN.CENTER,
    )
    _text(
        slide,
        f"{header.grade} {labels['grade']}",
        top=Emu(3962400),
        height=Emu(700000),
        size=28,
        color=GREY,
        lang=header.lang,
        align=PP_ALIGN.CENTER,
    )
    objectives = [
        f"{item['code']}{item.get('marker', '')} {item['text']}"
        for item in content.get("learning_objectives") or []
    ]
    if objectives:
        _notes(slide, [f"{labels['objective']}:", *objectives])

    goals = content.get("goals") or []
    if goals:
        slide = deck.slides.add_slide(blank)
        _bar(slide)
        _title(slide, labels["goals"], header.lang)
        _body(slide, goals, header.lang)

    for block, stage in STAGES:
        for item in content.get(block) or []:
            slide = deck.slides.add_slide(blank)
            _bar(slide)
            _text(
                slide,
                labels[stage],
                top=Emu(228600),
                height=Emu(380000),
                size=14,
                color=GREY,
                lang=header.lang,
                align=PP_ALIGN.RIGHT,
            )
            _title(slide, item.get("title", ""), header.lang)
            _body(slide, item.get("lines") or [], header.lang)
            notes = [item.get("notes", "")]
            if item.get("visual"):
                notes.append(f"{labels['show']}: {item['visual']}")
            _notes(slide, [line for line in notes if line])

    out = BytesIO()
    deck.save(out)
    return out.getvalue()


def _bar(slide) -> None:
    """Тонкая полоса слева — единственное украшение: видно, что слайды
    из одной презентации, и ничего не мешает читать."""
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Emu(152400), HEIGHT)
    bar.fill.solid()
    bar.fill.fore_color.rgb = ACCENT
    bar.line.fill.background()


def _title(slide, text: str, lang: str) -> None:
    _text(slide, text, top=Emu(609600), height=Emu(1000000), size=40, bold=True, lang=lang)


def _body(slide, lines: list[str], lang: str) -> None:
    # Размер — по объёму текста: слайд на доске читают издалека, и крупнее
    # всегда лучше, пока текст влезает. Сжатие поверх этого включено ниже
    total = sum(len(line) for line in lines)
    size = 36 if total <= 120 else 30 if total <= 240 else 24
    box = slide.shapes.add_textbox(
        MARGIN, Emu(1752600), WIDTH - 2 * MARGIN, HEIGHT - Emu(1752600) - MARGIN
    )
    frame = box.text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    for number, line in enumerate(lines):
        paragraph = frame.paragraphs[0] if number == 0 else frame.add_paragraph()
        paragraph.space_after = Pt(12)
        _run(paragraph, line, size=size, lang=lang)


def _text(
    slide,
    text: str,
    *,
    top,
    height,
    size: int,
    lang: str,
    bold=False,
    color=BLACK,
    align=PP_ALIGN.LEFT,
) -> None:
    box = slide.shapes.add_textbox(MARGIN, top, WIDTH - 2 * MARGIN, height)
    frame = box.text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    paragraph = frame.paragraphs[0]
    paragraph.alignment = align
    _run(paragraph, text, size=size, lang=lang, bold=bold, color=color)


def _run(paragraph, text: str, *, size: int, lang: str, bold=False, color=BLACK) -> None:
    run = paragraph.add_run()
    run.text = text
    font = run.font
    font.name = FONT
    font.size = Pt(size)
    font.bold = bold
    font.color.rgb = color
    # Язык текста — для проверки орфографии: без него PowerPoint подчёркивает
    # казахский слайд целиком как русский с ошибками
    font._rPr.set("lang", LANG[lang])


def _notes(slide, lines: list[str]) -> None:
    slide.notes_slide.notes_text_frame.text = "\n".join(lines)
