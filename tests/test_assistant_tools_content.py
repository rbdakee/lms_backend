"""Отзывы, вопросы и категории глазами ассистента.

Проверяется не то, что инструмент отвечает, а то, чего он **не** отвечает:
ни фамилии автора отзыва, ни телефона, ни имени того, кто задал вопрос.
Второе — что у ответа живому человеку и у всего удаляющего есть описание
последствий с числами и площадкой, а персданные не доезжают и из результата.

Персональные данные в сцене вымышленные, как и везде в тестах.
"""

import json

import pytest
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import Category, Review, ThreadMessage
from app.assistant import registry
from app.assistant.registry import ToolError
from tests.conftest import (
    execute,
    login_admin,
    make_course,
    make_lesson,
    make_module,
    make_review,
    make_thread_message,
    make_user,
    plan,
    refusal,
)

# Номера вымышленные: настоящих в тестах не бывает (общий CLAUDE.md).
TEACHER_PHONE = "+77010000001"
OTHER_PHONE = "+77010000002"


def dumped(result: dict) -> str:
    """Ответ инструмента ровно в том виде, в каком он уходит вызывающему —
    сериализованным в JSON.
    Искать персданные надо именно здесь, а не в отдельных полях."""
    return json.dumps(result, ensure_ascii=False, default=str)


def scene(client, sms) -> int:
    """Вошедший админ: его id нужен контексту вызова."""
    return login_admin(client, sms).json()["id"]


# -- маскирование ----------------------------------------------------------


def test_reviews_list_has_no_names_and_no_phones(client, sms, tool_ctx):
    """Списки маскированы всегда, без исключений: сценарий отдаёт ФИО автора
    тремя полями, до ответа инструмента оно не доходит."""
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course(title="Критериальное оценивание")
    make_review(teacher.id, course.id)

    result = registry.call("reviews_list", {}, tool_ctx(admin_id))

    text = dumped(result)
    assert "Смагулова" not in text
    assert "Гульмира" not in text
    assert TEACHER_PHONE not in text and "7701" not in text
    assert f"Учитель #{teacher.id}" in text
    assert result["items"][0]["teacher"] == {
        "user_id": teacher.id,
        "ref": f"Учитель #{teacher.id}",
    }


def test_questions_list_has_no_names_and_no_phones(client, sms, tool_ctx):
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course()
    lesson = make_lesson(make_module(course.id).id)
    make_thread_message(lesson.id, course.id, teacher.id)

    result = registry.call("questions_list", {}, tool_ctx(admin_id))

    text = dumped(result)
    assert "Смагулова" not in text
    assert TEACHER_PHONE not in text and "7701" not in text
    assert f"Учитель #{teacher.id}" in text


def test_question_thread_names_answering_teachers_by_number(client, sms, tool_ctx):
    """В ветке видно, кто отвечал, — но номером, а не именем: ответ коллеги
    и ответ академии это разные вещи, а фамилия для этого не нужна."""
    admin_id = scene(client, sms)
    asked = make_user(TEACHER_PHONE)
    answered = make_user(OTHER_PHONE, last_name="Ахметова", first_name="Динара")
    course = make_course()
    lesson = make_lesson(make_module(course.id).id)
    question = make_thread_message(lesson.id, course.id, asked.id)
    make_thread_message(lesson.id, course.id, answered.id, parent_id=question.id)

    result = registry.call("question_thread", {"message_id": question.id}, tool_ctx(admin_id))

    text = dumped(result)
    assert "Смагулова" not in text and "Ахметова" not in text
    assert result["replies"][0]["teacher"]["ref"] == f"Учитель #{answered.id}"
    assert result["replies"][0]["from_academy"] is False


def test_question_thread_opens_from_any_message_of_the_branch(client, sms, tool_ctx):
    """С экрана приходят с идентификатором ответа не реже, чем вопроса."""
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course()
    lesson = make_lesson(make_module(course.id).id)
    question = make_thread_message(lesson.id, course.id, teacher.id)
    reply = make_thread_message(lesson.id, course.id, teacher.id, parent_id=question.id)

    result = registry.call("question_thread", {"message_id": reply.id}, tool_ctx(admin_id))

    assert result["id"] == question.id
    assert [row["id"] for row in result["replies"]] == [reply.id]


# -- числа по площадкам ----------------------------------------------------


def test_reviews_without_platform_count_both(client, sms, tool_ctx):
    """«12 отзывов» без площадки — брак: отзывы свои у каждой площадки."""
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    make_review(teacher.id, course.id, platform="p1")
    make_review(teacher.id, course.id, platform="p2", rating=4)
    make_review(teacher.id, course.id, platform="p2", rating=3)

    both = registry.call("reviews_list", {}, tool_ctx(admin_id))
    assert both["total"] == 3
    assert both["by_platform"] == {"p1": 1, "p2": 2}

    only = registry.call("reviews_list", {"platform": "p2"}, tool_ctx(admin_id))
    assert only["total"] == 2
    # Разбивка приходит и с фильтром: по ней видно, что осталось за кадром
    assert only["by_platform"] == {"p1": 1, "p2": 2}
    assert {row["platform"] for row in only["items"]} == {"p2"}


def test_questions_without_platform_count_both(client, sms, tool_ctx):
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    lesson = make_lesson(make_module(course.id).id)
    make_thread_message(lesson.id, course.id, teacher.id, platform="p1")
    make_thread_message(lesson.id, course.id, teacher.id, platform="p2")

    result = registry.call("questions_list", {}, tool_ctx(admin_id))

    assert result["total"] == 2
    assert result["by_platform"] == {"p1": 1, "p2": 1}


def test_unanswered_filter_is_the_queue(client, sms, tool_ctx):
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    other = make_user(OTHER_PHONE)
    course = make_course()
    lesson = make_lesson(make_module(course.id).id)
    answered = make_thread_message(lesson.id, course.id, teacher.id, text="Первый")
    make_thread_message(lesson.id, course.id, other.id, parent_id=answered.id)
    make_thread_message(lesson.id, course.id, teacher.id, text="Второй")

    result = registry.call("questions_list", {"answered": False}, tool_ctx(admin_id))

    assert result["total"] == 1
    assert result["items"][0]["text"] == "Второй"


# -- опасные действия ------------------------------------------------------


def test_answering_a_person_and_deleting_need_confirmation():
    """Ответ живому человеку ассистент вправе написать, но опубликовать его
    от имени академии решает админ. Всё удаляющее — тем более."""
    for name in (
        "review_reply",
        "review_delete",
        "question_answer",
        "question_delete",
        "category_delete",
    ):
        assert registry.get(name).danger == "confirm", name


def test_review_reply_is_described_before_it_is_published(client, sms, tool_ctx):
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course(title="Критериальное оценивание", platforms={"p2": 60000})
    review = make_review(teacher.id, course.id, rating=4, platform="p2")

    called = tool_ctx(admin_id)
    result = plan(
        "review_reply",
        {"review_id": review.id, "text": "Спасибо, разберём этот случай в модуле 3."},
        called,
    )
    called.db.commit()
    what = result["what_happens"]
    assert "Критериальное оценивание" in what
    assert "4 из 5" in what
    # Описание опасного действия называет площадку (ASSISTANT_BRIEF, 4)
    assert "p2" in what
    assert f"Учитель #{teacher.id}" in what
    # Ответа под отзывом нет: реестр его не публиковал
    assert called.db.get(Review, review.id).reply_text is None

    execute(result)
    with OrmSession(get_engine()) as db:
        assert db.get(Review, review.id).reply_text.startswith("Спасибо")


def test_published_reply_comes_back_masked(client, sms, tool_ctx):
    """Сценарий возвращает элемент ленты с ФИО автора — из результата
    подтверждения оно тоже не должно доехать до модели."""
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    review = make_review(teacher.id, make_course().id)

    called = tool_ctx(admin_id)
    proposal = plan(
        "review_reply", {"review_id": review.id, "text": "Ответ академии"}, called
    )
    called.db.commit()

    executed = execute(proposal)

    text = dumped(executed)
    assert "Смагулова" not in text
    assert executed["reply"]["text"] == "Ответ академии"


def test_review_delete_does_not_delete_until_confirmed(client, sms, tool_ctx):
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    review = make_review(teacher.id, make_course(title="Оценивание").id)

    called = tool_ctx(admin_id)
    result = plan("review_delete", {"review_id": review.id}, called)
    called.db.commit()

    assert "Оценивание" in result["what_happens"]
    assert "5 из 5" in result["what_happens"]
    assert called.db.get(Review, review.id).deleted_at is None

    execute(result)
    with OrmSession(get_engine()) as db:
        assert db.get(Review, review.id).deleted_at is not None


def test_deleted_review_is_not_shown_anymore(client, sms, tool_ctx):
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    review = make_review(teacher.id, make_course().id)

    called = tool_ctx(admin_id)
    proposal = plan("review_delete", {"review_id": review.id}, called)
    called.db.commit()
    execute(proposal)

    # Отказ инструмента ловит цикл вызовов и показывает модели как есть
    with pytest.raises(ToolError) as exc:
        registry.call("review_card", {"review_id": review.id}, tool_ctx(admin_id))
    assert exc.value.code == "not_found"


def test_answer_lands_on_the_platform_of_the_question(client, sms, tool_ctx):
    """Ответ ложится на площадку **своего вопроса**, а не запроса: админ
    отвечает из общей очереди, и тред обязан остаться целым там, где его
    задали (`app/application/questions.py`)."""
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course(title="Оценивание", platforms={"p1": 45000, "p2": 60000})
    lesson = make_lesson(make_module(course.id).id, title="Критерии")
    question = make_thread_message(lesson.id, course.id, teacher.id, platform="p2")

    called = tool_ctx(admin_id)
    result = plan(
        "question_answer",
        {"message_id": question.id, "text": "Посмотрите разбор в конце урока."},
        called,
    )
    called.db.commit()

    what = result["what_happens"]
    assert "Критерии" in what and "Оценивание" in what and "p2" in what
    # Ничего не опубликовано: под вопросом по-прежнему пусто
    assert called.db.query(ThreadMessage).filter_by(parent_id=question.id).count() == 0

    executed = execute(result)
    assert executed["platform"] == "p2"
    with OrmSession(get_engine()) as db:
        posted = db.query(ThreadMessage).filter_by(parent_id=question.id).one()
        assert posted.platform == "p2"


def test_answer_result_does_not_carry_the_admin_name(client, sms, tool_ctx):
    """Сценарий подписывает сообщение `author_name` — именем самого админа.
    Наружу оно не идёт: персданные не приходят сами."""
    admin_id = scene(client, sms)
    client.patch("/me", json={"last_name": "Досмуханова", "first_name": "Асель"})
    teacher = make_user(TEACHER_PHONE)
    course = make_course()
    lesson = make_lesson(make_module(course.id).id)
    question = make_thread_message(lesson.id, course.id, teacher.id)

    called = tool_ctx(admin_id)
    proposal = plan(
        "question_answer", {"message_id": question.id, "text": "Ответ академии"}, called
    )
    called.db.commit()

    executed = execute(proposal)
    assert "Досмуханова" not in dumped(executed)


def test_answering_a_reply_still_answers_the_question(client, sms, tool_ctx):
    """Второго уровня вложенности в треде нет: адресуется корень ветки."""
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course()
    lesson = make_lesson(make_module(course.id).id)
    question = make_thread_message(lesson.id, course.id, teacher.id)
    reply = make_thread_message(lesson.id, course.id, teacher.id, parent_id=question.id)

    called = tool_ctx(admin_id)
    proposal = plan("question_answer", {"message_id": reply.id, "text": "Дополню"}, called)
    called.db.commit()
    execute(proposal)

    with OrmSession(get_engine()) as db:
        posted = db.query(ThreadMessage).filter_by(text="Дополню").one()
        assert posted.parent_id == question.id


def test_question_delete_preview_counts_the_replies(client, sms, tool_ctx):
    """«Удалить вопрос» ничего не говорит: с ним из ленты уходит вся ветка."""
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    other = make_user(OTHER_PHONE)
    course = make_course()
    lesson = make_lesson(make_module(course.id).id, title="Критерии")
    question = make_thread_message(lesson.id, course.id, teacher.id)
    make_thread_message(lesson.id, course.id, other.id, parent_id=question.id)
    make_thread_message(lesson.id, course.id, other.id, parent_id=question.id)

    called = tool_ctx(admin_id)
    result = plan("question_delete", {"message_id": question.id}, called)
    called.db.commit()

    assert "2 ответа" in result["what_happens"]
    assert "Критерии" in result["what_happens"]
    assert called.db.get(ThreadMessage, question.id).deleted_at is None


def test_deleting_a_reply_leaves_the_question(client, sms, tool_ctx):
    admin_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course()
    lesson = make_lesson(make_module(course.id).id)
    question = make_thread_message(lesson.id, course.id, teacher.id)
    reply = make_thread_message(lesson.id, course.id, teacher.id, parent_id=question.id)

    called = tool_ctx(admin_id)
    result = plan("question_delete", {"message_id": reply.id}, called)
    called.db.commit()

    assert "Сам вопрос останется" in result["what_happens"]
    execute(result)
    with OrmSession(get_engine()) as db:
        assert db.get(ThreadMessage, reply.id).deleted_at is not None
        assert db.get(ThreadMessage, question.id).deleted_at is None


# -- категории -------------------------------------------------------------


def test_categories_list_counts_courses(client, sms, tool_ctx):
    """По числу курсов видно, можно ли категорию удалить."""
    admin_id = scene(client, sms)
    make_course(category_id=3)

    result = registry.call("categories_list", {}, tool_ctx(admin_id))

    counts = {row["id"]: row["courses_count"] for row in result["items"]}
    assert result["total"] == 6
    assert counts[3] == 1
    assert counts[1] == 0


def test_category_create_and_rename_happen_at_once(client, sms, tool_ctx):
    """Правка справочника — `write`: подтверждения у неё нет, зато результат
    виден сразу."""
    admin_id = scene(client, sms)
    called = tool_ctx(admin_id)

    created = registry.call("category_create", {"title": "Функциональная грамотность"}, called)
    renamed = registry.call(
        "category_rename", {"category_id": created["id"], "title": "Грамотность"}, called
    )
    called.db.commit()

    assert renamed["title"] == "Грамотность"
    with OrmSession(get_engine()) as db:
        assert db.get(Category, created["id"]).title == "Грамотность"


def test_category_delete_says_how_many_courses_block_it(client, sms, tool_ctx):
    admin_id = scene(client, sms)
    make_course(category_id=3)

    called = tool_ctx(admin_id)
    result = plan("category_delete", {"category_id": 3}, called)
    called.db.commit()

    assert "1 курс" in result["what_happens"]
    assert "Не удалится" in result["what_happens"]

    assert refusal(result) == "category_in_use"


def test_empty_category_is_deleted(client, sms, tool_ctx):
    admin_id = scene(client, sms)

    called = tool_ctx(admin_id)
    execute(plan("category_delete", {"category_id": 5}, called))
    with OrmSession(get_engine()) as db:
        assert db.get(Category, 5) is None
