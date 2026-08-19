from app.adapters.db.models import QuizAttempt, Submission
from app.adapters.db.repos import now_utc
from tests.conftest import (
    login,
    login_admin,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_progress,
    make_quiz,
    make_task,
    seed,
    user_id,
)


def statuses(program) -> list[tuple[str, str]]:
    """Плоский список (title, status) по всей программе — сквозной порядок."""
    return [(item["title"], item["status"]) for module in program for item in module["items"]]


def make_learning_course(client, **kw):
    """Курс из двух модулей: два урока, задание и тест модуля — плюс итоговый
    тест во втором модуле. Доступ у вошедшего учителя уже есть."""
    course = make_course(**kw)
    first = make_module(course.id, title="Модуль 1", order_index=1)
    second = make_module(course.id, title="Модуль 2", order_index=2)
    make_lesson(first.id, title="Урок 1", order_index=1)
    make_lesson(first.id, title="Урок 2", order_index=2)
    make_task(first.id, title="Задание", order_index=3)
    make_quiz(first.id, title="Тест модуля", order_index=4)
    make_quiz(second.id, title="Итоговый тест", order_index=1, is_final=True)
    make_enrollment(user_id(client), course.id)
    return course


def lesson_ids(program) -> list[int]:
    return [
        item["id"]
        for module in program
        for item in module["items"]
        if item["kind"] in ("video", "text")
    ]


def test_program_requires_auth(client):
    course = make_course()
    assert client.get(f"/courses/{course.id}/program").status_code == 401


def test_program_404_before_403(client, sms):
    draft = make_course(status="draft")
    stranger_course = make_course()
    login(client, sms)

    # Невидимый и несуществующий курс — 404 даже без доступа
    assert client.get(f"/courses/{draft.id}/program").status_code == 404
    assert client.get("/courses/999999/program").status_code == 404

    # Существующий чужой курс — 403: названия и id уроков и так публичные
    resp = client.get(f"/courses/{stranger_course.id}/program")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_program_403_when_enrollment_revoked(client, sms):
    course = make_course()
    login(client, sms)
    make_enrollment(user_id(client), course.id, revoked_at=now_utc())
    assert client.get(f"/courses/{course.id}/program").status_code == 403


def test_program_empty_course(client, sms):
    course = make_course()
    login(client, sms)
    make_enrollment(user_id(client), course.id)
    assert client.get(f"/courses/{course.id}/program").json() == {"program": []}


def test_program_free_order_locks_only_final_quiz(client, sms):
    login(client, sms)
    course = make_learning_course(client)

    program = client.get(f"/courses/{course.id}/program").json()["program"]
    assert statuses(program) == [
        ("Урок 1", "available"),
        ("Урок 2", "available"),
        ("Задание", "available"),
        ("Тест модуля", "available"),
        # Итоговый ждёт все уроки курса независимо от strict_order
        ("Итоговый тест", "locked"),
    ]


def test_program_strict_order_locks_after_first_undone(client, sms):
    login(client, sms)
    uid = user_id(client)
    course = make_learning_course(client, strict_order=True)
    program = client.get(f"/courses/{course.id}/program").json()["program"]
    make_progress(uid, lesson_ids(program)[0])

    program = client.get(f"/courses/{course.id}/program").json()["program"]
    assert statuses(program) == [
        ("Урок 1", "done"),
        # Первый непройденный открыт, всё после него закрыто
        ("Урок 2", "available"),
        ("Задание", "locked"),
        ("Тест модуля", "locked"),
        ("Итоговый тест", "locked"),
    ]


def test_program_final_quiz_opens_after_all_lessons(client, sms):
    login(client, sms)
    uid = user_id(client)
    # Правило итогового теста одно при любом strict_order
    for strict_order in (True, False):
        course = make_course(strict_order=strict_order)
        module = make_module(course.id, title="Уроки", order_index=1)
        make_lesson(module.id, title="Урок 1", order_index=1)
        make_lesson(module.id, title="Урок 2", order_index=2)
        final_module = make_module(course.id, title="Итог", order_index=2)
        make_quiz(final_module.id, title="Итоговый тест", order_index=1, is_final=True)
        make_enrollment(uid, course.id)

        program = client.get(f"/courses/{course.id}/program").json()["program"]
        assert statuses(program)[-1] == ("Итоговый тест", "locked"), strict_order

        for lesson_id in lesson_ids(program):
            make_progress(uid, lesson_id)
        program = client.get(f"/courses/{course.id}/program").json()["program"]
        assert statuses(program)[-1] == ("Итоговый тест", "available"), strict_order


def test_program_hides_hidden_lesson(client, sms):
    course = make_course(strict_order=True)
    module = make_module(course.id)
    make_lesson(module.id, title="Видимый", order_index=1)
    hidden = make_lesson(module.id, title="Скрытый", order_index=2, is_hidden=True)
    make_quiz(module.id, title="Итоговый", order_index=3, is_final=True)

    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    program = client.get(f"/courses/{course.id}/program").json()["program"]
    make_progress(uid, lesson_ids(program)[0])

    program = client.get(f"/courses/{course.id}/program").json()["program"]
    # Скрытый урок не показан и не держит ни строгий порядок, ни итоговый тест
    assert statuses(program) == [("Видимый", "done"), ("Итоговый", "available")]
    assert hidden.id not in [item["id"] for item in program[0]["items"]]

    access = client.get(f"/courses/{course.id}").json()["access"]
    assert access["total_count"] == 2
    assert access["done_count"] == 1


def test_program_hides_hidden_quiz_and_task(client, sms):
    course = make_course()
    module = make_module(course.id)
    make_lesson(module.id, title="Урок", order_index=1)
    hidden_quiz = make_quiz(module.id, title="Скрытый тест", order_index=2, is_hidden=True)
    hidden_task = make_task(module.id, title="Скрытое задание", order_index=3, is_hidden=True)

    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    # Скрытое человек успел пройти: правило одно на числитель и знаменатель —
    # из обоих оно выпадает целиком, иначе проценты уедут
    seed(QuizAttempt(user_id=uid, quiz_id=hidden_quiz.id, finished_at=now_utc(), passed=True))
    seed(Submission(user_id=uid, task_id=hidden_task.id, status="accepted"))

    program = client.get(f"/courses/{course.id}/program").json()["program"]
    assert statuses(program) == [("Урок", "available")]

    access = client.get(f"/courses/{course.id}").json()["access"]
    assert access["total_count"] == 1
    assert access["done_count"] == 0
    assert access["progress_percent"] == 0


def test_new_stub_does_not_show_up_in_an_open_course(client, client2, sms):
    """Заготовка заводится скрытой. Курс, где уже учатся, правят на ходу,
    и пустой урок появился бы у учителя в программе в ту же секунду:
    открылся бы и не проигрался. Пустой тест хуже — он вошёл бы в условия
    сертификата и отдавал бы 409 на попытку, то есть документ по курсу
    перестал бы получать кто бы то ни было.
    """
    course = make_course(cert_require_lessons=True)
    module = make_module(course.id)
    lesson = make_lesson(module.id, title="Урок", order_index=1)
    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_progress(uid, lesson.id)

    login_admin(client2, sms)
    stub = client2.post(
        f"/admin/modules/{module.id}/lessons",
        json={"title": "Заготовка урока", "kind": "video", "time_required_min": 15},
    ).json()
    quiz_stub = client2.post(
        f"/admin/modules/{module.id}/quizzes",
        json={"title": "Заготовка теста", "time_required_min": 15,
              "is_final": False, "pass_score": 70},
    ).json()
    task_stub = client2.post(
        f"/admin/modules/{module.id}/tasks",
        json={"title": "Заготовка задания", "time_required_min": 40},
    ).json()
    assert [item["is_hidden"] for item in (stub, quiz_stub, task_stub)] == [True] * 3

    # У учителя программа не изменилась, и по прямой ссылке заготовки нет:
    # скрытый элемент исчезает целиком, а не только из списка
    program = client.get(f"/courses/{course.id}/program").json()["program"]
    assert statuses(program) == [("Урок", "done")]
    assert client.get(f"/lessons/{stub['id']}").status_code == 404
    assert client.get(f"/quizzes/{quiz_stub['id']}").status_code == 404
    assert client.get(f"/tasks/{task_stub['id']}").status_code == 404
    # И условие сертификата пустая заготовка не ломает
    assert client.get(f"/courses/{course.id}/completion").json()["can_issue"] is True


def test_done_needs_counted_passed_attempt_and_accepted_task(client, sms):
    course = make_course()
    module = make_module(course.id)
    passed = make_quiz(module.id, title="Сдан", order_index=1)
    unfinished = make_quiz(module.id, title="Не завершён", order_index=2)
    failed = make_quiz(module.id, title="Провален", order_index=3)
    uncounted = make_quiz(module.id, title="Не зачётный", order_index=4)
    accepted = make_task(module.id, title="Зачтено", order_index=5)
    pending = make_task(module.id, title="На проверке", order_index=6)

    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    # Сценариев сессии 5 ещё нет — попытки и сдачи кладём в базу напрямую
    seed(QuizAttempt(user_id=uid, quiz_id=passed.id, finished_at=now_utc(), passed=True))
    seed(QuizAttempt(user_id=uid, quiz_id=unfinished.id, passed=True))
    seed(QuizAttempt(user_id=uid, quiz_id=failed.id, finished_at=now_utc(), passed=False))
    seed(
        QuizAttempt(
            user_id=uid,
            quiz_id=uncounted.id,
            finished_at=now_utc(),
            passed=True,
            is_counted=False,
        )
    )
    seed(Submission(user_id=uid, task_id=accepted.id, status="accepted"))
    seed(Submission(user_id=uid, task_id=pending.id, status="pending"))

    program = client.get(f"/courses/{course.id}/program").json()["program"]
    assert statuses(program) == [
        ("Сдан", "done"),
        ("Не завершён", "available"),
        ("Провален", "available"),
        ("Не зачётный", "available"),
        ("Зачтено", "done"),
        ("На проверке", "available"),
    ]
    assert client.get("/me/courses").json()["items"][0]["done_count"] == 2


def test_counters_cover_lessons_quizzes_and_tasks(client, sms):
    login(client, sms)
    uid = user_id(client)
    course = make_learning_course(client)

    card = client.get("/me/courses").json()["items"][0]
    # lessons_count считает уроки, total_count — все элементы программы
    assert client.get(f"/courses/{course.id}").json()["lessons_count"] == 2
    assert card["total_count"] == 5
    assert card["done_count"] == 0
    assert card["progress_percent"] == 0
    assert card["next_lesson"]["kind"] == "video"

    program = client.get(f"/courses/{course.id}/program").json()["program"]
    for lesson_id in lesson_ids(program):
        make_progress(uid, lesson_id)

    card = client.get("/me/courses").json()["items"][0]
    assert card["done_count"] == 2
    assert card["progress_percent"] == 40
    # Следующим оказалось задание: экран открывается по kind, а не по id
    assert card["next_lesson"] == {
        "id": program[0]["items"][2]["id"],
        "title": "Задание",
        "kind": "task",
    }

    # На странице курса те же числа, что в «моих курсах»
    access = client.get(f"/courses/{course.id}").json()["access"]
    assert access["next_lesson"] == card["next_lesson"]
    assert access["total_count"] == card["total_count"]
    assert access["done_count"] == card["done_count"]


def test_course_page_shows_strict_order(client):
    strict = make_course(strict_order=True)
    free = make_course()
    assert client.get(f"/courses/{strict.id}").json()["strict_order"] is True
    assert client.get(f"/courses/{free.id}").json()["strict_order"] is False
