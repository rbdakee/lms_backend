from sqlalchemy import text

from app.adapters.db.base import get_engine
from tests.conftest import (
    login,
    login_admin,
    make_admin,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_quiz,
    make_thread_message,
    user_id,
)


def make_question_scene(uid, **course_kw):
    """Урок в открытом курсе, доступ у учителя уже есть.
    Возвращает курс и урок — вопросы висят на уроке."""
    course = make_course(title="Критериальное оценивание", **course_kw)
    lesson = make_lesson(make_module(course.id).id, title="Критерии и дескрипторы")
    make_enrollment(uid, course.id)
    return course, lesson


def ask(client, lesson_id, text_="Дескрипторы на 34 человека реально успеть?", parent_id=None):
    return client.post(
        f"/lessons/{lesson_id}/questions", json={"text": text_, "parent_id": parent_id}
    )


def named(client, last_name, first_name, middle_name=""):
    client.patch(
        "/me",
        json={"last_name": last_name, "first_name": first_name, "middle_name": middle_name},
    )
    return user_id(client)


def notification_rows():
    with get_engine().begin() as conn:
        return conn.execute(
            text("SELECT user_id, type, params FROM notification ORDER BY id")
        ).all()


# -- доступ ------------------------------------------------------------


def test_questions_require_auth(client):
    course = make_course()
    lesson = make_lesson(make_module(course.id).id)
    assert client.get(f"/lessons/{lesson.id}/questions").status_code == 401
    assert ask(client, lesson.id).status_code == 401
    assert client.get("/admin/questions").status_code == 401


def test_403_without_enrollment(client, sms):
    course = make_course()
    lesson = make_lesson(make_module(course.id).id)
    login(client, sms)

    resp = client.get(f"/lessons/{lesson.id}/questions")
    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == "Доступ к курсу не открыт"
    assert ask(client, lesson.id).status_code == 403


def test_404_for_hidden_lesson_and_invisible_course(client, sms):
    login(client, sms)
    uid = user_id(client)
    course, lesson = make_question_scene(uid)
    hidden = make_lesson(lesson.module_id, is_hidden=True)
    _, draft_lesson = make_question_scene(uid, status="draft")

    assert client.get("/lessons/999999/questions").status_code == 404
    assert client.get(f"/lessons/{hidden.id}/questions").status_code == 404
    assert ask(client, hidden.id).status_code == 404
    # Курс-черновик прячет и вопросы под своими уроками, даже когда доступ выдан
    assert client.get(f"/lessons/{draft_lesson.id}/questions").status_code == 404


# -- вопрос и ответ ----------------------------------------------------


def test_ask_and_read_thread(client, sms):
    login(client, sms)
    uid = named(client, "Смагулова", "Гульмира", "Токтарбековна")
    course, lesson = make_question_scene(uid)

    resp = ask(client, lesson.id)
    assert resp.status_code == 201
    created = resp.json()
    # Как в отзывах: имя собирает сервер
    assert created["author_name"] == "Смагулова Гульмира Токтарбековна"
    assert created["author_is_admin"] is False
    assert created["replies"] == []

    body = client.get(f"/lessons/{lesson.id}/questions").json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == created["id"]
    # Признак «ждёт ответа» — пустой replies, отдельного статуса нет
    assert body["items"][0]["replies"] == []

    # lesson_id и course_id заполнены оба: админский экран фильтрует по курсу
    with get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT lesson_id, course_id, parent_id FROM thread_message")
        ).one()
    assert row == (lesson.id, course.id, None)


def test_reply_notifies_question_author(client, client2, sms):
    login(client, sms)
    uid = named(client, "Смагулова", "Гульмира")
    course, lesson = make_question_scene(uid)
    question = ask(client, lesson.id).json()

    login_admin(client2, sms)
    reply = client2.post(
        f"/lessons/{lesson.id}/questions",
        json={"text": "Успеть — да, если писать на группу.", "parent_id": question["id"]},
    )
    assert reply.status_code == 201
    assert reply.json()["author_is_admin"] is True

    body = client.get(f"/lessons/{lesson.id}/questions").json()
    assert body["total"] == 1
    replies = body["items"][0]["replies"]
    assert [r["id"] for r in replies] == [reply.json()["id"]]

    rows = notification_rows()
    assert len(rows) == 1
    n_user, n_type, params = rows[0]
    assert (n_user, n_type) == (uid, "answer_posted")
    assert params == {
        "lesson_id": lesson.id,
        "lesson_title": lesson.title,
        "course_id": course.id,
        "message_id": reply.json()["id"],
    }


def test_own_reply_does_not_notify_self(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, lesson = make_question_scene(uid)
    question = ask(client, lesson.id).json()

    resp = ask(client, lesson.id, "Сам отвечу: успел", parent_id=question["id"])
    assert resp.status_code == 201
    assert notification_rows() == []


def test_admin_answers_without_enrollment(client, client2, sms):
    login(client, sms)
    _, lesson = make_question_scene(user_id(client))
    question = ask(client, lesson.id).json()

    # У админа enrollment по курсу нет и не будет — отвечает он из своей очереди
    login_admin(client2, sms)
    assert client2.get(f"/lessons/{lesson.id}/questions").status_code == 200
    assert ask(client2, lesson.id, "Отвечаю", parent_id=question["id"]).status_code == 201


# -- два уровня и текст ------------------------------------------------


def test_reply_to_reply_is_422(client, sms):
    login(client, sms)
    _, lesson = make_question_scene(user_id(client))
    question = ask(client, lesson.id).json()
    reply = ask(client, lesson.id, "Ответ", parent_id=question["id"]).json()

    resp = ask(client, lesson.id, "Ответ на ответ", parent_id=reply["id"])
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "parent_id"


def test_parent_from_another_lesson_is_422(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, lesson = make_question_scene(uid)
    _, other_lesson = make_question_scene(uid)
    question = ask(client, other_lesson.id).json()

    # Корень чужого урока корнем этого не становится — проверка на сервере
    assert ask(client, lesson.id, "Не туда", parent_id=question["id"]).status_code == 422
    assert ask(client, lesson.id, "И не сюда", parent_id=999999).status_code == 422


def test_text_is_trimmed_and_bounded(client, sms):
    login(client, sms)
    _, lesson = make_question_scene(user_id(client))

    assert ask(client, lesson.id, "   ").status_code == 422
    assert ask(client, lesson.id, "x" * 2001).status_code == 422

    resp = ask(client, lesson.id, "  Вопрос с краевыми пробелами  ")
    assert resp.json()["text"] == "Вопрос с краевыми пробелами"


def test_rate_limit_per_user(client, sms, thread_limiter_clock):
    login(client, sms)
    _, lesson = make_question_scene(user_id(client))

    for number in range(3):
        assert ask(client, lesson.id, f"Вопрос {number}").status_code == 201
    resp = ask(client, lesson.id, "Четвёртый подряд")
    assert resp.status_code == 429
    assert resp.json()["error"]["details"]["retry_after_sec"] > 0

    # Окно уехало — форма снова открыта
    thread_limiter_clock.shift(61)
    assert ask(client, lesson.id, "Через минуту").status_code == 201


# -- очередь вопросов в админке ----------------------------------------


def test_admin_questions_only_for_admin(client, sms):
    login(client, sms)
    make_question_scene(user_id(client))
    assert client.get("/admin/questions").status_code == 403


def test_admin_questions_answered_filter_and_search(client, client2, sms):
    login(client, sms)
    uid = named(client, "Ахметова", "Динара", "Сериковна")
    course, lesson = make_question_scene(uid)
    answered = ask(client, lesson.id, "Где взять примеры дескрипторов?").json()
    waiting = ask(client, lesson.id, "А если в классе 34 человека?").json()

    login_admin(client2, sms)
    client2.post(
        f"/lessons/{lesson.id}/questions",
        json={"text": "Вот примеры", "parent_id": answered["id"]},
    )

    # Экран открывается очередью без ответа
    body = client2.get("/admin/questions", params={"answered": "false"}).json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == waiting["id"]
    assert item["replies"] == []
    assert item["teacher"] == {
        "id": uid,
        "last_name": "Ахметова",
        "first_name": "Динара",
        "middle_name": "Сериковна",
    }
    assert item["course"] == {"id": course.id, "title": course.title}

    answered_page = client2.get("/admin/questions", params={"answered": "true"}).json()
    assert [q["id"] for q in answered_page["items"]] == [answered["id"]]
    assert len(answered_page["items"][0]["replies"]) == 1

    # Поиск — по тексту вопроса и по ФИО автора
    assert client2.get("/admin/questions", params={"q": "дескрипторов"}).json()["total"] == 1
    assert client2.get("/admin/questions", params={"q": "ахмет"}).json()["total"] == 2
    assert client2.get("/admin/questions", params={"q": "Нурланова"}).json()["total"] == 0
    assert (
        client2.get("/admin/questions", params={"course_id": course.id}).json()["total"] == 2
    )
    other = client2.get("/admin/questions", params={"course_id": course.id + 999}).json()
    assert other == {"items": [], "total": 0, "page": 1, "per_page": 20}


def test_admin_questions_lesson_number_is_course_wide(client, client2, sms):
    login(client, sms)
    uid = user_id(client)
    course = make_course()
    first = make_module(course.id, order_index=1)
    second = make_module(course.id, order_index=2)
    make_lesson(first.id, title="Первый", order_index=1)
    make_quiz(first.id, order_index=2)  # тесты в нумерации не участвуют
    make_lesson(first.id, title="Второй", order_index=3)
    make_lesson(second.id, title="Скрытый", order_index=1, is_hidden=True)
    target = make_lesson(second.id, title="Создание теста за 10 минут", order_index=2)
    make_enrollment(uid, course.id)

    question = ask(client, target.id, "Успею за 10 минут?").json()

    login_admin(client2, sms)
    item = client2.get("/admin/questions").json()["items"][0]
    assert item["id"] == question["id"]
    # Сквозной номер среди видимых уроков: Первый, Второй, затем этот
    assert item["lesson"] == {"id": target.id, "number": 3, "title": target.title}


def test_admin_questions_empty_and_pagination(client, client2, sms):
    login(client, sms)
    uid = user_id(client)
    course, lesson = make_question_scene(uid)
    login_admin(client2, sms)
    assert client2.get("/admin/questions").json() == {
        "items": [], "total": 0, "page": 1, "per_page": 20,
    }

    # Мимо HTTP: лимит частоты к подготовке данных отношения не имеет
    for number in range(3):
        make_thread_message(lesson.id, course.id, uid, text=f"Вопрос {number}")
    page = client2.get("/admin/questions", params={"per_page": 2}).json()
    assert (page["total"], len(page["items"])) == (3, 2)
    # Свежие сверху
    assert page["items"][0]["text"] == "Вопрос 2"


def test_own_reply_does_not_clear_the_admin_queue(client, sms):
    """«Без ответа» в очереди админа — это «никто ещё не ответил», а не
    «в треде появилась вторая строка»: иначе автор снимает свой же вопрос
    с очереди, дописав к нему уточнение."""
    login(client, sms)
    uid = user_id(client)
    course, lesson = make_question_scene(uid)
    question = ask(client, lesson.id).json()
    ask(client, lesson.id, "Уточню: речь про 4 класс.", parent_id=question["id"])

    make_admin()
    resp = client.get("/admin/questions", params={"answered": False})
    assert [item["id"] for item in resp.json()["items"]] == [question["id"]]
