"""Презентация в PowerPoint — слайды, которые учитель правит под свой класс.

Настоящий `.pptx`, а не PDF и не «слайды в Word»: учитель почти всегда
правит слайд, а PDF не правится (TEACHER_AI_BRIEF, раздел 5). Чистая функция
над готовыми данными, как ҚМЖ в Word: на входе содержимое материала и шапка
комплекта, на выходе байты. Ни базы, ни FastAPI, порта нет — python-pptx
библиотека, а не внешняя система.

Оформление собрано кодом, внешнего `.potx` нет. Слайд читают с доски с пяти
метров, а проектор в школе бывает тусклым: крупный кегль (не меньше 22 pt
в тексте), тёмный текст на светлых плашках, один цвет акцента на этап урока.
Восемь типов слайдов (`schemas/presentation.py`) — восемь функций ниже; слайд
без типа — старый, он собирается как `image_text`. Анимации и переход —
`animation.py`. Цвета одни на обе площадки: нейтральная палитра, без бренда
(площадка в шапку документа не доходит).

Картинка — PNG из каталога (`app/lesson_ai/images`); ключа нет в каталоге —
слайд без картинки. «Что показать» уходит в заметки докладчика, как раньше.

Размер шрифта подбирается по объёму текста (`_fit`): PowerPoint «ужимает под
рамку» только при правке, а не при открытии, и без своего расчёта длинная
строка вылезла бы за плашку.
"""

from dataclasses import dataclass, field
from io import BytesIO
from math import ceil

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

from app.adapters.docx.common import DocHeader
from app.adapters.pptx.animation import Target, animate, fade_transition
from app.lesson_ai import images
from app.lesson_ai.schemas.presentation import DEFAULT_ANIMATION

# Arial есть на любой школьной машине и рисует все девять казахских букв
# (ә ғ қ ң ө ұ ү һ і); Times New Roman на доске читается хуже.
FONT = "Arial"
LANG = {"ru": "ru-RU", "kz": "kk-KZ"}

NAVY = "1B2A49"
INK = "1F2937"
WHITE = "FFFFFF"
SOFT = "CBD5E1"
PAPER = "FBFAF7"
GREEN = "1E9E5A"
GOLD = "F5B700"
# Акцент и светлая плашка по этапу урока: цвет подсказывает учителю, где он
# в уроке, и не несёт смысла для детей
STAGE_COLORS = {
    "goals": ("6B4FBB", "F0ECFA"),
    "stage_beginning": ("1E9E5A", "E8F6EE"),
    "stage_middle": ("2B59C3", "EAF1FD"),
    "stage_end": ("E8801A", "FDF0E2"),
}

# 16:9 — формат современных проекторов и интерактивных панелей
WIDTH, HEIGHT = Emu(12192000), Emu(6858000)
W_IN = 13.333
MARGIN = 0.6
CONTENT_TOP, CONTENT_H = 1.75, 5.0
FULL_W = W_IN - 2 * MARGIN
SPLIT_W = 7.5  # ширина текста, когда справа картинка

LABELS = {
    "ru": {
        "grade": "класс",
        "goals": "Сегодня на уроке",
        "stage_beginning": "Начало урока",
        "stage_middle": "Середина урока",
        "stage_end": "Конец урока",
        "objective": "Цель обучения",
        "show": "Что показать",
        "answer": "Ответ",
        "letters": "АБВГ",
    },
    "kz": {
        "grade": "сынып",
        "goals": "Бүгін сабақта",
        "stage_beginning": "Сабақтың басы",
        "stage_middle": "Сабақтың ортасы",
        "stage_end": "Сабақтың соңы",
        "objective": "Оқу мақсаты",
        "show": "Не көрсету керек",
        "answer": "Жауап",
        "letters": "АӘБВ",
    },
}

STAGES = (
    ("slides_beginning", "stage_beginning"),
    ("slides_middle", "stage_middle"),
    ("slides_end", "stage_end"),
)


@dataclass
class Ctx:
    """Что нужно слайду, чтобы нарисоваться: сам слайд, язык, цвета этапа и
    щелчки анимации, которые слайд насобирал по ходу."""

    slide: object
    lang: str
    labels: dict
    stage: str
    accent: str
    tint: str
    clicks: list[list[Target]] = field(default_factory=list)


def render_presentation(content: dict, header: DocHeader) -> bytes:
    labels = LABELS[header.lang]
    deck = Presentation()
    deck.slide_width, deck.slide_height = WIDTH, HEIGHT
    blank = deck.slide_layouts[6]

    # Титульный — из комплекта, а не от модели: тема и класс у всех материалов
    # одни. Цель обучения с кодом — в заметках, она для учителя, не для детей
    title = {
        "layout": "title",
        "title": header.topic,
        "lines": [f"{header.grade} {labels['grade']}"],
        "animation": "none",
    }
    slide = _build(deck, blank, title, header.lang, "goals")
    objectives = [
        f"{item['code']}{item.get('marker', '')} {item['text']}"
        for item in content.get("learning_objectives") or []
    ]
    if objectives:
        _notes(slide, [f"{labels['objective']}:", *objectives])

    goals = content.get("goals") or []
    if goals:
        agenda = {"layout": "agenda", "title": labels["goals"], "lines": goals}
        _build(deck, blank, agenda, header.lang, "goals")

    for block, stage in STAGES:
        for item in content.get(block) or []:
            slide = _build(deck, blank, item, header.lang, stage)
            notes = [item.get("notes", "")]
            if item.get("visual"):
                notes.append(f"{labels['show']}: {item['visual']}")
            _notes(slide, [line for line in notes if line])

    out = BytesIO()
    deck.save(out)
    return out.getvalue()


def _build(deck, blank, item: dict, lang: str, stage: str):
    slide = deck.slides.add_slide(blank)
    accent, tint = STAGE_COLORS[stage]
    ctx = Ctx(slide, lang, LABELS[lang], stage, accent, tint)
    layout = item.get("layout") or "image_text"
    # Слайд, у которого нет поля, — старый: умолчание по типу, как в схеме
    animation = item.get("animation") or DEFAULT_ANIMATION.get(layout, "appear")
    LAYOUTS.get(layout, _image_text)(ctx, item)
    if animation != "none" and ctx.clicks:
        animate(slide, ctx.clicks, animation)
        fade_transition(slide)
    return slide


# -- типы слайдов ----------------------------------------------------------------


def _title_slide(ctx: Ctx, item: dict) -> None:
    ctx.slide.background.fill.solid()
    ctx.slide.background.fill.fore_color.rgb = RGBColor.from_string(NAVY)
    image = _image(item)
    width = 7.9 if image else W_IN - 2 * 0.9
    _put(
        ctx,
        item.get("title", ""),
        0.9,
        1.6,
        width,
        2.6,
        sizes=(60, 54, 48, 42, 36),
        bold=True,
        color=WHITE,
        anchor=MSO_ANCHOR.BOTTOM,
    )
    _rect(ctx.slide, 0.9, 4.4, 1.8, 0.1, GOLD, rounded=False)
    _put(
        ctx,
        item.get("lines") or [],
        0.9,
        4.65,
        width,
        1.9,
        sizes=(32, 28, 26),
        color=SOFT,
        spacing=6,
    )
    if image:
        _picture(ctx, image, 9.0, 1.9, 3.9, "2D4373")


def _agenda(ctx: Ctx, item: dict) -> None:
    _frame(ctx, item)
    _list_plate(ctx, item, bullet="num")


def _image_text(ctx: Ctx, item: dict) -> None:
    _frame(ctx, item)
    _list_plate(ctx, item, bullet="dot")


def _two_columns(ctx: Ctx, item: dict) -> None:
    _frame(ctx, item)
    gap = 0.3
    width = (FULL_W - gap) / 2
    columns = (
        (item.get("left_title", ""), item.get("lines") or []),
        (item.get("right_title", ""), item.get("right_lines") or []),
    )
    for number, (head, lines) in enumerate(columns):
        x = MARGIN + number * (width + gap)
        _rect(ctx.slide, x, CONTENT_TOP, width, CONTENT_H, ctx.tint)
        body_top = CONTENT_TOP + 0.25
        if head:
            _put(
                ctx,
                head,
                x,
                CONTENT_TOP,
                width,
                1.0,
                sizes=(34, 30, 26),
                bold=True,
                color=WHITE,
                fill=ctx.accent,
                align=PP_ALIGN.CENTER,
                anchor=MSO_ANCHOR.MIDDLE,
            )
            body_top = CONTENT_TOP + 1.15
        shape = _put(
            ctx,
            lines,
            x + 0.15,
            body_top,
            width - 0.3,
            CONTENT_TOP + CONTENT_H - body_top - 0.15,
            sizes=(32, 30, 28, 26, 24),
            bullet="dot",
            spacing=14,
        )
        if shape is not None:
            ctx.clicks.extend([Target(shape.shape_id, i)] for i in range(len(lines)))


def _big_fact(ctx: Ctx, item: dict) -> None:
    _frame(ctx, item)
    image = _image(item)
    width = SPLIT_W if image else FULL_W
    caption = item.get("lines") or []
    fact_h = 3.3 if caption else CONTENT_H
    fact = _put(
        ctx,
        item.get("fact") or item.get("title", ""),
        MARGIN,
        CONTENT_TOP,
        width,
        fact_h,
        sizes=(120, 100, 84, 72, 60, 48),
        bold=True,
        color=WHITE,
        fill=ctx.accent,
        align=PP_ALIGN.CENTER,
        anchor=MSO_ANCHOR.MIDDLE,
    )
    if caption:
        _put(
            ctx,
            caption,
            MARGIN,
            CONTENT_TOP + fact_h + 0.2,
            width,
            CONTENT_H - fact_h - 0.2,
            sizes=(34, 30, 28, 24),
            fill=ctx.tint,
            align=PP_ALIGN.CENTER,
            anchor=MSO_ANCHOR.MIDDLE,
        )
    if image:
        _picture(ctx, image, 8.4, CONTENT_TOP + 0.25, 4.5, ctx.tint)
    # Число открывается по клику: сначала дети думают, потом видят
    ctx.clicks.append([Target(fact.shape_id)])


def _question(ctx: Ctx, item: dict) -> None:
    ctx.slide.background.fill.solid()
    ctx.slide.background.fill.fore_color.rgb = RGBColor.from_string(PAPER)
    _rect(ctx.slide, 0, 0, W_IN, 0.16, ctx.accent, rounded=False)
    # Заголовок слайда-вопроса и есть вопрос: одна плашка, а не две
    _put(
        ctx,
        item.get("title", ""),
        MARGIN,
        0.45,
        FULL_W,
        1.7,
        sizes=(40, 36, 32, 28, 26),
        bold=True,
        color=WHITE,
        fill=NAVY,
        anchor=MSO_ANCHOR.MIDDLE,
        margin=0.35,
    )
    _stage_chip(ctx)
    options = item.get("lines") or []
    answer = item.get("answer") or ""
    image = _image(item)
    if options:
        width = SPLIT_W if image else FULL_W
        _rows(ctx, options, MARGIN, 2.4, width, 2.95, marker="letters", clicks=False)
        if image:
            _picture(ctx, image, 9.1, 2.4, 3.2, ctx.tint)
        top, height, width = 5.55, 1.2, FULL_W
    else:
        top, height, width = 2.6, 4.15, 8.3 if image else FULL_W
        if image:
            _picture(ctx, image, 9.2, 2.6, 3.3, ctx.tint)
    if answer:
        shape = _put(
            ctx,
            f"{ctx.labels['answer']}: {answer}",
            MARGIN,
            top,
            width,
            height,
            sizes=(48, 44, 40, 36, 32, 28),
            bold=True,
            color=WHITE,
            fill=GREEN,
            align=PP_ALIGN.CENTER,
            anchor=MSO_ANCHOR.MIDDLE,
        )
        # Ответ открывается по клику учителя: дети успевают подумать
        ctx.clicks.append([Target(shape.shape_id)])


def _steps(ctx: Ctx, item: dict) -> None:
    _frame(ctx, item)
    _rows_with_image(ctx, item, "num")


def _summary(ctx: Ctx, item: dict) -> None:
    _frame(ctx, item)
    _rows_with_image(ctx, item, "dot")


def _rows_with_image(ctx: Ctx, item: dict, marker: str) -> None:
    image = _image(item)
    width = SPLIT_W if image else FULL_W
    _rows(ctx, item.get("lines") or [], MARGIN, CONTENT_TOP, width, CONTENT_H, marker=marker)
    if image:
        _picture(ctx, image, 8.4, CONTENT_TOP + 0.25, 4.5, ctx.tint)


LAYOUTS = {
    "title": _title_slide,
    "agenda": _agenda,
    "image_text": _image_text,
    "two_columns": _two_columns,
    "big_fact": _big_fact,
    "question": _question,
    "steps": _steps,
    "summary": _summary,
}


# -- общие части ------------------------------------------------------------------


def _frame(ctx: Ctx, item: dict) -> None:
    """Фон, полоса цвета этапа сверху, заголовок и метка этапа внизу."""
    ctx.slide.background.fill.solid()
    ctx.slide.background.fill.fore_color.rgb = RGBColor.from_string(PAPER)
    _rect(ctx.slide, 0, 0, W_IN, 0.16, ctx.accent, rounded=False)
    _put(
        ctx,
        item.get("title", ""),
        MARGIN,
        0.4,
        FULL_W,
        1.2,
        sizes=(46, 42, 38, 34, 30),
        bold=True,
        color=NAVY,
        anchor=MSO_ANCHOR.MIDDLE,
    )
    _stage_chip(ctx)


def _stage_chip(ctx: Ctx) -> None:
    if ctx.stage == "goals":
        return
    _put(
        ctx,
        ctx.labels[ctx.stage],
        MARGIN,
        6.98,
        2.7,
        0.34,
        sizes=(14,),
        bold=True,
        color=WHITE,
        fill=ctx.accent,
        align=PP_ALIGN.CENTER,
        anchor=MSO_ANCHOR.MIDDLE,
        margin=0.05,
        radius=0.5,
    )


def _list_plate(ctx: Ctx, item: dict, *, bullet: str) -> None:
    """Плашка с пунктами; справа — картинка, если она есть. Пункты
    появляются по абзацам — один щелчок на пункт."""
    image = _image(item)
    lines = item.get("lines") or []
    width = SPLIT_W if image else FULL_W
    shape = _put(
        ctx,
        lines,
        MARGIN,
        CONTENT_TOP,
        width,
        CONTENT_H,
        sizes=(40, 36, 32, 30, 28, 26, 24),
        fill=ctx.tint,
        bullet=bullet,
        spacing=16,
        anchor=MSO_ANCHOR.MIDDLE,
        margin=0.4,
    )
    if shape is not None:
        ctx.clicks.extend([Target(shape.shape_id, i)] for i in range(len(lines)))
    if image:
        _picture(ctx, image, 8.4, CONTENT_TOP + 0.25, 4.5, ctx.tint)


def _rows(
    ctx: Ctx,
    lines: list[str],
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    marker: str,
    clicks: bool = True,
) -> None:
    """Строки-плашки с кружком слева: цифра шага, буква варианта или точка.
    Плашка и кружок открываются одним щелчком."""
    if not lines:
        return
    gap = 0.18
    row_h = min(1.2, (height - gap * (len(lines) - 1)) / len(lines))
    disc = row_h * 0.7
    inset = 0.25 + disc + 0.3
    sizes = (36, 32, 30, 28, 26, 24, 22)
    size = min(_fit([line], width - inset - 0.3, row_h - 0.1, sizes) for line in lines)
    letters = ctx.labels["letters"]
    for number, line in enumerate(lines):
        top = y + number * (row_h + gap)
        plate = _put(
            ctx,
            line,
            x,
            top,
            width,
            row_h,
            sizes=(size,),
            fill=ctx.tint,
            anchor=MSO_ANCHOR.MIDDLE,
            margin=0.1,
            left=inset,
        )
        label = {"num": str(number + 1), "dot": "", "letters": letters[number % len(letters)]}[
            marker
        ]
        circle = _put(
            ctx,
            label,
            x + 0.25,
            top + (row_h - disc) / 2,
            disc,
            disc,
            sizes=(28,),
            bold=True,
            color=WHITE,
            fill=ctx.accent,
            align=PP_ALIGN.CENTER,
            anchor=MSO_ANCHOR.MIDDLE,
            margin=0,
            oval=True,
            keep_empty=True,
        )
        if clicks:
            ctx.clicks.append([Target(plate.shape_id), Target(circle.shape_id)])


def _image(item: dict):
    key = item.get("image") or ""
    return images.lookup(key) if key else None


def _picture(ctx: Ctx, image, x: float, y: float, size: float, plate: str) -> None:
    """Круг-подложка и картинка на нём: прозрачный фон эмодзи читается
    на любом цвете, а круг держит одну композицию на всех слайдах."""
    disc = ctx.slide.shapes.add_shape(
        MSO_SHAPE.OVAL, Inches(x), Inches(y), Inches(size), Inches(size)
    )
    _flat(disc, plate)
    inner = size * 0.72
    offset = (size - inner) / 2
    pic = ctx.slide.shapes.add_picture(
        BytesIO((images.FILES / image.file).read_bytes()),
        Inches(x + offset),
        Inches(y + offset),
        Inches(inner),
        Inches(inner),
    )
    # Описание картинки — для программ чтения с экрана и для тех, кто правит файл
    pic._element.nvPicPr.cNvPr.set("descr", image.key.replace("_", " "))


def _rect(slide, x, y, w, h, color: str, *, rounded: bool = True):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE,
        Inches(x),
        Inches(y),
        Inches(w),
        Inches(h),
    )
    if rounded:
        shape.adjustments[0] = 0.08
    _flat(shape, color)
    return shape


def _flat(shape, color: str) -> None:
    """Заливка без контура и тени: стиль фигуры по умолчанию рисует и то и другое."""
    style = shape._element.find(qn("p:style"))
    if style is not None:
        shape._element.remove(style)
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(color)
    shape.line.fill.background()


def _put(
    ctx: Ctx,
    text,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    sizes: tuple[int, ...],
    bold: bool = False,
    color: str = INK,
    fill: str | None = None,
    align=PP_ALIGN.LEFT,
    anchor=MSO_ANCHOR.TOP,
    bullet: str | None = None,
    spacing: int = 0,
    margin: float = 0.15,
    left: float | None = None,
    radius: float = 0.08,
    oval: bool = False,
    keep_empty: bool = False,
):
    """Фигура с текстом: плашка с заливкой или чистая надпись. Текст —
    строка или список строк (абзацы). Пустой текст не рисуется, если только
    фигура не нужна сама (кружок-маркер)."""
    lines = [text] if isinstance(text, str) else list(text)
    lines = [line for line in lines if line]
    if not lines and not keep_empty:
        return None
    if fill is None:
        shape = ctx.slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    else:
        kind = MSO_SHAPE.OVAL if oval else MSO_SHAPE.ROUNDED_RECTANGLE
        shape = ctx.slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
        if not oval:
            shape.adjustments[0] = min(radius, 0.5)
        _flat(shape, fill)
    frame = shape.text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.vertical_anchor = anchor
    frame.margin_left = Inches(left if left is not None else margin)
    frame.margin_right = frame.margin_top = frame.margin_bottom = Inches(margin)
    inner = w - (left if left is not None else margin) - margin
    size = _fit(lines, inner - (0.55 if bullet else 0), h - 2 * margin, sizes, spacing)
    for number, line in enumerate(lines or [""]):
        paragraph = frame.paragraphs[0] if number == 0 else frame.add_paragraph()
        paragraph.alignment = align
        if spacing and number < len(lines) - 1:
            paragraph.space_after = Pt(spacing)
        if line:
            _run(paragraph, line, size=size, lang=ctx.lang, bold=bold, color=color)
            if bullet:
                _bullet(paragraph, bullet, ctx.accent)
    return shape


def _bullet(paragraph, kind: str, color: str) -> None:
    """Настоящий маркер или нумерация PowerPoint, а не символ в тексте: при
    правке учитель получает обычный список, а в тексте абзаца нет «1.»."""
    pr = paragraph._p.get_or_add_pPr()
    pr.set("marL", str(Inches(0.55)))
    pr.set("indent", str(-Inches(0.55)))
    # Порядок детей a:pPr по схеме: интервалы, цвет маркера, шрифт, сам маркер
    clr = etree.SubElement(pr, qn("a:buClr"))
    etree.SubElement(clr, qn("a:srgbClr")).set("val", color)
    etree.SubElement(pr, qn("a:buFont")).set("typeface", FONT)
    if kind == "num":
        etree.SubElement(pr, qn("a:buAutoNum")).set("type", "arabicPeriod")
    else:
        etree.SubElement(pr, qn("a:buChar")).set("char", "•")


def _fit(
    lines: list[str], width: float, height: float, sizes: tuple[int, ...], spacing: int = 0
) -> int:
    """Самый крупный кегль из списка, при котором текст влезает в рамку.
    Ширина буквы Arial — около 0,55 кегля для кириллицы, строка — 1,2 кегля;
    расчёт грубый и с запасом: лучше мельче, чем за краем плашки."""
    for size in sizes:
        per_line = max(1, int(width * 72 / (size * 0.55)))
        rows = sum(max(1, ceil(len(line) / per_line)) for line in lines) or 1
        need = rows * size * 1.2 + max(len(lines) - 1, 0) * spacing
        if need <= height * 72:
            return size
    return sizes[-1]


def _run(paragraph, text: str, *, size: int, lang: str, bold=False, color=INK) -> None:
    run = paragraph.add_run()
    run.text = text
    font = run.font
    font.name = FONT
    font.size = Pt(size)
    font.bold = bold
    font.color.rgb = RGBColor.from_string(color)
    # Язык текста — для проверки орфографии: без него PowerPoint подчёркивает
    # казахский слайд целиком как русский с ошибками
    font._rPr.set("lang", LANG[lang])


def _notes(slide, lines: list[str]) -> None:
    slide.notes_slide.notes_text_frame.text = "\n".join(lines)
