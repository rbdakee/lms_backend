"""Колода из шаблонов — исполнитель сценария (PPTX_TEMPLATES_SPEC, раздел 3).

Чистая функция над готовыми данными, как старая сборка (`presentation.py`):
на входе содержимое материала-сценария, шапка комплекта и каталог библиотеки,
на выходе байты `.pptx` и предупреждения проверки.

python-pptx не умеет копировать слайды, поэтому шаблонный слайд переносится
клонированием: дерево фигур (`spTree`) копируется целиком, а связи слайда —
картинки, медиа, гиперссылки — регистрируются на клоне заново, и старые
`rId` в копии дерева переписываются на новые. Колода строится поверх самой
`library.pptx`, а не пустой: тема, мастер и макеты переезжают сами, и
клонирование остаётся внутри одного пакета. Исходные слайды библиотеки из
готовой колоды вынимаются — остаются титульный, «Сегодня на уроке» и
собранные слайды сценария.

Кегль исполнитель не выдумывает: точка отсчёта — кегль шаблона. Превышение
лимита каталога или рамки слота — кегль ступенями вниз, но не ниже нижней
границы (24 pt для слотов, 18 для подписи `caption`), и предупреждение
в результате проверки — модели при правке, учителю в лог.

Пустой необязательный слот удаляется целиком, с текстом-заполнителем шаблона,
а украшения из `deco.follows` уходят вместе с опустевшими слотами — на модель
это не перекладывается. Слот-картинка заменяется рисунком каталога, который
перенимает имя `slot:<name>`: пунктирной рамки под картинкой не остаётся.
"""

import copy
import logging
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_THEME_COLOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

from app.adapters.docx.common import DocHeader
from app.adapters.pptx.animation import Target, animate, fade_transition
from app.adapters.pptx.presentation import (
    LANG,
    _fit,
    build_server_slides,
    build_slide_in,
)
from app.lesson_ai import images
from app.lesson_ai.pptx_templates import DeckTemplate, Slot

log = logging.getLogger(__name__)

# Блоки слайдов сценария и их этап — те же, что у старой сборки
STAGE_STAGES = (
    ("slides_beginning", "stage_beginning"),
    ("slides_middle", "stage_middle"),
    ("slides_end", "stage_end"),
)

# Нижняя граница кегля: с доски с пяти метров мельче читать нечем.
# Подпись (caption) — не для детей, ей можно чуть мельче
SLOT_MIN_PT = 24
CAPTION_MIN_PT = 18
# kind=number по спеке — не длиннее 12 знаков, даже если каталог промолчал
NUMBER_MAX_CHARS = 12

# Стили надписей `add`: кегль, жирность, цвет. Цвета нейтральные — палитра
# шаблона живёт в его теме, и надпись не должна спорить с плашками шаблона
ADD_TEXT = {
    "title": (40, True, "1B2A49"),
    "body": (28, False, "1F2937"),
    "caption": (20, False, "6B7280"),
    "accent": (28, True, "1E9E5A"),
}
# Фигуры `add`; цвет — акцентный из темы шаблона, а не со стороны
ADD_SHAPE = {
    "circle": MSO_SHAPE.OVAL,
    "rounded": MSO_SHAPE.ROUNDED_RECTANGLE,
    "arrow": MSO_SHAPE.RIGHT_ARROW,
    "star": MSO_SHAPE.STAR_5_POINT,
}

# Пространство имён атрибутов-ссылок: `r:embed`, `r:link`, `r:id`
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


@dataclass
class DeckResult:
    """Собранный файл и предупреждения проверки: превышенные лимиты, пустые
    обязательные слоты, чужие ключи. Не ошибки: слайд собирается всегда."""

    data: bytes = b""
    warnings: list[str] = field(default_factory=list)

    def warn(self, slide_no: int, template: str, message: str) -> None:
        self.warnings.append(f"слайд {slide_no} [{template}]: {message}")


def is_scenario(content: dict) -> bool:
    """Форма содержимого: слайд с `template` — сценарий, с `layout` — старый.
    Генерация формы не смешивает, но правка из чата может — решает слайд."""
    return any(
        isinstance(item, dict) and "template" in item
        for block, _ in STAGE_STAGES
        for item in content.get(block) or []
    )


def render_deck(content: dict, header: DocHeader, library: Path) -> DeckResult:
    """Собрать колоду по сценарию из библиотеки шаблонов."""
    from app.lesson_ai import pptx_templates

    catalog = pptx_templates.load(library)
    deck = Presentation(BytesIO((library / "library.pptx").read_bytes()))
    originals = list(deck.slides)  # слайды библиотеки из готовой колоды вынимаются
    out = DeckResult()

    build_server_slides(deck, content, header)
    number = 3  # впереди титульный и «Сегодня на уроке»
    for block, stage in STAGE_STAGES:
        for item in content.get(block) or []:
            if not isinstance(item, dict) or "template" not in item:
                # Слайд прежней формы — старым путём, в ту же колоду
                build_slide_in(deck, item, header.lang, stage)
            else:
                _build_scenario_slide(deck, catalog, item, header.lang, number, out)
            number += 1
    _drop_slides(deck, originals)

    out.data = _bytes(deck)
    return out


def _build_scenario_slide(
    deck, catalog: dict[str, DeckTemplate], item: dict, lang: str, number: int, out: DeckResult
):
    template = catalog.get(item.get("template") or "")
    if template is None:
        # Слайд без шаблона собрать нечего; пропуск громче пустого слайда
        out.warnings.append(f"слайд {number}: шаблона {item.get('template')!r} нет в каталоге")
        return None
    slide = _clone_slide(deck, deck.slides[template.slide - 1])
    shapes = {shape.name: shape for shape in slide.shapes}
    targets: dict[str, int] = {}
    fills = {f.get("slot"): f for f in item.get("fill") or [] if isinstance(f, dict)}
    filled: dict[str, int] = {}  # слот → сколько содержимого в него легло

    for slot in template.slots:
        shape = shapes.get(f"slot:{slot.name}")
        if shape is None:  # каталог сверяется со слайдами при загрузке — не бывает
            continue
        lines = _slot_lines(slot, fills.get(slot.name))
        if not lines:
            # Пустой слот: обязательный — предупреждение, необязательный
            # уходит целиком, вместе с текстом-заполнителем шаблона
            if slot.required:
                out.warn(number, template.key, f"обязательный слот «{slot.name}» не заполнен")
            else:
                _drop_shape(shape)
            continue
        target, count = _fill_slot(slide, shape, slot, lines, lang, number, template.key, out)
        if target is not None:
            targets[slot.name] = target
        filled[slot.name] = count

    # Украшения-спутники уходят вместе с опустевшими слотами — сами,
    # без просьбы модели (спека, раздел 3, п. 4)
    _drop_following(shapes, template, filled)

    for name in item.get("remove") or []:
        shape = shapes.get(name) if isinstance(name, str) else None
        if shape is None or not name.startswith("deco:"):
            out.warn(number, template.key, f"удалить {name!r} нельзя — это не украшение шаблона")
            continue
        _drop_shape(shape)  # украшение может быть и группой — снимается целиком

    for index, added in enumerate(item.get("add") or [], start=1):
        shape = _add_item(slide, deck, added, number, template.key, out)
        if shape is not None:
            targets[f"add:{index}"] = shape.shape_id

    _apply_animation(slide, template, item, targets, number, out)
    notes = (item.get("notes") or "").strip()
    if notes:
        slide.notes_slide.notes_text_frame.text = notes
    return slide


def _slot_lines(slot: Slot, fill: dict | None) -> list[str]:
    """Содержимое слота непустыми строками; пустой список — слот пуст,
    как будто сценарий о нём вовсе не писал."""
    if fill is None:
        return []
    if slot.kind == "image":
        key = (fill.get("image") or "").strip()
        return [key] if key else []
    if slot.kind == "bullets":
        return [line for line in fill.get("items") or [] if line]
    text = (fill.get("text") or "").strip()
    return [text] if text else []


def _drop_shape(shape) -> None:
    parent = shape._element.getparent()
    if parent is not None:  # уже убранный слот или украшение не трогаем
        parent.remove(shape._element)


def _drop_following(shapes: dict, template: DeckTemplate, filled: dict[str, int]) -> None:
    """Удалить украшения из `deco.follows`: слоты, за которыми украшение
    следует, пусты или короче указанного числа пунктов — все условия
    сработали, украшению там делать нечего."""
    for name, conditions in template.follows.items():
        shape = shapes.get(f"deco:{name}")
        if shape is None or shape._element.getparent() is None:
            continue  # нет на слайде или уже убран сценарием
        if all(filled.get(slot, 0) < (at_least or 1) for slot, at_least in conditions):
            _drop_shape(shape)


# -- клон шаблонного слайда --------------------------------------------------------


def _clone_slide(deck, source):
    """Копия слайда внутри той же колоды: дерево фигур целиком и связи.

    Фигуры переносятся глубоким деревом (`spTree` без служебных вершин),
    каждая связь источника — картинка, медиа, гиперссылка — регистрируется
    на клоне заново, и старые `rId` в копии дерева заменяются на новые.
    Связь на макет не переносится: клон и так привязан к тому же макету.
    """
    slide = deck.slides.add_slide(source.slide_layout)
    # `add_slide` скопировал заполнители макета: у шаблона свои фигуры
    tree = slide.shapes._spTree
    for shape in list(slide.shapes):
        tree.remove(shape._element)
    # Фон у шаблонного слайда свой (цвет заливки в `p:bg`), в дерево фигур
    # он не входит — переносится отдельной первой вершиной `cSld`
    background = source._element.cSld.find(qn("p:bg"))
    if background is not None:
        slide._element.cSld.insert(0, copy.deepcopy(background))
    rid_map: dict[str, str] = {}
    for rel in list(source.part.rels.values()):
        if rel.reltype == RT.SLIDE_LAYOUT:
            continue
        if rel.is_external:
            rid_map[rel.rId] = slide.part.rels.get_or_add_ext_rel(rel.reltype, rel.target_ref)
        else:
            rid_map[rel.rId] = slide.part.relate_to(rel.target_part, rel.reltype)
    for child in source._element.cSld.spTree:
        if etree.QName(child).localname in ("nvGrpSpPr", "grpSpPr"):
            continue
        clone = copy.deepcopy(child)
        for node in clone.iter():
            for attr, value in list(node.attrib.items()):
                if attr.startswith(R_NS) and value in rid_map:
                    node.set(attr, rid_map[value])
        tree.append(clone)
    return slide


def _drop_slides(deck, slides: list) -> None:
    """Вынуть слайды из колоды: запись в списке слайдов и её связь.
    Часть слайда без связи из пакета не сохраняется — файл не пухнет
    неиспользованными шаблонами."""
    id_list = deck.slides._sldIdLst
    for slide in slides:
        for entry in list(id_list):
            r_id = entry.get(qn("r:id"))
            rel = deck.part.rels.get(r_id)
            if rel is not None and rel.target_part is slide.part:
                id_list.remove(entry)
                deck.part.drop_rel(r_id)
                break


# -- заполнение слотов ---------------------------------------------------------------


def _fill_slot(
    slide, shape, slot: Slot, lines: list[str], lang: str, number: int, key: str, out: DeckResult
) -> tuple[int, int]:
    """Вписать содержимое в слот; вернуть id фигуры, которую открывает
    анимация (у картинки это вставленный рисунок, а не заполнитель),
    и сколько содержимого легло (у картинки 1/0 — легла ли)."""
    if slot.kind == "image":
        return _fill_image(slide, shape, lines[0], number, key, out)
    original = _template_size(shape)
    floor = slot.min_pt or SLOT_MIN_PT
    limit = _limit(slot)
    if limit and any(len(line) > limit for line in lines):
        out.warn(
            number,
            key,
            f"слот «{slot.name}»: текст длиннее лимита ({limit} зн.), кегль {floor} pt",
        )
        size = floor
    else:
        size = _fit_size(shape, lines, original, floor)
        if size < original:
            out.warn(
                number,
                key,
                f"слот «{slot.name}»: текст не влезает, кегль снижен до {size} pt",
            )
    if slot.kind == "bullets" and slot.max_items and len(lines) > slot.max_items:
        out.warn(number, key, f"слот «{slot.name}»: пунктов {len(lines)} — больше лимита")
    _put_lines(shape, lines, lang=lang, size=size)
    return shape.shape_id, len(lines)


def _fill_image(
    slide, shape, key_value: str, number: int, key: str, out: DeckResult
) -> tuple[int, int]:
    image = images.lookup(key_value)
    if image is None:
        if key_value:  # пустой ключ молча оставляет слот пустым — как в старой сборке
            out.warn(number, key, f"картинки {key_value!r} нет в каталоге, слот пустой")
        return shape.shape_id, 0
    picture = _insert_picture(slide, image, shape.left, shape.top, shape.width, shape.height)
    # Картинка заменяет рамку и перенимает её имя `slot:<name>`: пунктир
    # и подпись «картинка» под рисунком остаться не должны (спека, п. 3)
    picture.name = shape.name
    _drop_shape(shape)
    return picture.shape_id, 1


def _insert_picture(slide, image, left: int, top: int, width: int, height: int):
    """Картинка каталога, вписанная в рамку с сохранением пропорций и по
    центру рамки: натянутая по рамке она была бы кривой. Размер PNG читается
    из его заголовка (IHDR) — каталог целиком из PNG."""
    blob = (images.FILES / image.file).read_bytes()
    px_w = int.from_bytes(blob[16:20], "big")
    px_h = int.from_bytes(blob[20:24], "big")
    px_emu = 9525  # 96 dpi, как считает сам python-pptx
    scale = min(width / (px_w * px_emu), height / (px_h * px_emu))
    fit_w, fit_h = int(px_w * px_emu * scale), int(px_h * px_emu * scale)
    picture = slide.shapes.add_picture(
        BytesIO(blob),
        left + (width - fit_w) // 2,
        top + (height - fit_h) // 2,
        fit_w,
        fit_h,
    )
    # Описание — для программ чтения с экрана и тех, кто правит файл
    picture._element.nvPicPr.cNvPr.set("descr", image.key.replace("_", " "))
    return picture


# -- кегль и текст ----------------------------------------------------------------


def _template_size(shape) -> int:
    """Кегль шаблона в слоте — из первого заполненного текста: оформление
    принадлежит дизайнеру, и исполнитель его не угадывает."""
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            if run.font.size is not None:
                return int(run.font.size.pt)
    return 28


def _limit(slot: Slot) -> int:
    if slot.max_chars:
        return slot.max_chars
    return NUMBER_MAX_CHARS if slot.kind == "number" else 0


def _fit_size(shape, lines: list[str], original: int, floor: int) -> int:
    """Самый крупный кегль от шаблонного до нижней границы (`min_pt` каталога,
    по умолчанию 24), при котором текст влезает в рамку слота. Расчёт тот же
    грубый, что у старой сборки: лучше мельче, чем за краем плашки."""
    frame = shape.text_frame
    inner_w = shape.width / 914400 - (frame.margin_left + frame.margin_right) / 914400
    inner_h = shape.height / 914400 - (frame.margin_top + frame.margin_bottom) / 914400
    # Шаблон мельче границы — граница не сильнее дизайнера
    sizes = tuple(range(original, floor - 1, -2)) if original > floor else (original,)
    return _fit(lines, inner_w, inner_h, sizes)


def _put_lines(shape, lines: list[str], *, lang: str, size: int) -> None:
    """Заменить текст слота, сохранив форматирование первого абзаца шаблона:
    модель пишет содержимое, а шрифт, цвет и выключку оставляет дизайну.
    Пункты `bullets` — по абзацу на пункт, как в шаблоне."""
    frame = shape.text_frame
    proto = frame.paragraphs[0]._p
    body = proto.getparent()
    ppr = proto.find(qn("a:pPr"))
    rpr = next(
        (r.find(qn("a:rPr")) for r in proto.iter(qn("a:r")) if r.find(qn("a:rPr")) is not None),
        None,
    )
    ppr_copy = copy.deepcopy(ppr) if ppr is not None else None
    rpr_copy = copy.deepcopy(rpr) if rpr is not None else None
    for paragraph in body.findall(qn("a:p")):
        body.remove(paragraph)
    for line in lines or [""]:
        paragraph = etree.SubElement(body, qn("a:p"))
        if ppr_copy is not None:
            paragraph.append(copy.deepcopy(ppr_copy))
        run = etree.SubElement(paragraph, qn("a:r"))
        if rpr_copy is not None:
            run_rpr = copy.deepcopy(rpr_copy)
            run.append(run_rpr)
        else:
            run_rpr = etree.SubElement(run, qn("a:rPr"))
        run_rpr.set("lang", LANG[lang])
        run_rpr.set("sz", str(size * 100))
        text = etree.SubElement(run, qn("a:t"))
        text.text = line


# -- добавленное (`add`) -------------------------------------------------------------


def _add_item(slide, deck, item: dict, number: int, key: str, out: DeckResult):
    kind = item.get("kind") if isinstance(item, dict) else None
    if kind not in ("text", "image", "shape"):
        out.warn(number, key, f"в add запись с kind={kind!r} — не текст, не картинка и не фигура")
        return None
    left = _percent(item.get("x"), deck.slide_width)
    top = _percent(item.get("y"), deck.slide_height)
    width = _percent(item.get("w"), deck.slide_width)
    height = _percent(item.get("h"), deck.slide_height)
    if kind == "image":
        image = images.lookup((item.get("image") or "").strip())
        if image is None:
            out.warn(number, key, "в add ключа картинки нет в каталоге")
            return None
        return _insert_picture(slide, image, left, top, width, height)
    if kind == "shape":
        shape = slide.shapes.add_shape(
            ADD_SHAPE.get(item.get("style") or "", MSO_SHAPE.ROUNDED_RECTANGLE),
            left,
            top,
            width,
            height,
        )
        shape.fill.solid()
        shape.fill.fore_color.theme_color = MSO_THEME_COLOR.ACCENT_1
        shape.line.fill.background()
        shape.shadow.inherit = False
        return shape
    size, bold, color = ADD_TEXT.get(item.get("style") or "body", ADD_TEXT["body"])
    bound = CAPTION_MIN_PT if item.get("style") == "caption" else SLOT_MIN_PT
    lines = [line for line in (item.get("text") or "").splitlines() if line]
    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.NONE
    fit = size
    if lines:
        # Поля надписи небольшие, но рамку не считать впритык
        sizes = tuple(range(size, bound - 1, -2))
        fit = _fit(lines, width / 914400 - 0.2, height / 914400 - 0.1, sizes)
    for number_, line in enumerate(lines or [""]):
        paragraph = frame.paragraphs[0] if number_ == 0 else frame.add_paragraph()
        run = paragraph.add_run()
        run.text = line
        run.font.size = Pt(max(fit, bound))
        run.font.bold = bold
        run.font.color.rgb = RGBColor.from_string(color)
    return box


def _percent(value, total: int) -> Emu:
    """Проценты сценария → EMU файла. Кривое значение — не отказ сборки."""
    try:
        return Emu(int(total * float(value) / 100))
    except (TypeError, ValueError):
        return Emu(0)


# -- анимация и сборка ----------------------------------------------------------------


def _apply_animation(slide, template: DeckTemplate, item: dict, targets: dict, number, out):
    animation = item.get("animation") or {}
    effect = animation.get("effect") or "appear"
    model_order = [name for name in animation.get("order") or [] if isinstance(name, str)]
    order = model_order or template.default_order
    if effect == "none" or not order:
        return
    groups = []
    for name in order:
        shape_id = targets.get(name)
        if shape_id is None:
            # В умолчании каталога нет пустых слотов — это нормально; чужое
            # имя в порядке от модели — ошибка, о ней и говорим
            if model_order:
                out.warn(
                    number,
                    template.key,
                    f"в animation.order имя {name!r} — не слот и не add этого слайда",
                )
            continue
        groups.append([Target(shape_id)])
    if groups:
        animate(slide, groups, effect)
        fade_transition(slide)


def _bytes(deck) -> bytes:
    out = BytesIO()
    deck.save(out)
    return out.getvalue()
