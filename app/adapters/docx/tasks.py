"""Задания на урок в Word: лист для учителя и карточки для разрезания.

Чистая функция над готовыми данными, как ҚМЖ: на входе содержимое материала
и шапка из комплекта, на выходе байты .docx.

Первая страница — учителю: подача, игровая часть, какую карточку к какому
шагу урока раздавать. С новой страницы — карточки таблицей с рамками: рамки
и есть линии реза, каждая карточка — ячейка. Печать чёрно-белая, поэтому
уровни различаются буквами А, В, С, а не цветом.

Ответов в этом файле нет: лист уходит детям, ответы собирает сервер в ключи
отдельным файлом (TEACHER_AI_BRIEF, раздел 4.2). Под каждым заданием —
строка для ответа от руки.
"""

from io import BytesIO

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.shared import Cm, Pt

from app.adapters.docx import common
from app.adapters.docx.common import TEXT_W, DocHeader

__all__ = ["LABELS", "render_tasks"]

LABELS = {
    "ru": {
        "title": "Задания к уроку",
        "section": "Раздел",
        "grade": "Класс",
        "topic": "Тема урока",
        "learning_objectives": "Цель обучения",
        "for_teacher": "Учителю",
        "intro": "Как подать задания",
        "game_rules": "Игровая часть",
        "cards_index": "Карточки по ходу урока",
        "card": "Карточка",
        "level": "Уровень",
        "plan_step": "Когда раздать",
        "teacher_notes": "Как раздать и проверить",
        "cards": "Карточки — разрезать по линиям",
        "answer": "Ответ",
        "cards_support": "Уровень А — с опорой",
        "cards_core": "Уровень В — основной",
        "cards_challenge": "Уровень С — повышенный",
    },
    "kz": {
        "title": "Сабаққа арналған тапсырмалар",
        "section": "Бөлім",
        "grade": "Сынып",
        "topic": "Сабақтың тақырыбы",
        "learning_objectives": "Оқу мақсаты",
        "for_teacher": "Мұғалімге",
        "intro": "Тапсырмаларды қалай ұсыну керек",
        "game_rules": "Ойын бөлігі",
        "cards_index": "Сабақ барысындағы карточкалар",
        "card": "Карточка",
        "level": "Деңгей",
        "plan_step": "Қашан тарату керек",
        "teacher_notes": "Қалай таратып, тексеру керек",
        "cards": "Карточкалар — сызық бойымен қиып алыңыз",
        "answer": "Жауабы",
        "cards_support": "А деңгейі — қолдаумен",
        "cards_core": "В деңгейі — негізгі",
        "cards_challenge": "С деңгейі — күрделендірілген",
    },
}

# Блоки карточек и буква уровня, которую видит ребёнок, — в порядке листа
CARD_BLOCKS = (("cards_support", "А"), ("cards_core", "В"), ("cards_challenge", "С"))

LABEL_W = 5.0
# Индекс карточек: название, уровень, шаг урока
INDEX_COLS = (6.0, 2.0, TEXT_W - 8.0)
# Две карточки в ряд: на A4 так помещается 6–8 карточек, а ячейка остаётся
# шире детской ладони — писать ответ есть где
CARD_W = TEXT_W / 2
ANSWER_LINE = "_" * 16


def render_tasks(content: dict, header: DocHeader) -> bytes:
    labels = LABELS[header.lang]
    doc = Document()
    common.page(doc)
    common.base_style(doc, header.lang)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    common.run(title, labels["title"], bold=True, size=Pt(14))

    _head_table(doc, content, header, labels)
    _teacher_part(doc, content, labels)
    _cards(doc, content, labels)

    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def _head_table(doc, content: dict, header: DocHeader, labels: dict) -> None:
    section = content.get("section") or {}
    rows = [
        ("section", [section.get("title", ""), section.get("subsection", "")]),
        ("grade", [str(header.grade)]),
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
    table = common.table(doc, 2, (LABEL_W, TEXT_W - LABEL_W))
    for kind, lines in rows:
        label, value = table.add_row().cells
        common.label(label, labels[kind])
        common.lines(value, [line for line in lines if line])
    common.widths(table, (LABEL_W, TEXT_W - LABEL_W))


def _teacher_part(doc, content: dict, labels: dict) -> None:
    _heading(doc, labels["for_teacher"], size=Pt(13))

    if content.get("intro"):
        _heading(doc, labels["intro"])
        common.run(doc.add_paragraph(), content["intro"], italic=True)

    rules = content.get("game_rules") or []
    if rules:
        _heading(doc, labels["game_rules"])
        for line in common.bullets(rules):
            common.run(doc.add_paragraph(), line)

    # Какую карточку к какому шагу — учителю, а не на карточку: ребёнку
    # шаг плана ни о чём не говорит, а учителю не придётся сверять с ҚМЖ
    cards = [(card, letter) for block, letter in CARD_BLOCKS for card in content.get(block) or []]
    if cards:
        _heading(doc, labels["cards_index"])
        table = common.table(doc, 3, INDEX_COLS)
        head = table.add_row().cells
        for cell, key in zip(head, ("card", "level", "plan_step"), strict=True):
            common.label(cell, labels[key], center=True)
        common.repeat_header(table.rows[0])
        for card, letter in cards:
            name, level, step = table.add_row().cells
            common.lines(name, [card.get("title") or ""])
            common.label(level, letter, center=True)
            common.lines(step, [card.get("plan_step") or ""])
        common.widths(table, INDEX_COLS)

    notes = content.get("teacher_notes") or []
    if notes:
        _heading(doc, labels["teacher_notes"])
        for line in common.bullets(notes):
            common.run(doc.add_paragraph(), line)


def _cards(doc, content: dict, labels: dict) -> None:
    blocks = [(block, letter, content.get(block) or []) for block, letter in CARD_BLOCKS]
    if not any(cards for _block, _letter, cards in blocks):
        return

    # Карточки — с новой страницы: лист учителя не режут вместе с ними
    heading = doc.add_paragraph()
    heading.paragraph_format.page_break_before = True
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    common.run(heading, labels["cards"], bold=True, size=Pt(13))

    for block, letter, cards in blocks:
        if not cards:
            continue
        _heading(doc, labels[block])
        table = common.table(doc, 2, (CARD_W, CARD_W))
        for start in range(0, len(cards), 2):
            row = table.add_row()
            _keep_whole(row)
            left, right = row.cells
            pair = cards[start : start + 2]
            if len(pair) == 1:
                # Нечётная карточка — во всю ширину: пустая ячейка рядом
                # стала бы лишним куском бумаги под ножницы. Ширину ставим
                # сами: `common.widths` дал бы объединённой ячейке половину
                left = left.merge(right)
                left.width = Cm(TEXT_W)
            else:
                left.width = right.width = Cm(CARD_W)
            for cell, card in zip((left, right), pair, strict=False):
                _card(cell, card, letter, labels)


def _card(cell, card: dict, letter: str, labels: dict) -> None:
    paragraph = cell.paragraphs[0]
    common.run(paragraph, card.get("title") or "", bold=True)
    common.run(paragraph, f"   ({labels['level']} {letter})", size=Pt(10))

    if card.get("instruction"):
        instruction = cell.add_paragraph()
        instruction.paragraph_format.space_before = Pt(3)
        common.run(instruction, card["instruction"], italic=True)

    for number, task in enumerate(card.get("tasks") or [], start=1):
        text = cell.add_paragraph()
        text.paragraph_format.space_before = Pt(6)
        common.run(text, f"{number}. {task.get('text') or ''}")
        # Место для ответа от руки — сам ответ уходит только в ключи
        common.run(cell.add_paragraph(), f"{labels['answer']}: {ANSWER_LINE}")

    # Отступ снизу, чтобы при резке не срезать последнюю строку
    cell.add_paragraph()


def _heading(doc, text: str, *, size=None) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(10)
    paragraph.paragraph_format.space_after = Pt(4)
    common.run(paragraph, text, bold=True, size=size)


def _keep_whole(row) -> None:
    """Карточка не рвётся между страницами: половина карточки на одном
    листе и половина на другом не вырезается."""
    tr_pr = row._tr.get_or_add_trPr()
    tr_pr.append(OxmlElement("w:cantSplit"))
