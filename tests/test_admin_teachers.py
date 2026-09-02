from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.adapters.db.models import QuizAttempt, Session
from app.adapters.db.repos import now_utc
from tests.conftest import (
    login,
    login_admin,
    make_certificate,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_progress,
    make_question,
    make_quiz,
    make_submission,
    make_task,
    make_user,
    seed,
    user_id,
)


def teacher(number: int, **kw):
    """Учитель с вымышленным номером: настоящих в тестах не бывает."""
    return make_user(f"+7702000{number:04d}", **kw)


def teacher_course(**course_kw):
    """Курс из двух уроков, задания и теста: четыре видимых элемента
    программы — процент прогресса считается четвертями, а `lessons_total`
    остаётся двойкой. На этом и видно, что процент считают не одни уроки."""
    fields = {"title": "Критериальное оценивание"}
    fields.update(course_kw)
    course = make_course(**fields)
    module = make_module(course.id)
    lesson_one = make_lesson(module.id, title="Урок 1", order_index=1)
    lesson_two = make_lesson(module.id, title="Урок 2", order_index=2)
    task = make_task(module.id, title="Составьте дескрипторы", order_index=3)
    quiz = make_quiz(module.id, title="Тест модуля 1", order_index=4)
    questions = [make_question(quiz.id, text="Вопрос 1"), make_question(quiz.id, text="Вопрос 2")]
    return course, lesson_one, lesson_two, task, quiz, questions


def attempt(uid, quiz, questions, **kw):
    """Попытка сырым объектом: проходить тест по HTTP ради одной строки
    вкладки «Тесты» — лишний шум."""
    fields = {
        "user_id": uid,
        "quiz_id": quiz.id,
        "question_order": [question.id for question in questions],
        "finished_at": now_utc(),
        "score": 1,
        "passed": False,
        "is_counted": True,
        "platform": "p1",
    }
    fields.update(kw)
    return seed(QuizAttempt(**fields))


def live_sessions(uid: int) -> int:
    """Сколько у человека неотозванных сессий: в ответах этого нет, а смена
    номера обязана обнулить их все."""
    with get_engine().begin() as conn:
        return conn.execute(
            text("SELECT count(*) FROM session WHERE user_id = :uid AND revoked_at IS NULL"),
            {"uid": uid},
        ).scalar_one()


# -- права ---------------------------------------------------------------


def test_teachers_require_admin(client, sms):
    person = teacher(1)
    enrollment = make_enrollment(person.id, make_course().id)

    def statuses() -> list[int]:
        return [
            client.get("/admin/teachers").status_code,
            client.get(f"/admin/teachers/{person.id}").status_code,
            client.patch(f"/admin/teachers/{person.id}", json={"is_blocked": True}).status_code,
            client.post(
                f"/admin/teachers/{person.id}/retakes",
                json={"quiz_id": 1, "platform": "p1", "reason": "интернет"},
            ).status_code,
            client.delete(f"/admin/enrollments/{enrollment.id}").status_code,
        ]

    assert statuses() == [401] * 5

    login(client, sms)
    assert statuses() == [403] * 5
    assert client.get("/admin/teachers").json()["error"]["code"] == "forbidden"


def test_404_for_unknown_teacher_and_enrollment(client, sms):
    login_admin(client, sms)
    resp = client.get("/admin/teachers/999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert client.patch("/admin/teachers/999999", json={"is_blocked": True}).status_code == 404
    assert client.delete("/admin/enrollments/999999").status_code == 404


# -- список --------------------------------------------------------------


def test_empty_list_is_not_an_error(client, sms):
    login_admin(client, sms)
    assert client.get("/admin/teachers").json() == {
        "items": [],
        "total": 0,
        "page": 1,
        "per_page": 20,
    }


def test_list_row_counts_courses_completed_and_certificates(client, sms):
    login_admin(client, sms)
    person = teacher(
        1,
        last_name="Нурланова",
        first_name="Айгуль",
        middle_name="Сериковна",
        school="КГУ «Школа-лицей №27»",
        region="Алматы",
        city="Алматы",
        subject="Начальные классы",
    )
    first = make_course(title="Оценивание")
    second = make_course(title="Информационная безопасность")
    third = make_course(title="Цифровые инструменты")
    make_enrollment(person.id, first.id, completed_at=now_utc())
    make_enrollment(person.id, second.id)
    # Отозванный доступ в courses_count не идёт, а сертификат по нему остаётся
    make_enrollment(person.id, third.id, revoked_at=now_utc())
    make_certificate(person.id, first.id, number="KZ-2026-XB7K2M")
    # Отозванный документ у человека не на руках — в счёт не идёт
    make_certificate(person.id, second.id, number="KZ-2026-AAAAAA", revoked_at=now_utc())

    body = client.get("/admin/teachers").json()
    assert body["items"][0].pop("created_at")
    assert body == {
        "items": [
            {
                "id": person.id,
                "last_name": "Нурланова",
                "first_name": "Айгуль",
                "middle_name": "Сериковна",
                "phone": person.phone,
                "school": "КГУ «Школа-лицей №27»",
                "region": "Алматы",
                "city": "Алматы",
                "subject": "Начальные классы",
                "courses_count": 2,
                "completed_count": 1,
                "certificates_count": 1,
                "is_blocked": False,
            }
        ],
        "total": 1,
        "page": 1,
        "per_page": 20,
    }


def test_list_has_no_admins(client, sms):
    """Учителя здесь — те же люди, что в счётчике дашборда: админ в список
    не попадает, иначе два экрана покажут разные числа."""
    login_admin(client, sms)
    teacher(1)

    body = client.get("/admin/teachers").json()
    assert body["total"] == 1
    assert body["items"][0]["id"] != user_id(client)


def test_list_filters_by_region_school_and_course(client, sms):
    login_admin(client, sms)
    course = make_course()
    almaty = teacher(1, region="Алматы", school="Школа-лицей №27")
    astana = teacher(2, region="Астана", school="Гимназия №5")
    make_enrollment(almaty.id, course.id)
    # Отозванный доступ курс человеку не открывает — в фильтр он не попадает
    make_enrollment(astana.id, course.id, revoked_at=now_utc())

    def found(query: str) -> list[int]:
        return [item["id"] for item in client.get(f"/admin/teachers?{query}").json()["items"]]

    assert found("region=Алматы") == [almaty.id]
    assert found("school=Гимназия №5") == [astana.id]
    assert found(f"course_id={course.id}") == [almaty.id]
    assert client.get("/admin/teachers?region=Шымкент").json()["total"] == 0


def test_search_finds_by_name_and_by_phone_written_by_hand(client, sms):
    """Телефон админ переписывает с бумажки как есть: «8 707…» обязано найти
    того же человека, что и +7707…"""
    login_admin(client, sms)
    person = make_user("+77071112233", last_name="Нурланова")
    make_user("+77012223344", last_name="Смагулова")

    assert client.get("/admin/teachers?q=Нурлан").json()["items"][0]["id"] == person.id
    assert client.get("/admin/teachers?q=8 707 111").json()["items"][0]["id"] == person.id
    assert client.get("/admin/teachers?q=+7 707 111 22 33").json()["items"][0]["id"] == person.id
    assert client.get("/admin/teachers?q=8 705").json()["total"] == 0


# -- карточка ------------------------------------------------------------


def test_card_of_just_registered_teacher_is_empty(client, client2, sms):
    """Вошёл по SMS и больше ничего: профиль пустой, вкладки пустые —
    и это не ошибка."""
    login(client, sms)
    uid = user_id(client)
    login_admin(client2, sms)

    card = client2.get(f"/admin/teachers/{uid}").json()
    assert card.pop("created_at")
    assert card == {
        "id": uid,
        "last_name": "",
        "first_name": "",
        "middle_name": "",
        "phone": "+77071234567",
        "email": "",
        "school": "",
        "position": "",
        "region": "",
        "city": "",
        "subject": "",
        "experience": None,
        "lang": "ru",
        "is_admin": False,
        "is_blocked": False,
        "courses": [],
        "quizzes": [],
        "submissions": [],
        "certificates": [],
    }


def test_card_shows_profile_and_four_tabs(client, sms):
    login_admin(client, sms)
    admin = user_id(client)
    course, lesson, _, task, quiz, questions = teacher_course()
    other = make_course(title="Информационная безопасность", hours=36)
    person = teacher(
        1,
        last_name="Нурланова",
        first_name="Айгуль",
        middle_name="Сериковна",
        email="a.nurlanova@example.kz",
        school="КГУ «Школа-лицей №27»",
        position="Учитель начальных классов",
        region="Алматы",
        city="Алматы",
        subject="Начальные классы",
        experience=12,
    )
    enrollment = make_enrollment(
        person.id, course.id, granted_by=admin, paid_note="Каспи, 45 000, 31 января"
    )
    make_progress(person.id, lesson.id)
    row = attempt(person.id, quiz, questions, score=1, passed=False)
    submission = make_submission(person.id, task.id, status="rework", reviewed_at=now_utc())
    certificate = make_certificate(
        person.id, other.id, course_title="Информационная безопасность", hours=36
    )

    card = client.get(f"/admin/teachers/{person.id}").json()
    assert card.pop("created_at")
    assert card["courses"][0].pop("granted_at")
    assert card["quizzes"][0]["attempts"][0].pop("started_at")
    assert card["quizzes"][0]["attempts"][0].pop("finished_at")
    assert card["submissions"][0].pop("created_at")
    assert card["submissions"][0].pop("reviewed_at")
    assert card["certificates"][0].pop("issued_at")
    assert card == {
        "id": person.id,
        "last_name": "Нурланова",
        "first_name": "Айгуль",
        "middle_name": "Сериковна",
        "phone": person.phone,
        "email": "a.nurlanova@example.kz",
        "school": "КГУ «Школа-лицей №27»",
        "position": "Учитель начальных классов",
        "region": "Алматы",
        "city": "Алматы",
        "subject": "Начальные классы",
        "experience": 12,
        "lang": "ru",
        "is_admin": False,
        "is_blocked": False,
        "courses": [
            {
                "enrollment_id": enrollment.id,
                "course": {"id": course.id, "lang": "ru", "title": "Критериальное оценивание"},
                "granted_by_admin": True,
                "paid_note": "Каспи, 45 000, 31 января",
                "revoked_at": None,
                "completed_at": None,
                # Пройден один урок из двух, а элементов программы четыре
                "lessons_done": 1,
                "lessons_total": 2,
                "progress_percent": 25,
            }
        ],
        "quizzes": [
            {
                "quiz_id": quiz.id,
                "title": "Тест модуля 1",
                "course_id": course.id,
                "course_title": "Критериальное оценивание",
                "retakable": False,
                "pass_score": 70,
                "can_allow_retake": True,
                "retake_blocker": None,
                "attempts": [
                    {
                        "id": row.id,
                        # Балл процентами: один вопрос из двух
                        "score": 50,
                        "passed": False,
                        "is_counted": True,
                        "uncounted_reason": None,
                        "uncounted_at": None,
                    }
                ],
            }
        ],
        "submissions": [
            {
                "id": submission.id,
                "task_id": task.id,
                "task_title": "Составьте дескрипторы",
                "course_id": course.id,
                "status": "rework",
            }
        ],
        "certificates": [
            {
                "id": certificate.id,
                "number": "KZ-2026-XB7K2M",
                "course_id": other.id,
                "course_title": "Информационная безопасность",
                "hours": 36,
                "revoked_at": None,
            }
        ],
    }


def test_card_keeps_revoked_access_with_its_progress(client, sms):
    """Закрытый доступ остаётся строкой карточки: прогресс и результаты
    при отзыве не удаляются, и админ обязан их видеть."""
    login_admin(client, sms)
    course, lesson, _, _, _, _ = teacher_course()
    person = teacher(1)
    enrollment = make_enrollment(person.id, course.id)
    make_progress(person.id, lesson.id)

    assert client.delete(f"/admin/enrollments/{enrollment.id}").status_code == 204

    row = client.get(f"/admin/teachers/{person.id}").json()["courses"][0]
    assert row["revoked_at"] is not None
    assert row["lessons_done"] == 1
    assert row["progress_percent"] == 25


def test_card_does_not_scale_with_courses(client, sms):
    """У человека курсов немного, но прогресс по каждому — самое дорогое
    место карточки: поход в базу за программой курса превращает вкладку
    «Курсы» в запрос на строку."""
    from sqlalchemy import event

    def queries_for(courses: int, number: int) -> int:
        person = teacher(number)
        for _ in range(courses):
            course, lesson, _, _, quiz, questions = teacher_course()
            make_enrollment(person.id, course.id)
            make_progress(person.id, lesson.id)
            attempt(person.id, quiz, questions)

        counted = []
        engine = get_engine()

        def count(*args):
            counted.append(1)

        event.listen(engine, "before_cursor_execute", count)
        try:
            resp = client.get(f"/admin/teachers/{person.id}")
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert resp.status_code == 200
        assert len(resp.json()["courses"]) == courses
        return len(counted)

    login_admin(client, sms)
    few = queries_for(2, number=1)
    many = queries_for(12, number=2)
    # Десять лишних курсов не должны стоить ни одного лишнего запроса
    assert many == few, f"{few} запросов на 2 курса, {many} на 12"


# -- блокировка ----------------------------------------------------------


def test_block_takes_effect_on_the_next_request(client, client2, sms):
    """Блокировка не ждёт конца сессии: следующий запрос заблокированного —
    уже отказ."""
    login(client, sms)
    uid = user_id(client)
    login_admin(client2, sms)

    resp = client2.patch(f"/admin/teachers/{uid}", json={"is_blocked": True})
    assert resp.status_code == 200
    assert resp.json()["is_blocked"] is True

    blocked = client.get("/me")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "blocked"

    # Профиль и документы на месте — это запрет входить, а не удаление человека
    assert client2.patch(f"/admin/teachers/{uid}", json={"is_blocked": False}).status_code == 200
    assert client.get("/me").status_code == 200


def test_admin_cannot_block_himself(client, sms):
    login_admin(client, sms)
    resp = client.patch(f"/admin/teachers/{user_id(client)}", json={"is_blocked": True})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "self_block"
    assert client.get("/me").status_code == 200


# -- смена номера --------------------------------------------------------


def test_phone_change_revokes_all_sessions(client, client2, sms):
    """Номер меняют, потому что старая симка потеряна: живая сессия на ней —
    чужая, и остаться не должна ни одна."""
    login(client, sms)
    uid = user_id(client)
    # Второе устройство того же человека
    seed(Session(user_id=uid, token_hash="второе-устройство"))
    login_admin(client2, sms)

    resp = client2.patch(f"/admin/teachers/{uid}", json={"phone": "8 701 555 44 33"})
    assert resp.status_code == 200
    # Номер нормализуется к +7XXXXXXXXXX, как при входе
    assert resp.json()["phone"] == "+77015554433"
    assert live_sessions(uid) == 0
    assert client.get("/me").status_code == 401


def test_phone_taken_by_another_teacher(client, sms):
    login_admin(client, sms)
    person = make_user("+77071112233")
    other = make_user("+77012223344")

    resp = client.patch(f"/admin/teachers/{person.id}", json={"phone": "+7 701 222 33 44"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "phone_taken"
    assert resp.json()["error"]["details"] == {"user_id": other.id}


def test_phone_that_is_not_kazakh_is_422(client, sms):
    login_admin(client, sms)
    person = teacher(1)

    resp = client.patch(f"/admin/teachers/{person.id}", json={"phone": "+1 202 555 0143"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "phone"


# -- пересдача -----------------------------------------------------------


def test_retake_uncounts_the_attempt_and_keeps_it(client, sms):
    """Строка попытки остаётся как была — с ответами, баллом и `passed`.
    Освобождается индекс `uq_quiz_attempt_counted`: следующая попытка
    человека снова становится зачётной."""
    login_admin(client, sms)
    course, _, _, _, quiz, questions = teacher_course()
    person = teacher(1)
    make_enrollment(person.id, course.id)
    row = attempt(person.id, quiz, questions, score=1, passed=False)

    resp = client.post(
        f"/admin/teachers/{person.id}/retakes",
        json={"quiz_id": quiz.id, "platform": "p1", "reason": "Пропал интернет на 12-й минуте."},
    )
    assert resp.status_code == 200
    kept = resp.json()["quizzes"][0]["attempts"][0]
    assert kept["id"] == row.id
    assert kept["score"] == 50
    assert kept["passed"] is False
    assert kept["is_counted"] is False
    assert kept["uncounted_reason"] == "Пропал интернет на 12-й минуте."
    assert kept["uncounted_at"] is not None
    # Кнопка на экране гаснет заранее: второй раз разрешать нечего
    assert resp.json()["quizzes"][0]["can_allow_retake"] is False
    assert resp.json()["quizzes"][0]["retake_blocker"] == "no_attempt"

    # Место зачётной попытки свободно — новая ложится без конфликта индекса
    fresh = attempt(person.id, quiz, questions, score=2, passed=True)
    body = client.get(f"/admin/teachers/{person.id}").json()
    assert [item["is_counted"] for item in body["quizzes"][0]["attempts"]] == [False, True]
    assert body["quizzes"][0]["attempts"][1]["id"] == fresh.id


def test_retake_409_for_a_retakable_quiz(client, sms):
    login_admin(client, sms)
    course = make_course()
    module = make_module(course.id)
    quiz = make_quiz(module.id, retakable=True)
    person = teacher(1)
    attempt(person.id, quiz, [make_question(quiz.id)])

    resp = client.post(
        f"/admin/teachers/{person.id}/retakes",
        json={"quiz_id": quiz.id, "platform": "p1", "reason": "интернет"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "quiz_retakable"


def test_retake_409_when_the_person_never_took_the_quiz(client, sms):
    login_admin(client, sms)
    _, _, _, _, quiz, _ = teacher_course()
    person = teacher(1)

    resp = client.post(
        f"/admin/teachers/{person.id}/retakes",
        json={"quiz_id": quiz.id, "platform": "p1", "reason": "интернет"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "no_attempt"


def test_retake_409_when_it_is_already_allowed(client, sms):
    """Пересдача уже открыта — зачётной попытки нет, снимать нечего."""
    login_admin(client, sms)
    _, _, _, _, quiz, questions = teacher_course()
    person = teacher(1)
    attempt(person.id, quiz, questions)
    body = {"quiz_id": quiz.id, "platform": "p1", "reason": "Пропал интернет"}
    assert client.post(f"/admin/teachers/{person.id}/retakes", json=body).status_code == 200

    resp = client.post(f"/admin/teachers/{person.id}/retakes", json=body)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "no_attempt"


def test_retake_409_while_the_attempt_is_running(client, sms):
    login_admin(client, sms)
    _, _, _, _, quiz, questions = teacher_course()
    person = teacher(1)
    attempt(person.id, quiz, questions, finished_at=None, score=None, passed=None)

    resp = client.post(
        f"/admin/teachers/{person.id}/retakes",
        json={"quiz_id": quiz.id, "platform": "p1", "reason": "интернет"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "attempt_in_progress"


def test_retake_409_after_the_certificate_was_issued(client, sms):
    """Иначе новая попытка на 40% отменит уже выданный документ."""
    login_admin(client, sms)
    course, _, _, _, quiz, questions = teacher_course()
    person = teacher(1)
    attempt(person.id, quiz, questions)
    make_certificate(person.id, course.id)

    resp = client.post(
        f"/admin/teachers/{person.id}/retakes",
        json={"quiz_id": quiz.id, "platform": "p1", "reason": "интернет"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "certificate_issued"
    card = client.get(f"/admin/teachers/{person.id}").json()
    assert card["quizzes"][0]["retake_blocker"] == "certificate_issued"
    assert card["quizzes"][0]["can_allow_retake"] is False


def test_retake_422_without_a_reason(client, sms):
    login_admin(client, sms)
    _, _, _, _, quiz, questions = teacher_course()
    person = teacher(1)
    attempt(person.id, quiz, questions)

    resp = client.post(
        f"/admin/teachers/{person.id}/retakes",
        json={"quiz_id": quiz.id, "platform": "p1", "reason": "   "},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "reason"


def test_retake_422_without_a_platform(client, sms):
    """Площадка обязательна: у админки `Origin` её не несёт, и подставить
    первую значило бы снять зачёт наугад."""
    login_admin(client, sms)
    _, _, _, _, quiz, questions = teacher_course()
    person = teacher(1)
    attempt(person.id, quiz, questions)

    resp = client.post(
        f"/admin/teachers/{person.id}/retakes",
        json={"quiz_id": quiz.id, "reason": "интернет"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "platform"


def test_retake_on_the_other_platform_leaves_the_attempt_counted(client, sms):
    """Зачётная попытка лежит на p1, пересдачу просят на p2 — снимать там
    нечего, и зачёт на p1 остаётся: это единственная попытка человека."""
    login_admin(client, sms)
    _, _, _, _, quiz, questions = teacher_course()
    person = teacher(1)
    row = attempt(person.id, quiz, questions, platform="p1")

    resp = client.post(
        f"/admin/teachers/{person.id}/retakes",
        json={"quiz_id": quiz.id, "platform": "p2", "reason": "интернет"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "no_attempt"

    kept = client.get(f"/admin/teachers/{person.id}").json()["quizzes"][0]["attempts"][0]
    assert kept["id"] == row.id
    assert kept["is_counted"] is True
    assert kept["uncounted_reason"] is None
    assert kept["uncounted_at"] is None


def test_retake_404_for_unknown_quiz(client, sms):
    login_admin(client, sms)
    person = teacher(1)
    resp = client.post(
        f"/admin/teachers/{person.id}/retakes",
        json={"quiz_id": 999999, "platform": "p1", "reason": "интернет"},
    )
    assert resp.status_code == 404


def test_retake_notifies_the_teacher_in_his_own_language(client, client2, sms):
    """Без колокольчика человек не узнает, что тест снова открыт, и будет
    считать курс потерянным. Текст собирается на языке читателя."""
    login(client, sms)
    uid = user_id(client)
    course, _, _, _, quiz, questions = teacher_course()
    make_enrollment(uid, course.id)
    attempt(uid, quiz, questions)
    login_admin(client2, sms)

    assert (
        client2.post(
            f"/admin/teachers/{uid}/retakes",
            json={"quiz_id": quiz.id, "platform": "p1", "reason": "Разрядился телефон"},
        ).status_code
        == 200
    )

    body = client.get("/notifications").json()
    assert body["items"][0]["type"] == "retake_allowed"
    assert body["items"][0]["text"] == "Открыта пересдача теста «Тест модуля 1»"
    assert body["items"][0]["params"] == {
        "course_id": course.id,
        "quiz_id": quiz.id,
        "quiz_title": "Тест модуля 1",
    }

    assert client.patch("/me", json={"lang": "kz"}).status_code == 200
    kz = client.get("/notifications").json()["items"][0]
    assert kz["text"] == "«Тест модуля 1» тестін қайта тапсыруға рұқсат берілді"


# -- закрытие доступа ----------------------------------------------------


def test_revoke_deletes_nothing(client, sms):
    login_admin(client, sms)
    course, lesson, _, task, quiz, questions = teacher_course()
    person = teacher(1)
    enrollment = make_enrollment(person.id, course.id)
    make_progress(person.id, lesson.id)
    attempt(person.id, quiz, questions)
    make_submission(person.id, task.id, status="accepted")
    make_certificate(person.id, course.id)

    assert client.delete(f"/admin/enrollments/{enrollment.id}").status_code == 204

    card = client.get(f"/admin/teachers/{person.id}").json()
    assert len(card["courses"]) == 1
    assert len(card["quizzes"][0]["attempts"]) == 1
    assert len(card["submissions"]) == 1
    assert len(card["certificates"]) == 1
    # В списке такой доступ уже не считается действующим
    assert client.get("/admin/teachers").json()["items"][0]["courses_count"] == 0


def test_second_revoke_keeps_the_first_time(client, sms):
    login_admin(client, sms)
    person = teacher(1)
    enrollment = make_enrollment(person.id, make_course().id)

    assert client.delete(f"/admin/enrollments/{enrollment.id}").status_code == 204
    first = client.get(f"/admin/teachers/{person.id}").json()["courses"][0]["revoked_at"]

    assert client.delete(f"/admin/enrollments/{enrollment.id}").status_code == 204
    assert client.get(f"/admin/teachers/{person.id}").json()["courses"][0]["revoked_at"] == first
