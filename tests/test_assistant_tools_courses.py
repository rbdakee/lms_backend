"""Курсы, площадки и программа глазами ассистента.

Проверяется не то, что сценарии работают — это дело `test_admin_courses.py`
и `test_admin_program.py`, — а то, что между моделью и сценарием стоит:
опасное не выполняется, а предлагается; предложение называет числа
и площадки; отказ сценария доезжает до модели ошибкой, а не пятисоткой.
"""

import pytest
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import Course, CoursePlatform, Module
from app.application.ports import AiToolCall
from app.assistant import registry
from app.assistant.loop import AssistantLoop
from app.assistant.registry import ToolCtx, ToolError
from tests.conftest import (
    login_admin,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_progress,
    make_quiz,
    make_user,
)


def called(client, sms, tool_ctx) -> ToolCtx:
    """Админ, его чат и контекст вызова: с этого начинается каждый тест —
    опасному действию нужен чат, иначе подтверждать его негде."""
    admin = login_admin(client, sms).json()["id"]
    thread = client.post("/admin/assistant/threads", json={}).json()
    return tool_ctx(admin, thread["id"])


def execute(client, result) -> dict:
    """Нажатие кнопки «Выполнить» — единственный способ исполнить `confirm`."""
    return client.post(f"/admin/assistant/proposals/{result['proposal_id']}/execute")


def platforms_of(course_id: int) -> dict[str, int | None]:
    with OrmSession(get_engine()) as db:
        rows = db.query(CoursePlatform).filter(CoursePlatform.course_id == course_id).all()
        return {row.platform: row.price for row in rows}


# -- пометки опасности ---------------------------------------------------


def test_deleting_and_publishing_tools_are_confirm():
    """Пометка проверяется тестом, а не чтением кода: `write` вместо `confirm`
    у удаления означает курс, снесённый без кнопки."""
    for name in ("course_delete", "module_delete", "course_unpublish"):
        assert registry.get(name).danger == "confirm", name
    # Публикация и цена наружу видны людям, поэтому тоже через кнопку
    for name in ("course_status", "course_publish"):
        assert registry.get(name).danger == "confirm", name


def test_writing_tools_are_write():
    for name in (
        "course_create",
        "course_update",
        "course_add_version",
        "course_duplicate",
        "module_add",
        "module_rename",
        "program_move",
    ):
        assert registry.get(name).danger == "write", name


# -- списки и площадки ---------------------------------------------------


def test_list_without_platform_counts_both(client, sms, tool_ctx):
    """«12 курсов» без площадки — брак: в ответе видно число по каждой."""
    ctx = called(client, sms, tool_ctx)
    make_course(title="Оценивание", platforms={"p1": 45000})
    make_course(title="Инклюзия", platforms={"p2": 60000})

    data = registry.call("courses_list", {}, ctx)

    assert data["total"] == 2
    assert data["by_platform"] == {"p1": 1, "p2": 1}
    assert data["shown"] == 2
    assert data["truncated"] is False


# -- заведение и правка курса --------------------------------------------


def test_course_is_created_as_a_draft_without_platforms(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)

    result = registry.call(
        "course_create",
        {"title": "Формативное оценивание", "lang": "ru", "category_id": 3, "hours": 72},
        ctx,
    )
    ctx.db.commit()

    assert result["status"] == "draft"
    assert result["platforms"] == []
    with OrmSession(get_engine()) as db:
        assert db.get(Course, result["course_id"]).title == "Формативное оценивание"


def test_course_update_is_seen_on_both_platforms(client, sms, tool_ctx):
    """Курс общий: правка полей задевает обе площадки, и набор их не трогает."""
    ctx = called(client, sms, tool_ctx)
    course = make_course(title="Старое название", platforms={"p1": 45000, "p2": 60000})

    result = registry.call(
        "course_update", {"course_id": course.id, "title": "Новое название", "hours": 36}, ctx
    )
    ctx.db.commit()

    assert result["title"] == "Новое название"
    assert result["changed"] == ["hours", "title"]
    assert platforms_of(course.id) == {"p1": 45000, "p2": 60000}


def test_course_update_without_fields_is_refused(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    with pytest.raises(ToolError) as exc:
        registry.call("course_update", {"course_id": course.id}, ctx)
    assert exc.value.code == "nothing_to_change"


def test_hours_outside_the_range_are_refused(client, sms, tool_ctx):
    """Границы схемы запроса инструмент проходит мимо, поэтому держит их сам."""
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    with pytest.raises(ToolError) as exc:
        registry.call("course_update", {"course_id": course.id, "hours": 100000}, ctx)
    assert exc.value.code == "bad_argument"


def test_refused_scenario_reaches_the_model_as_an_error(client, sms, tool_ctx):
    """Отказ сценария — обычный результат вызова, а не пятисотка: цикл
    показывает его модели, и та говорит правду вместо «кажется, получилось»."""
    ctx = called(client, sms, tool_ctx)
    course = make_course(status="open")

    step = AssistantLoop(ai=None, max_tool_calls=10)._one(
        AiToolCall(id="call_1", name="course_update", arguments={
            "course_id": course.id, "category_id": 4242
        }),
        ctx,
    )

    assert step.result["error"]["code"] == "validation_error"


# -- статус, публикация и цена -------------------------------------------


def test_status_change_is_proposed_with_platforms(client, sms, tool_ctx):
    """Снятие с публикации не выполняется вызовом: предложение называет
    площадки и говорит, что карточка исчезнет из каталога."""
    ctx = called(client, sms, tool_ctx)
    course = make_course(title="Оценивание", platforms={"p1": 45000, "p2": 60000})

    result = registry.call("course_status", {"course_id": course.id, "status": "hidden"}, ctx)
    ctx.db.commit()

    what = result["what_happens"]
    assert result["status"] == "waiting_confirmation"
    assert "идёт набор → скрыт" in what
    assert "p1 (45 000 ₸), p2 (60 000 ₸)" in what
    assert "исчезнет из каталога" in what
    with OrmSession(get_engine()) as db:
        assert db.get(Course, course.id).status == "open"

    assert execute(client, result).status_code == 200
    with OrmSession(get_engine()) as db:
        assert db.get(Course, course.id).status == "hidden"


def test_status_preview_counts_students(client, sms, tool_ctx):
    """«По курсу учатся» — числом: правки курса, где учатся, стоят дороже."""
    ctx = called(client, sms, tool_ctx)
    course = make_course(title="Оценивание")
    teacher = make_user("+77010000001")
    make_enrollment(teacher.id, course.id)

    result = registry.call("course_status", {"course_id": course.id, "status": "closed"}, ctx)
    ctx.db.commit()

    assert "По курсу учатся 1." in result["what_happens"]


def test_publish_touches_only_the_named_platform(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = make_course(title="Оценивание", platforms={"p1": 45000})

    result = registry.call(
        "course_publish", {"course_id": course.id, "platform": "p2", "price": 60000}, ctx
    )
    ctx.db.commit()

    what = result["what_happens"]
    assert "на площадке p2" in what
    assert "60 000 ₸" in what
    # До кнопки ничего не произошло
    assert platforms_of(course.id) == {"p1": 45000}

    assert execute(client, result).status_code == 200
    assert platforms_of(course.id) == {"p1": 45000, "p2": 60000}


def test_price_change_keeps_the_other_platform(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = make_course(platforms={"p1": 45000, "p2": 60000})

    result = registry.call(
        "course_publish", {"course_id": course.id, "platform": "p1", "price": 50000}, ctx
    )
    ctx.db.commit()
    assert "45 000 ₸ → 50 000 ₸" in result["what_happens"]

    assert execute(client, result).status_code == 200
    assert platforms_of(course.id) == {"p1": 50000, "p2": 60000}


def test_unpublish_leaves_the_second_platform_and_the_students(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = make_course(title="Оценивание", platforms={"p1": 45000, "p2": 60000})
    teacher = make_user("+77010000002")
    make_enrollment(teacher.id, course.id)

    result = registry.call("course_unpublish", {"course_id": course.id, "platform": "p2"}, ctx)
    ctx.db.commit()

    what = result["what_happens"]
    assert "с площадки p2" in what
    assert "Останется: p1" in what
    assert "учатся 1" in what

    assert execute(client, result).status_code == 200
    assert platforms_of(course.id) == {"p1": 45000}


def test_unpublish_where_the_course_is_not_published_says_so(client, sms, tool_ctx):
    """Предложение честно говорит, что снимать нечего, а кнопка отказывает
    кодом, а не молчанием."""
    ctx = called(client, sms, tool_ctx)
    course = make_course(platforms={"p1": 45000})

    result = registry.call("course_unpublish", {"course_id": course.id, "platform": "p2"}, ctx)
    ctx.db.commit()
    assert "снимать нечего" in result["what_happens"]

    resp = execute(client, result)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "not_published"


# -- версии и копии ------------------------------------------------------


def test_version_and_duplicate_start_as_drafts(client, sms, tool_ctx):
    """Казахская версия и копия заводятся черновиками без площадок: машинный
    перевод не выкладывается, а копия — отдельный курс."""
    ctx = called(client, sms, tool_ctx)
    course = make_course(title="Оценивание", lang="ru", platforms={"p1": 45000})
    make_module(course.id, title="Модуль 1")

    version = registry.call(
        "course_add_version", {"course_id": course.id, "lang": "kz", "copy_program": True}, ctx
    )
    copy = registry.call("course_duplicate", {"course_id": course.id}, ctx)
    ctx.db.commit()

    assert version["lang"] == "kz"
    assert version["status"] == "draft"
    assert version["platforms"] == []
    assert copy["title"] == "Оценивание (копия)"
    assert copy["platforms"] == []
    assert platforms_of(version["course_id"]) == {}


# -- модули и порядок ----------------------------------------------------


def test_module_add_and_rename(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = make_course()

    added = registry.call("module_add", {"course_id": course.id, "title": "Оценивание"}, ctx)
    renamed = registry.call(
        "module_rename", {"module_id": added["module_id"], "title": "Критерии"}, ctx
    )
    ctx.db.commit()

    assert renamed["title"] == "Критерии"
    with OrmSession(get_engine()) as db:
        assert db.get(Module, added["module_id"]).title == "Критерии"


def test_module_delete_says_what_dies_with_numbers(client, sms, tool_ctx):
    """Не «удалить модуль», а «удалится модуль „Оценивание“ и 4 урока в нём»."""
    ctx = called(client, sms, tool_ctx)
    course = make_course(title="Оценивание", platforms={"p1": 45000, "p2": 60000})
    module = make_module(course.id, title="Критериальное оценивание")
    make_lesson(module.id, title="Урок 1")
    make_lesson(module.id, title="Урок 2")
    make_quiz(module.id, title="Тест модуля")

    result = registry.call("module_delete", {"module_id": module.id}, ctx)
    ctx.db.commit()

    what = result["what_happens"]
    assert "Критериальное оценивание" in what
    assert "2 уроков, 1 тестов" in what
    assert "p1 (45 000 ₸), p2 (60 000 ₸)" in what
    # Модуль на месте: реестр его не трогал
    with OrmSession(get_engine()) as db:
        assert db.get(Module, module.id) is not None

    assert execute(client, result).status_code == 200
    with OrmSession(get_engine()) as db:
        assert db.get(Module, module.id) is None


def test_module_with_someones_progress_is_not_deleted(client, sms, tool_ctx):
    """Курс, где учатся, правится осторожно: отказ виден и в предложении,
    и по кнопке — кодом, а не пятисоткой."""
    ctx = called(client, sms, tool_ctx)
    course = make_course(title="Оценивание")
    module = make_module(course.id, title="Модуль 1")
    lesson = make_lesson(module.id, title="Урок про критерии")
    teacher = make_user("+77010000003")
    make_progress(teacher.id, lesson.id)

    result = registry.call("module_delete", {"module_id": module.id}, ctx)
    ctx.db.commit()
    assert "Не удалится" in result["what_happens"]
    assert "Урок про критерии" in result["what_happens"]

    resp = execute(client, result)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "module_in_use"


def test_program_move_carries_a_lesson_to_another_module(client, sms, tool_ctx):
    """Дерево сценарий принимает целиком — ассистент его не присылает,
    сервер читает и правит сам."""
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    first = make_module(course.id, title="Первый", order_index=0)
    second = make_module(course.id, title="Второй", order_index=1)
    lesson = make_lesson(first.id, title="Урок про критерии", order_index=0)
    make_lesson(first.id, title="Второй урок", order_index=1)

    result = registry.call(
        "program_move",
        {
            "course_id": course.id,
            "kind": "lesson",
            "item_id": lesson.id,
            "to_module_id": second.id,
            "position": 1,
        },
        ctx,
    )
    ctx.db.commit()

    modules = {module["title"]: module for module in result["program"]}
    assert [item["title"] for item in modules["Первый"]["items"]] == ["Второй урок"]
    assert [item["title"] for item in modules["Второй"]["items"]] == ["Урок про критерии"]


def test_program_move_reorders_modules(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    first = make_module(course.id, title="Первый", order_index=0)
    make_module(course.id, title="Второй", order_index=1)

    result = registry.call(
        "program_move", {"course_id": course.id, "kind": "module", "item_id": first.id}, ctx
    )
    ctx.db.commit()

    assert [module["title"] for module in result["program"]] == ["Второй", "Первый"]


def test_program_move_of_a_stranger_is_refused(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    make_module(course.id, title="Первый")

    with pytest.raises(ToolError) as exc:
        registry.call(
            "program_move",
            {"course_id": course.id, "kind": "lesson", "item_id": 4242},
            ctx,
        )
    assert exc.value.code == "not_found"
