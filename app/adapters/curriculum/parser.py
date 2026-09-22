"""Разбор типовой учебной программы: приказ Министра просвещения РК № 399
от 16.09.2022, приложение 26 — «Математика» для 1-4 классов.

Данные берёт не этот модуль: граница адаптера проходит по разбору, скачивание —
забота вызывающего кода. Откуда брать:

    GET https://adilet.zan.kz/api/documents/by-ngr/{НГР}?language=rus|kaz

НГР приказа — V2200029767, uuid русской редакции dc1f4914-3cc8-4592-b0c5-4a4e62e0b573,
казахской — 663d1192-e6c3-4d03-bab9-4d8f3bbd0a24. В ответе весь текст акта лежит
одной строкой HTML в `document.text_content` — она и приходит сюда.

Модуль настроен на приложение 26 и только на него. Другой предмет — другая вёрстка
таблиц: чужое приложение либо не найдётся (`CurriculumParseError`), либо насыплет
предупреждений в `anomalies`. Молча неверного ответа не будет — неверная программа
в базе дороже отказа её разобрать.

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
# добавление третьего предмета/языка не требовало правок в логике разбора.

_RULES: dict[str, Rules] = {
    "ru": {
        # заголовок приложения: по нему находим нужную программу среди ~96
        "appendix_title": re.compile(
            r'учебн\w*\s+предмет\w*\s*[«"“]?\s*Математика', re.I),
        "grades_in_title": re.compile(r"\b1\s*-\s*4\s*класс", re.I),
        "appendix_label": re.compile(r"Приложение\s*(\d+(?:\s*-\s*\d+)?)", re.I),
        # «Глава 2.» и «Параграф 1.» — в одном <h3> их бывает несколько подряд
        "heading_split": re.compile(r"(?=(?:Глава|Параграф)\s+\d+\s*\.)"),
        "note_prefix": re.compile(r"^(Примечание|Сноска)\b", re.I),
        "order_date_text": re.compile(r"от\s+\d{1,2}\s+\S+\s+\d{4}\s+года\s*№\s*\S+"),
        # «Раздел 1: Числа и величины»
        "section_row": re.compile(r"^Раздел\s*(\d+)\s*[:.\-–]?\s*(.+)$", re.I | re.S),
        # шапка таблицы целей: «1 класс» ... «4 класс»
        "grade_header": re.compile(r"(\d)\s*-?\s*класс", re.I),
        "subsection_header": re.compile(r"Подраздел", re.I),
        # «1 четверть»
        "quarter_row": re.compile(r"^(\d)\s*-?\s*четверт", re.I),
        # «1) 1 класс:» перед таблицей долгосрочного плана
        "ltp_grade": re.compile(r"(\d)\s*-?\s*класс\s*:?\s*$", re.I),
        "ltp_goals_header": re.compile(r"Цели\s+обучения", re.I),
    },
    "kz": {
        "appendix_title": re.compile(r'[«"“]?\s*Математика\s*[»"”]?\s*оқу\s+пәні', re.I),
        "grades_in_title": re.compile(r"\b1\s*-\s*4\s*-?\s*сынып", re.I),
        "appendix_label": re.compile(r"(\d+(?:\s*-\s*\d+)?)\s*-\s*қосымша", re.I),
        "heading_split": re.compile(r"(?=\d+\s*-\s*(?:тарау|параграф)\s*\.)"),
        "note_prefix": re.compile(r"^Ескерту\b", re.I),
        "order_date_text": re.compile(r"\d{4}\s+жылғы\s+\d{1,2}\s+\S+\s*№\s*\S+"),
        "section_row": re.compile(r"^(\d+)\s*-\s*бөлім\s*[:.\-–]?\s*(.+)$", re.I | re.S),
        "grade_header": re.compile(r"(\d)\s*-?\s*сынып", re.I),
        "subsection_header": re.compile(r"Бөлімше", re.I),
        "quarter_row": re.compile(r"^(\d)\s*-?\s*тоқсан", re.I),
        "ltp_grade": re.compile(r"(\d)\s*-?\s*сынып\s*:?\s*$", re.I),
        "ltp_goals_header": re.compile(r"Оқу\s+мақсаттар", re.I),
    },
}

LANGUAGES: tuple[str, ...] = tuple(_RULES)

# Код цели: класс.раздел.подраздел.номер. Пробелы вокруг точек реальны —
# в казахской редакции встречается «3.1.2. 1». Звёздочки после кода — пометка
# документа («реализуется частично»), частью кода не являются.
# Отрицательный просмотр назад отсекает хвосты вроде «12.1.1.1» и дат,
# просмотр вперёд — пятизначные конструкции.
RE_CODE = re.compile(
    r"(?<![\d.])(\d)\s*\.\s*(\d)\s*\.\s*(\d)\s*\.\s*(\d{1,2})(?!\s*\.\s*\d)\s*(\*{1,2})?"
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


def _find_appendix(html_text: str, rules: Rules) -> list[tuple[int, int, str]]:
    """Фрагменты приложений, чья «шапка» совпала с искомой программой."""
    hits = []
    for start, end in _split_appendices(html_text):
        frag = html_text[start:end]
        # заголовок ищем в первых килобайтах: дальше идёт тело программы,
        # где название предмета встречается десятки раз
        head = _strip_tags(frag[:2500])
        if rules["appendix_title"].search(head) and rules["grades_in_title"].search(head):
            hits.append((start, end, frag))
    return hits


# --------------------------------------------------------------------------
# Разбор
# --------------------------------------------------------------------------

def _parse_code(m: re.Match[str]) -> tuple[str, int, int, int, int, str]:
    """Совпадение RE_CODE → (код, класс, раздел, подраздел, номер, пометка).

    Код пересобираем из групп, а не берём срезом: в исходнике внутри него
    бывают пробелы, а ключ связи с долгосрочным планом должен быть один.
    """
    g, s, ss, n = (int(m.group(i)) for i in range(1, 5))
    marker = m.group(5) or ""
    return f"{g}.{s}.{ss}.{n}", g, s, ss, n, marker


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


def _parse_goals_table(
    table: Table, rules: Rules, anomalies: list[str]
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """
    Таблица «Система целей обучения».

    Строки трёх видов:
      * во всю ширину «Раздел N: …» — начинается новый раздел;
      * шапка «Подраздел | 1 класс | … | 4 класс» — задаёт колонки;
      * обычная — колонка 0 держит подраздел, остальные цели по классам.

    Класс берём из самого кода, а не из колонки: в казахской редакции
    «2-сынып» занимает две колонки (colspan=2), и позиционный расчёт врёт —
    цели 2 класса уехали бы в 3-й. Колонка остаётся перекрёстной проверкой.
    """
    sections: dict[int, dict[str, Any]] = {}  # номер раздела -> title + subsections
    goals: list[dict[str, Any]] = []
    col_grade: dict[int, int] = {}            # колонка сетки -> класс из шапки
    cur_section: int | None = None
    seen_cells: set[int] = set()              # ячейку с rowspan разбираем один раз

    for r in range(table.height):
        if table.spans_full_row(r):
            txt = table.row_text(r)
            m = rules["section_row"].match(txt)
            if m:
                no = int(m.group(1))
                cur_section = no
                sections.setdefault(no, {"title": "", "subsections": {}})
                sections[no]["title"] = _clean(m.group(2))
            elif txt:
                anomalies.append(
                    f"таблица целей, строка {r}: непонятная строка во всю ширину: "
                    f"{txt[:120]!r}")
            continue

        head = table.row_text(r)
        if rules["subsection_header"].search(head) and rules["grade_header"].search(head):
            col_grade = {}
            for c in range(table.width):
                cell = table.at(r, c)
                if cell is None:
                    continue
                m = rules["grade_header"].search(cell.value)
                if m:
                    col_grade[c] = int(m.group(1))
            continue

        # колонка 0 — подраздел; ячейка бывает пустой (<br>) при rowspan,
        # тогда подраздел определится по коду цели
        label = table.at(r, 0)
        sub_label = label.value if label is not None else ""
        msub = re.match(r"^(\d+)\s*\.\s*(\d+)\b\s*(.*)$", sub_label, re.S)
        if msub:
            sec_no, sub_no = int(msub.group(1)), int(msub.group(2))
            title = _clean(msub.group(3))
            sections.setdefault(sec_no, {"title": "", "subsections": {}})
            if title:
                sections[sec_no]["subsections"][sub_no] = title

        for c in range(table.width):
            if c == 0:
                continue
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
            if len(found) > 1:
                anomalies.append(
                    f"таблица целей, строка {r}, колонка {c}: в одной ячейке {len(found)} кодов "
                    f"({', '.join(g['code'] for g in found)})")
            for gl in found:
                gl["images"] = list(cell.images)
                gl["column_grade"] = col_grade.get(c)
                gl["row"] = r
                if gl["column_grade"] is not None and gl["column_grade"] != gl["grade"]:
                    anomalies.append(
                        f"цель {gl['code']}: класс по коду {gl['grade']}, "
                        f"а колонка таблицы — {gl['column_grade']} класс")
                if cur_section is not None and gl["section_no"] != cur_section:
                    anomalies.append(
                        f"цель {gl['code']} стоит в разделе {cur_section}, "
                        f"а по коду относится к разделу {gl['section_no']}")
                goals.append(gl)
    return sections, goals


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
    document: dict[str, Any] | None = None,
    source_name: str = "",
) -> dict[str, Any]:
    """Разбирает текст акта и возвращает программу словарём.

    `html_text` — содержимое `document.text_content` из ответа adilet.
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
    if not html_text:
        raise CurriculumParseError(f"{source_name or 'документ'}: текста акта нет")

    doc = document or {}

    hits = _find_appendix(html_text, rules)
    if not hits:
        raise CurriculumParseError(
            f"{source_name or 'документ'}: приложение с искомой программой не найдено")
    if len(hits) > 1:
        log.warning("подходящих приложений %d, беру первое", len(hits))
    start, end, frag = hits[0]

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

    goals_table = None
    ltp_tables = []
    for i, t in enumerate(tp.tables):
        # шапка таблицы целей: «Подраздел» + «1 класс»
        header_seen = False
        for r in range(min(t.height, 6)):
            txt = t.row_text(r)
            if rules["subsection_header"].search(txt) and rules["grade_header"].search(txt):
                header_seen = True
                break
        if header_seen and goals_table is None:
            goals_table = t
            continue
        if any(rules["ltp_goals_header"].search(t.row_text(r)) for r in range(min(t.height, 4))):
            ltp_tables.append((i, t))

    if goals_table is None:
        raise CurriculumParseError("таблица «Система целей обучения» не найдена")

    sections, goals = _parse_goals_table(goals_table, rules, anomalies)

    ltp_rows: list[dict[str, Any]] = []
    for i, t in ltp_tables:
        pos = top_level[i] if i < len(top_level) else 0
        grade = _find_ltp_grade(frag, pos, rules)
        if grade is None:
            # если абзац-подпись не распознан, класс возьмём из кодов целей
            anomalies.append(f"таблица долгосрочного плана #{i}: класс перед таблицей не распознан")
        rows = _parse_ltp_table(t, rules, grade, anomalies)
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
        "subject": "Математика",
        "grades": [1, 2, 3, 4],
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


def parse_curriculum_file(path: str | Path, lang: str) -> dict[str, Any]:
    """То же по сохранённому ответу adilet: `{"document": {...}}` или сам документ."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    doc = payload.get("document", payload)
    return parse_curriculum(
        doc.get("text_content") or "",
        lang,
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
    ap.add_argument("-o", "--out", help="куда положить результат (по умолчанию — stdout)")
    args = ap.parse_args()

    result = parse_curriculum_file(args.json_path, args.lang)
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
