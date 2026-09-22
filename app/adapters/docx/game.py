"""Игра к уроку в Word: правила для учителя, листы для печати и резки,
ключ ведущего и призы (TEACHER_AI_BRIEF, разделы 4.3 и 5).

Чистая функция над готовыми данными, как рендер ҚМЖ: содержимое материала
и шапка комплекта на входе, байты .docx на выходе.

Бумага — чёрно-белый принтер, А4, одна сторона. Карточки — таблицы, и рамки
таблицы служат линиями реза: учитель режет по линиям, ничего не размечая.

Призы рисует сервер, а не модель (L5_games_db_design, раздел 1.4): лист
жетонов и грамота победителю — в каждом файле игры, развёртка кубика
и игровые монеты — если их просят правила. Модель пишет только слова
награды: нарисованное моделью выходит кривым и стоит токенов.
"""

from io import BytesIO

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_ROW_HEIGHT_RULE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from app.adapters.docx import common
from app.adapters.docx.common import TEXT_W, DocHeader

__all__ = ["LABELS", "render_game"]

LABELS = {
    "ru": {
        "kind": "Игра к уроку",
        "section": "Раздел",
        "teacher": "Учитель",
        "grade": "Класс",
        "topic": "Тема урока",
        "learning_objectives": "Цель обучения",
        "for_teacher": "Для учителя",
        "story": "Сюжет",
        "placement": "Место в уроке",
        "stage_beginning": "Начало урока",
        "stage_middle": "Середина урока",
        "stage_end": "Конец урока",
        "whole_lesson": "Весь урок",
        "minutes": "мин",
        "mechanic": "Как устроена игра",
        "roles": "Роли",
        "role": "Роль",
        "who": "Кто",
        "does": "Что делает",
        "rules": "Правила по шагам",
        "preparation": "Подготовка",
        "cutting": "Что распечатать и вырезать",
        "sets": "Сколько комплектов",
        "room": "Класс и парты",
        "noise": "Шум",
        "quiet": "тихо, шёпотом",
        "voiced": "ответы вслух",
        "loud": "шумно, движение по классу",
        "props": "Понадобится ещё",
        "die_prop": "кубик — развёртка в конце файла",
        "coins_prop": "игровые деньги — листы в конце файла",
        "scoring": "Очки и победа",
        "reward": "Награда",
        "tokens_for": "Жетоны",
        "diploma_for": "Грамота",
        "differentiation": "Дифференциация",
        "support": "Кому трудно",
        "challenge": "Кто справился быстро",
        "print": "Распечатать и разрезать",
        "copies": "Сколько",
        "cut_grid": "Разрежьте по линиям.",
        "cut_none": "Не разрезать: лист целиком.",
        "host_key": "Ключ для ведущего",
        "item": "Что проверяем",
        "answer": "Ответ",
        "prizes": "Призы",
        "tokens": "Жетоны",
        "tokens_note": "Вырежьте по линиям. За что выдаётся жетон:",
        "die": "Кубик",
        "die_note": (
            "Вырежьте крест по внешнему контуру, согните по линиям и скрепите"
            " скотчем. Подойдёт и любой кубик из настольной игры."
        ),
        "coins": "Игровые деньги",
        "coins_note": "Вырежьте по линиям.",
        "currency": "тг",
        "diploma": "Грамота",
        "awarded": "награждается",
        "name_hint": "(имя и фамилия)",
        "in_game": "в игре «{title}»",
        "date": "Дата",
    },
    "kz": {
        "kind": "Сабаққа арналған ойын",
        "section": "Бөлім",
        "teacher": "Мұғалім",
        "grade": "Сынып",
        "topic": "Сабақтың тақырыбы",
        "learning_objectives": "Оқу мақсаты",
        "for_teacher": "Мұғалімге",
        "story": "Ойын желісі",
        "placement": "Сабақтағы орны",
        "stage_beginning": "Сабақтың басы",
        "stage_middle": "Сабақтың ортасы",
        "stage_end": "Сабақтың соңы",
        "whole_lesson": "Сабақ бойы",
        "minutes": "мин",
        "mechanic": "Ойын қалай құрылған",
        "roles": "Рөлдер",
        "role": "Рөл",
        "who": "Кім",
        "does": "Не істейді",
        "rules": "Ойын ережесі қадам бойынша",
        "preparation": "Дайындық",
        "cutting": "Не басып шығарып, қию керек",
        "sets": "Қанша жинақ",
        "room": "Сынып пен парталар",
        "noise": "Шу",
        "quiet": "тыныш, сыбырлап",
        "voiced": "жауаптар дауыстап",
        "loud": "шулы, сынып ішінде қозғалыс",
        "props": "Тағы қажет",
        "die_prop": "ойын текшесі — жазбасы файлдың соңында",
        "coins_prop": "ойын ақшасы — парақтары файлдың соңында",
        "scoring": "Ұпай және жеңіс",
        "reward": "Марапат",
        "tokens_for": "Жетондар",
        "diploma_for": "Мақтау қағазы",
        "differentiation": "Саралау",
        "support": "Қиналатындарға",
        "challenge": "Тез орындағандарға",
        "print": "Басып шығарып, қиып алыңыз",
        "copies": "Саны",
        "cut_grid": "Сызықтар бойымен қиыңыз.",
        "cut_none": "Қимаңыз: парақ тұтас қалады.",
        "host_key": "Жүргізушіге арналған кілт",
        "item": "Не тексеріледі",
        "answer": "Жауап",
        "prizes": "Сыйлықтар",
        "tokens": "Жетондар",
        "tokens_note": "Сызықтар бойымен қиыңыз. Жетон не үшін беріледі:",
        "die": "Ойын текшесі",
        "die_note": (
            "Айқышты сыртқы контуры бойымен қиып, сызықтар бойымен бүгіңіз"
            " де, скотчпен бекітіңіз. Кез келген үстел ойынының текшесі де жарайды."
        ),
        "coins": "Ойын ақшасы",
        "coins_note": "Сызықтар бойымен қиыңыз.",
        "currency": "тг",
        "diploma": "Мақтау қағазы",
        "awarded": "марапатталады",
        "name_hint": "(аты-жөні)",
        "in_game": "«{title}» ойынында",
        "date": "Күні",
    },
}

LABEL_W = 5.0
ROLE_COLS = (4.0, 4.5, TEXT_W - 8.5)
KEY_COLS = (TEXT_W - 5.5, 5.5)

# Шрифт карточки по числу столбцов: карточку читают с парты, а не с листа
# в руке учителя — крупно, но так, чтобы пример в строку влез в ячейку
CARD_PT = {1: 18, 2: 24, 3: 20, 4: 16}
# Длинный текст на карточке (задача словами) крупным кеглем не влезет
LONG_CARD = 40
LONG_CARD_PT = 14
# Высота вырезаемой карточки: меньше трёх сантиметров не удержат детские пальцы
CARD_H = Cm(3.0)

# Жетоны: сетка на лист, крупная надпись
TOKEN_COLS, TOKEN_ROWS = 5, 8
TOKEN_H = Cm(2.8)
# Монеты: по десять штук каждого номинала, пять в строке
COINS_EACH, COIN_COLS = 10, 5
# Развёртка кубика крестом: (строка, столбец) → грань. Противоположные грани
# в сумме дают семь, как у настоящего кубика: 1–6 и 2–5 в столбце, 3–4 по бокам
DIE_FACES = {(0, 1): "1", (1, 0): "3", (1, 1): "2", (1, 2): "4", (2, 1): "6", (3, 1): "5"}
DIE_SIDE = 4.0


def render_game(content: dict, header: DocHeader) -> bytes:
    labels = LABELS[header.lang]
    doc = Document()
    common.page(doc)
    common.base_style(doc, header.lang)
    title = content.get("title") or ""

    _title(doc, title, labels)
    _head_table(doc, content, header, labels)
    _teacher_part(doc, content, labels)
    _printables(doc, content.get("printables") or [], labels)
    rules = content.get("rules") or {}
    if rules.get("needs_die"):
        _die_net(doc, labels)
    if rules.get("coins"):
        _coins(doc, rules["coins"], labels)
    _host_key(doc, content.get("host_key") or [], labels)
    reward = content.get("reward") or {}
    _tokens(doc, reward, labels)
    _diploma(doc, title, reward, header, labels)

    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def _title(doc, title: str, labels: dict) -> None:
    kind = doc.add_paragraph()
    kind.alignment = WD_ALIGN_PARAGRAPH.CENTER
    common.run(kind, labels["kind"], italic=True)
    if title:
        name = doc.add_paragraph()
        name.alignment = WD_ALIGN_PARAGRAPH.CENTER
        common.run(name, title, bold=True, size=Pt(18))
    doc.add_paragraph()


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
                f"{item.get('code', '')}{item.get('marker', '')} {item.get('text', '')}".strip()
                for item in content.get("learning_objectives") or []
            ],
        ),
        ("teacher", [header.teacher_name]),
    ]
    _label_rows(doc, [(labels[key], [line for line in value if line]) for key, value in rows])


def _teacher_part(doc, content: dict, labels: dict) -> None:
    _heading(doc, labels["for_teacher"])

    placement = content.get("placement") or {}
    stage = placement.get("stage")
    where = labels.get(stage, "") if stage else ""
    if placement.get("minutes"):
        where = f"{where}, {placement['minutes']} {labels['minutes']}".strip(", ")
    preparation = content.get("preparation") or {}
    rules = content.get("rules") or {}
    props = list(preparation.get("props") or [])
    # Кубик и деньги рисует сервер, но учителю о них надо сказать тут же,
    # где перечислено, что ещё понадобится
    if rules.get("needs_die"):
        props.append(labels["die_prop"])
    if rules.get("coins"):
        props.append(labels["coins_prop"])
    noise = preparation.get("noise")
    reward = content.get("reward") or {}
    differentiation = content.get("differentiation") or {}

    _label_rows(
        doc,
        [
            (labels["story"], [content.get("story") or ""]),
            (labels["placement"], [where, placement.get("fit") or ""]),
            (labels["mechanic"], [content.get("mechanic") or ""]),
        ],
    )
    _roles(doc, content.get("roles") or [], labels)

    _subheading(doc, labels["rules"])
    for number, step in enumerate(rules.get("steps") or [], start=1):
        common.run(doc.add_paragraph(), f"{number}. {step}")

    _subheading(doc, labels["preparation"])
    _label_rows(
        doc,
        [
            (labels["cutting"], [preparation.get("cutting") or ""]),
            (labels["sets"], [preparation.get("sets") or ""]),
            (labels["room"], [preparation.get("room") or ""]),
            (labels["noise"], [labels.get(noise, "") if noise else ""]),
            (labels["props"], common.bullets(props)),
        ],
    )

    doc.add_paragraph()
    _label_rows(
        doc,
        [
            (labels["scoring"], [content.get("scoring") or ""]),
            (
                labels["reward"],
                [
                    _pair(labels["tokens_for"], reward.get("tokens_for")),
                    _pair(labels["diploma_for"], reward.get("diploma_text")),
                ],
            ),
            (
                labels["differentiation"],
                [
                    _pair(labels["support"], differentiation.get("support")),
                    _pair(labels["challenge"], differentiation.get("challenge")),
                ],
            ),
        ],
    )


def _roles(doc, roles: list, labels: dict) -> None:
    if not roles:
        return
    _subheading(doc, labels["roles"])
    table = common.table(doc, 3, ROLE_COLS)
    head = table.add_row().cells
    for cell, key in zip(head, ("role", "who", "does"), strict=True):
        common.label(cell, labels[key], center=True)
    for role in roles:
        cells = table.add_row().cells
        for cell, key in zip(cells, ("name", "who", "does"), strict=True):
            common.lines(cell, [role.get(key) or ""])
    common.widths(table, ROLE_COLS)


def _printables(doc, printables: list, labels: dict) -> None:
    """Каждый лист печати — с новой страницы: учитель печатает его отдельно
    и столько раз, сколько сказано в «Сколько»."""
    for item in printables:
        cells = [cell for cell in item.get("cells") or [] if cell]
        if not cells:
            continue
        _page_break(doc)
        _heading(doc, f"{labels['print']}: {item.get('title') or ''}".rstrip(": "))
        note = doc.add_paragraph()
        common.run(note, item.get("instructions") or "", italic=True, size=Pt(10))
        grid = item.get("cut") == "grid"
        copies = item.get("copies") or ""
        tail = labels["cut_grid"] if grid else labels["cut_none"]
        common.run(
            doc.add_paragraph(),
            f"{labels['copies']}: {copies}. {tail}" if copies else tail,
            italic=True,
            size=Pt(10),
        )
        doc.add_paragraph()
        columns = item.get("columns") or 3
        columns = columns if columns in CARD_PT else 3
        _card_grid(doc, cells, columns, height=CARD_H if grid else None)


def _card_grid(doc, cells: list[str], columns: int, *, height=None, size=None) -> None:
    """Карточки таблицей: рамки — линии реза, текст по центру ячейки.
    Недостающие в последней строке ячейки остаются пустыми."""
    width = TEXT_W / columns
    table = common.table(doc, columns, (width,) * columns)
    for start in range(0, len(cells), columns):
        row = table.add_row()
        if height is not None:
            row.height = height
            row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
        for cell, text in zip(row.cells, cells[start : start + columns], strict=False):
            pt = size or (LONG_CARD_PT if len(text) > LONG_CARD else CARD_PT[columns])
            _centered(cell, text, pt)
    common.widths(table, (width,) * columns)


def _centered(cell, text: str, pt: int, *, bold=True) -> None:
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    for number, line in enumerate(text.split("\n")):
        paragraph = cell.paragraphs[0] if number == 0 else cell.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        common.run(paragraph, line, bold=bold, size=Pt(pt))


def _die_net(doc, labels: dict) -> None:
    """Развёртка кубика крестом 3×4. Рамки только у граней: пустые клетки
    таблицы не режутся, и контур креста — это линия реза."""
    _page_break(doc)
    _heading(doc, labels["die"])
    common.run(doc.add_paragraph(), labels["die_note"], italic=True, size=Pt(10))
    doc.add_paragraph()
    table = doc.add_table(rows=4, cols=3)
    table.autofit = False
    for column in table.columns:
        column.width = Cm(DIE_SIDE)
    for number, row in enumerate(table.rows):
        row.height = Cm(DIE_SIDE)
        row.height_rule = WD_ROW_HEIGHT_RULE.EXACTLY
        for column, cell in enumerate(row.cells):
            cell.width = Cm(DIE_SIDE)
            face = DIE_FACES.get((number, column))
            if face is not None:
                _cell_borders(cell)
                _centered(cell, face, 48)


def _coins(doc, coins: list, labels: dict) -> None:
    values = [value for value in coins if isinstance(value, int) and value > 0]
    if not values:
        return
    _page_break(doc)
    _heading(doc, labels["coins"])
    common.run(doc.add_paragraph(), labels["coins_note"], italic=True, size=Pt(10))
    doc.add_paragraph()
    cells = [f"{value} {labels['currency']}" for value in values for _ in range(COINS_EACH)]
    _card_grid(doc, cells, COIN_COLS, height=Cm(2.5), size=18)


def _host_key(doc, items: list, labels: dict) -> None:
    """Ключ — отдельной страницей перед призами: учитель держит его у себя,
    а карточки раздаёт."""
    if not items:
        return
    _page_break(doc)
    _heading(doc, labels["host_key"])
    table = common.table(doc, 2, KEY_COLS)
    head = table.add_row().cells
    common.label(head[0], labels["item"], center=True)
    common.label(head[1], labels["answer"], center=True)
    common.repeat_header(table.rows[0])
    for item in items:
        cells = table.add_row().cells
        common.lines(cells[0], [item.get("item") or ""])
        common.lines(cells[1], [item.get("answer") or ""])
    common.widths(table, KEY_COLS)


def _tokens(doc, reward: dict, labels: dict) -> None:
    """Лист жетонов — в каждом файле игры, даже если модель о призах забыла:
    награда в конце игры обещана брифом (раздел 4.3)."""
    _page_break(doc)
    _heading(doc, f"{labels['prizes']}. {labels['tokens']}")
    note = doc.add_paragraph()
    common.run(note, labels["tokens_note"], italic=True, size=Pt(10))
    if reward.get("tokens_for"):
        common.run(note, f" {reward['tokens_for']}", italic=True, size=Pt(10))
    doc.add_paragraph()
    label = reward.get("token_label") or "+1"
    _card_grid(doc, [label] * (TOKEN_COLS * TOKEN_ROWS), TOKEN_COLS, height=TOKEN_H, size=16)


def _diploma(doc, title: str, reward: dict, header: DocHeader, labels: dict) -> None:
    """Грамота — рамкой из одной ячейки на весь лист. Имя победителя и дату
    учитель впишет от руки: имён детей у продукта нет и быть не должно."""
    _page_break(doc)
    table = common.table(doc, 1, (TEXT_W,))
    cell = table.add_row().cells[0]
    table.rows[0].height = Cm(22.0)
    table.rows[0].height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    common.widths(table, (TEXT_W,))

    def line(text: str, *, pt: int = 14, bold=False, italic=False, before: int = 0):
        paragraph = cell.paragraphs[0] if not cell.paragraphs[0].text else cell.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_before = Pt(before)
        common.run(paragraph, text, bold=bold, italic=italic, size=Pt(pt))

    game = labels["in_game"].format(title=title) if title else ""
    reason = reward.get("diploma_text") or ""
    line(labels["diploma"].upper(), pt=40, bold=True)
    # Порядок слов у языков разный: по-русски «награждается» — перед именем,
    # по-казахски «марапатталады» закрывает фразу после имени и заслуги
    if header.lang == "kz":
        line("_" * 36, pt=16, before=36)
        line(labels["name_hint"], pt=10, italic=True)
        line(game, pt=16, before=18)
        line(reason, pt=16)
        line(labels["awarded"], pt=18, bold=True, before=12)
    else:
        line(labels["awarded"], pt=18, bold=True, before=36)
        line("_" * 36, pt=16, before=18)
        line(labels["name_hint"], pt=10, italic=True)
        line(reason, pt=16, before=18)
        line(game, pt=16)
    line(f"{labels['topic']}: {header.topic}", pt=12, before=36)
    line(f"{labels['date']}: ____________", pt=12, before=36)
    line(f"{labels['teacher']}: {header.teacher_name}", pt=12, before=6)


def _label_rows(doc, rows: list[tuple[str, list[str]]]) -> None:
    table = common.table(doc, 2, (LABEL_W, TEXT_W - LABEL_W))
    for label, lines in rows:
        left, right = table.add_row().cells
        common.label(left, label)
        common.lines(right, [line for line in lines if line])
    common.widths(table, (LABEL_W, TEXT_W - LABEL_W))


def _pair(label: str, text: str | None) -> str:
    return f"{label}: {text}" if text else ""


def _heading(doc, text: str) -> None:
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(12)
    paragraph.paragraph_format.space_after = Pt(6)
    common.run(paragraph, text, bold=True, size=Pt(14))


def _subheading(doc, text: str) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(12)
    paragraph.paragraph_format.space_after = Pt(4)
    common.run(paragraph, text, bold=True)


def _page_break(doc) -> None:
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def _cell_borders(cell) -> None:
    """Рамка одной ячейки — у развёртки кубика режутся только грани."""
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for edge in ("top", "left", "bottom", "right"):
        line = OxmlElement(f"w:{edge}")
        line.set(qn("w:val"), "single")
        line.set(qn("w:sz"), "8")
        line.set(qn("w:space"), "0")
        line.set(qn("w:color"), "000000")
        borders.append(line)
    tc_pr.append(borders)
