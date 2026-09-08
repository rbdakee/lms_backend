"""Реестр: механика, из-за которой модель не может удалить курс молча."""

import pytest
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import AssistantProposal, Course
from app.adapters.db.repos import now_utc
from app.application.assistant_chat import end_of_day
from app.assistant import registry
from app.assistant.registry import Arg, ToolCtx, ToolError
from app.domain.platform import PLATFORMS
from tests.conftest import login_admin, make_course, make_enrollment, make_module, make_user


def test_every_confirm_tool_has_preview():
    """Забытый `@preview` — это карточка подтверждения без последствий:
    админ жмёт «Выполнить», не прочитав, что произойдёт."""
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
    assert registry.check_args(spec("courses_list"), {"status": None, "q": "оцен"}) == {
        "q": "оцен"
    }


def test_bad_type_is_refused():
    with pytest.raises(ToolError) as exc:
        registry.check_args(spec("course_card"), {"course_id": "не число"})
    assert exc.value.code == "bad_argument"


# -- предложения ---------------------------------------------------------


def test_confirm_tool_does_not_execute(client, sms, tool_ctx):
    """Главная проверка этой сессии: опасный инструмент не исполняется вовсе,
    а заводит предложение. Обещание модели механизмом не является."""
    admin = login_admin(client, sms).json()["id"]
    thread = client.post("/admin/assistant/threads", json={}).json()
    course = make_course(title="Критериальное оценивание")
    make_module(course.id)

    called = tool_ctx(admin, thread["id"])
    result = registry.call("course_delete", {"course_id": course.id}, called)
    called.db.commit()

    assert result["status"] == "waiting_confirmation"
    assert result["proposal_id"]
    # Курс на месте: реестр его не трогал
    assert called.db.get(Course, course.id) is not None


def test_proposal_says_what_happens_with_numbers(client, sms, tool_ctx):
    """«Удалить курс» ничего не говорит. Последствия считаются по данным."""
    admin = login_admin(client, sms).json()["id"]
    thread = client.post("/admin/assistant/threads", json={}).json()
    course = make_course(title="Оценивание", platforms={"p1": 45000, "p2": 60000})
    make_module(course.id)
    make_module(course.id)

    called = tool_ctx(admin, thread["id"])
    result = registry.call("course_delete", {"course_id": course.id}, called)
    called.db.commit()

    what = result["what_happens"]
    assert "Оценивание" in what
    assert "2 модул" in what
    # Предложение опасного действия называет площадку (раздел 4)
    assert "p1, p2" in what


def test_execute_takes_arguments_from_the_row(client, sms, tool_ctx):
    """Аргументы берутся из строки, а не из тела запроса: иначе подтверждение
    подтверждает не то, что было показано. У ручки тела нет вовсе."""
    login_admin(client, sms)
    thread = client.post("/admin/assistant/threads", json={}).json()
    doomed = make_course(title="Черновик")
    spared = make_course(title="Живой курс")

    called = tool_ctx(client.get("/me").json()["id"], thread["id"])
    proposal_id = registry.call("course_delete", {"course_id": doomed.id}, called)[
        "proposal_id"
    ]
    called.db.commit()

    resp = client.post(f"/admin/assistant/proposals/{proposal_id}/execute")
    assert resp.status_code == 200, resp.text
    assert resp.json()["proposal"]["status"] == "executed"

    with OrmSession(get_engine()) as db:
        assert db.get(Course, doomed.id) is None
        assert db.get(Course, spared.id) is not None


def test_proposal_is_one_shot(client, sms, tool_ctx):
    login_admin(client, sms)
    thread = client.post("/admin/assistant/threads", json={}).json()
    course = make_course()
    called = tool_ctx(client.get("/me").json()["id"], thread["id"])
    proposal_id = registry.call("course_delete", {"course_id": course.id}, called)[
        "proposal_id"
    ]
    called.db.commit()

    assert client.post(f"/admin/assistant/proposals/{proposal_id}/execute").status_code == 200
    again = client.post(f"/admin/assistant/proposals/{proposal_id}/execute")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_executed"


def test_expired_proposal_is_not_executed(client, sms, tool_ctx):
    """Предложение живёт час: нажатое назавтра, оно подтверждает забытый текст."""
    login_admin(client, sms)
    thread = client.post("/admin/assistant/threads", json={}).json()
    course = make_course()
    called = tool_ctx(client.get("/me").json()["id"], thread["id"])
    proposal_id = registry.call("course_delete", {"course_id": course.id}, called)[
        "proposal_id"
    ]
    row = called.db.get(AssistantProposal, proposal_id)
    row.expires_at = now_utc()
    called.db.commit()

    resp = client.post(f"/admin/assistant/proposals/{proposal_id}/execute")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "expired"


def test_refused_tool_still_reports_honestly(client, sms, tool_ctx):
    """Курс с доступами не удаляется. Отказ должен доехать до человека,
    а не превратиться в «кажется, получилось»."""
    admin = login_admin(client, sms).json()["id"]
    thread = client.post("/admin/assistant/threads", json={}).json()
    course = make_course()
    teacher = make_user("+77010000001")
    make_enrollment(teacher.id, course.id)

    called = tool_ctx(admin, thread["id"])
    result = registry.call("course_delete", {"course_id": course.id}, called)
    # В тексте предложения видно, что удаление не пройдёт
    assert "Не удалится" in result["what_happens"]
    called.db.commit()

    resp = client.post(f"/admin/assistant/proposals/{result['proposal_id']}/execute")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "course_in_use"


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


def test_dangerous_action_needs_a_thread_to_be_confirmed_in():
    """Черновик ответа зовёт реестр без чата — опасному действию там негде
    подтверждаться, и оно не выполняется, а отказывает."""
    called = ToolCtx(db=None, admin=None, now=now_utc(), thread_id=None)
    with pytest.raises(ToolError) as exc:
        registry.call("course_delete", {"course_id": 1}, called)
    assert exc.value.code == "no_thread"


def test_end_of_day_is_counted_by_almaty():
    """Допуск живёт до конца суток — казахстанских, а не UTC."""
    from datetime import UTC, datetime

    # 20:30 UTC — это уже 01:30 следующего дня в Алматы
    late = datetime(2026, 9, 9, 20, 30, tzinfo=UTC)
    assert end_of_day(late) == datetime(2026, 9, 10, 19, 0, tzinfo=UTC)
