from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.adapters.db.models import QuizAttempt
from tests.conftest import (
    login,
    login_admin,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_progress,
    make_question,
    make_quiz,
    make_submission,
    make_task,
    make_thread_message,
    make_user,
    seed,
    user_id,
)

TEACHER_PHONE = "+7 (707) 123-45-67"


def rows(table: str, where: str) -> int:
    """Заглянуть в базу там, где ответа уже нет: после удаления."""
    with get_engine().begin() as conn:
        return conn.scalar(text(f"SELECT count(*) FROM {table} WHERE {where}"))


def make_two_modules(course):
    """Два модуля: в первом видеоурок и текстовый урок, во втором тест
    и задание. Порядок сквозной — на нём проверяется сортировка."""
    first = make_module(course.id, title="Модуль 1", order_index=1)
    second = make_module(course.id, title="Модуль 2", order_index=2)
    lesson = make_lesson(first.id, title="Урок 1", time_required_min=20, order_index=1)
    text = make_lesson(
        first.id,
        title="Урок 2",
        kind="text",
        video_url=None,
        body={"html": "<p>Текст</p>"},
        time_required_min=25,
        order_index=2,
    )
    quiz = make_quiz(second.id, title="Тест", time_required_min=15, order_index=1)
    task = make_task(
        second.id,
        title="Задание",
        statement={"html": "<p>Условие</p>"},
        time_required_min=40,
        order_index=2,
    )
    return first, second, lesson, text, quiz, task


def tree(client, course_id) -> list[tuple[str, list[tuple[str, int]]]]:
    """Дерево программы плоско: (название модуля, [(kind, id) элементов])."""
    program = client.get(f"/admin/courses/{course_id}").json()["program"]
    return [
        (module["title"], [(item["kind"], item["id"]) for item in module["items"]])
        for module in program
    ]


# -- права и 404 -------------------------------------------------------


def test_401_and_403_on_program(client, sms):
    course = make_course()
    module = make_module(course.id)
    assert client.post(f"/admin/courses/{course.id}/modules",
                       json={"title": "М"}).status_code == 401
    assert client.patch(f"/admin/modules/{module.id}", json={"title": "М"}).status_code == 401
    assert client.delete(f"/admin/modules/{module.id}").status_code == 401
    assert client.put(f"/admin/courses/{course.id}/program_order",
                      json={"modules": []}).status_code == 401

    login(client, sms)
    resp = client.post(f"/admin/courses/{course.id}/modules", json={"title": "М"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert client.delete(f"/admin/modules/{module.id}").status_code == 403
    assert client.put(f"/admin/courses/{course.id}/program_order",
                      json={"modules": []}).status_code == 403


def test_404_on_missing_module_and_course(client, sms):
    login_admin(client, sms)
    assert client.post("/admin/courses/999999/modules", json={"title": "М"}).status_code == 404
    resp = client.patch("/admin/modules/999999", json={"title": "М"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert client.delete("/admin/modules/999999").status_code == 404
    assert client.put("/admin/courses/999999/program_order",
                      json={"modules": []}).status_code == 404


# -- модули ------------------------------------------------------------


def test_module_is_added_last_and_moves_the_course(client, sms):
    course = make_course()
    make_module(course.id, title="Модуль 1", order_index=1)
    login_admin(client, sms)
    before = client.get(f"/admin/courses/{course.id}").json()["updated_at"]

    resp = client.post(f"/admin/courses/{course.id}/modules",
                       json={"title": "Модуль 5. Обратная связь"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"id": body["id"], "title": "Модуль 5. Обратная связь", "items": []}
    assert [title for title, _ in tree(client, course.id)] == [
        "Модуль 1",
        "Модуль 5. Обратная связь",
    ]
    # Правка программы — это правка курса: столбец «Изменён» двигается
    assert client.get(f"/admin/courses/{course.id}").json()["updated_at"] > before


def test_module_is_renamed_with_its_items(client, sms):
    course = make_course()
    first, *_ = make_two_modules(course)
    login_admin(client, sms)

    body = client.patch(f"/admin/modules/{first.id}", json={"title": "Модуль 1. Начало"}).json()
    assert body["title"] == "Модуль 1. Начало"
    assert [item["title"] for item in body["items"]] == ["Урок 1", "Урок 2"]


def test_empty_module_title_is_refused(client, sms):
    course = make_course()
    login_admin(client, sms)
    resp = client.post(f"/admin/courses/{course.id}/modules", json={"title": "  "})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "title"


def test_module_is_deleted_with_its_content(client, sms):
    course = make_course()
    first, second, lesson, text, quiz, task = make_two_modules(course)
    make_question(quiz.id)
    login_admin(client, sms)

    assert client.delete(f"/admin/modules/{second.id}").status_code == 204
    # Ушёл модуль со своими тестом и заданием, соседний остался целым
    assert tree(client, course.id) == [
        ("Модуль 1", [("video", lesson.id), ("text", text.id)])
    ]


def test_module_delete_takes_questions_under_its_lessons(client, sms):
    """Вопросы учителей под уроками уходят вместе с модулем: каскада
    у thread_message нет, а поштучное удаление урока их сносит — пакетное
    обязано делать то же самое, иначе внешний ключ отбивает удаление."""
    course = make_course()
    first, _, lesson, text_lesson, _, _ = make_two_modules(course)
    teacher = make_user("+77010000011")
    root = make_thread_message(lesson.id, course.id, teacher.id)
    make_thread_message(lesson.id, course.id, teacher.id, parent_id=root.id, text="Ответ")
    make_thread_message(text_lesson.id, course.id, teacher.id)
    login_admin(client, sms)

    assert client.delete(f"/admin/modules/{first.id}").status_code == 204
    assert rows("thread_message", f"lesson_id IN ({lesson.id}, {text_lesson.id})") == 0
    assert rows("lesson", f"module_id = {first.id}") == 0


def test_module_with_other_peoples_data_is_not_deleted(client, client2, sms):
    course = make_course()
    first, second, lesson, text, quiz, task = make_two_modules(course)
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    make_progress(teacher, lesson.id)
    seed(QuizAttempt(user_id=teacher, quiz_id=quiz.id))
    make_submission(teacher, task.id)

    login_admin(client2, sms)
    resp = client2.delete(f"/admin/modules/{second.id}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "module_in_use"
    # В details сказано, что именно держит: экран не гадает
    assert resp.json()["error"]["details"] == {
        "items": [
            {"kind": "quiz", "id": quiz.id, "title": "Тест", "reason": "has_attempts"},
            {"kind": "task", "id": task.id, "title": "Задание", "reason": "has_submissions"},
        ]
    }
    resp = client2.delete(f"/admin/modules/{first.id}")
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["items"] == [
        {"kind": "video", "id": lesson.id, "title": "Урок 1", "reason": "has_progress"}
    ]
    # Отбитое удаление не уносит ничего
    assert len(tree(client2, course.id)) == 2


# -- сортировка дерева -------------------------------------------------


def test_program_order_sorts_and_moves_between_modules(client, sms):
    course = make_course()
    first, second, lesson, text, quiz, task = make_two_modules(course)
    login_admin(client, sms)

    resp = client.put(
        f"/admin/courses/{course.id}/program_order",
        json={
            "modules": [
                {"id": second.id, "items": [{"kind": "task", "id": task.id}]},
                {
                    "id": first.id,
                    "items": [
                        {"kind": "quiz", "id": quiz.id},
                        {"kind": "text", "id": text.id},
                        {"kind": "video", "id": lesson.id},
                    ],
                },
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # Экран перерисовывает дерево ответом сервера
    assert [(module["id"], [item["id"] for item in module["items"]])
            for module in body["program"]] == [
        (second.id, [task.id]),
        (first.id, [quiz.id, text.id, lesson.id]),
    ]
    assert body["program_minutes"] == 20 + 25 + 15 + 40
    # Перестановка пережила запрос: следующий GET показывает то же
    assert tree(client, course.id) == [
        ("Модуль 2", [("task", task.id)]),
        ("Модуль 1", [("quiz", quiz.id), ("text", text.id), ("video", lesson.id)]),
    ]


def test_incomplete_tree_loses_nothing(client, sms):
    course = make_course()
    first, second, lesson, text, quiz, task = make_two_modules(course)
    login_admin(client, sms)
    before = tree(client, course.id)

    # Не хватает одного урока — того самого, о котором экран не знал
    resp = client.put(
        f"/admin/courses/{course.id}/program_order",
        json={
            "modules": [
                {"id": first.id, "items": [{"kind": "video", "id": lesson.id}]},
                {
                    "id": second.id,
                    "items": [{"kind": "quiz", "id": quiz.id}, {"kind": "task", "id": task.id}],
                },
            ]
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"] == [
        {"field": "modules", "message": "В дереве не хватает элементов курса: 1"}
    ]
    assert tree(client, course.id) == before

    # Не хватает модуля целиком
    resp = client.put(
        f"/admin/courses/{course.id}/program_order",
        json={
            "modules": [
                {
                    "id": first.id,
                    "items": [
                        {"kind": "video", "id": lesson.id},
                        {"kind": "text", "id": text.id},
                        {"kind": "quiz", "id": quiz.id},
                        {"kind": "task", "id": task.id},
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["message"] == (
        "В дереве не хватает модулей курса: 1"
    )
    assert tree(client, course.id) == before


def test_foreign_element_is_refused(client, sms):
    course = make_course()
    first, second, lesson, text, quiz, task = make_two_modules(course)
    stranger = make_course()
    stranger_lesson = make_lesson(make_module(stranger.id).id)
    login_admin(client, sms)
    before = tree(client, course.id)

    resp = client.put(
        f"/admin/courses/{course.id}/program_order",
        json={
            "modules": [
                {
                    "id": first.id,
                    "items": [
                        {"kind": "video", "id": lesson.id},
                        {"kind": "text", "id": text.id},
                        {"kind": "video", "id": stranger_lesson.id},
                    ],
                },
                {
                    "id": second.id,
                    "items": [{"kind": "quiz", "id": quiz.id}, {"kind": "task", "id": task.id}],
                },
            ]
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["message"] == (
        "В дереве есть чужие элементы: 1"
    )
    assert tree(client, course.id) == before
    # Чужой курс не задет
    assert tree(client, stranger.id)[0][1] == [("video", stranger_lesson.id)]


def test_program_order_of_empty_course(client, sms):
    course = make_course()
    login_admin(client, sms)
    body = client.put(f"/admin/courses/{course.id}/program_order", json={"modules": []}).json()
    assert body == {"program": [], "program_minutes": 0}


def test_hidden_lesson_stays_in_the_tree(client, sms):
    course = make_course()
    module = make_module(course.id)
    visible = make_lesson(module.id, title="Видимый", time_required_min=15, order_index=1)
    hidden = make_lesson(module.id, title="Скрытый", time_required_min=30, order_index=2,
                         is_hidden=True)
    login_admin(client, sms)

    body = client.get(f"/admin/courses/{course.id}").json()
    items = body["program"][0]["items"]
    assert [item["id"] for item in items] == [visible.id, hidden.id]
    assert [item["is_hidden"] for item in items] == [False, True]
    # Скрытый урок не занимает времени ни у кого, но в дереве он есть
    assert body["program_minutes"] == 15
