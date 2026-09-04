import pytest
from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.api import deps
from tests.conftest import (
    ADMIN_PHONE,
    login_admin,
    login_named,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_option,
    make_question,
    make_quiz,
    make_task,
    make_thread_message,
    make_user,
    user_id,
)

PDF = b"%PDF-1.4 fake content"
OTHER_PHONE = "+7 (701) 555-33-22"

# Таблицы, в которых после прохода по кабинету в режиме предпросмотра
# не должно появиться ни строки (BACKEND_NOTES, раздел 12).
WRITTEN_TABLES = (
    "lesson_progress",
    "quiz_attempt",
    "answer",
    "submission",
    "review",
    "thread_message",
    "certificate",
    "notification",
    "lead",
    "enrollment",
)


def counts():
    with get_engine().begin() as conn:
        return {
            table: conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar()
            for table in WRITTEN_TABLES
        }


def make_full_course(**kw):
    """Курс со всем содержимым: два урока, тест с вопросом, задание и итоговый
    тест. Возвращает словарь — тесту нужны разные его части."""
    course = make_course(**kw)
    module = make_module(course.id)
    lesson = make_lesson(module.id, title="Дескрипторы", order_index=1)
    second = make_lesson(module.id, title="Обратная связь", order_index=2)
    quiz = make_quiz(module.id, title="Тест модуля", order_index=3, time_limit_min=15)
    question = make_question(quiz.id, text="Что фиксирует дескриптор?")
    right = make_option(question.id, text="Наблюдаемое действие", is_correct=True, order_index=1)
    make_option(question.id, text="Отметку в журнале", order_index=2)
    task = make_task(module.id, title="Составьте дескрипторы", order_index=4)
    final = make_quiz(module.id, title="Итоговый тест", is_final=True, order_index=5)
    make_option(make_question(final.id).id, text="Верно", is_correct=True)
    return {
        "course": course,
        "lesson": lesson,
        "second": second,
        "quiz": quiz,
        "question": question,
        "right": right,
        "task": task,
        "final": final,
    }


def enter(client, course_id):
    return client.post("/admin/preview/enter", json={"course_id": course_id})


def session_id(client):
    """id текущей сессии — по нему в памяти лежит попытка предпросмотра."""
    items = client.get("/me/sessions").json()["items"]
    return next(item["id"] for item in items if item["is_current"])


def screen_codes(client, data):
    """Коды ответа всех экранов курса — тех, что открывает учитель.

    Словарём, а не списком проверок: упавший экран видно по имени, а не по
    номеру строки. Начало теста здесь повторяемо — попытка предпросмотра
    живёт в памяти, и второй вызов возвращает ту же.
    """
    course, lesson, quiz = data["course"], data["lesson"], data["quiz"]
    return {
        "course": client.get(f"/courses/{course.id}").status_code,
        "program": client.get(f"/courses/{course.id}/program").status_code,
        "lesson": client.get(f"/lessons/{lesson.id}").status_code,
        "quiz": client.get(f"/quizzes/{quiz.id}").status_code,
        "quiz_attempt": client.post(f"/quizzes/{quiz.id}/quiz_attempts").status_code,
        "task": client.get(f"/tasks/{data['task'].id}").status_code,
        "completion": client.get(f"/courses/{course.id}/completion").status_code,
        "questions": client.get(f"/lessons/{lesson.id}/questions").status_code,
    }


def walk_the_cabinet(client, data):
    """Весь путь учителя: отметить урок, пройти тест до разбора, сдать работу
    с файлом, оставить отзыв, задать вопрос, попросить сертификат."""
    course, lesson, quiz = data["course"], data["lesson"], data["quiz"]
    assert client.post(f"/lessons/{lesson.id}/complete").status_code == 200

    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    assert (
        client.post(
            f"/quiz_attempts/{attempt['id']}/answers",
            json={"question_id": data["question"].id, "option_ids": [data["right"].id]},
        ).status_code
        == 204
    )
    assert client.post(f"/quiz_attempts/{attempt['id']}/finish").status_code == 200
    assert client.get(f"/quiz_attempts/{attempt['id']}/review").status_code == 200

    key = client.post("/files", files={"file": ("Дескрипторы.pdf", PDF)}).json()["key"]
    assert (
        client.post(
            f"/tasks/{data['task'].id}/submissions",
            json={"text": "Мой вариант", "files": [{"key": key, "name": "Дескрипторы.pdf"}]},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/courses/{course.id}/reviews", json={"rating": 5, "text": "Полезный курс"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/lessons/{lesson.id}/questions", json={"text": "Где взять шаблон?"}
        ).status_code
        == 201
    )
    return client.post(f"/courses/{course.id}/certificate")


# -- вход и выход из режима --------------------------------------------


def test_preview_requires_auth(client):
    course = make_course()
    assert enter(client, course.id).status_code == 401
    assert client.post("/admin/preview/exit").status_code == 401


def test_preview_forbidden_for_teacher(client, sms):
    login_named(client, sms)
    course = make_course()
    resp = enter(client, course.id)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert client.post("/admin/preview/exit").status_code == 403


def test_enter_unknown_course_is_404(client, sms):
    login_admin(client, sms)
    resp = enter(client, 999999)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_enter_sees_draft_and_hidden(client, sms):
    """Черновик и скрытая версия админу видны — ради них режим и заведён."""
    login_admin(client, sms)
    assert enter(client, make_course(status="draft").id).status_code == 204
    assert enter(client, make_course(status="hidden").id).status_code == 204


def test_exit_outside_preview_is_204(client, sms):
    login_admin(client, sms)
    # Кнопку «Выйти» нажимают дважды: повторный вызов вне режима — тоже успех
    assert client.post("/admin/preview/exit").status_code == 204
    assert client.post("/admin/preview/exit").status_code == 204


def test_me_shows_preview(client, sms):
    login_admin(client, sms)
    course = make_course()
    assert client.get("/me").json()["preview"] is None

    assert enter(client, course.id).status_code == 204
    assert client.get("/me").json()["preview"] == {"course_id": course.id}
    # Правка профиля в режиме не гасит его: признак приходит и с PATCH
    assert client.patch("/me", json={"city": "Астана"}).json()["preview"] == {
        "course_id": course.id
    }

    assert client.post("/admin/preview/exit").status_code == 204
    assert client.get("/me").json()["preview"] is None


def test_verify_code_returns_preview_null(client, sms):
    # Свежая сессия режима не наследует: поле приходит и здесь (CONTRACT, 6)
    assert login_named(client, sms).json()["preview"] is None


# -- черновик и скрытая версия открываются целиком -----------------------


@pytest.mark.parametrize("course_status", ["draft", "hidden"])
def test_preview_opens_course_in_any_status(client, sms, course_status):
    """Кнопка «Предпросмотр как учитель» стоит в редакторе урока: курс в этот
    момент почти всегда черновик. Режим обязан показать его целиком, а не
    только пустить в себя."""
    login_admin(client, sms)
    data = make_full_course(status=course_status)
    # До входа курса для площадки нет вовсе — 404 на каждом экране
    assert set(screen_codes(client, data).values()) == {404}

    assert enter(client, data["course"].id).status_code == 204
    codes = screen_codes(client, data)
    assert set(codes.values()) == {200}, codes
    # Курс на своей же странице подписан статусом, а не выдаёт себя за открытый
    assert client.get(f"/courses/{data['course'].id}").json()["status"] == course_status


def test_preview_opens_only_the_previewed_draft(client, sms):
    """Видимость снимается ровно с одного курса: черновик соседнего курса
    в режиме остаётся несуществующим."""
    login_admin(client, sms)
    data = make_full_course(status="draft")
    other = make_full_course(status="draft")
    enter(client, data["course"].id)

    assert set(screen_codes(client, data).values()) == {200}
    codes = screen_codes(client, other)
    assert set(codes.values()) == {404}, codes


def test_draft_closes_after_exit(client, sms):
    login_admin(client, sms)
    data = make_full_course(status="draft")
    enter(client, data["course"].id)
    assert set(screen_codes(client, data).values()) == {200}

    assert client.post("/admin/preview/exit").status_code == 204
    codes = screen_codes(client, data)
    assert set(codes.values()) == {404}, codes


def test_draft_stays_closed_for_teacher(client, sms):
    """Учителю черновик недоступен всегда — даже с выданным доступом:
    для площадки этого курса не существует, а режим учителю не положен."""
    login_named(client, sms)
    data = make_full_course(status="draft")
    make_enrollment(user_id(client), data["course"].id)

    codes = screen_codes(client, data)
    assert set(codes.values()) == {404}, codes
    assert enter(client, data["course"].id).status_code == 403


# -- доступ и замки ------------------------------------------------------


def test_preview_opens_content_without_enrollment(client, sms):
    login_admin(client, sms)
    data = make_full_course()
    other = make_full_course()

    assert client.get(f"/lessons/{data['lesson'].id}").status_code == 403

    assert enter(client, data["course"].id).status_code == 204
    assert client.get(f"/lessons/{data['lesson'].id}").status_code == 200
    assert client.get(f"/quizzes/{data['quiz'].id}").status_code == 200
    assert client.get(f"/tasks/{data['task'].id}").status_code == 200
    assert client.get(f"/courses/{data['course'].id}/program").status_code == 200
    # Курс страницы показывает доступ открытым, но участником админ не стал
    page = client.get(f"/courses/{data['course'].id}").json()
    assert page["access"]["state"] == "granted"
    assert page["students_count"] == 0

    # Режим привязан к курсу: на соседний доступа по-прежнему нет
    assert client.get(f"/lessons/{other['lesson'].id}").status_code == 403
    assert client.get(f"/quizzes/{other['quiz'].id}").status_code == 403
    assert client.get(f"/tasks/{other['task'].id}").status_code == 403


def test_preview_access_ends_with_mode(client, sms):
    login_admin(client, sms)
    data = make_full_course()
    enter(client, data["course"].id)
    assert client.post("/admin/preview/exit").status_code == 204
    assert client.get(f"/lessons/{data['lesson'].id}").status_code == 403


def test_strict_order_does_not_lock_preview(client, sms):
    """Админ приходит из редактора урока 12 и с пустым прогрессом: замки
    строгого порядка и итогового теста в режиме не действуют."""
    login_admin(client, sms)
    data = make_full_course(strict_order=True)
    enter(client, data["course"].id)

    items = [
        item
        for module in client.get(f"/courses/{data['course'].id}/program").json()["program"]
        for item in module["items"]
    ]
    assert len(items) == 5
    assert {item["status"] for item in items} == {"available"}


def test_strict_order_still_locks_teacher(client, sms):
    """Тот же курс вне режима запирает всё после первого элемента —
    иначе безвредность записи оказалась бы поломкой рабочего пути."""
    login_named(client, sms)
    data = make_full_course(strict_order=True)
    make_enrollment(user_id(client), data["course"].id)

    items = [
        item
        for module in client.get(f"/courses/{data['course'].id}/program").json()["program"]
        for item in module["items"]
    ]
    assert [item["status"] for item in items] == ["available"] + ["locked"] * 4


# -- главное: в режиме не пишется ничего --------------------------------


def test_preview_writes_nothing(client, sms, storage, telegram):
    login_admin(client, sms)
    data = make_full_course(strict_order=True)
    before = counts()
    enter(client, data["course"].id)

    certificate = walk_the_cabinet(client, data)
    assert certificate.status_code == 200
    # Форма ответа настоящая, а строки за ней нет: id заявки нулевой
    assert certificate.json()["id"] == 0
    assert certificate.json()["status"] == "requested"
    # Заявка на курс — тоже без следа: очередь админа мусор не собирает
    assert client.post(f"/courses/{make_course().id}/lead").status_code == 200

    assert counts() == before
    assert all(value == 0 for value in before.values())
    # Файл не лёг и в хранилище — положенный оттуда уже не убрать
    assert storage.objects == {}
    # Ни работы на проверку, ни заявки в бот
    assert telegram.sent == []
    # Счётчик участников курса не изменился
    assert client.get(f"/courses/{data['course'].id}").json()["students_count"] == 0


def test_same_path_outside_preview_writes(client, sms, storage, telegram):
    login_named(client, sms)
    data = make_full_course()
    make_enrollment(user_id(client), data["course"].id)

    certificate = walk_the_cabinet(client, data)
    assert certificate.status_code == 200
    assert client.post(f"/courses/{make_course().id}/lead").status_code == 200

    after = counts()
    assert after["lesson_progress"] == 1
    assert after["quiz_attempt"] == 1
    assert after["answer"] == 1
    assert after["submission"] == 1
    assert after["review"] == 1
    assert after["thread_message"] == 1
    assert after["lead"] == 1
    # Заявка на сертификат легла в базу — документа по ней ещё нет, поэтому
    # и колокольчика нет: его пишет выдача, а её делает админ
    assert after["certificate"] == 1
    assert certificate.json()["status"] == "requested"
    assert certificate.json()["number"] is None
    assert after["notification"] == 0
    assert list(storage.objects.values()) == [PDF]
    # Три сообщения админам: работа на проверку, заявка на сертификат и заявка
    # на курс
    assert len(telegram.sent) == 3


def test_preview_lesson_complete_keeps_progress_empty(client, sms):
    login_admin(client, sms)
    data = make_full_course()
    enter(client, data["course"].id)

    body = client.post(f"/lessons/{data['lesson'].id}/complete").json()
    # Форма ответа та же, что у настоящей отметки, но прогресса не прибавилось
    assert body["is_completed"] is True
    assert body["done_count"] == 0
    assert client.get(f"/lessons/{data['lesson'].id}").json()["is_completed"] is False


def test_preview_certificate_request_is_not_stored(client, sms, telegram):
    """Кнопка «Запросить сертификат» в режиме отвечает как настоящая, но
    заявки за ответом нет: ни строки в базе, ни сообщения админам — админ
    пришёл посмотреть экран, а не занять место в собственной очереди."""
    login_admin(client, sms)
    data = make_full_course()
    enter(client, data["course"].id)

    body = client.post(f"/courses/{data['course'].id}/certificate").json()

    assert body == {
        "id": 0,
        "status": "requested",
        "number": None,
        "requested_at": body["requested_at"],
        "issued_at": None,
    }
    assert body["requested_at"] is not None
    assert counts()["certificate"] == 0
    assert telegram.sent == []
    # И повторное нажатие остаётся тем же ответом, а не «заявка уже подана»
    assert client.post(f"/courses/{data['course'].id}/certificate").json()["id"] == 0


def test_admin_work_on_other_courses_is_written(client, sms):
    """Режим привязан к курсу, а кука одна на оба домена: ответ админа
    из очереди вопросов по соседнему курсу обязан дойти до базы."""
    login_admin(client, sms)
    data = make_full_course()
    other = make_full_course()
    teacher = make_user(OTHER_PHONE)
    root = make_thread_message(other["lesson"].id, other["course"].id, teacher.id)
    enter(client, data["course"].id)

    resp = client.post(
        f"/lessons/{other['lesson'].id}/questions",
        json={"text": "Шаблон приложен к уроку", "parent_id": root.id},
    )
    assert resp.status_code == 201
    assert counts()["thread_message"] == 2
    # Учителю ушёл колокольчик — как и всегда при ответе на его вопрос
    assert counts()["notification"] == 1


# -- попытка теста в памяти ---------------------------------------------


def test_preview_quiz_attempt_lives_in_memory(client, sms):
    login_admin(client, sms)
    data = make_full_course()
    quiz, question, right = data["quiz"], data["question"], data["right"]
    enter(client, data["course"].id)

    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    assert attempt["quiz_id"] == quiz.id
    assert [q["id"] for q in attempt["questions"]] == [question.id]
    assert attempt["remaining_sec"] <= 15 * 60
    # Двойной клик по «Начать тест» возвращает ту же попытку
    assert client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()["id"] == attempt["id"]

    # Экран теста видит идущую попытку и после перезагрузки
    state = client.get(f"/quizzes/{quiz.id}").json()["state"]
    assert state["status"] == "in_progress"
    assert state["attempt"]["id"] == attempt["id"]

    assert (
        client.post(
            f"/quiz_attempts/{attempt['id']}/answers",
            json={"question_id": question.id, "option_ids": [right.id]},
        ).status_code
        == 204
    )
    assert client.get(f"/quizzes/{quiz.id}").json()["state"]["attempt"]["answers"] == [
        {"question_id": question.id, "option_ids": [right.id]}
    ]

    result = client.post(f"/quiz_attempts/{attempt['id']}/finish").json()
    assert (result["score"], result["max_score"], result["score_percent"]) == (1, 1, 100)
    assert result["passed"] is True

    review = client.get(f"/quiz_attempts/{attempt['id']}/review").json()
    assert review["questions"][0]["earned_points"] == 1
    assert [opt["is_chosen"] for opt in review["questions"][0]["options"]] == [True, False]

    finished = client.get(f"/quizzes/{quiz.id}").json()
    assert finished["state"]["status"] == "finished"
    assert finished["state"]["result"]["score"] == 1
    assert len(finished["attempts"]) == 1

    with get_engine().begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM quiz_attempt")).scalar() == 0
        assert conn.execute(text('SELECT count(*) FROM "answer"')).scalar() == 0


def test_preview_attempt_dropped_on_exit(client, sms):
    login_admin(client, sms)
    data = make_full_course()
    quiz = data["quiz"]
    enter(client, data["course"].id)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()

    assert client.post("/admin/preview/exit").status_code == 204
    assert deps.get_preview_attempts().get(session_id(client)) is None

    enter(client, data["course"].id)
    assert client.get(f"/quizzes/{quiz.id}").json()["state"]["status"] == "not_started"
    # Старая попытка не откликается даже по своему id
    assert (
        client.post(
            f"/quiz_attempts/{attempt['id']}/answers",
            json={"question_id": data["question"].id, "option_ids": []},
        ).status_code
        == 404
    )


def test_preview_does_not_touch_real_attempts(client, client2, sms):
    """Попытка учителя, идущая своим чередом, режимом не задета."""
    teacher_data = make_full_course()
    login_named(client2, sms, OTHER_PHONE)
    make_enrollment(user_id(client2), teacher_data["course"].id)
    real = client2.post(f"/quizzes/{teacher_data['quiz'].id}/quiz_attempts").json()

    login_admin(client, sms, ADMIN_PHONE)
    enter(client, teacher_data["course"].id)
    # Чужая попытка не находится и в режиме: id попыток не публикуются
    assert client.post(f"/quiz_attempts/{real['id']}/finish").status_code == 404
    assert client2.post(f"/quiz_attempts/{real['id']}/finish").status_code == 200


def test_reentering_the_same_course_keeps_the_attempt(client, sms):
    """Фронт зовёт enter при открытии экрана предпросмотра. Повторный вход
    в тот же курс не должен ронять уже начатый тест — попытка живёт в памяти,
    и восстановить её неоткуда."""
    login_admin(client, sms)
    data = make_full_course()
    enter(client, data["course"].id)

    attempt = client.post(f"/quizzes/{data['quiz'].id}/quiz_attempts").json()
    enter(client, data["course"].id)

    resp = client.post(f"/quiz_attempts/{attempt['id']}/finish")
    assert resp.status_code == 200, resp.text

    # А смена курса попытку выбрасывает: она принадлежала прошлому предпросмотру
    other = make_full_course()
    enter(client, other["course"].id)
    assert client.post(f"/quiz_attempts/{attempt['id']}/finish").status_code == 404
