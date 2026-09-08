"""Уроки, тесты и задания глазами ассистента.

Проверяется то, что стоит между моделью и сценарием: удаляющее не выполняется,
а предлагается; предложение считает числа и называет площадки; курс, по которому
уже учатся, отбивает правку отказом, который доезжает до модели, а не пятисоткой.
"""

import pytest
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import Lesson, Question, Quiz, QuizAttempt, Task
from app.adapters.db.repos import now_utc
from app.application.ports import AiToolCall
from app.assistant import registry
from app.assistant.loop import AssistantLoop
from app.assistant.registry import ToolCtx, ToolError
from tests.conftest import (
    login_admin,
    make_course,
    make_lesson,
    make_module,
    make_option,
    make_progress,
    make_question,
    make_quiz,
    make_submission,
    make_task,
    make_user,
    seed,
)


def called(client, sms, tool_ctx) -> ToolCtx:
    """Админ, его чат и контекст вызова: опасному действию нужен чат, иначе
    подтверждать его негде."""
    admin = login_admin(client, sms).json()["id"]
    thread = client.post("/admin/assistant/threads", json={}).json()
    return tool_ctx(admin, thread["id"])


def execute(client, result):
    """Нажатие кнопки «Выполнить» — единственный способ исполнить `confirm`."""
    return client.post(f"/admin/assistant/proposals/{result['proposal_id']}/execute")


def as_model_sees(ctx: ToolCtx, name: str, **args) -> dict:
    """Результат вызова глазами модели: отказ сценария — обычный результат,
    а не упавший ход (`AssistantLoop._one`)."""
    call = AiToolCall(id="call_1", name=name, arguments=args)
    return AssistantLoop(ai=None, max_tool_calls=10)._one(call, ctx).result


def attempted(quiz_id: int, question_ids: list[int], user_id: int) -> QuizAttempt:
    """Попытка сырым объектом, как в `test_admin_quizzes`: снимок состава — это
    и есть «вопрос показывали», и проходить тест по HTTP ради него лишний шум."""
    return seed(
        QuizAttempt(
            user_id=user_id,
            quiz_id=quiz_id,
            question_order=question_ids,
            finished_at=now_utc(),
            score=1,
            passed=True,
            platform="p1",
        )
    )


def two_platform_course(**kw):
    """Курс на обеих площадках: предложение обязано называть их обе."""
    return make_course(platforms={"p1": 45000, "p2": 60000}, **kw)


# -- пометки опасности ---------------------------------------------------


def test_deleting_tools_are_confirm():
    """Пометка проверяется тестом, а не чтением кода: `write` вместо `confirm`
    у удаления означает урок, снесённый без кнопки."""
    for name in ("lesson_delete", "quiz_delete", "quiz_question_delete", "task_delete"):
        assert registry.get(name).danger == "confirm", name


def test_editing_tools_are_write_and_cards_are_read():
    for name in (
        "lesson_create",
        "lesson_update",
        "quiz_create",
        "quiz_update",
        "quiz_question_add",
        "quiz_question_update",
        "task_create",
        "task_update",
    ):
        assert registry.get(name).danger == "write", name
    for name in ("lesson_card", "quiz_card", "task_card"):
        assert registry.get(name).danger == "read", name


# -- урок ----------------------------------------------------------------


def test_lesson_card_has_the_text(client, sms, tool_ctx):
    """В программе текстов нет — за ними идут в карточку."""
    ctx = called(client, sms, tool_ctx)
    course = make_course(title="Оценивание")
    module = make_module(course.id, title="Модуль 1")
    lesson = make_lesson(
        module.id,
        title="Критерии и дескрипторы",
        kind="text",
        video_url=None,
        body={"html": "<p>Дескриптор описывает наблюдаемое действие.</p>"},
    )

    card = registry.call("lesson_card", {"lesson_id": lesson.id}, ctx)

    assert card["title"] == "Критерии и дескрипторы"
    assert "наблюдаемое действие" in card["body"]["html"]
    assert card["course"]["title"] == "Оценивание"


def test_lesson_is_created_hidden(client, sms, tool_ctx):
    """Заготовка заводится скрытой: в курсе, где учатся, пустой урок открылся
    бы людям сразу."""
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    module = make_module(course.id)

    result = registry.call(
        "lesson_create",
        {"module_id": module.id, "title": "Урок про критерии", "kind": "text"},
        ctx,
    )
    ctx.db.commit()

    assert result["is_hidden"] is True
    assert result["is_ready"] is False
    with OrmSession(get_engine()) as db:
        assert db.get(Lesson, result["lesson_id"]).module_id == module.id


def test_lesson_update_writes_the_text_and_shows_the_lesson(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    module = make_module(course.id)
    created = registry.call(
        "lesson_create",
        {"module_id": module.id, "title": "Урок про критерии", "kind": "text"},
        ctx,
    )

    result = registry.call(
        "lesson_update",
        {
            "lesson_id": created["lesson_id"],
            "body_html": "<p>Дескриптор описывает наблюдаемое действие.</p>",
            "is_hidden": False,
        },
        ctx,
    )
    ctx.db.commit()

    assert result["is_ready"] is True
    assert result["is_hidden"] is False
    assert result["changed"] == ["body", "is_hidden"]


def test_lesson_without_text_is_refused_as_an_error(client, sms, tool_ctx):
    """Текстовый урок без текста не сохранится: отказ сценария приходит модели
    результатом вызова, а не пятисоткой."""
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    module = make_module(course.id)
    created = registry.call(
        "lesson_create", {"module_id": module.id, "title": "Пустой", "kind": "text"}, ctx
    )

    result = as_model_sees(
        ctx, "lesson_update", lesson_id=created["lesson_id"], title="Всё ещё пустой"
    )

    assert result["error"]["code"] == "validation_error"


def test_lesson_with_progress_is_hidden_not_deleted(client, sms, tool_ctx):
    """`BACKEND_NOTES`, раздел 10: урок с чужим прогрессом не удаляется.
    Предложение говорит это заранее и числом, кнопка отказывает кодом."""
    ctx = called(client, sms, tool_ctx)
    course = two_platform_course(title="Оценивание")
    module = make_module(course.id, title="Модуль 1")
    lesson = make_lesson(module.id, title="Урок про критерии")
    teacher = make_user("+77010000011")
    make_progress(teacher.id, lesson.id)

    result = registry.call("lesson_delete", {"lesson_id": lesson.id}, ctx)
    ctx.db.commit()

    what = result["what_happens"]
    assert result["status"] == "waiting_confirmation"
    assert "Урок про критерии" in what
    assert "p1 (45 000 ₸), p2 (60 000 ₸)" in what
    assert "урок прошли 1" in what
    with OrmSession(get_engine()) as db:
        assert db.get(Lesson, lesson.id) is not None

    resp = execute(client, result)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "has_progress"

    # Ровно то, что предлагает текст отказа: спрятать
    hidden = registry.call("lesson_update", {"lesson_id": lesson.id, "is_hidden": True}, ctx)
    ctx.db.commit()
    assert hidden["is_hidden"] is True


def test_lesson_without_progress_is_deleted_after_the_button(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = two_platform_course()
    module = make_module(course.id)
    lesson = make_lesson(module.id, title="Черновой урок")

    result = registry.call("lesson_delete", {"lesson_id": lesson.id}, ctx)
    ctx.db.commit()
    assert "учатся 0" in result["what_happens"]

    assert execute(client, result).status_code == 200
    with OrmSession(get_engine()) as db:
        assert db.get(Lesson, lesson.id) is None


# -- тест ----------------------------------------------------------------


def test_quiz_card_shows_correct_answers_and_explanations(client, sms, tool_ctx):
    """Это админский инструмент: правильные ответы и разъяснения админ видит
    и так, и модели они нужны, чтобы объяснить вопрос."""
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    module = make_module(course.id)
    quiz = make_quiz(module.id, title="Тест модуля 1")
    question = make_question(
        quiz.id,
        text="Что описывает дескриптор?",
        explanation="Дескриптор — про наблюдаемое действие.",
    )
    make_option(question.id, text="Наблюдаемое действие", is_correct=True, order_index=0)
    make_option(question.id, text="Отношение к предмету", order_index=1)

    card = registry.call("quiz_card", {"quiz_id": quiz.id}, ctx)

    assert card["questions"][0]["explanation"] == "Дескриптор — про наблюдаемое действие."
    correct = [row["text"] for row in card["questions"][0]["options"] if row["is_correct"]]
    assert correct == ["Наблюдаемое действие"]


def test_quiz_is_created_hidden_and_the_second_final_is_refused(client, sms, tool_ctx):
    """Итоговый тест в курсе один: отказ сценария доезжает до модели ошибкой."""
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    module = make_module(course.id)

    first = registry.call(
        "quiz_create",
        {"module_id": module.id, "title": "Итоговый тест", "pass_score": 70, "is_final": True},
        ctx,
    )
    assert first["is_hidden"] is True

    second = as_model_sees(
        ctx, "quiz_create", module_id=module.id, title="Ещё один итоговый",
        pass_score=70, is_final=True,
    )
    assert second["error"]["code"] == "final_quiz_exists"


def test_quiz_update_clears_the_timer_and_checks_the_range(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    module = make_module(course.id)
    quiz = make_quiz(module.id, time_limit_min=15)

    result = registry.call("quiz_update", {"quiz_id": quiz.id, "time_limit_min": 0}, ctx)
    ctx.db.commit()
    assert result["time_limit_min"] is None
    with OrmSession(get_engine()) as db:
        assert db.get(Quiz, quiz.id).time_limit_min is None

    with pytest.raises(ToolError) as exc:
        registry.call("quiz_update", {"quiz_id": quiz.id, "pass_score": 900}, ctx)
    assert exc.value.code == "bad_argument"


def test_quiz_with_attempts_is_not_deleted(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = two_platform_course(title="Оценивание")
    module = make_module(course.id, title="Модуль 1")
    quiz = make_quiz(module.id, title="Тест модуля 1")
    question = make_question(quiz.id)
    teacher = make_user("+77010000012")
    attempted(quiz.id, [question.id], teacher.id)

    result = registry.call("quiz_delete", {"quiz_id": quiz.id}, ctx)
    ctx.db.commit()

    what = result["what_happens"]
    assert "1 вопрос" in what
    assert "p1 (45 000 ₸), p2 (60 000 ₸)" in what
    assert "по тесту 1 попытка" in what

    resp = execute(client, result)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "has_attempts"


# -- вопросы теста -------------------------------------------------------


def test_question_is_added_with_numbers_of_correct_options(client, sms, tool_ctx):
    """Варианты — список строк, правильные — их номера: список объектов внутри
    массива схема аргумента не описывает, а невыразимое модель угадывает."""
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    module = make_module(course.id)
    quiz = make_quiz(module.id)

    result = registry.call(
        "quiz_question_add",
        {
            "quiz_id": quiz.id,
            "type": "single",
            "text": "Что описывает дескриптор?",
            "options": ["Наблюдаемое действие", "Отношение к предмету"],
            "correct": [1],
            "explanation": "Дескриптор — про наблюдаемое действие.",
        },
        ctx,
    )
    ctx.db.commit()

    assert [row["is_correct"] for row in result["options"]] == [True, False]


def test_correct_outside_the_options_is_refused(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    module = make_module(course.id)
    quiz = make_quiz(module.id)

    with pytest.raises(ToolError) as exc:
        registry.call(
            "quiz_question_add",
            {
                "quiz_id": quiz.id,
                "type": "single",
                "text": "Вопрос",
                "options": ["Да", "Нет"],
                "correct": [3],
            },
            ctx,
        )
    assert exc.value.code == "bad_argument"


def test_question_with_attempts_is_not_edited_but_hiding_passes(client, sms, tool_ctx):
    """Вопрос, который люди видели, не редактируется: разбор показал бы им то,
    чего они не отвечали. Спрятать — можно, и это тот же инструмент."""
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    module = make_module(course.id)
    quiz = make_quiz(module.id)
    question = make_question(quiz.id, text="Что описывает дескриптор?")
    make_option(question.id, text="Наблюдаемое действие", is_correct=True, order_index=0)
    make_option(question.id, text="Отношение к предмету", order_index=1)
    teacher = make_user("+77010000013")
    attempted(quiz.id, [question.id], teacher.id)

    refused = as_model_sees(
        ctx, "quiz_question_update", question_id=question.id, text="Другой текст"
    )
    assert refused["error"]["code"] == "has_attempts"

    hidden = registry.call(
        "quiz_question_update", {"question_id": question.id, "is_hidden": True}, ctx
    )
    ctx.db.commit()
    assert hidden["is_hidden"] is True
    assert hidden["has_attempts"] is True


def test_question_delete_is_proposed_and_counts_attempts(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = two_platform_course(title="Оценивание")
    module = make_module(course.id)
    quiz = make_quiz(module.id, title="Тест модуля 1")
    question = make_question(quiz.id, text="Что описывает дескриптор?")
    make_option(question.id, text="Наблюдаемое действие", is_correct=True)
    teacher = make_user("+77010000014")
    attempted(quiz.id, [question.id], teacher.id)

    result = registry.call("quiz_question_delete", {"question_id": question.id}, ctx)
    ctx.db.commit()

    what = result["what_happens"]
    assert "Что описывает дескриптор?" in what
    assert "p1 (45 000 ₸), p2 (60 000 ₸)" in what
    assert "был в 1 попытке" in what
    with OrmSession(get_engine()) as db:
        assert db.get(Question, question.id) is not None

    resp = execute(client, result)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "has_attempts"


def test_question_without_attempts_is_deleted_after_the_button(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    module = make_module(course.id)
    quiz = make_quiz(module.id)
    question = make_question(quiz.id, text="Черновой вопрос")

    result = registry.call("quiz_question_delete", {"question_id": question.id}, ctx)
    ctx.db.commit()

    assert execute(client, result).status_code == 200
    with OrmSession(get_engine()) as db:
        assert db.get(Question, question.id) is None


# -- задание -------------------------------------------------------------


def test_task_card_and_update(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    module = make_module(course.id)
    task = make_task(module.id, title="Разработать дескрипторы")

    result = registry.call(
        "task_update",
        {
            "task_id": task.id,
            "statement_html": "<p>Опишите три дескриптора к своему уроку.</p>",
            "submit_format": "file",
            "allowed_ext": ["PDF", ".docx"],
            "is_hidden": False,
        },
        ctx,
    )
    ctx.db.commit()

    card = registry.call("task_card", {"task_id": task.id}, ctx)
    assert result["is_ready"] is True
    assert "три дескриптора" in card["statement"]["html"]
    # Расширения сервер приводит к тому виду, в каком их сравнивает сдача работы
    assert card["allowed_ext"] == ["pdf", "docx"]


def test_task_is_created_hidden(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = make_course()
    module = make_module(course.id)

    result = registry.call(
        "task_create", {"module_id": module.id, "title": "Разработать дескрипторы"}, ctx
    )
    ctx.db.commit()

    assert result["is_hidden"] is True
    assert result["is_ready"] is False


def test_task_with_submissions_is_not_deleted(client, sms, tool_ctx):
    ctx = called(client, sms, tool_ctx)
    course = two_platform_course(title="Оценивание")
    module = make_module(course.id, title="Модуль 1")
    task = make_task(module.id, title="Разработать дескрипторы")
    teacher = make_user("+77010000015")
    make_submission(teacher.id, task.id)

    result = registry.call("task_delete", {"task_id": task.id}, ctx)
    ctx.db.commit()

    what = result["what_happens"]
    assert "Разработать дескрипторы" in what
    assert "p1 (45 000 ₸), p2 (60 000 ₸)" in what
    assert "сдали 1 работ" in what
    with OrmSession(get_engine()) as db:
        assert db.get(Task, task.id) is not None

    resp = execute(client, result)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "has_submissions"
