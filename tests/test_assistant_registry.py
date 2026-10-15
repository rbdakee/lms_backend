"""Реестр действий: имена, пометки опасности, проверка аргументов, допуски."""

import pytest
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import Course
from app.adapters.db.repos import now_utc
from app.assistant import registry
from app.assistant.registry import Arg, ToolCtx, ToolError
from app.domain.errors import AppError
from app.domain.platform import PLATFORMS
from tests.conftest import login_admin, make_course, make_enrollment, make_module, make_user


def test_every_confirm_tool_has_preview():
    """Забытый `@preview` — это опасное действие без описания последствий."""
    assert registry.check_registry() == []


def test_danger_marks_are_known():
    for name, spec in registry.all_tools().items():
        assert spec.danger in registry.DANGER_LEVELS, name


def test_descriptions_are_written_for_the_model():
    """«Список курсов» — бесполезное описание: модель по нему не решит,
    когда инструмент применять."""
    for name, spec in registry.all_tools().items():
        assert len(spec.description) > 40, name


def test_confirm_tools_say_they_are_dangerous_to_the_model():
    schemas = {item["function"]["name"]: item["function"] for item in registry.openai_tools()}
    for name, spec in registry.all_tools().items():
        if spec.danger == "confirm":
            assert "ОПАСНОЕ ДЕЙСТВИЕ" in schemas[name]["description"], name


def test_platform_argument_lists_both_platforms():
    """Площадка приходит аргументом, а не из `Origin`: админка одна на обе."""
    schemas = {item["function"]["name"]: item["function"] for item in registry.openai_tools()}
    platform = schemas["courses_list"]["parameters"]["properties"]["platform"]
    assert platform["enum"] == list(PLATFORMS)


def test_schema_has_required_and_forbids_extra():
    schema = next(
        item for item in registry.openai_tools() if item["function"]["name"] == "course_card"
    )["function"]["parameters"]
    assert schema["required"] == ["course_id"]
    assert schema["additionalProperties"] is False


# -- проверка аргументов -------------------------------------------------


def spec(name: str):
    return registry.get(name)


def test_unknown_argument_is_refused():
    with pytest.raises(ToolError) as exc:
        registry.check_args(spec("course_card"), {"course_id": 1, "cource_id": 2})
    assert exc.value.code == "unknown_argument"


def test_missing_required_argument_is_refused():
    with pytest.raises(ToolError) as exc:
        registry.check_args(spec("course_card"), {})
    assert exc.value.code == "missing_argument"


def test_number_in_a_string_is_accepted():
    """Модель регулярно присылает «12» вместо 12 — это то же самое число."""
    assert registry.check_args(spec("course_card"), {"course_id": "12"}) == {"course_id": 12}


def test_value_outside_enum_is_refused():
    with pytest.raises(ToolError) as exc:
        registry.check_args(spec("courses_list"), {"platform": "p3"})
    assert exc.value.code == "bad_argument"


def test_empty_optional_argument_is_dropped():
    """null от модели значит «не задал», а не «задал пустым»."""
    assert registry.check_args(spec("courses_list"), {"status": None, "q": "оцен"}) == {"q": "оцен"}


def test_bad_type_is_refused():
    with pytest.raises(ToolError) as exc:
        registry.check_args(spec("course_card"), {"course_id": "не число"})
    assert exc.value.code == "bad_argument"


# -- исполнение ----------------------------------------------------------


def test_confirm_tool_runs_directly(client, sms, tool_ctx):
    """Пометка `confirm` — данные для вызывающего, а не механизм: реестр
    исполняет действие сразу, без промежуточного предложения."""
    admin = login_admin(client, sms).json()["id"]
    doomed = make_course(title="Черновик")
    spared = make_course(title="Живой курс")

    called = tool_ctx(admin)
    registry.call("course_delete", {"course_id": doomed.id}, called)
    called.db.commit()

    with OrmSession(get_engine()) as db:
        assert db.get(Course, doomed.id) is None
        assert db.get(Course, spared.id) is not None


def test_confirm_preview_says_what_happens_with_numbers(client, sms, tool_ctx):
    """«Удалить курс» ничего не говорит. Последствия считаются по данным."""
    admin = login_admin(client, sms).json()["id"]
    course = make_course(title="Оценивание", platforms={"p1": 45000, "p2": 60000})
    make_module(course.id)
    make_module(course.id)

    called = tool_ctx(admin)
    what = registry.get("course_delete").preview(called, course_id=course.id)

    assert "Оценивание" in what
    assert "2 модул" in what
    # Описание опасного действия называет площадку (раздел 4)
    assert "p1, p2" in what


def test_refused_tool_raises_with_a_code(client, sms, tool_ctx):
    """Курс с доступами не удаляется. Отказ — ошибка сценария со своим
    кодом, а не молчаливое «получилось»."""
    admin = login_admin(client, sms).json()["id"]
    course = make_course()
    teacher = make_user("+77010000001")
    make_enrollment(teacher.id, course.id)

    called = tool_ctx(admin)
    assert "Не удалится" in registry.get("course_delete").preview(called, course_id=course.id)
    with pytest.raises(AppError) as exc:
        registry.call("course_delete", {"course_id": course.id}, called)
    assert exc.value.code == "course_in_use"


# -- допуски -------------------------------------------------------------


def test_require_pd_without_grant_refuses():
    called = ToolCtx(db=None, admin=None, now=now_utc(), grants={})
    with pytest.raises(ToolError) as exc:
        called.require_pd(418)
    assert exc.value.code == "pd_grant_required"
    assert exc.value.details["subject_user_id"] == 418


def test_read_grant_does_not_open_write():
    """Допуск открывает дверь, но правка чужих данных требует своего уровня."""
    called = ToolCtx(db=None, admin=None, now=now_utc(), grants={418: "read"})
    assert called.require_pd(418) == "read"
    with pytest.raises(ToolError) as exc:
        called.require_pd(418, "write")
    assert exc.value.code == "pd_grant_level"


# -- регистрация ---------------------------------------------------------


def test_duplicate_tool_name_is_refused():
    with pytest.raises(RuntimeError, match="уже зарегистрирован"):

        @registry.tool(name="courses_list", danger="read", description="дубль" * 20)
        def _(ctx):  # pragma: no cover — регистрация падает раньше вызова
            return {}


def test_unknown_danger_is_refused():
    with pytest.raises(RuntimeError, match="danger"):

        @registry.tool(name="whatever_tool", danger="delete", description="х" * 50)
        def _(ctx):  # pragma: no cover
            return {}


def test_argument_must_be_declared_through_arg():
    with pytest.raises(RuntimeError, match="Arg"):

        @registry.tool(
            name="another_tool",
            danger="read",
            description="х" * 50,
            args={"status": "draft|open"},
        )
        def _(ctx, status=None):  # pragma: no cover
            return {}


def test_arg_schema_shape():
    assert Arg("string", "Статус", enum=["a", "b"]).schema() == {
        "type": "string",
        "description": "Статус",
        "enum": ["a", "b"],
    }
    assert Arg("array", "Коды", items="integer").schema()["items"] == {"type": "integer"}
