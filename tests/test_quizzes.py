from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.repos import AttemptRepo, now_utc
from tests.conftest import (
    login,
    make_certificate,
    make_certificate_request,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_option,
    make_question,
    make_quiz,
    user_id,
)


def make_learning_quiz(uid, **quiz_kw):
    """Тест на 4 балла: single за 1 балл и multi за 3. Доступ у учителя уже есть.
    Возвращает курс, тест и два вопроса — варианты берутся через option_ids."""
    course = make_course()
    quiz_kw.setdefault("time_limit_min", 15)
    quiz = make_quiz(make_module(course.id).id, **quiz_kw)
    single = make_question(
        quiz.id,
        text="Что фиксирует дескриптор?",
        order_index=1,
        explanation="Дескриптор описывает действие, которое можно увидеть.",
    )
    make_option(single.id, text="Наблюдаемое действие", is_correct=True, order_index=1)
    make_option(single.id, text="Отметку в журнале", order_index=2)
    multi = make_question(
        quiz.id, type="multi", text="Признаки критерия", points=3, order_index=2
    )
    make_option(multi.id, text="Понятен ученику", is_correct=True, order_index=1)
    make_option(multi.id, text="Проверяем", is_correct=True, order_index=2)
    make_option(multi.id, text="Зависит от настроения", order_index=3)
    make_enrollment(uid, course.id)
    return course, quiz, single, multi


def option_ids(question_id, *, correct=None):
    """Варианты вопроса прямо из базы: is_correct наружу не уходит до разбора."""
    with get_engine().begin() as conn:
        rows = conn.execute(
            text("SELECT id, is_correct FROM option WHERE question_id = :q"
                 " ORDER BY order_index, id"),
            {"q": question_id},
        ).all()
    return [oid for oid, is_correct in rows if correct is None or is_correct == correct]


def answer(client, attempt_id, question_id, options):
    return client.post(
        f"/quiz_attempts/{attempt_id}/answers",
        json={"question_id": question_id, "option_ids": options},
    )


def age_attempt(attempt_id, minutes):
    """Сдвигает старт попытки в прошлое: истечение таймера не ждут sleep'ом."""
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE quiz_attempt SET started_at = started_at"
                " - make_interval(mins => :m) WHERE id = :id"
            ),
            {"m": minutes, "id": attempt_id},
        )


def attempt_rows(quiz_id):
    with get_engine().begin() as conn:
        return conn.execute(
            text(
                "SELECT id, is_counted, finished_at FROM quiz_attempt"
                " WHERE quiz_id = :q ORDER BY id"
            ),
            {"q": quiz_id},
        ).all()


# -- доступ ------------------------------------------------------------


def test_quiz_requires_auth(client):
    course = make_course()
    quiz = make_quiz(make_module(course.id).id)
    assert client.get(f"/quizzes/{quiz.id}").status_code == 401
    assert client.post(f"/quizzes/{quiz.id}/quiz_attempts").status_code == 401
    assert client.post("/quiz_attempts/1/answers", json={"question_id": 1,
                                                         "option_ids": []}).status_code == 401
    assert client.post("/quiz_attempts/1/finish").status_code == 401
    assert client.get("/quiz_attempts/1/review").status_code == 401


def test_quiz_404_for_missing_and_invisible_course(client, sms):
    draft = make_course(status="draft")
    draft_quiz = make_quiz(make_module(draft.id).id)

    login(client, sms)
    make_enrollment(user_id(client), draft.id)

    assert client.get("/quizzes/999999").status_code == 404
    assert client.post("/quizzes/999999/quiz_attempts").status_code == 404
    # Курс-черновик прячет и свои тесты, даже когда доступ выдан
    assert client.get(f"/quizzes/{draft_quiz.id}").status_code == 404
    assert client.post(f"/quizzes/{draft_quiz.id}/quiz_attempts").status_code == 404


def test_hidden_quiz_is_404(client, sms):
    """Скрытый тест исчезает у учителя целиком: не открывается и по прямой
    ссылке, даже когда доступ к курсу выдан (CONTRACT, сессия 7а)."""
    course = make_course()
    quiz = make_quiz(make_module(course.id).id, is_hidden=True)

    login(client, sms)
    make_enrollment(user_id(client), course.id)

    assert client.get(f"/quizzes/{quiz.id}").status_code == 404
    assert client.post(f"/quizzes/{quiz.id}/quiz_attempts").status_code == 404


def test_quiz_403_without_enrollment(client, sms):
    stranger = make_course()
    stranger_quiz = make_quiz(make_module(stranger.id).id)
    login(client, sms)

    resp = client.get(f"/quizzes/{stranger_quiz.id}")
    assert resp.status_code == 403
    assert resp.json()["error"] == {
        "code": "forbidden",
        "message": "Тест доступен после выдачи доступа к курсу",
    }
    assert client.post(f"/quizzes/{stranger_quiz.id}/quiz_attempts").status_code == 403


def test_attempt_403_when_access_revoked_mid_attempt(client, sms):
    login(client, sms)
    uid = user_id(client)
    course, quiz, single, _ = make_learning_quiz(uid)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()

    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE enrollment SET revoked_at = now() WHERE user_id = :u"), {"u": uid}
        )

    # Доступ проверяется на каждый вызов, а не только на старте
    assert answer(client, attempt["id"], single.id, []).status_code == 403
    assert client.post(f"/quiz_attempts/{attempt['id']}/finish").status_code == 403
    assert client.get(f"/quiz_attempts/{attempt['id']}/review").status_code == 403
    assert client.get(f"/quizzes/{quiz.id}").status_code == 403


def test_foreign_attempt_is_404(client, client2, sms):
    login(client, sms)
    uid = user_id(client)
    course, quiz, single, _ = make_learning_quiz(uid)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()

    login(client2, sms, phone="+7 (701) 555-33-11")
    make_enrollment(user_id(client2), course.id)

    # Попытки личные, их id не публикуются — отсюда 404, а не 403
    assert answer(client2, attempt["id"], single.id, []).status_code == 404
    assert client2.post(f"/quiz_attempts/{attempt['id']}/finish").status_code == 404
    assert client2.get(f"/quiz_attempts/{attempt['id']}/review").status_code == 404


# -- старт попытки -----------------------------------------------------


def test_start_returns_snapshot_without_correct_flags(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, multi = make_learning_quiz(uid)

    body = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    assert body["quiz_id"] == quiz.id
    assert body["remaining_sec"] == 900
    assert body["answers"] == []
    assert [q["id"] for q in body["questions"]] == [single.id, multi.id]
    assert body["questions"][0] == {
        "id": single.id,
        "type": "single",
        "text": "Что фиксирует дескриптор?",
        "points": 1,
        "options": [
            {"id": option_ids(single.id)[0], "text": "Наблюдаемое действие"},
            {"id": option_ids(single.id)[1], "text": "Отметку в журнале"},
        ],
    }
    # Правильные ответы и пояснение не уходят до разбора
    assert "is_correct" not in body["questions"][0]["options"][0]
    assert "explanation" not in body["questions"][0]


def test_start_without_time_limit(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, _, _ = make_learning_quiz(uid, time_limit_min=None)

    assert client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()["remaining_sec"] is None


def test_start_skips_hidden_question(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, multi = make_learning_quiz(uid)
    hidden = make_question(quiz.id, text="Скрытый", order_index=3, is_hidden=True)

    body = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    assert [q["id"] for q in body["questions"]] == [single.id, multi.id]
    assert hidden.id not in [q["id"] for q in body["questions"]]


def test_start_shuffle_fixes_order(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, multi = make_learning_quiz(uid, shuffle=True)
    ordered = [single.id, multi.id] + [
        make_question(quiz.id, text=f"Вопрос {i}", order_index=i).id for i in range(3, 15)
    ]

    body = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    shuffled = [q["id"] for q in body["questions"]]
    # Состав тот же, порядок другой — на 14 вопросах совпадение исключено
    assert sorted(shuffled) == sorted(ordered)
    assert shuffled != ordered

    # Порядок запомнен в попытке: возврат на экран показывает ровно тот же
    again = client.get(f"/quizzes/{quiz.id}").json()["state"]["attempt"]
    assert [q["id"] for q in again["questions"]] == shuffled
    assert client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()["questions"] == body[
        "questions"
    ]


def test_restart_returns_same_attempt_with_answers(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, _ = make_learning_quiz(uid)
    first = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    chosen = option_ids(single.id, correct=True)
    assert answer(client, first["id"], single.id, chosen).status_code == 204

    second = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    assert second["id"] == first["id"]
    assert second["answers"] == [{"question_id": single.id, "option_ids": chosen}]
    # Двойной клик по «Начать тест» не создаёт вторую строку
    assert len(attempt_rows(quiz.id)) == 1


def test_second_counted_attempt_blocked_by_index(client, sms):
    """Гонку двойного старта ловит частичный индекс, а не проверка в коде:
    сценарий до второй вставки не доходит, поэтому проверяем репозиторий."""
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, _ = make_learning_quiz(uid)

    with OrmSession(get_engine()) as db:
        repo = AttemptRepo(db)
        # Обе строки на одной площадке: индекс проверяется именно так
        first = repo.create(uid, quiz.id, [single.id], "p1", is_counted=True)
        second = repo.create(uid, quiz.id, [single.id], "p1", is_counted=True)
        db.commit()

    assert first is not None
    assert second is None
    assert len(attempt_rows(quiz.id)) == 1


def test_start_409_quiz_empty(client, sms):
    course = make_course()
    quiz = make_quiz(make_module(course.id).id)
    hidden = make_question(quiz.id, is_hidden=True)
    make_option(hidden.id, is_correct=True)

    login(client, sms)
    make_enrollment(user_id(client), course.id)

    resp = client.post(f"/quizzes/{quiz.id}/quiz_attempts")
    assert resp.status_code == 409
    assert resp.json()["error"] == {
        "code": "quiz_empty",
        "message": "Тест ещё наполняется — вопросов пока нет",
    }


def test_second_attempt_blocked_for_non_retakable(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, _, _ = make_learning_quiz(uid)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    client.post(f"/quiz_attempts/{attempt['id']}/finish")

    resp = client.post(f"/quizzes/{quiz.id}/quiz_attempts")
    assert resp.status_code == 409
    assert resp.json()["error"] == {
        "code": "attempt_used",
        "message": "Попытка уже использована — тест нельзя пройти повторно",
    }
    state = client.get(f"/quizzes/{quiz.id}").json()["state"]
    assert state["status"] == "finished"
    assert state["can_retake"] is False


# -- ответы ------------------------------------------------------------


def test_answer_upsert_and_clear(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, _ = make_learning_quiz(uid)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    options = option_ids(single.id)

    answer(client, attempt["id"], single.id, [options[0]])
    answer(client, attempt["id"], single.id, [options[1]])
    saved = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()["answers"]
    # Повторный ответ заменяет прежний, а не добавляется вторым
    assert saved == [{"question_id": single.id, "option_ids": [options[1]]}]

    assert answer(client, attempt["id"], single.id, []).status_code == 204
    saved = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()["answers"]
    assert saved == [{"question_id": single.id, "option_ids": []}]


def test_answer_422_on_bad_input(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, multi = make_learning_quiz(uid)
    other_quiz = make_quiz(make_module(make_course().id).id)
    foreign = make_question(other_quiz.id)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()

    resp = answer(client, attempt["id"], single.id, option_ids(single.id))
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"] == [
        {"field": "option_ids", "message": "Вопрос принимает один вариант ответа"}
    ]

    resp = answer(client, attempt["id"], foreign.id, [])
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "question_id"

    resp = answer(client, attempt["id"], multi.id, option_ids(single.id))
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"] == [
        {"field": "option_ids", "message": "Вариант не из этого вопроса"}
    ]


def test_answer_409_after_finish(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, _ = make_learning_quiz(uid)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    client.post(f"/quiz_attempts/{attempt['id']}/finish")

    resp = answer(client, attempt["id"], single.id, [])
    assert resp.status_code == 409
    assert resp.json()["error"] == {"code": "attempt_finished", "message": "Попытка уже завершена"}


# -- таймер ------------------------------------------------------------


def test_expired_attempt_rejects_answers_and_times_out(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, multi = make_learning_quiz(uid)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    answer(client, attempt["id"], single.id, option_ids(single.id, correct=True))
    age_attempt(attempt["id"], 20)

    resp = answer(client, attempt["id"], multi.id, option_ids(multi.id, correct=True))
    assert resp.status_code == 409
    assert resp.json()["error"] == {
        "code": "time_expired",
        "message": "Время истекло — ответы отправлены на подсчёт",
    }

    # Попытка ещё не завершена: экран показывает нулевой таймер и зовёт finish
    state = client.get(f"/quizzes/{quiz.id}").json()["state"]
    assert state["status"] == "in_progress"
    assert state["attempt"]["remaining_sec"] == 0

    result = client.post(f"/quiz_attempts/{attempt['id']}/finish").json()
    assert result["timed_out"] is True
    # Счёт по сохранённому: ответ на multi после дедлайна не принят
    assert result["score"] == 1
    assert result["minutes_spent"] == 15

    rows = attempt_rows(quiz.id)
    started, finished = _times(attempt["id"])
    # finished_at ставится концом лимита, а не моментом клика
    assert (finished - started).total_seconds() == 15 * 60
    assert len(rows) == 1


def _times(attempt_id):
    with get_engine().begin() as conn:
        return conn.execute(
            text("SELECT started_at, finished_at FROM quiz_attempt WHERE id = :id"),
            {"id": attempt_id},
        ).one()


# -- завершение и счёт -------------------------------------------------


def test_finish_is_idempotent(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, _ = make_learning_quiz(uid)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    answer(client, attempt["id"], single.id, option_ids(single.id, correct=True))

    first = client.post(f"/quiz_attempts/{attempt['id']}/finish").json()
    finished_at = _times(attempt["id"])[1]
    second = client.post(f"/quiz_attempts/{attempt['id']}/finish").json()

    assert first == second
    # Повторный клик не сдвигает время завершения
    assert _times(attempt["id"])[1] == finished_at


def test_finish_scores_multi_all_or_nothing(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, multi = make_learning_quiz(uid)
    correct = option_ids(multi.id, correct=True)

    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    answer(client, attempt["id"], single.id, option_ids(single.id, correct=True))
    # Половина верного набора — не половина балла
    answer(client, attempt["id"], multi.id, correct[:1])
    partial = client.post(f"/quiz_attempts/{attempt['id']}/finish").json()
    assert partial["score"] == 1
    assert partial["max_score"] == 4
    assert partial["score_percent"] == 25
    assert partial["passed"] is False

    _, retakable, single2, multi2 = make_learning_quiz(uid, retakable=True)
    attempt = client.post(f"/quizzes/{retakable.id}/quiz_attempts").json()
    answer(client, attempt["id"], single2.id, option_ids(single2.id, correct=True))
    answer(client, attempt["id"], multi2.id, option_ids(multi2.id, correct=True))
    full = client.post(f"/quiz_attempts/{attempt['id']}/finish").json()
    assert full == {
        "id": attempt["id"],
        "score": 4,
        "max_score": 4,
        "score_percent": 100,
        "pass_score": 70,
        "passed": True,
        "is_counted": True,
        "timed_out": False,
        "minutes_spent": 0,
        "review_available": True,
    }


def make_plain_quiz(uid, count, **quiz_kw):
    """Тест из `count` вопросов по одному баллу — чтобы считать проценты в уме."""
    course = make_course()
    quiz = make_quiz(make_module(course.id).id, **quiz_kw)
    questions = []
    for i in range(1, count + 1):
        question = make_question(quiz.id, text=f"Вопрос {i}", order_index=i)
        make_option(question.id, text="Верно", is_correct=True, order_index=1)
        make_option(question.id, text="Неверно", order_index=2)
        questions.append(question)
    make_enrollment(uid, course.id)
    return quiz, questions


def test_score_percent_rounds_to_whole(client, sms):
    login(client, sms)
    uid = user_id(client)
    quiz, questions = make_plain_quiz(uid, 3, pass_score=67)

    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    for question in questions[:2]:
        answer(client, attempt["id"], question.id, option_ids(question.id, correct=True))
    result = client.post(f"/quiz_attempts/{attempt['id']}/finish").json()

    # 2 из 3 — это 66,67%, округляется до 67 и ровно равно проходному:
    # сравнение >=, а не >, иначе граница отрезает сдавшего
    assert result["score"] == 2
    assert result["max_score"] == 3
    assert result["score_percent"] == 67
    assert result["passed"] is True


def test_score_percent_rounds_down_below_boundary(client, sms):
    login(client, sms)
    uid = user_id(client)
    quiz, questions = make_plain_quiz(uid, 3, pass_score=34)

    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    answer(client, attempt["id"], questions[0].id, option_ids(questions[0].id, correct=True))
    result = client.post(f"/quiz_attempts/{attempt['id']}/finish").json()

    # 1 из 3 — 33,33%, вниз до 33: до проходного одного балла не хватило
    assert result["score_percent"] == 33
    assert result["passed"] is False


# -- пересдача ---------------------------------------------------------


def test_retake_switches_counted_attempt(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, multi = make_learning_quiz(uid, retakable=True)

    first = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    answer(client, first["id"], single.id, option_ids(single.id, correct=True))
    answer(client, first["id"], multi.id, option_ids(multi.id, correct=True))
    assert client.post(f"/quiz_attempts/{first['id']}/finish").json()["passed"] is True

    second = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    assert second["id"] != first["id"]
    worse = client.post(f"/quiz_attempts/{second['id']}/finish").json()
    assert worse["passed"] is False
    assert worse["is_counted"] is True

    # Зачётная — последняя завершённая; в базе она ровно одна
    rows = {row.id: row.is_counted for row in attempt_rows(quiz.id)}
    assert rows == {first["id"]: False, second["id"]: True}


def test_worse_retake_drops_done_in_program(client, sms):
    login(client, sms)
    uid = user_id(client)
    course = make_course()
    module = make_module(course.id)
    make_lesson(module.id, title="Урок", order_index=1)
    quiz = make_quiz(module.id, title="Тест", order_index=2, retakable=True, pass_score=50)
    question = make_question(quiz.id, text="Вопрос")
    make_option(question.id, text="Верно", is_correct=True, order_index=1)
    make_option(question.id, text="Неверно", order_index=2)
    make_enrollment(uid, course.id)

    first = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    answer(client, first["id"], question.id, option_ids(question.id, correct=True))
    client.post(f"/quiz_attempts/{first['id']}/finish")

    def quiz_status():
        program = client.get(f"/courses/{course.id}/program").json()["program"]
        return program[0]["items"][1]["status"]

    assert quiz_status() == "done"

    second = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    client.post(f"/quiz_attempts/{second['id']}/finish")
    # Статус элемента следует за зачётной попыткой: пересдал хуже — done пропал
    assert quiz_status() == "available"


# -- сертификат --------------------------------------------------------


def revoke(certificate_id: int) -> None:
    """Отзыв сырым SQL: ручка отзыва админская, а этим тестам важно только
    то, что отозванная строка замок больше не держит."""
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE certificate SET revoked_at = now() WHERE id = :id"),
            {"id": certificate_id},
        )


def test_certificate_closes_new_attempts(client, sms):
    login(client, sms)
    uid = user_id(client)
    course, quiz, _, _ = make_learning_quiz(uid, retakable=True)
    certificate = make_certificate(uid, course.id)

    resp = client.post(f"/quizzes/{quiz.id}/quiz_attempts")
    assert resp.status_code == 409
    assert resp.json()["error"] == {
        "code": "certificate_issued",
        "message": "Сертификат уже выдан — результаты теста изменить нельзя",
    }
    assert client.get(f"/quizzes/{quiz.id}").json()["state"]["can_start"] is False

    revoke(certificate.id)
    # Отозванный сертификат больше не держит: результат снова можно менять
    assert client.post(f"/quizzes/{quiz.id}/quiz_attempts").status_code == 200


def test_certificate_request_closes_new_attempts_with_its_own_text(client, sms):
    """Заявка запирает тест наравне с выданным документом: админ выпишет
    бумагу по чек-листу, закрытому в момент заявки, и поехавший результат
    попал бы под неё.

    Код отказа при этом прежний — экран уже умеет его читать, — а текст свой:
    искать несуществующий документ в кабинете человека посылать нельзя.
    """
    login(client, sms)
    uid = user_id(client)
    course, quiz, _, _ = make_learning_quiz(uid, retakable=True)
    request = make_certificate_request(uid, course.id)

    resp = client.post(f"/quizzes/{quiz.id}/quiz_attempts")

    assert resp.status_code == 409
    assert resp.json()["error"] == {
        "code": "certificate_issued",
        "message": "Заявка на сертификат отправлена — результаты теста изменить нельзя",
    }
    assert client.get(f"/quizzes/{quiz.id}").json()["state"]["can_start"] is False

    # Отзыв ошибочной заявки — единственный выход из этого замка, и он
    # возвращает попытку так же, как возвращает её отзыв документа
    revoke(request.id)
    assert client.post(f"/quizzes/{quiz.id}/quiz_attempts").status_code == 200


def test_certificate_closes_retake_after_finish(client, sms):
    login(client, sms)
    uid = user_id(client)
    course, quiz, _, _ = make_learning_quiz(uid, retakable=True)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    client.post(f"/quiz_attempts/{attempt['id']}/finish")
    make_certificate(uid, course.id)

    state = client.get(f"/quizzes/{quiz.id}").json()["state"]
    assert state["status"] == "finished"
    assert state["can_retake"] is False


def test_certificate_request_closes_retake_after_finish(client, sms):
    """Пересдачу закрывает и заявка: до выдачи результат менять нельзя
    ровно по той же причине, что и после неё."""
    login(client, sms)
    uid = user_id(client)
    course, quiz, _, _ = make_learning_quiz(uid, retakable=True)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    client.post(f"/quiz_attempts/{attempt['id']}/finish")
    make_certificate_request(uid, course.id)

    state = client.get(f"/quizzes/{quiz.id}").json()["state"]
    assert state["status"] == "finished"
    assert state["can_retake"] is False


# -- GET /quizzes/{id} -------------------------------------------------


def test_quiz_page_not_started(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, _, _ = make_learning_quiz(uid, title="Тест модуля 1", is_final=True)
    make_question(quiz.id, text="Скрытый", order_index=3, points=5, is_hidden=True)

    body = client.get(f"/quizzes/{quiz.id}").json()
    assert body == {
        "id": quiz.id,
        "module_id": quiz.module_id,
        "title": "Тест модуля 1",
        "is_final": True,
        "pass_score": 70,
        "time_limit_min": 15,
        "retakable": False,
        "show_review": True,
        "time_required_min": 0,
        # Скрытый вопрос не показан и не посчитан
        "questions_count": 2,
        "max_score": 4,
        "state": {"status": "not_started", "can_start": True},
        "attempts": [],
    }


def test_quiz_page_history_oldest_first(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, multi = make_learning_quiz(uid, retakable=True, show_review=False)

    first = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    client.post(f"/quiz_attempts/{first['id']}/finish")
    second = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    answer(client, second["id"], single.id, option_ids(single.id, correct=True))
    answer(client, second["id"], multi.id, option_ids(multi.id, correct=True))
    client.post(f"/quiz_attempts/{second['id']}/finish")

    body = client.get(f"/quizzes/{quiz.id}").json()
    assert [a["id"] for a in body["attempts"]] == [first["id"], second["id"]]
    assert [a["is_counted"] for a in body["attempts"]] == [False, True]
    assert body["attempts"][0]["score"] == 0
    assert body["attempts"][0]["score_percent"] == 0
    assert body["attempts"][1] == {
        "id": second["id"],
        "started_at": body["attempts"][1]["started_at"],
        "finished_at": body["attempts"][1]["finished_at"],
        "minutes_spent": 0,
        "score": 4,
        "max_score": 4,
        "score_percent": 100,
        "passed": True,
        "timed_out": False,
        "is_counted": True,
    }
    assert body["state"] == {
        "status": "finished",
        "result": {
            "id": second["id"],
            "score": 4,
            "max_score": 4,
            "score_percent": 100,
            "pass_score": 70,
            "passed": True,
            "is_counted": True,
            "timed_out": False,
            "minutes_spent": 0,
            # У пересдаваемого разбор по настройке теста
            "review_available": False,
        },
        "can_retake": True,
        "review_available": False,
    }


def test_quiz_page_in_progress_beats_history(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, _, _ = make_learning_quiz(uid, retakable=True)
    first = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    client.post(f"/quiz_attempts/{first['id']}/finish")
    second = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()

    body = client.get(f"/quizzes/{quiz.id}").json()
    # Активная попытка главнее завершённых: экран продолжает её
    assert body["state"]["status"] == "in_progress"
    assert body["state"]["attempt"]["id"] == second["id"]
    # Незавершённой попытки в истории нет — она в state
    assert [a["id"] for a in body["attempts"]] == [first["id"]]


# -- разбор ------------------------------------------------------------


def test_review_shows_correct_and_chosen(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, multi = make_learning_quiz(uid)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    answer(client, attempt["id"], single.id, option_ids(single.id, correct=True))
    answer(client, attempt["id"], multi.id, option_ids(multi.id, correct=True)[:1])
    result = client.post(f"/quiz_attempts/{attempt['id']}/finish").json()

    body = client.get(f"/quiz_attempts/{attempt['id']}/review").json()
    assert body["result"] == result
    # Вопросы идут в порядке попытки
    assert [q["id"] for q in body["questions"]] == [q["id"] for q in attempt["questions"]]
    assert body["questions"][0] == {
        "id": single.id,
        "type": "single",
        "text": "Что фиксирует дескриптор?",
        "points": 1,
        "earned_points": 1,
        "explanation": "Дескриптор описывает действие, которое можно увидеть.",
        "options": [
            {
                "id": option_ids(single.id)[0],
                "text": "Наблюдаемое действие",
                "is_correct": True,
                "is_chosen": True,
            },
            {
                "id": option_ids(single.id)[1],
                "text": "Отметку в журнале",
                "is_correct": False,
                "is_chosen": False,
            },
        ],
    }
    partial = body["questions"][1]
    # Неполный набор — ноль баллов, но видно и как надо было, и что выбрал человек
    assert partial["earned_points"] == 0
    assert partial["explanation"] is None
    assert [(o["is_correct"], o["is_chosen"]) for o in partial["options"]] == [
        (True, True),
        (True, False),
        (False, False),
    ]


def test_review_open_for_non_retakable_after_fail(client, sms):
    login(client, sms)
    uid = user_id(client)
    # show_review выключен, но тест непересдаваемый — разбор всё равно есть
    _, quiz, _, _ = make_learning_quiz(uid, show_review=False)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    result = client.post(f"/quiz_attempts/{attempt['id']}/finish").json()

    assert result["passed"] is False
    assert result["review_available"] is True
    assert client.get(f"/quiz_attempts/{attempt['id']}/review").status_code == 200


def test_review_403_when_disabled_for_retakable(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, _, _ = make_learning_quiz(uid, retakable=True, show_review=False)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    client.post(f"/quiz_attempts/{attempt['id']}/finish")

    resp = client.get(f"/quiz_attempts/{attempt['id']}/review")
    assert resp.status_code == 403
    assert resp.json()["error"] == {
        "code": "review_unavailable",
        "message": "Разбор у этого теста не показывается",
    }


def test_review_409_while_attempt_runs(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, _, _ = make_learning_quiz(uid)
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()

    resp = client.get(f"/quiz_attempts/{attempt['id']}/review")
    assert resp.status_code == 409
    assert resp.json()["error"] == {
        "code": "attempt_not_finished",
        "message": "Сначала завершите тест",
    }


def test_review_open_for_any_own_finished_attempt(client, sms):
    login(client, sms)
    uid = user_id(client)
    _, quiz, _, _ = make_learning_quiz(uid, retakable=True)
    first = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    client.post(f"/quiz_attempts/{first['id']}/finish")
    second = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    client.post(f"/quiz_attempts/{second['id']}/finish")

    # Смотреть можно разбор любой своей завершённой попытки, не только зачётной
    body = client.get(f"/quiz_attempts/{first['id']}/review").json()
    assert body["result"]["id"] == first["id"]
    assert body["result"]["is_counted"] is False


def test_review_404_for_missing_attempt(client, sms):
    login(client, sms)
    make_learning_quiz(user_id(client))
    assert client.get("/quiz_attempts/999999/review").status_code == 404
    assert client.post("/quiz_attempts/999999/finish").status_code == 404


def test_attempt_started_at_is_not_shifted_by_now(client, sms):
    """started_at ставится часами приложения — теми же, что считают таймер."""
    login(client, sms)
    uid = user_id(client)
    _, quiz, _, _ = make_learning_quiz(uid)
    before = now_utc()
    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts").json()
    started = _times(attempt["id"])[0]
    assert before <= started <= now_utc()


def test_second_active_attempt_blocked_by_index(client, sms):
    """Активную попытку тоже держит база: у пересдаваемого теста зачётный
    индекс молчит, и без второго индекса гонка двойного старта рождала бы
    попытку-фантом, которая прячет результат и затирает зачёт."""
    login(client, sms)
    uid = user_id(client)
    _, quiz, single, _ = make_learning_quiz(uid, retakable=True)

    with OrmSession(get_engine()) as db:
        repo = AttemptRepo(db)
        # Обе строки на одной площадке: индекс проверяется именно так
        first = repo.create(uid, quiz.id, [single.id], "p1", is_counted=False)
        second = repo.create(uid, quiz.id, [single.id], "p1", is_counted=False)
        db.commit()

    assert first is not None
    assert second is None
    assert len(attempt_rows(quiz.id)) == 1


def test_start_409_when_all_points_zero(client, sms):
    """Тест из вопросов по нулю баллов не сдаётся ни при каком ответе —
    старт отвечает quiz_empty, а не тратит единственную попытку впустую."""
    course = make_course()
    quiz = make_quiz(make_module(course.id).id)
    question = make_question(quiz.id, points=0)
    make_option(question.id, is_correct=True)

    login(client, sms)
    make_enrollment(user_id(client), course.id)

    resp = client.post(f"/quizzes/{quiz.id}/quiz_attempts")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "quiz_empty"


# -- сессия 8: строгий порядок закрывает старт попытки ----------------------


def strict_course_with_quiz(uid):
    """Курс со строгим порядком: урок, за ним тест. Доступ у учителя есть,
    урок не пройден — значит тест закрыт."""
    course = make_course(strict_order=True)
    module = make_module(course.id)
    lesson = make_lesson(module.id, order_index=1)
    quiz = make_quiz(module.id, order_index=2, time_limit_min=15)
    question = make_question(quiz.id, text="Что фиксирует дескриптор?", points=1)
    make_option(question.id, text="Наблюдаемое действие", is_correct=True)
    make_option(question.id, text="Настроение ученика")
    make_enrollment(uid, course.id)
    return course, lesson, quiz


def test_a_locked_quiz_does_not_start(client, sms):
    """Замок программы у теста — отказ, а не только вид на экране.

    Попытка единственная и не возвращается: сгоревшая не в свой черёд стоит
    человеку курса. Уроки и задания замком по-прежнему не закрываются —
    заглянувший вперёд ничего не теряет.
    """
    login(client, sms)
    course, lesson, quiz = strict_course_with_quiz(user_id(client))

    # Экран честно рисует замок
    program = client.get(f"/courses/{course.id}/program").json()["program"]
    statuses = {item["kind"]: item["status"] for item in program[0]["items"]}
    assert statuses == {"video": "available", "quiz": "locked"}

    resp = client.post(f"/quizzes/{quiz.id}/quiz_attempts")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"
    # Попытка не завелась: потратить единственную мимо экрана нельзя
    page = client.get(f"/quizzes/{quiz.id}").json()
    assert page["attempts"] == []
    # И экран теста говорит то же, что сервер: живая кнопка «Начать тест»
    # при отказе на нажатие — это хуже, чем закрытый тест
    assert page["state"] == {"status": "not_started", "can_start": False}

    # Урок пройден — тест открылся и на экране, и на сервере
    assert client.post(f"/lessons/{lesson.id}/complete").status_code == 200
    state = client.get(f"/quizzes/{quiz.id}").json()["state"]
    assert state == {"status": "not_started", "can_start": True}
    assert client.post(f"/quizzes/{quiz.id}/quiz_attempts").status_code == 200


def test_a_lesson_out_of_turn_is_still_only_a_screen_rule(client, sms):
    """Урок и задание остались правилом показа — так решил владелец:
    отказ заводится только там, где ошибка необратима."""
    login(client, sms)
    course, lesson, quiz = strict_course_with_quiz(user_id(client))
    second = make_lesson(make_module(course.id, title="Второй модуль").id, order_index=1)

    assert client.get(f"/lessons/{second.id}").status_code == 200
