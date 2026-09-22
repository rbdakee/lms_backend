"""Импорт справочника учебной программы и его автопроверки.

Справочник наполняется только импортом из приказа, эндпоинтов у него пока нет —
поэтому сценарий зовётся напрямую, прямой сессией базы. Программы здесь
синтетические и нарочно крошечные: настоящий разбор математики 1-4 классов
весит 400 КБ, и в тесте его не прочитать глазами, а форма у него та же.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import (
    CurriculumIssue,
    CurriculumProgram,
    CurriculumSection,
    CurriculumSubject,
    LearningObjective,
)
from app.adapters.db.repos import CurriculumRepo
from app.application.curriculum_checks import check_all, check_pair, check_program
from app.application.curriculum_import import CurriculumImportService
from app.domain.errors import ConflictError, ValidationAppError

# Формулировка настоящей длины и без цифр: короткая или пустая ловится
# отдельными проверками, и попадаться в них случайно она не должна.
TEXT = "считать в прямом и обратном порядке в пределах десяти"


def make_objective(code: str, **kw) -> dict:
    """Одна разобранная цель. Всё, что выводится из кода, выводится из него:
    класс, раздел, подраздел и номер — иначе половина тестов проверяла бы
    опечатку в фикстуре, а не автопроверку."""
    grade, section_no, subsection_no, index = code.split(".")
    fields = {
        "code": code,
        "grade": int(grade),
        "section_no": int(section_no),
        "subsection_code": f"{section_no}.{subsection_no}",
        "subsection_title": f"Подраздел {section_no}.{subsection_no}",
        "index": int(index),
        "marker": "",
        "text": TEXT,
        "quarters": [1],
    }
    fields.update(kw)
    return fields


def sections_for(objectives: list[dict]) -> list[dict]:
    """Разделы собираются по целям, а не задаются отдельно: раздел без целей —
    это тоже подозрение, и ловить его в каждом тесте незачем."""
    sections: dict[int, dict] = {}
    for obj in objectives:
        section = sections.setdefault(
            obj["section_no"],
            {"no": obj["section_no"], "title": f"Раздел {obj['section_no']}", "subsections": {}},
        )
        section["subsections"].setdefault(
            obj["subsection_code"],
            {
                "code": obj["subsection_code"],
                "no": int(obj["subsection_code"].split(".")[1]),
                "title": obj["subsection_title"],
            },
        )
    return [
        {"no": s["no"], "title": s["title"], "subsections": list(s["subsections"].values())}
        for s in sorted(sections.values(), key=lambda s: s["no"])
    ]


def make_parsed(codes=("1.1.1.1", "1.1.1.2"), objectives=None, **kw) -> dict:
    """Разбор одной языковой редакции — то, что парсер отдаёт импорту.

    По умолчанию программа чистая: один раздел, один подраздел, две цели
    подряд, у каждой четверть и внятная формулировка. Тест ломает ровно то,
    что проверяет, и остальные подозрения молчат.
    """
    if objectives is None:
        objectives = [make_objective(code) for code in codes]
    data = {
        "subject": "Математика",
        "grades": sorted({obj["grade"] for obj in objectives}),
        "program_title": "Типовая учебная программа по учебному предмету «Математика»",
        "source": {
            "order_number": "399",
            "order_date": "2022-09-16",
            "order_date_text": "от 16 сентября 2022 года № 399",
            "document_id": "dc1f4914-3cc8-4592-b0c5-4a4e62e0b573",
            "group_number": "V2200029767",
        },
        "sections": sections_for(objectives),
        "objectives": objectives,
    }
    data.update(kw)
    return data


def make_broken_pair() -> dict:
    """Пара с ошибкой: в казахской редакции потерян код. Это error, и с ним
    программу не публикуют."""
    return {"ru": make_parsed(), "kz": make_parsed(codes=["1.1.1.1"])}


def import_program(parsed: dict, subject_code: str = "math"):
    """Импорт мимо HTTP: у справочника эндпоинтов пока нет."""
    with OrmSession(get_engine()) as db:
        service = CurriculumImportService(CurriculumRepo(db), commit=db.commit)
        return service.load(subject_code, parsed)


def publish_program(program_id: int) -> None:
    with OrmSession(get_engine()) as db:
        service = CurriculumImportService(CurriculumRepo(db), commit=db.commit)
        service.publish(db.get(CurriculumProgram, program_id))


def resolve_issues(program_id: int) -> None:
    """Человек разобрал подозрения: строка гасится resolved_at, а не удаляется."""
    with OrmSession(get_engine()) as db:
        for issue in db.scalars(
            select(CurriculumIssue).where(CurriculumIssue.program_id == program_id)
        ):
            issue.resolved_at = datetime.now(UTC)
        db.commit()


def count(db, model, *where) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*where))


def kinds(issues) -> list[str]:
    return [issue.kind for issue in issues]


# -- импорт --------------------------------------------------------------


def test_load_creates_one_program_per_edition_under_a_single_subject():
    """Обе редакции приказа грузятся вместе: предмет один на оба языка,
    программ две, и число целей в каждой совпало со входом."""
    ru, kz = make_parsed(), make_parsed()

    result = import_program({"ru": ru, "kz": kz})

    assert set(result.program_ids) == {"ru", "kz"}
    assert result.objectives == {"ru": 2, "kz": 2}
    assert result.issues_total == 0

    with OrmSession(get_engine()) as db:
        subjects = list(db.scalars(select(CurriculumSubject)))
        assert [subject.code for subject in subjects] == ["math"]

        programs = list(db.scalars(select(CurriculumProgram).order_by(CurriculumProgram.id)))
        assert [program.lang for program in programs] == ["ru", "kz"]
        assert {program.subject_id for program in programs} == {subjects[0].id}
        assert {program.status for program in programs} == {"loaded"}

        repo = CurriculumRepo(db)
        for program in programs:
            assert repo.objectives_count(program.id) == len(ru["objectives"])


def test_load_lays_sections_and_subsections_out_in_two_levels():
    """Раздел и подраздел лежат в одной таблице двумя уровнями: у раздела
    parent_id пуст, подраздел ссылается на раздел, а цель — на подраздел."""
    codes = ["1.1.1.1", "1.1.2.1"]

    result = import_program({"ru": make_parsed(codes=codes), "kz": make_parsed(codes=codes)})

    with OrmSession(get_engine()) as db:
        program_id = result.program_ids["ru"]
        rows = list(
            db.scalars(
                select(CurriculumSection)
                .where(CurriculumSection.program_id == program_id)
                .order_by(CurriculumSection.id)
            )
        )
        roots = [row for row in rows if row.parent_id is None]
        subsections = [row for row in rows if row.parent_id is not None]

        assert [root.code for root in roots] == ["1"]
        assert sorted(sub.code for sub in subsections) == ["1.1", "1.2"]
        assert {sub.parent_id for sub in subsections} == {roots[0].id}

        code_by_id = {sub.id: sub.code for sub in subsections}
        objectives = list(
            db.scalars(
                select(LearningObjective)
                .where(LearningObjective.program_id == program_id)
                .order_by(LearningObjective.code)
            )
        )
        assert [code_by_id[obj.section_id] for obj in objectives] == ["1.1", "1.2"]


def test_second_import_replaces_the_first():
    """Повторный импорт того же приказа заменяет программу, а не удваивает
    справочник: старые разделы, цели и подозрения уходят вместе с ней."""
    first = import_program({"ru": make_parsed(), "kz": make_parsed()})
    second = import_program({"ru": make_parsed(), "kz": make_parsed()})

    assert set(second.program_ids.values()) != set(first.program_ids.values())

    with OrmSession(get_engine()) as db:
        assert count(db, CurriculumSubject) == 1
        programs = list(db.scalars(select(CurriculumProgram)))
        assert {program.id for program in programs} == set(second.program_ids.values())
        assert count(db, LearningObjective) == 4
        assert (
            count(
                db,
                LearningObjective,
                LearningObjective.program_id.in_(first.program_ids.values()),
            )
            == 0
        )


def test_second_import_of_a_published_program_is_refused():
    """Опубликованную программу импорт не трогает: на её цели уже могут
    ссылаться готовые планы уроков."""
    result = import_program({"ru": make_parsed(), "kz": make_parsed()})
    publish_program(result.program_ids["ru"])

    with pytest.raises(ConflictError):
        import_program({"ru": make_parsed(), "kz": make_parsed()})

    with OrmSession(get_engine()) as db:
        assert count(db, CurriculumProgram) == 2
        assert db.get(CurriculumProgram, result.program_ids["ru"]).status == "published"


def test_load_without_the_second_edition_is_refused():
    """Редакции грузятся только вместе: главная проверка импорта — сверка
    кодов между ними, а сверять нечего, если вторая приедет отдельно."""
    with pytest.raises(ValidationAppError):
        import_program({"ru": make_parsed()})

    with OrmSession(get_engine()) as db:
        assert count(db, CurriculumProgram) == 0


def test_objective_pointing_at_an_unknown_subsection_is_refused():
    """Цель без подраздела — ошибка разбора, а не данные: положить её «никуда»
    значит потерять её из справочника молча, поэтому отбивается весь импорт."""
    ru = make_parsed()
    ru["objectives"][0]["subsection_code"] = "9.9"

    with pytest.raises(ValidationAppError):
        import_program({"ru": ru, "kz": make_parsed()})

    with OrmSession(get_engine()) as db:
        assert count(db, CurriculumProgram) == 0
        assert count(db, LearningObjective) == 0


def test_image_tag_in_the_text_sets_has_image():
    """[img] вместо математического знака — метка парсера: такую цель импорт
    отмечает, и на сверке она показывается человеку первой."""
    ru = make_parsed()
    ru["objectives"][0]["text"] = "сравнивать числа с использованием знака [img]"

    result = import_program({"ru": ru, "kz": make_parsed()})

    with OrmSession(get_engine()) as db:
        rows = list(
            db.scalars(
                select(LearningObjective)
                .where(LearningObjective.program_id == result.program_ids["ru"])
                .order_by(LearningObjective.code)
            )
        )
        assert [obj.has_image for obj in rows] == [True, False]


def test_publish_is_refused_while_errors_are_unresolved():
    """Справочник с кривой целью хуже пустого: по нему сгенерируется
    правдоподобный план урока, который завернёт завуч."""
    result = import_program(make_broken_pair())
    assert result.errors > 0

    with pytest.raises(ConflictError):
        publish_program(result.program_ids["ru"])

    with OrmSession(get_engine()) as db:
        assert db.get(CurriculumProgram, result.program_ids["ru"]).status == "loaded"


def test_publish_passes_a_program_without_issues():
    """Ошибок нет — публикация проходит, и программа получает дату: генерация
    видит только опубликованное."""
    result = import_program({"ru": make_parsed(), "kz": make_parsed()})

    publish_program(result.program_ids["ru"])

    with OrmSession(get_engine()) as db:
        program = db.get(CurriculumProgram, result.program_ids["ru"])
        assert program.status == "published"
        assert program.published_at is not None


def test_publish_passes_when_errors_are_marked_resolved():
    """Подозрение гасится resolved_at, а не удаляется: иначе повторный импорт
    показал бы то же самое во второй раз. Разобранное публикации не мешает."""
    result = import_program(make_broken_pair())
    resolve_issues(result.program_ids["ru"])

    publish_program(result.program_ids["ru"])

    with OrmSession(get_engine()) as db:
        assert db.get(CurriculumProgram, result.program_ids["ru"]).status == "published"
        # Казахскую редакцию никто не разбирал — её подозрения остались своими
        assert db.get(CurriculumProgram, result.program_ids["kz"]).status == "loaded"


# -- автопроверки --------------------------------------------------------


def test_codes_missing_on_one_side_are_reported_in_both_directions():
    """Коды целей двух редакций одного приказа обязаны совпадать: расхождение
    означает потерю при разборе, и видно оно без знания предмета."""
    ru = make_parsed(codes=["1.1.1.1", "1.1.1.2"])
    kz = make_parsed(codes=["1.1.1.1", "1.1.1.3"])

    issues = check_pair(ru, kz)

    assert {(issue.kind, issue.objective_code) for issue in issues} == {
        ("missing_in_kz", "1.1.1.2"),
        ("missing_in_ru", "1.1.1.3"),
    }
    assert {issue.severity for issue in issues} == {"error"}


def test_one_code_in_different_subsections_is_a_mismatch():
    """Один и тот же код не может жить в разных подразделах: в приказе
    это одна цель."""
    ru = make_parsed(codes=["1.1.1.1"])
    kz = make_parsed(codes=["1.1.1.1"])
    kz["objectives"][0]["subsection_code"] = "1.2"

    issues = check_pair(ru, kz)

    assert kinds(issues) == ["subsection_mismatch"]
    assert issues[0].objective_code == "1.1.1.1"
    assert issues[0].severity == "error"


def test_duplicate_code_is_an_error():
    """В справочнике код цели один: по нему завуч сверяет план урока."""
    program = make_parsed(codes=["1.1.1.1", "1.1.1.1"])

    issues = check_program(program, "ru")

    assert kinds(issues) == ["duplicate_code"]
    assert issues[0].severity == "error"


def test_grade_that_disagrees_with_the_code_is_an_error():
    """Класс берётся из первой цифры кода, колонка таблицы — только сверка:
    расхождение значит, что цель уехала в чужой класс при разборе."""
    program = make_parsed(codes=["1.1.1.1"])
    program["objectives"][0]["grade"] = 2

    issues = check_program(program, "ru")

    assert kinds(issues) == ["grade_mismatch"]
    assert issues[0].severity == "error"


def test_a_foreign_code_inside_the_text_is_an_error():
    """Чужой код внутри формулировки означает, что в одну ячейку слиплись
    две цели."""
    program = make_parsed(codes=["1.1.1.1"])
    program["objectives"][0]["text"] = "сравнивать числа, как этого требует цель 1.1.1.2"

    issues = check_program(program, "ru")

    assert kinds(issues) == ["code_inside_text"]
    assert issues[0].objective_code == "1.1.1.1"


def test_image_tag_in_the_text_is_an_error():
    """Знак вставлен в приказ картинкой: снять метку молча значит потерять
    знак, поэтому проставляет его человек руками."""
    program = make_parsed(codes=["1.1.1.1"])
    program["objectives"][0]["text"] = "сравнивать числа с использованием знака [img]"

    issues = check_program(program, "ru")

    assert kinds(issues) == ["image_in_text"]
    assert issues[0].severity == "error"


def test_empty_text_is_an_error():
    """Цель без формулировки — обрубок разбора, а не цель."""
    program = make_parsed(codes=["1.1.1.1"])
    program["objectives"][0]["text"] = ""

    issues = check_program(program, "ru")

    assert kinds(issues) == ["empty_text"]
    assert issues[0].severity == "error"


def test_short_text_is_a_warning():
    """Совсем короткая формулировка почти наверняка обрубок ячейки — но
    решает человек, поэтому это предупреждение."""
    program = make_parsed(codes=["1.1.1.1"])
    program["objectives"][0]["text"] = "считать"

    issues = check_program(program, "ru")

    assert kinds(issues) == ["short_text"]
    assert issues[0].severity == "warning"


def test_objective_without_a_quarter_is_a_warning():
    """Цель, не привязанную ни к одной четверти, учитель не найдёт в плане."""
    program = make_parsed(codes=["1.1.1.1"])
    program["objectives"][0]["quarters"] = []

    issues = check_program(program, "ru")

    assert kinds(issues) == ["no_quarter"]
    assert issues[0].severity == "warning"


def test_numbering_gap_is_a_warning_not_an_error():
    """Разрыв в нумерации бывает законным: цель могли исключить приказом
    об изменениях. Ошибкой он быть не может — иначе публикация встанет
    на ровном месте."""
    program = make_parsed(codes=["1.1.1.1", "1.1.1.3"])

    issues = check_program(program, "ru")

    assert kinds(issues) == ["numbering_gap"]
    assert issues[0].severity == "warning"


def test_a_clean_pair_of_programs_raises_no_suspicion():
    """Важнее остальных проверок: та, что срабатывает всегда, бесполезна —
    человек перестанет читать список и пропустит настоящую ошибку."""
    assert check_all(make_parsed(), make_parsed()) == []


# -- подозрения, оставшиеся без теста ---------------------------------------


def test_truncated_formulation_is_caught():
    """Цели пишутся со строчной буквы и без точки, поэтому признак обрыва —
    не «нет точки», а висящая запятая: так оборвана 4.1.3.2 в самом приказе."""
    program = make_parsed(codes=["1.1.1.1"])
    program["objectives"][0]["text"] = "переводить единицы измерения площади га, ар,"

    issues = check_program(program, "ru")

    assert kinds(issues) == ["truncated_text"]
    assert issues[0].severity == "warning"


def test_quarters_that_disagree_between_editions_are_caught():
    """Четверть у цели — свойство приказа, а не перевода. Разошлась между
    редакциями — значит одну из таблиц разобрало криво."""
    ru = make_parsed(codes=["1.1.1.1"])
    kz = make_parsed(codes=["1.1.1.1"])
    kz["objectives"][0]["quarters"] = [3]

    issues = check_pair(ru, kz)

    assert kinds(issues) == ["quarters_mismatch"]
    assert issues[0].objective_code == "1.1.1.1"
    assert issues[0].severity == "warning"


def test_editions_with_a_different_number_of_objectives_are_caught():
    """Разное число целей — потеря при разборе. Ловится и поимённо, и счётом:
    счёт нужен на случай, когда потерялись коды сразу с обеих сторон."""
    ru = make_parsed(codes=["1.1.1.1", "1.1.1.2"])
    kz = make_parsed(codes=["1.1.1.1"])

    issues = check_pair(ru, kz)

    assert "count_mismatch" in kinds(issues)
    assert "missing_in_kz" in kinds(issues)


def test_section_without_objectives_is_caught():
    """Раздел, в который не попало ни одной цели, — признак съехавшей ячейки:
    в приказе пустых разделов не бывает."""
    program = make_parsed(codes=["1.1.1.1"])
    program["sections"].append(
        {"no": 9, "title": "Раздел без целей", "subsections": []},
    )

    issues = check_program(program, "ru")

    assert kinds(issues) == ["empty_section"]


def test_subsection_without_objectives_is_caught():
    """То же на уровень ниже: подраздел есть в шапке таблицы, а целей в нём нет."""
    program = make_parsed(codes=["1.1.1.1"])
    program["sections"][0]["subsections"].append(
        {"code": "1.9", "no": 9, "title": "Подраздел без целей"},
    )

    issues = check_program(program, "ru")

    assert kinds(issues) == ["empty_subsection"]


def test_a_currency_sign_in_a_formulation_is_caught():
    """Знак ∈ в приказе выгружен шрифтом Symbol и приезжает как €. Формулировка
    при этом выглядит правдоподобно, и глазами такое не находят — нашлось
    на живых данных в цели 2.5.2.2."""
    program = make_parsed(codes=["1.1.1.1"])
    program["objectives"][0]["text"] = "обозначать принадлежность элемента множеству знаком €"

    issues = check_program(program, "ru")

    assert kinds(issues) == ["suspicious_symbol"]
    assert issues[0].severity == "error"
