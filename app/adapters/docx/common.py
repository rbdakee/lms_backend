"""Общее у всех документов «Жоспара» в Word: страница, шрифт, таблицы, строки.

Вынесено из рендера ҚМЖ (сессия L4), когда документов стало шесть: план,
задания, домашнее, проверочная, игра и ключи. Всё здесь — про бумагу, а не
про форму конкретного материала: форма у каждого вида в своём файле.
"""

from dataclasses import dataclass

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from docx.table import _Cell

# Times New Roman — шрифт школьной документации, и в Word, и в LibreOffice
# в нём есть все девять казахских букв (ә ғ қ ң ө ұ ү һ і). Имя шрифта
# ставится во все четыре слота rFonts: Word выбирает слот по диапазону
# символа, и неназначенный слот подменял бы кириллицу шрифтом темы.
FONT = "Times New Roman"
FONT_SIZE = Pt(12)

# Язык текста для проверки орфографии в Word: без него казахский документ
# подчёркивается красным целиком как «русский с ошибками».
WORD_LANG = {"ru": "ru-RU", "kz": "kk-KZ"}

# Ширина текста на A4 книжном при полях 2 см слева и 1,5 см справа.
TEXT_W = 21.0 - 2.0 - 1.5


@dataclass(frozen=True)
class DocHeader:
    """Шапка документа — поля комплекта и учителя, а не содержимого
    материала: ФИО в содержимое не входит, иначе уехало бы в банк готовых
    материалов (L7).

    Даты урока и числа присутствующих в заказе нет — в ҚМЖ эти строки
    пустые, учитель впишет их от руки.
    """

    lang: str
    teacher_name: str
    grade: int
    topic: str


def page(doc) -> None:
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21.0), Cm(29.7)
    section.left_margin, section.right_margin = Cm(2.0), Cm(1.5)
    section.top_margin, section.bottom_margin = Cm(1.5), Cm(1.5)


def base_style(doc, lang: str) -> None:
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


def table(doc, cols: int, widths: tuple[float, ...]):
    table = doc.add_table(rows=0, cols=cols)
    table.autofit = False
    borders(table)
    for column, width in zip(table.columns, widths, strict=True):
        column.width = Cm(width)
    return table


def borders(table) -> None:
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


def widths(table, widths: tuple[float, ...]) -> None:
    """Ширина — ещё и каждой ячейке: Word верстает по ширине ячеек, а сетку
    столбцов читает только LibreOffice."""
    for row in table.rows:
        for cell, width in zip(row.cells, widths, strict=True):
            cell.width = Cm(width)


def repeat_header(row) -> None:
    """Шапка таблицы хода урока повторяется на каждой странице: таблица
    почти всегда переезжает на вторую."""
    tr_pr = row._tr.get_or_add_trPr()
    flag = OxmlElement("w:tblHeader")
    flag.set(qn("w:val"), "true")
    tr_pr.append(flag)


def label(cell: _Cell, text: str, *, center: bool = False) -> None:
    paragraph = cell.paragraphs[0]
    if center:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run(paragraph, text, bold=True)


def lines(cell: _Cell, lines: list[str]) -> None:
    """Строки — отдельными абзацами ячейки. Пустой список — пустая ячейка:
    её заполнит учитель от руки."""
    for number, line in enumerate(lines):
        paragraph = cell.paragraphs[0] if number == 0 else cell.add_paragraph()
        run(paragraph, line)


def bullets(items) -> list[str]:
    return [f"• {item}" for item in items or []]


def run(paragraph, text: str, *, bold=False, italic=False, size=None) -> None:
    run = paragraph.add_run(text)
    run.bold = bold
    run.italic = italic
    if size is not None:
        run.font.size = size
