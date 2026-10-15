"""Разбор типовой учебной программы: приказ Министра просвещения РК № 399
от 16.09.2022. Предмет выбирается из реестра `SUBJECTS` — по нему же приложение
находится среди ~130 приложений акта.

Данные берёт не этот модуль: граница адаптера проходит по разбору, скачивание —
забота вызывающего кода. Откуда брать:

    GET https://adilet.zan.kz/api/documents/by-ngr/{НГР}?language=rus|kaz

НГР приказа — V2200029767, uuid русской редакции dc1f4914-3cc8-4592-b0c5-4a4e62e0b573,
казахской — 663d1192-e6c3-4d03-bab9-4d8f3bbd0a24. В ответе весь текст акта лежит
одной строкой HTML в `document.text_content` — она и приходит сюда.

У предметов разная вёрстка таблиц: одна или несколько таблиц целей, разделы
строкой во всю ширину или колонкой, подписи «Подраздел» / «Направление» /
«Ключевые навыки». Разбор покрывает эти семейства; что не узнал, складывает
в `anomalies`. Молча неверного ответа не будет — неверная программа в базе
дороже отказа её разобрать.

Про базу и HTTP модуль не знает ничего: на входе строка, на выходе `dict`.
Только стандартная библиотека — `html.parser` и `re`.

    uv run python -m app.adapters.curriculum.parser doc399.json --lang ru -o math_ru.json
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

__all__ = ["LANGUAGES", "CurriculumParseError", "parse_curriculum", "parse_curriculum_file"]

log = logging.getLogger("curriculum")

Rules = dict[str, re.Pattern[str]]


class CurriculumParseError(RuntimeError):
    """Программу разобрать не удалось: не тот документ, не то приложение или
    сменившаяся вёрстка. Отказ, а не половина данных: наполовину разобранная
    программа в базе тише и дороже."""


# --------------------------------------------------------------------------
# Языковые словарики
# --------------------------------------------------------------------------
# Документ существует в двух редакциях с разной разметкой заголовков, но с
# одинаковой структурой таблиц. Всё языкозависимое собрано здесь, чтобы
# добавление третьего языка не требовало правок в логике разбора.

_RULES: dict[str, Rules] = {
    "ru": {
        "appendix_label": re.compile(r"Приложение\s*(\d+(?:\s*-\s*\d+)?)", re.I),
        # «Глава 2.» и «Параграф 1.» — в одном <h3> их бывает несколько подряд
        "heading_split": re.compile(r"(?=(?:Глава|Параграф)\s+\d+\s*\.)"),
        "note_prefix": re.compile(r"^(Примечание|Сноска)\b", re.I),
        "order_date_text": re.compile(r"от\s+\d{1,2}\s+\S+\s+\d{4}\s+года\s*№\s*\S+"),
        # шапка таблицы целей: «1 класс» ... «4 класс»
        "grade_header": re.compile(r"(\d)\s*[-–—]?\s*класс", re.I),
        # «Подраздел», но у физкультуры — «Направление», у языков — «Ключевые навыки»,
        # у однолетних программ первого класса — «Основные навыки»
        "subsection_header": re.compile(
            r"Подраздел|Направление|Ключевые\s+навык|Основные\s+навык", re.I),
        # глава приказа о варианте программы с сокращённой нагрузкой: цели в нём
        # те же, и разбор взял бы каждую дважды
        "reduced_load": re.compile(r"с\s+сокращением\s+учебной\s+нагрузки", re.I),
        # «1 четверть»
        "quarter_row": re.compile(r"^(\d)\s*-?\s*четверт", re.I),
        # «1) 1 класс:» перед таблицей долгосрочного плана
        "ltp_grade": re.compile(r"(\d)\s*-?\s*класс\s*:?\s*$", re.I),
        "ltp_goals_header": re.compile(r"Цели\s+обучения", re.I),
    },
    "kz": {
        "appendix_label": re.compile(r"(\d+(?:\s*-\s*\d+)?)\s*-?\s*қосымша", re.I),
        "heading_split": re.compile(r"(?=\d+\s*-\s*(?:тарау|параграф)\s*\.)"),
        "note_prefix": re.compile(r"^Ескерту\b", re.I),
        "order_date_text": re.compile(r"\d{4}\s+жылғы\s+\d{1,2}\s+\S+\s*№\s*\S+"),
        "grade_header": re.compile(r"(\d)\s*[-–—]?\s*сынып", re.I),
        "subsection_header": re.compile(
            r"Бөлімше|Бағыт|Кілттік\s+дағды|Негізгі\s+дағды", re.I),
        "reduced_load": re.compile(r"төмендетілген\s+оқу\s+жүктемесімен", re.I),
        "quarter_row": re.compile(r"^(\d)\s*-?\s*тоқсан", re.I),
        "ltp_grade": re.compile(r"(\d)\s*-?\s*сынып\s*:?\s*$", re.I),
        "ltp_goals_header": re.compile(r"Оқу\s+мақсаттар", re.I),
    },
}

# Строки во всю ширину, начинающие раздел программы. Форматов два:
# «Раздел 1: Числа и величины» (математика) и «1) „Я и общество“»
# (познание мира, естествознание). Общие на обе редакции.
_SECTION_ROWS: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    (
        re.compile(r"^Раздел\s*(\d+)\s*[:.\-–]?\s*(.+)$", re.I | re.S),
        re.compile(r"^(\d+)\s*-\s*бөлім\s*[:.\-–]?\s*(.+)$", re.I | re.S),
    ),
    (
        re.compile(r'^(\d+)\s*\)\s*[«"“"]?\s*(.+)$', re.I | re.S),
        re.compile(r'^(\d+)\s*\)\s*[«"“"]?\s*(.+)$', re.I | re.S),
    ),
)

# --------------------------------------------------------------------------
# Предметы
# --------------------------------------------------------------------------
# Название предмета в приложении приказа — по нему программа находится среди
# ~130 приложений. Названия в редакциях разные не переводом, а по существу:
# «Трудовое обучение» и «Еңбекке баулу» — один предмет.

@dataclass(frozen=True)
class SubjectSpec:
    """Как найти приложение предмета и как назвать его для человека.

    Две пары названий намеренно разделены: `title`/`title_kz` видит учитель,
    `name_ru`/`name_kz` — формулировка из самого приказа, по которой парсер
    находит приложение среди ~130. Показать приказу «Казахский язык (Т1)»
    нельзя: в тексте акта такой скобки нет и приложение не найдётся.
    """

    code: str
    # Русское название для показа; оно же лежит в базе в curriculum_subject.title
    title: str
    # Казахское название для показа. По умолчанию совпадает с name_kz —
    # отдельное значение нужно только тем, кого по-казахски называют иначе
    title_kz: str
    # Названия предмета в редакциях приказа — только для поиска приложения
    name_ru: str
    name_kz: str
    grades: tuple[int, ...]
    # Язык обучения, если у предмета несколько программ по нему: «Казахский язык»
    # в школах с казахским и с неказахским языком обучения — разные приложения
    # с разными целями. Пара регулярок (русская редакция, казахская) для скобки
    # в шапке приложения; None — у предмета программа одна.
    instruction: tuple[str, str] | None = None
    # Язык, на котором написаны сами цели, если он не язык редакции: цели
    # иностранных языков в приказе — на изучаемом языке в обеих редакциях.
    # None — цели на языке редакции (русская по-русски, казахская по-казахски)
    objectives_language: str | None = None


def _subject(code: str, title: str, name_ru: str, name_kz: str,
             grades: tuple[int, ...],
             instruction: tuple[str, str] | None = None,
             objectives_language: str | None = None, *,
             title_kz: str | None = None) -> SubjectSpec:
    """Запись реестра. Казахское название для показа не задано — значит
    по-казахски предмет звучит как в приказе, и это самая частая ситуация."""
    return SubjectSpec(code=code, title=title, title_kz=title_kz or name_kz,
                       name_ru=name_ru, name_kz=name_kz,
                       grades=grades, instruction=instruction,
                       objectives_language=objectives_language)


# Скобка языка обучения в шапке приложения. «С казахским» не лежит внутри
# «с неказахским», поэтому пары друг друга не перекрывают.
_INSTR_KZ = (r"с\s+казахским\s+языком\s+обучения", r"оқыту\s+қазақ\s+тілінде\s*\)")
_INSTR_NOT_KZ = (r"с\s+неказахским\s+языком\s+обучения",
                 r"оқыту\s+қазақ\s+тілінде\s+емес")
_INSTR_RU = (r"с\s+русским\s+языком\s+обучения", r"оқыту\s+орыс\s+тілінде\s*\)")
_INSTR_NOT_RU = (r"с\s+нерусским\s+языком\s+обучения",
                 r"оқыту\s+орыс\s+тілінде\s+емес")


_SUBJECTS: dict[str, SubjectSpec] = {
    spec.code: spec
    for spec in (
        _subject("math", "Математика", "Математика", "Математика", (1, 2, 3, 4)),
        _subject("music", "Музыка", "Музыка", "Музыка", (1, 2, 3, 4)),
        _subject("labor", "Трудовое обучение", "Трудовое обучение",
                 "Еңбекке баулу", (1, 2, 3, 4)),
        _subject("art", "Изобразительное искусство", "Изобразительное искусство",
                 "Бейнелеу өнері", (1, 2, 3, 4)),
        # «Художественный труд» 3-4 сюда не входит: приложение 32 приказа № 399
        # исключено приказом № 323 от 05.11.2024 (действует с 01.09.2025)
        _subject("physical_culture", "Физическая культура", "Физическая культура",
                 "Дене шынықтыру", (1, 2, 3, 4)),
        _subject("world_around", "Познание мира", "Познание мира",
                 "Дүниетану", (1, 2, 3, 4)),
        _subject("natural_science", "Естествознание", "Естествознание",
                 "Жаратылыстану", (1, 2, 3, 4)),
        _subject("digital_literacy", "Цифровая грамотность",
                 "Цифровая грамотность", "Цифрлық сауаттылық", (1, 2, 3, 4)),
        # Языковые предметы. Вариант программы по языку обучения — это отдельный
        # предмет со своим кодом, а не поле: схема базы одна на все предметы,
        # а цели у вариантов разные («Казахский язык» для школ с русским
        # обучением — язык как второй, с уровнями А1–А2, и вовсе другой набор
        # целей, чем «Қазақ тілі» для своих). Суффикс _l1 — язык обучения совпадает
        # с изучаемым, _l2 — изучаемый язык не родной для школы.
        # В показе владелец называет варианты по-учительски: Т1/Т2 и Я1/Я2,
        # скобка про язык обучения там не нужна (решение владельца 16.10.2026).
        # Скобки в `name_ru`/`name_kz` не появляются — это названия приказа.
        _subject("literacy_ru", "Обучение грамоте",
                 "Обучение грамоте", "Обучение грамоте", (1,), _INSTR_RU,
                 title_kz="Сауат ашу"),
        _subject("native_language_kz", "Ана тілі",
                 "Ана тілі", "Ана тілі", (1,), _INSTR_KZ),
        _subject("kazakh_lang_l1", "Казахский язык (Т1)",
                 "Казахский язык", "Қазақ тілі", (2, 3, 4), _INSTR_KZ,
                 title_kz="Қазақ тілі (Т1)"),
        _subject("kazakh_lang_l2", "Казахский язык (Т2)",
                 "Казахский язык", "Қазақ тілі", (1, 2, 3, 4), _INSTR_NOT_KZ,
                 title_kz="Қазақ тілі (Т2)"),
        # Литературное чтение — два разных предмета: русская программа и
        # казахская. Для показа скобок не нужно вовсе: русскую программу
        # в казахском интерфейсе тоже называют «Литературное чтение»,
        # казахскую — «Әдебиеттік оқу» в обоих языках
        _subject("literary_reading_kz", "Әдебиеттік оқу",
                 "Литературное чтение", "Әдебиеттік оқу", (2, 3, 4), _INSTR_KZ),
        _subject("russian_lang_l1", "Русский язык (Я1)",
                 "Русский язык", "Орыс тілі", (2, 3, 4), _INSTR_RU,
                 title_kz="Орыс тілі (Я1)"),
        _subject("russian_lang_l2", "Русский язык (Я2)",
                 "Русский язык", "Орыс тілі", (2, 3, 4), _INSTR_NOT_RU,
                 title_kz="Орыс тілі (Я2)"),
        _subject("literary_reading_ru", "Литературное чтение",
                 "Литературное чтение", "Әдебиеттік оқу", (2, 3, 4), _INSTR_RU,
                 title_kz="Литературное чтение"),
        _subject("english", "Английский язык", "Английский язык", "Ағылшын тілі", (3, 4),
                 objectives_language="en"),
        _subject("german", "Немецкий язык", "Немецкий язык", "Неміс тілі", (3, 4),
                 objectives_language="de"),
        _subject("french", "Французский язык", "Французский язык", "Француз тілі", (3, 4),
                 objectives_language="fr"),
    )
}

SUBJECT_CODES: tuple[str, ...] = tuple(_SUBJECTS)


def objectives_language(code: str, edition: str) -> str:
    """Язык текста целей предмета: `ru`, `kz`, `en`, `de` или `fr`. Поиск по целям
    лексический, поэтому язык запроса должен совпасть с ним, а не с языком чата.
    Предмета нет в реестре — язык редакции."""
    spec = _SUBJECTS.get(code)
    return spec.objectives_language if spec and spec.objectives_language else edition



def subject_title_kz(code: str, fallback: str) -> str:
    """Казахское название предмета для учителя. В базе у предмета одно русское
    название, казахское живёт только в реестре разбора. Скобки о языке обучения
    в показе больше нет — учителя говорят Т1/Т2 и Я1/Я2, и варианты так и
    названы. Кода нет в реестре — отдаём `fallback`, а не пустую строку."""
    spec = _SUBJECTS.get(code)
    return spec.title_kz if spec else fallback


def _appendix_patterns(
    spec: SubjectSpec, lang: str
) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Заголовок приложения ищем по названию предмета и классам сразу:
    «Музыка» встречается и в 1-4, и в 5-6 классах — номер приложения
    при правках приказа меняется, название и классы остаются."""
    lo, hi = spec.grades[0], spec.grades[-1]
    if lang == "ru":
        title = rf'учебн\w*\s+предмет\w*\s*[«"“]?\s*{re.escape(spec.name_ru)}'
        grades = (rf"\b{lo}\s*-\s*{hi}\s*класс" if lo != hi else rf"\b{lo}\s*класс")
    else:
        title = rf'[«"“]?\s*{re.escape(spec.name_kz)}\s*[»"”]?\s*оқу\s+пәні'
        # «1-4-сыныптарына» и «1-4-сыныбына» — обе формы одной шапки
        grades = (rf"\b{lo}\s*-\s*{hi}\s*-?\s*сыны" if lo != hi
                  else rf"\b{lo}\s*-?\s*сыны")
    return re.compile(title, re.I), re.compile(grades, re.I)


def _instruction_pattern(spec: SubjectSpec, lang: str) -> re.Pattern[str] | None:
    if spec.instruction is None:
        return None
    return re.compile(spec.instruction[0 if lang == "ru" else 1], re.I)

LANGUAGES: tuple[str, ...] = tuple(_RULES)

# Код цели: класс.раздел.подраздел.номер. Пробелы вокруг точек реальны —
# в казахской редакции встречается «3.1.2. 1». Звёздочки после кода — пометка
# документа («реализуется частично»), частью кода не являются.
# Отрицательный просмотр назад отсекает хвосты вроде «12.1.1.1» и дат,
# просмотр вперёд — всё длиннее пяти чисел. Пятое число допустимо: так записаны
# две половины цели 3.5.1.2 русского языка («3.5.1.2.1» и «3.5.1.2.2»).
RE_CODE = re.compile(
    r"(?<![\d.])(\d)\s*\.\s*(\d)\s*\.\s*(\d)\s*\.\s*(\d{1,2})"
    r"(?:\.(\d))?(?!\s*\.\s*\d)\s*(\*{1,2})?"
)

# Каждое приложение начинается одинаково: выровненная вправо таблица-«шапка»
# с текстом «Приложение N к приказу…» / «…бұйрығына N-қосымша». Это
# единственная надёжная граница: <h3> у приложений оформлены по-разному,
# в казахской редакции заголовок приложения и «1-тарау» слиты в один <h3>.
RE_APPENDIX_HEAD = re.compile(r'<p align="right">\s*<table\b', re.I)


# --------------------------------------------------------------------------
# HTML → таблицы
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Cell:
    """Ячейка таблицы. Один объект на ячейку, даже если она растянута:
    по `id` объекта видно, что растянутую ячейку уже разобрали."""

    rowspan: int
    colspan: int
    text: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    # строка, в которой ячейка объявлена, а не продолжена растяжкой
    row: int = -1

    @property
    def value(self) -> str:
        return _clean("".join(self.text))


class Table:
    """Таблица, разложенная в сетку с учётом rowspan/colspan."""

    def __init__(self, rows: list[list[Cell]]) -> None:
        self.rows = rows                        # исходные строки: список списков Cell
        self.grid: dict[tuple[int, int], Cell] = {}  # растянутые ячейки продублированы
        self.width = 0
        self._layout()

    def _layout(self) -> None:
        for r, row in enumerate(self.rows):
            c = 0
            for cell in row:
                while (r, c) in self.grid:
                    c += 1
                cell.row = r
                for dr in range(cell.rowspan):
                    for dc in range(cell.colspan):
                        self.grid[(r + dr, c + dc)] = cell
                c += cell.colspan
            self.width = max(self.width, c)

    def at(self, r: int, c: int) -> Cell | None:
        return self.grid.get((r, c))

    def row_text(self, r: int) -> str:
        """Текст строки целиком — для опознания служебных строк."""
        seen: set[int] = set()
        out = []
        for c in range(self.width):
            cell = self.at(r, c)
            if cell is not None and id(cell) not in seen:
                seen.add(id(cell))
                out.append(cell.value)
        return " ".join(x for x in out if x)

    def spans_full_row(self, r: int) -> bool:
        """Строка из одной ячейки во всю ширину — заголовок раздела/четверти."""
        cells = {id(self.at(r, c)) for c in range(self.width) if self.at(r, c) is not None}
        if len(cells) != 1:
            return False
        cell = self.at(r, 0)
        return cell is not None and cell.row == r and cell.colspan >= self.width

    @property
    def height(self) -> int:
        return max((r for r, _ in self.grid), default=-1) + 1


class TableParser(HTMLParser):
    """
    Вытаскивает таблицы из фрагмента акта.

    Разметка адилета — «суп»: <p> внутри ячейки закрывается тегом, который
    открылся снаружи, <br> стоит посреди предложения, формулы вставлены
    картинками. Поэтому дерево не строим вовсе: держим плоский автомат
    table → tr → td и копим текст, а вложенность <p> игнорируем.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []
        self.nested_tables = 0
        self._depth = 0
        self._rows: list[list[Cell]] | None = None
        self._row: list[Cell] | None = None
        self._cell: Cell | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._rows = []
            else:
                # вложенных таблиц в разобранных приложениях нет; если появятся —
                # текст всё равно попадёт в текущую ячейку, но факт фиксируем
                self.nested_tables += 1
        elif tag == "tr" and self._depth == 1 and self._rows is not None:
            self._close_cell()
            self._row = []
            self._rows.append(self._row)
        elif tag in ("td", "th") and self._depth >= 1 and self._row is not None:
            self._close_cell()
            self._cell = Cell(_span(a.get("rowspan")), _span(a.get("colspan")))
            self._row.append(self._cell)
        elif tag == "br":
            self._emit("\n")
        elif tag == "img":
            # формулы и математические знаки (≠, ∩, ∪, ∅) вставлены картинками;
            # подменяем меткой, а адрес сохраняем — иначе знак молча исчезнет
            # из текста цели, и цель поменяет смысл, оставшись правдоподобной
            src = a.get("src") or ""
            self._emit("[img]")
            if self._cell is not None and src:
                self._cell.images.append(src)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._depth == 1 and self._rows is not None:
                self._close_cell()
                self.tables.append(Table(self._rows))
                self._rows = None
                self._row = None
            self._depth = max(0, self._depth - 1)
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag == "p":
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        self._emit(data)

    def _emit(self, s: str) -> None:
        if self._cell is not None:
            self._cell.text.append(s)

    def _close_cell(self) -> None:
        self._cell = None


def _span(v: str | None) -> int:
    try:
        n = int(str(v).strip())
        return n if n > 0 else 1
    except (TypeError, ValueError):
        return 1


def _clean(s: str) -> str:
    """Текст ячейки → одна строка без мусора.

    Неразрывный пробел и пробел нулевой ширины в вёрстке адилета встречаются
    посреди слов: без замены они срывают и сравнение строк, и поиск кодов.
    """
    s = unescape(s)
    s = s.replace("\xa0", " ").replace("​", "")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" \t")


def _strip_tags(s: str) -> str:
    return _clean(re.sub(r"<[^>]+>", " ", s))


# --------------------------------------------------------------------------
# Поиск приложения
# --------------------------------------------------------------------------

def _split_appendices(html_text: str) -> list[tuple[int, int]]:
    """Список (начало, конец) всех приложений акта."""
    starts = [m.start() for m in RE_APPENDIX_HEAD.finditer(html_text)]
    return [(s, starts[i + 1] if i + 1 < len(starts) else len(html_text))
            for i, s in enumerate(starts)]


def _find_appendix(
    html_text: str, rules: Rules, spec: SubjectSpec, lang: str
) -> list[tuple[int, int, str]]:
    """Фрагменты приложений, чья «шапка» совпала с искомой программой."""
    title, grades = _appendix_patterns(spec, lang)
    instruction = _instruction_pattern(spec, lang)
    hits = []
    for start, end in _split_appendices(html_text):
        frag = html_text[start:end]
        # заголовок ищем в первых килобайтах: дальше идёт тело программы,
        # где название предмета встречается десятки раз
        head = _strip_tags(frag[:2500])
        if instruction is not None and not instruction.search(head):
            continue
        if title.search(head) and grades.search(head):
            hits.append((start, end, frag))
    return hits


# --------------------------------------------------------------------------
# Разбор
# --------------------------------------------------------------------------

def _cut_reduced_load(frag: str, rules: Rules) -> str:
    """Приложение языкового предмета кончается главой «… (с сокращением учебной
    нагрузки)»: те же цели и тот же долгосрочный план, урезанный по часам. Берём
    программу с полной нагрузкой — её цели не дублируются, а сокращённый вариант
    остаётся в приказе, но в справочник не идёт."""
    for m in re.finditer(r"<h3[^>]*>(.*?)</h3>", frag, re.S):
        if rules["reduced_load"].search(_strip_tags(m.group(1))):
            return frag[:m.start()]
    return frag


def _parse_code(m: re.Match[str]) -> tuple[str, int, int, int, int, str]:
    """Совпадение RE_CODE → (код, класс, раздел, подраздел, номер, пометка).

    Код пересобираем из групп, а не берём срезом: в исходнике внутри него
    бывают пробелы, а ключ связи с долгосрочным планом должен быть один.
    """
    g, s, ss, n = (int(m.group(i)) for i in range(1, 5))
    marker = m.group(6) or ""
    # у русского языка 3 класса цель 3.5.1.2 разбита на «3.5.1.2.1» и «3.5.1.2.2»:
    # пятое число — часть цели, и без него обе слились бы в одну строку
    part = f".{m.group(5)}" if m.group(5) else ""
    return f"{g}.{s}.{ss}.{n}{part}", g, s, ss, n, marker


def _split_goals(text: str) -> list[dict[str, Any]]:
    """
    Ячейку с целями режем по кодам: код — единственный надёжный разделитель.
    В долгосрочном плане цели в одной ячейке разделены то <br>, то «;», то
    ничем; в таблице целей цель обычно одна, но полагаться на это нельзя.
    """
    ms = list(RE_CODE.finditer(text))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        body = text[m.end():end]
        code, g, s, ss, n, marker = _parse_code(m)
        out.append({
            "code": code, "grade": g, "section_no": s, "subsection_no": ss,
            "index": n, "marker": marker, "text": _tidy_goal(body),
        })
    return out


def _tidy_goal(s: str) -> str:
    """Хвосты разделителей и «висящая» пунктуация от разрезания по коду."""
    s = _clean(s)
    s = re.sub(r"^[\s;:,.\-–—]+", "", s)
    s = re.sub(r"[\s;]+$", "", s)
    return s


# «Раздел (виды речевой деятельности) – письмо», «Бөлім (…) – оқылым»
_UNNUMBERED_SECTION = re.compile(r"^(Раздел|Бөлім)\s*\([^)]*\)\s*[–—-]\s*(.+)$", re.I | re.S)


def _header_title(table: Table, r: int, rules: Rules) -> str:
    """Название раздела из шапки без классов: «Подраздел | Аудирование».
    «Цели обучения» в шапке названием не считается."""
    seen: set[int] = set()
    parts = []
    for c in range(table.width):
        cell = table.at(r, c)
        if cell is None or id(cell) in seen:
            continue
        seen.add(id(cell))
        if cell.value and not rules["subsection_header"].search(cell.value) \
                and not rules["ltp_goals_header"].search(cell.value):
            parts.append(cell.value)
    return " ".join(parts)


def _parse_goals_table(
    table: Table, rules: Rules, lang: str, anomalies: list[str]
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """
    Таблица «Система целей обучения».

    Строки трёх видов:
      * во всю ширину «Раздел N: …» или «N) …» — начинается новый раздел;
      * шапка «Подраздел | 1 класс | … | 4 класс» — задаёт колонки;
      * обычная — подписи слева, цели по колонкам классов.

    Подписи бывают в одной колонке («1.1 Я и моя семья» — сразу подраздел)
    или в двух (у ИЗО: «1. Основы художественной грамотности» — раздел,
    «1.1 Графика» — подраздел). Разбираем каждую непустую подписную колонку:
    `N.M …` — подраздел, `N. …` при двух подписных колонках — раздел,
    при одной — подраздел, номер которого уточняется по кодам строки.

    Класс берём из самого кода, а не из колонки: в казахской редакции
    «2-сынып» занимает две колонки (colspan=2), и позиционный расчёт врёт —
    цели 2 класса уехали бы в 3-й. Колонка остаётся перекрёстной проверкой.
    """
    sections: dict[int, dict[str, Any]] = {}  # номер раздела -> title + subsections
    goals: list[dict[str, Any]] = []
    col_grade: dict[int, int] = {}            # колонка сетки -> класс из шапки
    cur_section: int | None = None
    seen_cells: set[int] = set()              # ячейку с rowspan разбираем один раз

    def _section_match(txt: str) -> re.Match[str] | None:
        for ru_pat, kz_pat in _SECTION_ROWS:
            m = (ru_pat if lang == "ru" else kz_pat).match(txt)
            if m:
                return m
        return None

    def _register_sub(sec_no: int, sub_no: int, title: str) -> None:
        sec = sections.setdefault(sec_no, {"title": "", "subsections": {}})
        if title:
            sec["subsections"][sub_no] = title

    # мультзаголовок шапки бывает в две строки («Подраздел | Цели обучения»
    # поверх «Подраздел | 1 класс | …») — обе гасим без аномалий
    header_rows = 0
    # Название раздела без номера («Раздел (виды речевой деятельности) – письмо»,
    # «Тыңдалым мен айтылым») стоит строкой выше целей. Номер раздела у него
    # возьмём из кода первой цели под ним.
    pending_title = ""
    for r in range(table.height):
        # «3) жазылым:» в казахской редакции разбито на две ячейки, поэтому
        # строка не «во всю ширину»; узнаём её по виду и по отсутствию кодов
        if table.spans_full_row(r) or (
                not RE_CODE.search(table.row_text(r)) and _section_match(table.row_text(r))):
            txt = table.row_text(r)
            m = _section_match(txt)
            if m:
                no = int(m.group(1))
                cur_section = no
                sections.setdefault(no, {"title": "", "subsections": {}})
                sections[no]["title"] = _clean(m.group(2)).rstrip(":").strip()
            elif _UNNUMBERED_SECTION.match(txt):
                pending_title = _clean(_UNNUMBERED_SECTION.match(txt).group(2)).rstrip(": ")
            elif txt and not col_grade and not rules["subsection_header"].search(txt) \
                    and not RE_CODE.search(txt) and not txt.endswith(":"):
                # таблица без классовой шапки (программа одного класса): голая
                # строка во всю ширину — название раздела
                pending_title = txt.rstrip(": ")
            elif txt and not rules["subsection_header"].search(txt) and not txt.endswith(":"):
                # строки вида «Обучающиеся должны знать:» — заголовки блоков,
                # а не данные; с ними аномалия была бы на каждой программе
                anomalies.append(
                    f"таблица целей, строка {r}: непонятная строка во всю ширину: "
                    f"{txt[:120]!r}")
            continue

        head = table.row_text(r)
        has_sub = rules["subsection_header"].search(head)
        if rules["grade_header"].search(head) and not RE_CODE.search(head) and (
                has_sub or not col_grade):
            col_grade = {}
            for c in range(table.width):
                cell = table.at(r, c)
                if cell is None:
                    continue
                m = rules["grade_header"].search(cell.value)
                if m:
                    col_grade[c] = int(m.group(1))
            header_rows += 1
            continue
        if has_sub and not RE_CODE.search(head):
            # шапка без классов — «Подраздел | Цели обучения», в языковых предметах
            # она повторяется над каждым разделом, а иногда несёт его название
            header_rows += 1
            rest = _header_title(table, r, rules)
            if rest:
                pending_title = rest
            continue

        # колонки классов — те, что шапка пронумеровала; остальные — подписи.
        # Без шапки (продолжение таблицы) — подпись одна, колонка 0
        if col_grade:
            goal_cols = [c for c in range(table.width) if c in col_grade]
            label_cols = [c for c in range(table.width) if c not in col_grade]
        else:
            goal_cols = [c for c in range(1, table.width)]
            label_cols = [0]

        # подписи слева: подраздел «N.M …» или раздел «N. …»
        row_sec_no = cur_section
        sub_titles: list[tuple[int, str]] = []
        for c in label_cols:
            cell = table.at(r, c)
            if cell is None or id(cell) in seen_cells:
                continue
            seen_cells.add(id(cell))
            label = cell.value
            if not label:
                continue
            # \b заменён на (?\d): в казахской редакции подпись пишут
            # слитно — «1.1Өнер туындыларын қабылдау» — и словесной границы
            # между номером и заголовком нет
            msub = re.match(r"^(\d+)\s*\.\s*(\d+)(?!\d)\s*(.*)$", label, re.S)
            if msub:
                _register_sub(int(msub.group(1)), int(msub.group(2)),
                              _clean(msub.group(3)))
                continue
            # «1) The use of…» у английского — та же подпись, что «1. Phonological…»
            msec = re.match(r"^(\d+)\s*[.)]\s*([^\d].*)$", label, re.S)
            if msec and len(label_cols) > 1:
                no = int(msec.group(1))
                row_sec_no = no
                sections.setdefault(no, {"title": "", "subsections": {}})
                sections[no]["title"] = _clean(msec.group(2))
                continue
            if msec:
                # одна подписная колонка: «N. Заголовок» — подраздел,
                # раздел узнается по кодам целей этой строки
                sub_titles.append((int(msec.group(1)), _clean(msec.group(2))))
                continue
            # подпись без номера («Нотная грамота») — продолжение прежнего
            # подраздела на следующей строке, а не ошибка разбора

        for c in goal_cols:
            cell = table.at(r, c)
            if cell is None or id(cell) in seen_cells:
                continue
            seen_cells.add(id(cell))
            if not cell.value:
                continue
            found = _split_goals(cell.value)
            if not found:
                anomalies.append(
                    f"таблица целей, строка {r}, колонка {c}: кода не видно: "
                    f"{cell.value[:120]!r}")
                continue
            if pending_title:
                sec = sections.setdefault(found[0]["section_no"], {"title": "", "subsections": {}})
                sec["title"] = sec["title"] or pending_title
                pending_title = ""
            # ячейка растянута на несколько классов, если верстальщик склеил колонки:
            # совпасть с кодом должна любая из занятых ею колонок
            spanned = {col_grade.get(cc) for cc in range(c, c + cell.colspan)}
            # текст до первого кода молча терялся: «3.1.4. использовать…» (в приказе
            # у кода пропала последняя цифра) выглядел как ячейка без цели
            lead = cell.value[:RE_CODE.search(cell.value).start()].strip()
            if len(lead) >= 15:
                anomalies.append(
                    f"таблица целей, строка {r}, колонка {c}: текст до первого кода "
                    f"не попал в цель: {lead[:100]!r}")
            for gl in found:
                gl["images"] = list(cell.images)
                gl["column_grade"] = col_grade.get(c)
                gl["row"] = r
                if gl["column_grade"] is not None and gl["grade"] not in spanned:
                    anomalies.append(
                        f"цель {gl['code']}: класс по коду {gl['grade']}, "
                        f"а колонка таблицы — {gl['column_grade']} класс")
                if row_sec_no is not None and gl["section_no"] != row_sec_no:
                    anomalies.append(
                        f"цель {gl['code']} стоит в разделе {row_sec_no}, "
                        f"а по коду относится к разделу {gl['section_no']}")
                goals.append(gl)

        # подписи вида «N. …» при одной колонке — подразделы этого раздела
        for sub_no, title in sub_titles:
            sec_no = next((g["section_no"] for g in goals
                           if g["row"] == r and g["subsection_no"] == sub_no),
                         cur_section)
            if sec_no is not None:
                _register_sub(sec_no, sub_no, title)
    return sections, goals


def _is_goals_table(table: Table, rules: Rules) -> bool:
    """Шапка таблицы целей. Три вида: «Подраздел» + «1 класс» в одной строке;
    «1 класс» в строке без кодов (у немецкого шапка называет не подраздел, а
    компетенцию); «Подраздел» + «Цели обучения» без классов вовсе — у программ
    одного класса и у русского языка, где классы стоят колонками без подписи.
    Последний вид совпадает по словам с шапкой долгосрочного плана, поэтому
    таблица с «N четверть» в него не входит."""
    has_quarters = any(rules["quarter_row"].match(table.row_text(r))
                       for r in range(table.height))
    for r in range(min(table.height, 6)):
        txt = table.row_text(r)
        sub = rules["subsection_header"].search(txt)
        grade = rules["grade_header"].search(txt)
        if sub and grade:
            return True
        if grade and not RE_CODE.search(txt) and not has_quarters:
            return True
        if sub and rules["ltp_goals_header"].search(txt) and not has_quarters:
            return True
    return False


def _parse_ltp_table(
    table: Table, rules: Rules, grade_hint: int | None, anomalies: list[str]
) -> list[dict[str, Any]]:
    """
    Таблица долгосрочного плана одного класса.
    Колонки: сквозные темы | раздел долгосрочного плана | подразделы | цели.
    Строка во всю ширину «N четверть» переключает четверть.

    Сквозные темы и раздел плана документ пишет один раз на блок строк, дальше
    ставит растянутую пустую ячейку (<td rowspan="9"><br></td>). Пустоту
    оставляем как есть: «раздел не указан» и «раздел тот же, что выше» — разные
    утверждения, и додумывать за документ парсер не должен.
    """
    rows: list[dict[str, Any]] = []
    quarter: int | None = None
    goals_col: int | None = None
    seen: set[int] = set()

    for r in range(table.height):
        if table.spans_full_row(r):
            txt = table.row_text(r)
            m = rules["quarter_row"].match(txt)
            if m:
                quarter = int(m.group(1))
            elif txt:
                anomalies.append(
                    f"долгосрочный план {grade_hint} кл., строка {r}: {txt[:100]!r}")
            continue

        head = table.row_text(r)
        if goals_col is None and rules["ltp_goals_header"].search(head):
            for c in range(table.width):
                cell = table.at(r, c)
                if cell is not None and rules["ltp_goals_header"].search(cell.value):
                    goals_col = c
            continue
        if goals_col is None:
            continue

        cell = table.at(r, goals_col)
        if cell is None or not cell.value:
            continue
        # ячейка целей rowspan'ов не имеет, но страхуемся
        if id(cell) in seen:
            continue
        seen.add(id(cell))

        found = _split_goals(cell.value)
        if not found:
            # «N четверть» встречается и обычной строкой, не только во всю
            # ширину — для разбора это смена четверти, а не мусор
            mq = rules["quarter_row"].match(cell.value)
            if mq:
                quarter = int(mq.group(1))
                continue
            anomalies.append(
                f"долгосрочный план {grade_hint} кл., строка {r}: кода не видно: "
                f"{cell.value[:100]!r}")
            continue

        themes = _cell_value(table, r, 0)
        ltp_sec = _cell_value(table, r, 1) if table.width > 1 else ""
        sub_lbl = _cell_value(table, r, goals_col - 1) if goals_col >= 1 else ""
        rows.append({
            "grade": grade_hint,
            "quarter": quarter,
            "cross_cutting_themes": themes,
            "ltp_section": ltp_sec,
            "subsection_label": sub_lbl,
            "goals": found,
            "images": list(cell.images),
        })
    return rows


def _parse_ltp_by_skill(
    table: Table, rules: Rules, grade_hint: int | None, anomalies: list[str]
) -> list[dict[str, Any]]:
    """Долгосрочный план, где цели стоят в колонках по видам речевой деятельности:
    «Тема | Аудирование и говорение | Чтение | Письмо». Подраздела и раздела плана
    в такой таблице нет, поэтому поля остаются пустыми, а не додумываются."""
    rows: list[dict[str, Any]] = []
    quarter: int | None = None
    seen: set[int] = set()
    for r in range(table.height):
        if table.spans_full_row(r):
            m = rules["quarter_row"].match(table.row_text(r))
            if m:
                quarter = int(m.group(1))
            continue
        if quarter is None:
            continue  # шапка над первой четвертью
        found = []
        for c in range(1, table.width):
            cell = table.at(r, c)
            if cell is None or id(cell) in seen or not cell.value:
                continue
            seen.add(id(cell))
            found.extend(_split_goals(cell.value))
        if not found:
            continue
        rows.append({
            "grade": grade_hint,
            "quarter": quarter,
            "cross_cutting_themes": _cell_value(table, r, 0),
            "ltp_section": "",
            "subsection_label": "",
            "goals": found,
            "images": [],
        })
    return rows


def _cell_value(table: Table, r: int, c: int) -> str:
    cell = table.at(r, c)
    return cell.value if cell is not None else ""


def _find_ltp_grade(frag: str, table_start: int, rules: Rules) -> int | None:
    """Класс таблицы долгосрочного плана — из абзаца «1) 1 класс:» прямо перед ней.

    Берём последнее совпадение в этом «хвосте»: ближайшее к таблице, а не первое
    попавшееся упоминание класса в предыдущем абзаце.
    """
    tail = _strip_tags(frag[max(0, table_start - 400):table_start])
    found = rules["ltp_grade"].findall(tail)
    return int(found[-1]) if found else None


# --------------------------------------------------------------------------
# Сборка
# --------------------------------------------------------------------------

def parse_curriculum(
    html_text: str,
    lang: str,
    *,
    subject: str = "math",
    document: dict[str, Any] | None = None,
    source_name: str = "",
) -> dict[str, Any]:
    """Разбирает текст акта и возвращает программу словарём.

    `html_text` — содержимое `document.text_content` из ответа adilet.
    `subject` — код предмета из `SUBJECT_CODES`: по нему находится приложение
    и подставляется название предмета.
    `document` — тот же `document` целиком: из него берутся выходные данные
    приказа (номер, даты, название). Без него блок `source` остаётся пустым,
    но сама программа разбирается ровно так же.
    `source_name` — как называть источник в `source.file`; на разбор не влияет.

    Не сложившееся — в `anomalies` списком, а не исключением: документ
    действительно бывает неровным (у цели 2.1.2.6 нет четверти, в нумерации
    подраздела 5.2 у 4 класса дыра), и решать, терпимо ли это, вызывающему коду.
    """
    rules = _RULES.get(lang)
    if rules is None:
        raise CurriculumParseError(
            f"неизвестный язык редакции: {lang!r}, есть {', '.join(LANGUAGES)}")
    spec = _SUBJECTS.get(subject)
    if spec is None:
        raise CurriculumParseError(
            f"неизвестный предмет: {subject!r}, есть {', '.join(SUBJECT_CODES)}")
    if not html_text:
        raise CurriculumParseError(f"{source_name or 'документ'}: текста акта нет")

    doc = document or {}

    hits = _find_appendix(html_text, rules, spec, lang)
    if not hits:
        raise CurriculumParseError(
            f"{source_name or 'документ'}: приложение с искомой программой не найдено")
    if len(hits) > 1:
        log.warning("подходящих приложений %d, беру первое", len(hits))
    start, end, frag = hits[0]
    frag = _cut_reduced_load(frag, rules)

    anomalies: list[str] = []
    tp = TableParser()
    tp.feed(frag)
    tp.close()
    if tp.nested_tables:
        anomalies.append(
            f"во фрагменте {tp.nested_tables} вложенных таблиц — разбор ячеек может слипнуться")

    # Позиции таблиц верхнего уровня нужны, чтобы привязать таблицу
    # долгосрочного плана к её классу («1) 1 класс:» стоит абзацем выше).
    top_level = []
    depth = 0
    for m in re.finditer(r"<table\b|</table>", frag, re.I):
        if m.group(0).lower().startswith("</"):
            depth -= 1
        else:
            if depth == 0:
                top_level.append(m.start())
            depth += 1

    # У части предметов (труд, физкультура) таблиц целей несколько —
    # по одной на раздел. Разбираем каждую и складываем цели вместе.
    # У цифровой грамотности куски таблиц цели идут вовсе без шапки —
    # продолжение одной таблицы, разрезанной вёрсткой; ловим их по кодам.
    goals_tables: list[Table] = []
    ltp_tables = []
    for i, t in enumerate(tp.tables):
        if _is_goals_table(t, rules):
            goals_tables.append(t)
            continue
        if any(rules["ltp_goals_header"].search(t.row_text(r)) for r in range(min(t.height, 4))):
            ltp_tables.append((i, t))
            continue
        # без шапки: это продолжение таблицы целей, если в ней есть коды
        # и ни одной пометки долгосрочного плана («четверть», «Сквозные темы»)
        has_codes = any(
            RE_CODE.search(cell.value)
            for row in t.rows for cell in row if cell.value
        )
        looks_like_ltp = any(
            rules["quarter_row"].match(t.row_text(r)) for r in range(t.height)
        ) or any(
            rules["ltp_goals_header"].search(t.row_text(r)) for r in range(min(t.height, 4))
        )
        if has_codes and not looks_like_ltp:
            goals_tables.append(t)
        elif has_codes:
            # план без колонки «Цели обучения»: цели разнесены по колонкам видов
            # речевой деятельности (аудирование | чтение | письмо)
            ltp_tables.append((i, t))

    if not goals_tables:
        raise CurriculumParseError("таблица «Система целей обучения» не найдена")

    sections: dict[int, dict[str, Any]] = {}
    goals: list[dict[str, Any]] = []
    for table in goals_tables:
        one_sections, one_goals = _parse_goals_table(table, rules, lang, anomalies)
        for no, sec in one_sections.items():
            target = sections.setdefault(no, {"title": "", "subsections": {}})
            if sec["title"]:
                target["title"] = sec["title"]
            target["subsections"].update(sec["subsections"])
        goals.extend(one_goals)

    ltp_rows: list[dict[str, Any]] = []
    for i, t in ltp_tables:
        pos = top_level[i] if i < len(top_level) else 0
        grade = _find_ltp_grade(frag, pos, rules)
        if grade is None:
            # если абзац-подпись не распознан, класс возьмём из кодов целей
            anomalies.append(f"таблица долгосрочного плана #{i}: класс перед таблицей не распознан")
        has_goals_header = any(
            rules["ltp_goals_header"].search(t.row_text(r)) for r in range(min(t.height, 4)))
        parse_ltp = _parse_ltp_table if has_goals_header else _parse_ltp_by_skill
        rows = parse_ltp(t, rules, grade, anomalies)
        for row in rows:
            if grade is None and row["goals"]:
                row["grade"] = row["goals"][0]["grade"]
        ltp_rows.extend(rows)

    # четверти и контекст долгосрочного плана прикручиваем к целям по коду:
    # одна и та же цель встречается в нескольких четвертях, и текст в плане
    # у неё каждый раз свой — урезанный до той части, которую берут сейчас
    ltp_by_code: dict[str, list[dict[str, Any]]] = {}
    for row in ltp_rows:
        for gl in row["goals"]:
            ltp_by_code.setdefault(gl["code"], []).append({
                "quarter": row["quarter"],
                "ltp_section": row["ltp_section"],
                "subsection_label": row["subsection_label"],
                "cross_cutting_themes": row["cross_cutting_themes"],
                "marker": gl["marker"],
                "text_in_plan": gl["text"],
            })

    objectives = []
    for gl in goals:
        sec = sections.get(gl["section_no"], {})
        occ = ltp_by_code.get(gl["code"], [])
        quarters = sorted({o["quarter"] for o in occ if o["quarter"] is not None})
        obj = {
            "code": gl["code"],
            "grade": gl["grade"],
            "section_no": gl["section_no"],
            "section_title": sec.get("title", ""),
            "subsection_code": f"{gl['section_no']}.{gl['subsection_no']}",
            "subsection_title": sec.get("subsections", {}).get(gl["subsection_no"], ""),
            "index": gl["index"],
            "marker": gl["marker"],
            "text": gl["text"],
            "quarters": quarters,
            "long_term_plan": occ,
        }
        if gl["images"]:
            obj["images"] = gl["images"]
        objectives.append(obj)

    # дубли кодов внутри таблицы целей — сигнал, что ячейку разрезало не там
    seen: dict[str, list[dict[str, Any]]] = {}
    for o in objectives:
        seen.setdefault(o["code"], []).append(o)
    for code, lst in seen.items():
        if len(lst) > 1:
            anomalies.append(f"код {code} встречается в таблице целей {len(lst)} раза")

    # цели, которые есть в долгосрочном плане, но отсутствуют в таблице целей
    for code in sorted(set(ltp_by_code) - set(seen)):
        anomalies.append(f"код {code} есть в долгосрочном плане, но не в таблице целей")

    app_header = _appendix_header(frag)
    m_date = rules["order_date_text"].search(app_header)
    headings = _headings(frag, rules)

    meta = {
        "file": source_name,
        "document_id": doc.get("id"),
        "group_number": doc.get("group_number"),
        "document_language": doc.get("language"),
        "edition_language": lang,
        "order_number": doc.get("adoption_number"),
        # дата приказа — 16.09.2022; в метаданных adilet она лежит в start_date,
        # а adoption_date/justice_reg_date — это дата регистрации в Минюсте
        "order_date": _act_date(doc.get("start_date")),
        "order_date_text": m_date.group(0) if m_date else None,
        "order_registered": _act_date(doc.get("justice_reg_date")),
        "order_reg_number": doc.get("justice_reg_number"),
        # adilet держит оба поля на языке самой редакции: title_ru — краткое
        # название акта, title_kz — официальная строка с датой и номером
        "order_title": doc.get("title_ru"),
        "order_official_name": doc.get("title_kz"),
        "appendix_header": app_header,
        "appendix_number": _appendix_number(app_header, rules),
        "fragment_offset": [start, end],
        "fragment_length": end - start,
    }

    return {
        "source": meta,
        "subject": spec.title,
        "subject_code": spec.code,
        "grades": list(spec.grades),
        "program_title": headings[0] if headings else "",
        "program_structure": headings,
        "notes": _notes(frag, rules),
        "sections": [
            {
                "no": no,
                "title": sections[no]["title"],
                "subsections": [
                    {"code": f"{no}.{sn}", "no": sn, "title": st}
                    for sn, st in sorted(sections[no]["subsections"].items())
                ],
            }
            for no in sorted(sections)
        ],
        "objectives": objectives,
        "long_term_plan": [
            {
                "grade": r["grade"],
                "quarter": r["quarter"],
                "cross_cutting_themes": r["cross_cutting_themes"],
                "ltp_section": r["ltp_section"],
                "subsection_label": r["subsection_label"],
                "codes": [g["code"] for g in r["goals"]],
            }
            for r in ltp_rows
        ],
        "stats": _stats(sections, objectives, ltp_rows),
        "anomalies": anomalies,
    }


def parse_curriculum_file(
    path: str | Path, lang: str, subject: str = "math"
) -> dict[str, Any]:
    """То же по сохранённому ответу adilet: `{"document": {...}}` или сам документ."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    doc = payload.get("document", payload)
    return parse_curriculum(
        doc.get("text_content") or "",
        lang,
        subject=subject,
        document=doc,
        source_name=str(path),
    )


def _appendix_number(head: str, rules: Rules) -> str | None:
    m = rules["appendix_label"].search(head)
    return re.sub(r"\s+", "", m.group(1)) if m else None


def _appendix_header(frag: str) -> str:
    """Текст «шапки» приложения — первая выровненная вправо таблица."""
    m = re.search(r'<p align="right">\s*<table\b.*?</table>', frag, re.I | re.S)
    return _strip_tags(m.group(0)) if m else ""


def _headings(frag: str, rules: Rules) -> list[str]:
    """
    Заголовки приложения из <h3>.

    В казахской редакции вёрстка слепила в один <h3> и название программы,
    и «1-тарау», и «1-параграф», поэтому режем по маркерам главы/параграфа.
    """
    out = []
    for m in re.finditer(r"<h3[^>]*>(.*?)</h3>", frag, re.S):
        t = _strip_tags(m.group(1))
        for part in rules["heading_split"].split(t):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _notes(frag: str, rules: Rules) -> list[str]:
    """
    Примечания и сноски приложения — они объясняют пометки * и **.

    В казахской редакции «Ескерту:» и его текст разнесены по двум <p>,
    поэтому обрубок, кончающийся двоеточием, склеиваем со следующим абзацем.
    """
    paras = [_strip_tags(m.group(1))
             for m in re.finditer(r"<p[^>]*>(.*?)(?=<p\b|<table\b|<h3\b|$)", frag, re.S)]
    out: list[str] = []
    for i, t in enumerate(paras):
        if not t or not rules["note_prefix"].match(t):
            continue
        if t.endswith(":") and i + 1 < len(paras) and paras[i + 1]:
            t = t + " " + paras[i + 1]
        if t not in out:
            out.append(t)
    return out


def _act_date(iso: str | None) -> str | None:
    """Календарная дата приказа по месту принятия.

    Акт принят в Астане, adilet отдаёт даты в UTC, поэтому «16 сентября» лежит
    в JSON как 15-е 18:00Z. Смещение здесь своё, +6, а не `domain.kz_time.ALMATY`:
    та — сегодняшний единый UTC+5, введённый в 2024 году, и по ней приказ 2022 года
    датировался бы 15-м числом.
    """
    if not iso:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2})", iso)
    if not m:
        return iso[:10]
    dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return (dt + timedelta(hours=6)).date().isoformat()


def _stats(
    sections: dict[int, dict[str, Any]],
    objectives: list[dict[str, Any]],
    ltp_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    by_grade: dict[int, int] = {}
    for o in objectives:
        by_grade[o["grade"]] = by_grade.get(o["grade"], 0) + 1
    subs = sum(len(s["subsections"]) for s in sections.values())
    return {
        "sections": len(sections),
        "subsections": subs,
        "objectives": len(objectives),
        "objectives_by_grade": {str(k): by_grade[k] for k in sorted(by_grade)},
        "objectives_with_quarter": sum(1 for o in objectives if o["quarters"]),
        "objectives_in_several_quarters": sum(1 for o in objectives if len(o["quarters"]) > 1),
        # пометка «*»/«**» стоит не в таблице целей, а в долгосрочном плане:
        # там она означает, что в этой четверти цель берётся частично
        "objectives_marked_in_goals_table": sum(1 for o in objectives if o["marker"]),
        "objectives_marked_in_plan": sum(
            1 for o in objectives if any(p["marker"] for p in o["long_term_plan"])),
        "long_term_plan_rows": len(ltp_rows),
    }


def main() -> None:
    """Разбор из командной строки — им сверяют выход после правок парсера."""
    # argparse нужен только здесь, а модуль тянет за собой сервис: держим
    # импорт внутри, чтобы запуск приложения за него не платил
    import argparse

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json_path", help="JSON от API adilet (document.text_content внутри)")
    ap.add_argument("--lang", choices=sorted(LANGUAGES), required=True,
                    help="язык редакции: ru или kz")
    ap.add_argument("--subject", choices=sorted(SUBJECT_CODES), default="math",
                    help="код предмета из реестра (по умолчанию math)")
    ap.add_argument("-o", "--out", help="куда положить результат (по умолчанию — stdout)")
    args = ap.parse_args()

    result = parse_curriculum_file(args.json_path, args.lang, args.subject)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        s = result["stats"]
        print(f"{args.out}: разделов {s['sections']}, подразделов {s['subsections']}, "
              f"целей {s['objectives']} {s['objectives_by_grade']}, "
              f"аномалий {len(result['anomalies'])}")
    else:
        print(text)


if __name__ == "__main__":
    main()
