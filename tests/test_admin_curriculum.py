"""Экран сверки справочника учебной программы: чтение, правка формулировок,
разбор подозрений и публикация.

Сцена собирается строками мимо HTTP: справочник наполняется импортом приказа,
и заводить его через ручки нечем — их у импорта нет и не будет. Программа
нарочно крошечная: настоящий разбор математики 1-4 весит 400 КБ, а форма
ответа у него та же.

Формы ответов сверяются целиком с `CONTRACT.md`, раздел «Справочник учебной
программы»: фронт пишется от этих примеров.
"""

from datetime import UTC, datetime

import pytest
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
from app.application.curriculum_import import CurriculumImportService
from app.domain.errors import ConflictError
from tests.conftest import ADMIN_PHONE, login, login_named, make_admin, seed

# Разбор одной редакции приказа берётся у тестов импорта целиком: форма у него
# сложная, а нужен он здесь ровно в одном сюжете — повторный импорт поверх
# ручных правок.
from tests.test_curriculum_import import make_parsed

# Шапка приказа из контракта: № 399 от 16.09.2022, математика 1-4 классов.
IMPORTED_AT = datetime(2026, 9, 19, 15, 50, tzinfo=UTC)
TITLE = (
    'Типовая учебная программа по учебному предмету "Математика" '
    "для 1-4 классов уровня начального образования"
)


def make_subject(code="math", title="Математика") -> CurriculumSubject:
    return seed(CurriculumSubject(code=code, title=title, order_index=1))


def make_program(subject_id: int, **kw) -> CurriculumProgram:
    fields = {
        "subject_id": subject_id,
        "lang": "ru",
        "grade_from": 1,
        "grade_to": 4,
        "order_number": "399",
        "order_date": "2022-09-16",
        "order_date_text": "от 16 сентября 2022 года № 399",
        "source_document_id": "dc1f4914-3cc8-4592-b0c5-4a4e62e0b573",
        "source_url": "https://adilet.zan.kz/rus/docs/V2200029767",
        "title": TITLE,
        "status": "loaded",
        "imported_at": IMPORTED_AT,
    }
    fields.update(kw)
    return seed(CurriculumProgram(**fields))


def make_section(program_id: int, code: str, title: str, **kw) -> CurriculumSection:
    fields = {"program_id": program_id, "code": code, "title": title,
              "parent_id": None, "order_index": 1}
    fields.update(kw)
    return seed(CurriculumSection(**fields))


def make_objective(program_id: int, section_id: int, code: str, **kw) -> LearningObjective:
    grade, _, _, index = code.split(".")
    fields = {
        "program_id": program_id,
        "section_id": section_id,
        "code": code,
        "grade": int(grade),
        "index": int(index),
        "marker": "",
        "text": "считать в прямом и обратном порядке в пределах десяти",
        "quarters": [1],
        "has_image": False,
    }
    fields.update(kw)
    return seed(LearningObjective(**fields))


def make_issue(program_id: int, **kw) -> CurriculumIssue:
    fields = {
        "program_id": program_id,
        "kind": "numbering_gap",
        "severity": "warning",
        "objective_code": "1.1.2.1",
        "message": "[ru] в подразделе 1.2 у 1 класса пропущены номера: 2",
    }
    fields.update(kw)
    return seed(CurriculumIssue(**fields))


class Scene:
    """Программа из контракта: один раздел, два подраздела, три цели.

    Цели разложены так, чтобы каждый фильтр экрана отсекал ровно одну из них:
    класс, подраздел, метка картинки, поиск по тексту и по коду.
    """

    def __init__(self):
        self.subject = make_subject()
        self.program = make_program(self.subject.id)
        self.section = make_section(self.program.id, "1", "Числа и величины")
        self.sub1 = make_section(
            self.program.id, "1.1", "Натуральные числа и число 0. Дроби",
            parent_id=self.section.id, order_index=1,
        )
        self.sub2 = make_section(
            self.program.id, "1.2", "Операции над числами",
            parent_id=self.section.id, order_index=2,
        )
        self.first = make_objective(
            self.program.id, self.sub1.id, "1.1.1.1",
            text=(
                "понимать образование натуральных чисел/числа нуль, считать "
                "в прямом и обратном порядке в пределах 10/11-20"
            ),
            quarters=[1, 2, 4],
        )
        self.with_image = make_objective(
            self.program.id, self.sub2.id, "1.1.2.3",
            marker="**",
            text="сравнивать числа, использовать знаки >, <, =, [img]",
            # В долгосрочном плане приказа цели нет — так бывает
            quarters=[],
            has_image=True,
        )
        self.second_grade = make_objective(
            self.program.id, self.sub1.id, "2.1.1.1", text="считать в пределах 100"
        )


@pytest.fixture
def scene() -> Scene:
    return Scene()


def admin(client, sms):
    """Админ с заполненным ФИО: его фамилию и инициал показывает resolved_by.

    Номер вымышленный, как и везде в тестах.
    """
    resp = login_named(
        client, sms, phone=ADMIN_PHONE, last_name="Нурланова", first_name="Айгуль"
    )
    make_admin(resp.json()["phone"])
    return resp


def program_out(scene: Scene, **kw) -> dict:
    """Программа в форме элемента списка — то, чем отвечают список,
    карточка, публикация и снятие публикации."""
    body = {
        "id": scene.program.id,
        "subject_code": "math",
        "subject_title": "Математика",
        "lang": "ru",
        "grade_from": 1,
        "grade_to": 4,
        "order_number": "399",
        "order_date": "2022-09-16",
        "order_date_text": "от 16 сентября 2022 года № 399",
        "source_url": "https://adilet.zan.kz/rus/docs/V2200029767",
        "title": TITLE,
        "status": "loaded",
        "imported_at": "2026-09-19T15:50:00Z",
        "published_at": None,
        "objectives_count": 3,
        "issues_open": 0,
        "errors_open": 0,
    }
    body.update(kw)
    return body


def import_program(parsed: dict, *, force: bool = False):
    with OrmSession(get_engine()) as db:
        service = CurriculumImportService(CurriculumRepo(db), commit=db.commit)
        return service.load("math", parsed, force=force)


# -- права ----------------------------------------------------------------


def calls(client, scene: Scene) -> list[int]:
    """Все девять ручек справочника одним списком."""
    program, objective, issue = scene.program.id, scene.first.id, 1
    return [
        client.get("/admin/curriculum/programs").status_code,
        client.get(f"/admin/curriculum/programs/{program}").status_code,
        client.get(f"/admin/curriculum/programs/{program}/objectives").status_code,
        client.patch(
            f"/admin/curriculum/objectives/{objective}", json={"text": "новая формулировка"}
        ).status_code,
        client.get(f"/admin/curriculum/programs/{program}/issues").status_code,
        client.post(f"/admin/curriculum/issues/{issue}/resolve").status_code,
        client.delete(f"/admin/curriculum/issues/{issue}/resolve").status_code,
        client.post(f"/admin/curriculum/programs/{program}/publish").status_code,
        client.post(f"/admin/curriculum/programs/{program}/unpublish").status_code,
    ]


def test_curriculum_requires_admin(client, sms, scene):
    """Справочник вычитывает владелец: учителю здесь нет ничего, и отказ
    приходит до того, как ручка что-нибудь скажет о самой программе."""
    assert calls(client, scene) == [401] * 9

    login(client, sms)
    assert calls(client, scene) == [403] * 9
    assert client.get("/admin/curriculum/programs").json()["error"]["code"] == "forbidden"

    # Отказ был именно отказом: правка цели не прошла
    assert scene.first.text.startswith("понимать образование")


# -- список программ ------------------------------------------------------


def test_programs_list_is_empty_before_the_first_import(client, sms):
    admin(client, sms)
    assert client.get("/admin/curriculum/programs").json() == {
        "items": [],
        "total": 0,
        "page": 1,
        "per_page": 20,
    }


def test_programs_list_counts_objectives_and_open_issues(client, sms, scene):
    """Счётчики — то, ради чего экран открывают: пока errors_open больше нуля,
    кнопка публикации неактивна. Разобранное в них не входит."""
    admin(client, sms)
    make_issue(scene.program.id, kind="image_in_text", severity="error")
    make_issue(scene.program.id)
    make_issue(scene.program.id, severity="error", resolved_at=datetime.now(UTC))

    resp = client.get("/admin/curriculum/programs?page=1&per_page=20")
    assert resp.status_code == 200
    assert resp.json() == {
        "items": [program_out(scene, issues_open=2, errors_open=1)],
        "total": 1,
        "page": 1,
        "per_page": 20,
    }


def test_programs_list_filters(client, sms, scene):
    """Фильтры экрана: предмет, языковая редакция, состояние."""
    admin(client, sms)
    other = make_subject(code="phys", title="Физика")
    kz = make_program(scene.subject.id, lang="kz", status="published")
    phys = make_program(other.id, order_number="400")

    def ids(query: str) -> list[int]:
        body = client.get(f"/admin/curriculum/programs?{query}").json()
        return [item["id"] for item in body["items"]]

    assert ids("subject_code=math") == [kz.id, scene.program.id]
    assert ids("subject_code=phys") == [phys.id]
    assert ids("lang=kz") == [kz.id]
    assert ids("status=published") == [kz.id]
    assert ids("subject_code=math&status=loaded") == [scene.program.id]
    assert ids("subject_code=nothing") == []
    assert client.get("/admin/curriculum/programs?lang=de").status_code == 422


# -- карточка программы ---------------------------------------------------


def test_program_card_carries_the_sections_tree(client, sms, scene):
    """Дерево двух уровней: раздел и его подразделы, у подраздела — счётчик
    целей. Он считает цели именно этого подраздела, а не всей программы."""
    admin(client, sms)

    resp = client.get(f"/admin/curriculum/programs/{scene.program.id}")
    assert resp.status_code == 200
    assert resp.json() == program_out(
        scene,
        sections=[
            {
                "id": scene.section.id,
                "code": "1",
                "title": "Числа и величины",
                "subsections": [
                    {
                        "id": scene.sub1.id,
                        "code": "1.1",
                        "title": "Натуральные числа и число 0. Дроби",
                        "objectives_count": 2,
                    },
                    {
                        "id": scene.sub2.id,
                        "code": "1.2",
                        "title": "Операции над числами",
                        "objectives_count": 1,
                    },
                ],
            }
        ],
    )


def test_unknown_program_is_404(client, sms):
    admin(client, sms)
    resp = client.get("/admin/curriculum/programs/999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert client.get("/admin/curriculum/programs/999999/objectives").status_code == 404
    assert client.get("/admin/curriculum/programs/999999/issues").status_code == 404
    assert client.post("/admin/curriculum/programs/999999/publish").status_code == 404
    assert client.post("/admin/curriculum/programs/999999/unpublish").status_code == 404


# -- цели -----------------------------------------------------------------


def test_objectives_page_shape(client, sms, scene):
    admin(client, sms)

    resp = client.get(
        f"/admin/curriculum/programs/{scene.program.id}/objectives?grade=1&page=1&per_page=20"
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "items": [
            {
                "id": scene.first.id,
                "code": "1.1.1.1",
                "grade": 1,
                "index": 1,
                "marker": "",
                "text": (
                    "понимать образование натуральных чисел/числа нуль, считать "
                    "в прямом и обратном порядке в пределах 10/11-20"
                ),
                "quarters": [1, 2, 4],
                "has_image": False,
                "edited_at": None,
                "subsection_code": "1.1",
                "subsection_title": "Натуральные числа и число 0. Дроби",
            },
            {
                "id": scene.with_image.id,
                "code": "1.1.2.3",
                "grade": 1,
                "index": 3,
                "marker": "**",
                "text": "сравнивать числа, использовать знаки >, <, =, [img]",
                # Пустой список — цели нет в долгосрочном плане приказа
                "quarters": [],
                "has_image": True,
                "edited_at": None,
                "subsection_code": "1.2",
                "subsection_title": "Операции над числами",
            },
        ],
        "total": 2,
        "page": 1,
        "per_page": 20,
    }


def test_objectives_filters(client, sms, scene):
    """Фильтры экрана сверки. `q` ищет и по тексту, и по коду: на экране это
    одна строка поиска."""
    admin(client, sms)
    base = f"/admin/curriculum/programs/{scene.program.id}/objectives"

    def codes(query: str = "") -> list[str]:
        body = client.get(f"{base}?{query}").json()
        return [item["code"] for item in body["items"]]

    # Порядок — по коду: класс, раздел, подраздел, номер
    assert codes() == ["1.1.1.1", "1.1.2.3", "2.1.1.1"]
    assert codes("grade=2") == ["2.1.1.1"]
    assert codes("subsection=1.2") == ["1.1.2.3"]
    assert codes("q=сравнивать") == ["1.1.2.3"]
    assert codes("q=2.1.1") == ["2.1.1.1"]
    assert codes("has_image=true") == ["1.1.2.3"]
    assert codes("has_image=false") == ["1.1.1.1", "2.1.1.1"]
    assert codes("edited=true") == []
    assert codes("grade=1&subsection=1.1") == ["1.1.1.1"]

    body = client.get(f"{base}?q=такого+текста+нет").json()
    assert body == {"items": [], "total": 0, "page": 1, "per_page": 20}


def test_edit_marks_the_objective_and_recounts_has_image(client, sms, scene):
    """Знак вставлен в приказ картинкой, и вместо него стоит [img]: человек
    проставляет знак руками, а метка has_image снимается сама."""
    admin(client, sms)

    resp = client.patch(
        f"/admin/curriculum/objectives/{scene.with_image.id}",
        json={"text": "сравнивать числа, использовать знаки >, <, =, ≠"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["edited_at"] is not None
    assert body == {
        "id": scene.with_image.id,
        "code": "1.1.2.3",
        "grade": 1,
        "index": 3,
        "marker": "**",
        "text": "сравнивать числа, использовать знаки >, <, =, ≠",
        "quarters": [],
        "has_image": False,
        "edited_at": body["edited_at"],
        "subsection_code": "1.2",
        "subsection_title": "Операции над числами",
    }

    # Правка видна списку: цель ушла из «с картинкой» и попала в «поправленные»
    base = f"/admin/curriculum/programs/{scene.program.id}/objectives"
    assert client.get(f"{base}?has_image=true").json()["items"] == []
    edited = client.get(f"{base}?edited=true").json()
    assert [item["code"] for item in edited["items"]] == ["1.1.2.3"]
    assert edited["items"][0]["edited_at"] == body["edited_at"]


def test_edit_keeps_the_image_mark_when_it_is_still_there(client, sms, scene):
    """Метка пересчитывается по новому тексту, а не гасится фактом правки:
    поправили соседнее слово — картинка осталась картинкой."""
    admin(client, sms)

    resp = client.patch(
        f"/admin/curriculum/objectives/{scene.with_image.id}",
        json={"text": "сравнивать числа, использовать знаки >, <, =, [img]!"},
    )
    assert resp.json()["has_image"] is True


def test_edit_refuses_an_empty_text(client, sms, scene):
    admin(client, sms)

    resp = client.patch(
        f"/admin/curriculum/objectives/{scene.first.id}", json={"text": "   "}
    )
    assert resp.status_code == 422
    assert resp.json() == {
        "error": {
            "code": "validation_error",
            "message": "Проверьте заполнение полей",
            "details": {
                "fields": [
                    {"field": "text", "message": "Формулировка цели не может быть пустой"}
                ]
            },
        }
    }

    # Код, класс и подраздел приказом заданы и не правятся: лишнее поле в теле —
    # это опечатка фронта, и принимать её молча нельзя
    extra = client.patch(
        f"/admin/curriculum/objectives/{scene.first.id}",
        json={"text": "новая формулировка", "code": "9.9.9.9"},
    )
    assert extra.status_code == 422


def test_edit_in_a_published_program_is_refused(client, sms, scene):
    """Опубликованную программу видит генерация: менять под ней формулировку
    значит менять уже сделанные планы задним числом."""
    admin(client, sms)
    assert client.post(
        f"/admin/curriculum/programs/{scene.program.id}/publish"
    ).status_code == 200

    resp = client.patch(
        f"/admin/curriculum/objectives/{scene.first.id}", json={"text": "новая формулировка"}
    )
    assert resp.status_code == 409
    assert resp.json() == {
        "error": {
            "code": "conflict",
            "message": "Программа опубликована: сначала снять публикацию",
        }
    }


def test_edit_of_an_unknown_objective_is_404(client, sms, scene):
    admin(client, sms)
    resp = client.patch(
        "/admin/curriculum/objectives/999999", json={"text": "новая формулировка"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# -- подозрения -----------------------------------------------------------


def test_issues_page_shape_and_order(client, sms, scene):
    """Ошибки сверху, внутри — по коду цели. Подозрение о программе целиком
    идёт без кода."""
    admin(client, sms)
    gap = make_issue(scene.program.id)
    image = make_issue(
        scene.program.id,
        kind="image_in_text",
        severity="error",
        objective_code="1.1.2.3",
        message="[ru] вместо математического знака стоит [img] — проставить руками",
    )
    counts = make_issue(
        scene.program.id,
        kind="count_mismatch",
        severity="error",
        objective_code=None,
        message="[ru] в подразделе 1.2 целей 1, в казахской редакции 2",
    )

    resp = client.get(
        f"/admin/curriculum/programs/{scene.program.id}/issues?resolved=false"
        "&page=1&per_page=20"
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "items": [
            {
                "id": image.id,
                "kind": "image_in_text",
                "severity": "error",
                "objective_code": "1.1.2.3",
                "message": "[ru] вместо математического знака стоит [img] — проставить руками",
                "resolved_at": None,
                "resolved_by": None,
            },
            {
                "id": counts.id,
                "kind": "count_mismatch",
                "severity": "error",
                "objective_code": None,
                "message": "[ru] в подразделе 1.2 целей 1, в казахской редакции 2",
                "resolved_at": None,
                "resolved_by": None,
            },
            {
                "id": gap.id,
                "kind": "numbering_gap",
                "severity": "warning",
                "objective_code": "1.1.2.1",
                "message": "[ru] в подразделе 1.2 у 1 класса пропущены номера: 2",
                "resolved_at": None,
                "resolved_by": None,
            },
        ],
        "total": 3,
        "page": 1,
        "per_page": 20,
    }


def test_issues_filters(client, sms, scene):
    admin(client, sms)
    make_issue(scene.program.id, kind="image_in_text", severity="error")
    gap = make_issue(scene.program.id)
    base = f"/admin/curriculum/programs/{scene.program.id}/issues"

    def kinds(query: str = "") -> list[str]:
        return [item["kind"] for item in client.get(f"{base}?{query}").json()["items"]]

    assert kinds("severity=error") == ["image_in_text"]
    assert kinds("severity=warning") == ["numbering_gap"]
    assert kinds("resolved=true") == []

    assert client.post(f"/admin/curriculum/issues/{gap.id}/resolve").status_code == 200
    assert kinds("resolved=true") == ["numbering_gap"]
    assert kinds("resolved=false") == ["image_in_text"]
    assert client.get(f"{base}?resolved=true&severity=error").json() == {
        "items": [],
        "total": 0,
        "page": 1,
        "per_page": 20,
    }


def test_resolve_names_who_decided_and_survives_a_double_click(client, sms, scene):
    """«Я посмотрел, так и должно быть»: данные не исправляются, а через месяц
    видно, кто принял решение. Повторный клик — та же строка, а не ошибка."""
    admin(client, sms)
    issue = make_issue(scene.program.id)

    resp = client.post(f"/admin/curriculum/issues/{issue.id}/resolve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_at"] is not None
    assert body == {
        "id": issue.id,
        "kind": "numbering_gap",
        "severity": "warning",
        "objective_code": "1.1.2.1",
        "message": "[ru] в подразделе 1.2 у 1 класса пропущены номера: 2",
        "resolved_at": body["resolved_at"],
        "resolved_by": "Нурланова А.",
    }

    again = client.post(f"/admin/curriculum/issues/{issue.id}/resolve")
    assert again.status_code == 200
    assert again.json() == body


def test_unresolve_gives_the_row_back(client, sms, scene):
    """Передумал: строка возвращается в разбор той же формой."""
    admin(client, sms)
    issue = make_issue(scene.program.id)
    client.post(f"/admin/curriculum/issues/{issue.id}/resolve")

    resp = client.delete(f"/admin/curriculum/issues/{issue.id}/resolve")
    assert resp.status_code == 200
    assert resp.json() == {
        "id": issue.id,
        "kind": "numbering_gap",
        "severity": "warning",
        "objective_code": "1.1.2.1",
        "message": "[ru] в подразделе 1.2 у 1 класса пропущены номера: 2",
        "resolved_at": None,
        "resolved_by": None,
    }
    assert client.post(f"/admin/curriculum/issues/{issue.id}/resolve").json()["resolved_by"] == (
        "Нурланова А."
    )


def test_unknown_issue_is_404(client, sms):
    admin(client, sms)
    assert client.post("/admin/curriculum/issues/999999/resolve").status_code == 404
    assert client.delete("/admin/curriculum/issues/999999/resolve").status_code == 404


# -- публикация -----------------------------------------------------------


def test_publish_is_refused_while_errors_are_unresolved(client, sms, scene):
    """Справочник с кривой целью хуже пустого: по нему сгенерируется
    правдоподобный план урока, который завернёт завуч."""
    admin(client, sms)
    for _ in range(3):
        make_issue(scene.program.id, kind="image_in_text", severity="error")

    resp = client.post(f"/admin/curriculum/programs/{scene.program.id}/publish")
    assert resp.status_code == 409
    assert resp.json() == {
        "error": {
            "code": "conflict",
            "message": "В программе осталось 3 неразобранных ошибок — сначала разобрать их",
        }
    }
    card = client.get(f"/admin/curriculum/programs/{scene.program.id}").json()
    assert card["status"] == "loaded"
    assert card["errors_open"] == 3


def test_warnings_do_not_block_publishing(client, sms, scene):
    """Предупреждения на то и предупреждения: разрыв в нумерации бывает
    законным — цель могли исключить приказом об изменениях."""
    admin(client, sms)
    make_issue(scene.program.id)

    resp = client.post(f"/admin/curriculum/programs/{scene.program.id}/publish")
    assert resp.status_code == 200
    assert resp.json()["status"] == "published"


def test_publish_and_unpublish_go_back_and_forth(client, sms, scene):
    """Снятие публикации не удаляет ничего: программа просто перестаёт
    предлагаться в новых планах."""
    admin(client, sms)

    published = client.post(f"/admin/curriculum/programs/{scene.program.id}/publish")
    assert published.status_code == 200
    body = published.json()
    assert body["published_at"] is not None
    assert body == program_out(scene, status="published", published_at=body["published_at"])

    back = client.post(f"/admin/curriculum/programs/{scene.program.id}/unpublish")
    assert back.status_code == 200
    assert back.json() == program_out(scene)

    # Цели и разделы на месте, и публиковать можно снова
    card = client.get(f"/admin/curriculum/programs/{scene.program.id}").json()
    assert card["objectives_count"] == 3
    assert len(card["sections"][0]["subsections"]) == 2
    assert client.post(
        f"/admin/curriculum/programs/{scene.program.id}/publish"
    ).status_code == 200


# -- повторный импорт поверх ручных правок --------------------------------


def test_reimport_over_manual_edits_is_refused_and_force_passes(client, sms):
    """Повторный импорт сносит программу целиком. Ручная вычитка — чужая
    работа, и стирается она только по явному согласию."""
    admin(client, sms)
    first = import_program({"ru": make_parsed(), "kz": make_parsed()})
    program_id = first.program_ids["ru"]

    objectives = client.get(
        f"/admin/curriculum/programs/{program_id}/objectives"
    ).json()["items"]
    resp = client.patch(
        f"/admin/curriculum/objectives/{objectives[0]['id']}",
        json={"text": "сравнивать числа, использовать знаки >, <, =, ≠"},
    )
    assert resp.status_code == 200

    with pytest.raises(ConflictError) as refused:
        import_program({"ru": make_parsed(), "kz": make_parsed()})
    assert "1 цель поправлена" in refused.value.message

    # Программа на месте вместе с правкой: отказ ничего не снёс по дороге
    edited = client.get(
        f"/admin/curriculum/programs/{program_id}/objectives?edited=true"
    ).json()
    assert edited["total"] == 1

    second = import_program({"ru": make_parsed(), "kz": make_parsed()}, force=True)
    assert second.program_ids["ru"] != program_id
    assert client.get(f"/admin/curriculum/programs/{program_id}").status_code == 404
    fresh = client.get(
        f"/admin/curriculum/programs/{second.program_ids['ru']}/objectives"
    ).json()
    assert [item["edited_at"] for item in fresh["items"]] == [None, None]
