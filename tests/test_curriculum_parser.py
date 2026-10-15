"""Разбор приказа № 399 для языковых предметов начальной школы.

Документ на 20 МБ в тест не кладём и сети не трогаем: приложения собираются из
крошечных кусков HTML той же формы, что у настоящих. Каждая форма — отдельная
беда, которую парсер уже пережил на живом приказе (12.10.2026).
"""

import pytest

from app.adapters.curriculum.parser import (
    SUBJECT_CODES,
    CurriculumParseError,
    parse_curriculum,
)
from app.application.curriculum_checks import check_program


def _cell(text: str, **span) -> str:
    attrs = "".join(f' {k}="{v}"' for k, v in span.items())
    return f"<td{attrs}>{text}</td>"


def table(*rows: list[str]) -> str:
    return "<table>" + "".join("<tr>" + "".join(r) + "</tr>" for r in rows) + "</table>"


def appendix(number: str, title: str, *body: str) -> str:
    """Приложение: правая «шапка» с номером — граница приложений, затем заголовок."""
    head = (
        f'<p align="right"><table><tr><td>Приложение {number} к приказу Министра</td></tr></table>'
    )
    return head + f"<h3>{title}</h3>" + "".join(body)


def doc(*appendices: str) -> str:
    return "".join(appendices)


GRADE_HEAD = [
    _cell("Подраздел (ключевые навыки)"),
    _cell("2 класс"),
    _cell("3 класс"),
    _cell("4 класс"),
]


def goals_row(label: str, *cells: str) -> list[str]:
    return [_cell(label), *(_cell(c) for c in cells)]


def parse(text: str, subject: str, lang: str = "ru") -> dict:
    return parse_curriculum(text, lang, subject=subject)


KAZ_L1 = (
    'Типовая учебная программа по учебному предмету "Казахский язык" для 2-4 классов '
    "уровня начального образования (с казахским языком обучения)"
)
KAZ_L2 = (
    'Типовая учебная программа по учебному предмету "Казахский язык" для 1-4 классов '
    "уровня начального образования (с неказахским языком обучения)"
)


def kazakh_pair() -> str:
    """Два приложения с одним названием: цели у них разные, а различает только
    скобка про язык обучения и классы."""
    l2 = table(
        [_cell("Подраздел (ключевые навыки)"), _cell("1 класс"), _cell("2 класс")],
        goals_row(
            "1.1 Слушание",
            "1.1.1.1 слушать и понимать речь учителя",
            "2.1.1.1 слушать и понимать простой текст",
        ),
    )
    l1 = table(
        GRADE_HEAD,
        goals_row(
            "1.1 Тыңдау",
            "2.1.1.1 тыңдаған материал бойынша сөздерді белгілеу",
            "3.1.1.1 тыңдаған материал бойынша тірек сөздерді белгілеу",
            "4.1.1.1 тыңдаған материал бойынша негізгі тұстарын жазу",
        ),
    )
    return doc(appendix("11", KAZ_L2, l2), appendix("12", KAZ_L1, l1))


def test_language_of_instruction_separates_same_named_programs():
    text = kazakh_pair()
    l2 = parse(text, "kazakh_lang_l2")
    l1 = parse(text, "kazakh_lang_l1")
    assert [o["code"] for o in l2["objectives"]] == ["1.1.1.1", "2.1.1.1"]
    assert [o["code"] for o in l1["objectives"]] == ["2.1.1.1", "3.1.1.1", "4.1.1.1"]
    assert l1["source"]["appendix_number"] == "12"
    assert l2["source"]["appendix_number"] == "11"


def test_kazakh_edition_uses_kazakh_bracket():
    head = (
        'Бастауыш білім беру деңгейінің 2-4-сыныптарына арналған "Қазақ тілі" '
        "оқу пәні бойынша үлгілік оқу бағдарламасы (оқыту қазақ тілінде)"
    )
    other = head.replace("(оқыту қазақ тілінде)", "(оқыту қазақ тілінде емес)")
    body = table(
        [_cell("Бөлімше"), _cell("2-сынып"), _cell("3-сынып"), _cell("4-сынып")],
        goals_row(
            "1.1 Тыңдау",
            "2.1.1.1 тыңдаған материал бойынша сөз белгілеу",
            "3.1.1.1 тірек сөздерді белгілеу",
            "4.1.1.1 негізгі тұстарын жазу",
        ),
    )
    text = doc(
        appendix("1", other, table([_cell("x")])),
        appendix("2", head, body),
    )
    result = parse_curriculum(text, "kz", subject="kazakh_lang_l1")
    assert len(result["objectives"]) == 3
    with pytest.raises(CurriculumParseError):
        parse_curriculum(text, "kz", subject="kazakh_lang_l2")


def test_missing_language_variant_is_refused():
    only_l1 = doc(
        appendix(
            "12",
            KAZ_L1,
            table(
                GRADE_HEAD,
                goals_row(
                    "1.1 Тыңдау",
                    "2.1.1.1 тыңдаған материал",
                    "3.1.1.1 тыңдаған материал",
                    "4.1.1.1 тыңдаған материал",
                ),
            ),
        )
    )
    with pytest.raises(CurriculumParseError):
        parse(only_l1, "kazakh_lang_l2")


RU_L2 = (
    'Типовая учебная программа по учебному предмету "Русский язык" для 2-4 классов '
    "уровня начального образования (с нерусским языком обучения)"
)


def test_header_without_grades_and_section_name_in_header():
    """Русский язык (нерусский язык обучения): классы колонками без подписей,
    название раздела — во второй ячейке шапки «Подраздел | Аудирование»."""
    body = table(
        [_cell("Подраздел", rowspan="2"), _cell("Аудирование", colspan="3")],
        [_cell("Цели обучения", colspan="3")],
        goals_row(
            "1.1 Приёмы слушания",
            "2.1.1.1 слушать и понимать речь",
            "3.1.1.1 слушать и понимать устную речь",
            "4.1.1.1 слушать устную речь",
        ),
        [_cell("Подраздел", rowspan="2"), _cell("Говорение", colspan="3")],
        [_cell("Цели обучения", colspan="3")],
        goals_row(
            "2.1 Словарь",
            "2.2.1.1 использовать слова для знакомства",
            "3.2.1.1 использовать тематическую лексику",
            "4.2.1.1 использовать слова для поддержания разговора",
        ),
    )
    result = parse(doc(appendix("22", RU_L2, body)), "russian_lang_l2")
    assert [o["code"] for o in result["objectives"]] == [
        "2.1.1.1",
        "3.1.1.1",
        "4.1.1.1",
        "2.2.1.1",
        "3.2.1.1",
        "4.2.1.1",
    ]
    assert [s["title"] for s in result["sections"]] == ["Аудирование", "Говорение"]
    assert result["anomalies"] == []


def test_reduced_load_chapter_is_not_loaded_twice():
    goals = table(
        GRADE_HEAD,
        goals_row(
            "1.1 Слушание",
            "2.1.1.1 слушать и понимать речь",
            "3.1.1.1 слушать и понимать речь",
            "4.1.1.1 слушать и понимать речь",
        ),
    )
    reduced = "<h3>Глава 3. Организация содержания (с сокращением учебной нагрузки)</h3>"
    text = doc(appendix("22", RU_L2, goals, reduced, goals))
    result = parse(text, "russian_lang_l2")
    assert len(result["objectives"]) == 3
    assert result["anomalies"] == []


def test_five_number_code_keeps_both_halves():
    """В приказе цель 3.5.1.2 записана как «3.5.1.2.1» и «3.5.1.2.2» — без пятого
    числа обе слились бы в одну строку, а вторая потерялась."""
    body = table(
        GRADE_HEAD,
        goals_row(
            "5.1 Грамматика",
            "2.5.1.2 использовать слова-предметы",
            "3.5.1.2.1 согласовывать слова-признаки со словами-предметами; "
            "3.5.1.2.2 согласовывать слова, обозначающие количество",
            "4.5.1.2 согласовывать слова-признаки",
        ),
    )
    result = parse(doc(appendix("22", RU_L2, body)), "russian_lang_l2")
    codes = [o["code"] for o in result["objectives"]]
    assert codes == ["2.5.1.2", "3.5.1.2.1", "3.5.1.2.2", "4.5.1.2"]
    assert {o["index"] for o in result["objectives"]} == {2}


def test_section_row_split_in_two_cells_switches_section():
    """В казахской редакции «3) жазылым:» разбито на две ячейки — строка не во
    всю ширину, но раздел сменить обязана, иначе цели раздела 3 лягут под 2."""
    head = (
        'Бастауыш білім беру деңгейінің 2-4-сыныптарына арналған "Әдебиеттік оқу" '
        "оқу пәні бойынша үлгілік оқу бағдарламасы (оқыту қазақ тілінде)"
    )
    kz_head = [_cell("Бөлімше"), _cell("2-сынып"), _cell("3-сынып"), _cell("4-сынып")]
    body = table(
        kz_head,
        [_cell("2) оқылым:", colspan="2"), _cell("", colspan="2")],
        goals_row(
            "2.1 Оқу түрлері",
            "2.2.1.1 шығарманы дауыстап оқу",
            "3.2.1.1 шығарманы рөлге бөліп оқу",
            "4.2.1.1 шығарманы мәнерлеп оқу",
        ),
        [_cell("3) жазылым:", colspan="2"), _cell("", colspan="2")],
        goals_row(
            "3.1 Жоспар құру",
            "2.3.1.1 шығарманы бөліктерге бөлу",
            "3.3.1.1 шығарманы логикалық бөліктерге бөлу",
            "4.3.1.1 оқиға жүйесін анықтау",
        ),
    )
    result = parse_curriculum(doc(appendix("13", head, body)), "kz", subject="literary_reading_kz")
    assert len(result["objectives"]) == 6
    assert result["anomalies"] == []
    assert [s["title"] for s in result["sections"]] == ["оқылым", "жазылым"]


def test_grade_header_with_en_dash_and_no_subsection_word():
    """У немецкого шапка называет не подраздел, а компетенцию, а в казахской
    редакции «4 – сынып» набрано с тире: без обоих правил 4 класс уходил в подписи."""
    head = (
        'Бастауыш білім беру деңгейінің 3-4-сыныптарына арналған "Неміс тілі" '
        "оқу пәні бойынша үлгілік оқу бағдарламасы"
    )
    body = table(
        [_cell("Kompetenzen des Hörens"), _cell("3-сынып (А1.1)"), _cell("4 – сынып (А1.2)")],
        [
            _cell("1. Hörverstehen"),
            _cell("3. 1. 1. 1 alltägliche Wörter erkennen"),
            _cell("4. 1. 1. 1 einfache Sätze erkennen"),
        ],
    )
    result = parse_curriculum(doc(appendix("24", head, body)), "kz", subject="german")
    assert [o["code"] for o in result["objectives"]] == ["3.1.1.1", "4.1.1.1"]
    assert result["anomalies"] == []


def test_every_registry_code_is_unique_and_variants_are_distinguished():
    assert len(SUBJECT_CODES) == len(set(SUBJECT_CODES))
    from app.adapters.curriculum.parser import _SUBJECTS

    # Две записи с одним названием и классами различимы только скобкой про язык
    # обучения: без неё parse взял бы первое приложение и молча отдал чужую программу.
    seen: dict[tuple, str] = {}
    for spec in _SUBJECTS.values():
        key = (spec.name_ru, spec.grades)
        if key in seen:
            assert spec.instruction and _SUBJECTS[seen[key]].instruction, (spec.code, seen[key])
        seen[key] = spec.code


def test_display_names_say_t1_t2_and_ya1_ya2():
    """Владелец: учителя говорят Т1/Т2 и Я1/Я2, скобка про язык обучения
    в названии для показа не нужна (16.10.2026). Названия приказа
    `name_ru`/`name_kz` при этом остаются прежними — по ним ищется приложение."""
    from app.adapters.curriculum.parser import _SUBJECTS, subject_title_kz

    ru_titles = {
        "kazakh_lang_l1": "Казахский язык (Т1)",
        "kazakh_lang_l2": "Казахский язык (Т2)",
        "russian_lang_l1": "Русский язык (Я1)",
        "russian_lang_l2": "Русский язык (Я2)",
        "literary_reading_ru": "Литературное чтение",
        "literary_reading_kz": "Әдебиеттік оқу",
        "native_language_kz": "Ана тілі",
        "literacy_ru": "Обучение грамоте",
    }
    kz_titles = {
        "kazakh_lang_l1": "Қазақ тілі (Т1)",
        "kazakh_lang_l2": "Қазақ тілі (Т2)",
        "russian_lang_l1": "Орыс тілі (Я1)",
        "russian_lang_l2": "Орыс тілі (Я2)",
        "literary_reading_ru": "Литературное чтение",
        "literary_reading_kz": "Әдебиеттік оқу",
        "native_language_kz": "Ана тілі",
        "literacy_ru": "Сауат ашу",
    }
    for code, title in ru_titles.items():
        assert _SUBJECTS[code].title == title, code
    for code, title in kz_titles.items():
        assert subject_title_kz(code, "нет в реестре") == title, code
    # поиск приложения в приказе не тронут: названия остались приказными
    assert _SUBJECTS["kazakh_lang_l2"].name_ru == "Казахский язык"
    assert _SUBJECTS["kazakh_lang_l2"].name_kz == "Қазақ тілі"
    assert _SUBJECTS["literacy_ru"].name_kz == "Обучение грамоте"
    # неизвестному коду — fallback, а не пустая строка
    assert subject_title_kz("astrology", "Небесная механика") == "Небесная механика"


def test_broken_code_in_text_is_an_error():
    program = {
        "objectives": [
            {
                "code": "2.1.4.1",
                "grade": 2,
                "index": 1,
                "section_no": 1,
                "subsection_code": "1.4",
                "quarters": [1],
                "text": "использовать в речи пословицы 3.1.4. использовать в речи выражения",
            }
        ],
        "sections": [],
    }
    kinds = {i.kind for i in check_program(program, "ru")}
    assert "broken_code_in_text" in kinds


def test_text_before_first_code_is_reported_not_lost():
    """«3.1.4. использовать…» — в приказе у кода пропала последняя цифра. Цель без
    кода в справочник не попадёт, но молчать о ней нельзя."""
    body = table(
        GRADE_HEAD,
        goals_row(
            "1.4 Привлечение внимания",
            "2.1.4.1 использовать в речи пословицы",
            "3.1.4. использовать в речи пословицы и поговорки; 3.1.4.2 сопровождать речь "
            "иллюстрациями",
            "4.1.4.1 использовать в речи отрывки из стихотворений",
        ),
    )
    result = parse(
        doc(
            appendix(
                "15",
                RU_L2.replace("Русский язык", "Литературное чтение").replace(
                    "с нерусским", "с русским"
                ),
                body,
            )
        ),
        "literary_reading_ru",
    )
    assert [o["code"] for o in result["objectives"]] == ["2.1.4.1", "3.1.4.2", "4.1.4.1"]
    assert any("до первого кода" in a for a in result["anomalies"])
