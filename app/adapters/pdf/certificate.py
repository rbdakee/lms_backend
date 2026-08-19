"""Сертификат бумагой: один зашитый шаблон, A4 альбомный (DESIGN_BRIEF, 5.11).

Чистая функция над готовыми данными: ни базы, ни хранилища, ни FastAPI —
на входе снимок из строки сертификата и байты трёх картинок, на выходе байты
PDF. Порта под неё нет: fpdf2 — библиотека, а не внешняя система, и заводить
интерфейс под будущее, которого нет в плане, незачем.

Жирного начертания у шрифта нет — в `assets/fonts` лежит только обычный
DejaVuSans, — поэтому `style="B"` не используется вовсе: разницу между
строками делают кегль, цвет и разрядка.
"""

import logging
from datetime import datetime
from io import BytesIO
from pathlib import Path

import segno
from fpdf import FPDF

from app.domain.kz_time import ALMATY
from app.domain.plural import plural

log = logging.getLogger("certificate")

FONT = "DejaVu"
# Покрытие казахских букв (ә ғ қ ң ө ұ ү һ і) проверено: без него ФИО
# и название курса на казахском печатались бы квадратами.
FONT_PATH = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "DejaVuSans.ttf"

# A4 альбомный в миллиметрах.
PAGE_W = 297.0
PAGE_H = 210.0
MARGIN = 22.0
LINE_W = PAGE_W - 2 * MARGIN

INK = (26, 32, 44)  # основной текст
FRAME = (31, 56, 100)  # рамка и заголовок
MUTED = (110, 118, 130)  # подписи под линиями

# QR стоит в средней колонке низа — между подписью слева и печатью справа,
# в единственном свободном месте листа. Выше линии подписи (172) с запасом:
# адрес под кодом не должен вставать с ней в одну строку.
QR_Y = 134.0
# Сторона вместе с белым полем. Символ ссылки проверки — 29 модулей, поле
# 4 модуля с каждой стороны, итого 37: при стороне 30 мм модуль выходит
# 0.81 мм, а сам символ 23.5 мм — телефон снимает такой с бумаги уверенно.
QR_SIDE = 30.0
# Белое поле вокруг символа. Без него сканеры не находят границу кода —
# это самая частая причина «QR не читается», и на бумаге её уже не исправить.
QR_QUIET = 4

TEXTS = {
    "ru": {
        "title": "СЕРТИФИКАТ",
        "intro": "Настоящий сертификат подтверждает, что",
        "passed": "прошёл(-ла) курс повышения квалификации",
        "issued_at": "Дата выдачи",
        "number": "Номер документа",
        "sign": "Подпись",
        "stamp": "М.П.",
        "verify": "Проверить подлинность",
    },
    # Казахские строки сочинены при верстке шаблона и ждут вычитки владельцем.
    "kz": {
        "title": "СЕРТИФИКАТ",
        "intro": "Осы сертификат мынаны растайды:",
        "passed": "біліктілікті арттыру курсынан өтті",
        "issued_at": "Берілген күні",
        "number": "Құжат нөмірі",
        "sign": "Қолы",
        "stamp": "М.О.",
        "verify": "Түпнұсқалығын тексеру",
    },
}


def render_certificate(
    document: dict, images: dict[str, bytes | None], verify_url: str | None = None
) -> bytes:
    """Байты PDF по снимку сертификата.

    `document` — то, что записано в строке сертификата: `holder_name`,
    `course_title`, `hours`, `issued_at`, `number`, `lang`. Живых таблиц
    здесь нет по определению: курс переименуют или удалят — документ
    обязан остаться прежним.

    `images` — байты картинок настроек по слотам `logo`, `sign`, `stamp`.
    Любой из них может быть None, и тогда место остаётся пустым.

    `verify_url` — адрес страницы проверки этого документа, он уходит в QR
    (DESIGN_BRIEF, 5.11). Пусто — кода на листе нет: QR, ведущий в никуда,
    хуже отсутствующего, потому что с бумаги его уже не исправить.
    """
    words = TEXTS.get(document["lang"], TEXTS["ru"])

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    # Шаблон одностраничный: перенос строки на вторую страницу его сломает
    pdf.set_auto_page_break(False)
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.add_font(FONT, "", str(FONT_PATH))
    pdf.add_page()

    _frame(pdf)
    _place_image(pdf, images.get("logo"), "logo", x=(PAGE_W - 46) / 2, y=20, w=46, h=18)

    _line(pdf, y=44, size=30, color=FRAME, text=_spaced(words["title"]))
    _line(pdf, y=62, size=11, color=MUTED, text=words["intro"])

    name = document["holder_name"]
    bottom = _block(pdf, y=72, size=_name_size(name), color=INK, text=name)
    _rule(pdf, y=bottom + 3, width=150)

    _line(pdf, y=bottom + 9, size=11, color=MUTED, text=words["passed"])
    title = document["course_title"]
    bottom = _block(pdf, y=bottom + 18, size=_title_size(title), color=FRAME, text=f"«{title}»")
    _line(pdf, y=bottom + 4, size=12, color=INK, text=_hours(document["hours"], document["lang"]))

    _signature(pdf, words, images)
    _verify_block(pdf, words, verify_url)
    _footer(pdf, words, document)

    return bytes(pdf.output())


# -- части шаблона ---------------------------------------------------------


def _frame(pdf: FPDF) -> None:
    """Двойная рамка: толстая снаружи, тонкая внутри."""
    pdf.set_draw_color(*FRAME)
    pdf.set_line_width(1.2)
    pdf.rect(10, 10, PAGE_W - 20, PAGE_H - 20)
    pdf.set_line_width(0.3)
    pdf.rect(13.5, 13.5, PAGE_W - 27, PAGE_H - 27)


def _signature(pdf: FPDF, words: dict, images: dict[str, bytes | None]) -> None:
    """Место под подпись и печать: картинки настроек, а под ними — линия
    с подписью и «М.П.». Картинок может не быть, места остаются пустыми."""
    _place_image(pdf, images.get("sign"), "sign", x=52, y=150, w=56, h=20)
    _rule(pdf, y=172, width=80, center=80)
    _line(pdf, y=173, size=9, color=MUTED, text=words["sign"], x=40, width=80)

    # Печать заканчивается там же, где линия подписи: подписи под ними
    # стоят на одной строке, иначе низ документа выглядит перекошенным
    _place_image(pdf, images.get("stamp"), "stamp", x=197, y=140, w=40, h=32)
    _line(pdf, y=173, size=9, color=MUTED, text=words["stamp"], x=177, width=80)


def _verify_block(pdf: FPDF, words: dict, verify_url: str | None) -> None:
    """QR на страницу проверки и короткий адрес под ним.

    Стоит между подписью и печатью — в единственном свободном месте низа.
    Без адреса проверки блока нет вовсе, и лист от этого не разъезжается:
    место просто остаётся пустым, как у неустановленной печати.
    """
    if not verify_url:
        return

    _qr(pdf, verify_url, x=(PAGE_W - QR_SIDE) / 2, y=QR_Y, side=QR_SIDE)
    # Короткий адрес без протокола — его читают глазами, когда сканировать
    # нечем; на экране сертификата стоит ровно он же (frontend, useOrigin)
    _line(pdf, y=QR_Y + QR_SIDE + 1, size=8, color=INK, text=_verify_host(verify_url))
    _line(pdf, y=QR_Y + QR_SIDE + 5, size=7.5, color=MUTED, text=words["verify"])


def _qr(pdf: FPDF, url: str, *, x: float, y: float, side: float) -> None:
    """Символ QR прямоугольниками, а не картинкой: документ печатают,
    и вектор остаётся чётким на любом размере.

    Уровень коррекции M — тот же, что у кода на экране сертификата, чтобы
    бумага и экран давали один и тот же символ.
    """
    rows = [list(row) for row in segno.make(url, error="m").matrix_iter(border=QR_QUIET)]
    module = side / len(rows)

    # Белое поле рисуется явно: лист может быть не белым в месте кода,
    # а сканеру нужна чистая рамка вокруг символа
    pdf.set_fill_color(255, 255, 255)
    pdf.rect(x, y, side, side, style="F")

    pdf.set_fill_color(0, 0, 0)
    for row_index, row in enumerate(rows):
        # Подряд идущие тёмные модули рисуются одним прямоугольником: между
        # соседними fpdf оставляет волосяной шов, и по нему сканер спотыкается
        column = 0
        while column < len(row):
            if not row[column]:
                column += 1
                continue
            start = column
            while column < len(row) and row[column]:
                column += 1
            pdf.rect(
                x + start * module,
                y + row_index * module,
                (column - start) * module,
                module,
                style="F",
            )


def _footer(pdf: FPDF, words: dict, document: dict) -> None:
    """Дата и номер по нижнему краю: дата слева, номер справа."""
    pdf.set_font(FONT, "", 10)
    pdf.set_text_color(*MUTED)
    pdf.set_xy(MARGIN, 186)
    pdf.cell(LINE_W / 2, 6, f"{words['issued_at']}: {_date(document['issued_at'])}", align="L")
    pdf.set_xy(MARGIN + LINE_W / 2, 186)
    pdf.cell(LINE_W / 2, 6, f"{words['number']}: {document['number']}", align="R")


# -- кирпичики -------------------------------------------------------------


def _line(
    pdf: FPDF,
    *,
    y: float,
    size: float,
    color: tuple[int, int, int],
    text: str,
    x: float = MARGIN,
    width: float = LINE_W,
) -> None:
    """Строка по центру отведённой ширины — она же не переносится."""
    pdf.set_font(FONT, "", size)
    pdf.set_text_color(*color)
    pdf.set_xy(x, y)
    pdf.cell(width, size * 0.45, text, align="C")


def _block(
    pdf: FPDF, *, y: float, size: float, color: tuple[int, int, int], text: str
) -> float:
    """Абзац по центру с переносом; возвращает нижнюю границу.

    ФИО и название курса приходят снимком и бывают длинными: следующие
    строки шаблона встают от того места, где этот абзац кончился.
    """
    pdf.set_font(FONT, "", size)
    pdf.set_text_color(*color)
    pdf.set_xy(MARGIN, y)
    pdf.multi_cell(LINE_W, size * 0.5, text, align="C")
    return pdf.get_y()


def _rule(pdf: FPDF, *, y: float, width: float, center: float = PAGE_W / 2) -> None:
    pdf.set_draw_color(*MUTED)
    pdf.set_line_width(0.2)
    pdf.line(center - width / 2, y, center + width / 2, y)


def _place_image(
    pdf: FPDF, data: bytes | None, slot: str, *, x: float, y: float, w: float, h: float
) -> None:
    """Картинка в отведённое место — или пустое место.

    Настройки пускают в эти слоты только png и jpeg, но битый файл под ними
    остаётся возможным. Отсутствие печати не повод отказать человеку
    в сертификате, поэтому неудача вставки гасится здесь: в лог уходит слот
    и сам факт, без байтов и без ФИО (правило ПД).
    """
    if data is None:
        return
    try:
        pdf.image(BytesIO(data), x=x, y=y, w=w, h=h, keep_aspect_ratio=True)
    except Exception:
        log.warning("Картинка сертификата не вставилась, слот %s", slot)


# -- текст -----------------------------------------------------------------


def _verify_host(verify_url: str) -> str:
    """Адрес для печати: без протокола и без номера — «domain.kz/verify»,
    как на экране сертификата (frontend, useOrigin).

    Номер отрезается, а не отбрасывается вместе со всем путём: адрес
    проверки может стоять и не в корне домена.
    """
    base = verify_url.rpartition("/verify/")[0] or verify_url
    return f"{base.split('://', 1)[-1].strip('/')}/verify"


def _spaced(text: str) -> str:
    """Разрядка вместо жирного: файла жирного начертания рядом со шрифтом нет."""
    return " ".join(text)


def _hours(hours: int, lang: str) -> str:
    if lang == "kz":
        # В казахском счётное существительное не склоняется: «72 сағат»
        return f"көлемі {hours} сағат"
    return f"объёмом {hours} {plural(hours, 'час', 'часа', 'часов')}"


def _date(issued_at: datetime) -> str:
    """Дата по казахстанскому времени (BACKEND_NOTES, раздел 7): в 01:00
    по Алматы документ, выданный час назад, не должен оказаться вчерашним.

    Формат числовой — так не приходится переводить названия месяцев
    на казахский и выдумывать склонения.
    """
    return issued_at.astimezone(ALMATY).strftime("%d.%m.%Y")


def _name_size(holder_name: str) -> float:
    # ФИО целиком бывает длинным: кегль подбирается так, чтобы имя обычной
    # длины уместилось в строку, а не рассыпалось на три
    return 26 if len(holder_name) <= 34 else 20


def _title_size(course_title: str) -> float:
    return 17 if len(course_title) <= 60 else 13
