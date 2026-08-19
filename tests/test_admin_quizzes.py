from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.adapters.db.models import QuizAttempt
from app.adapters.db.repos import QuizAdminRepo, now_utc
from tests.conftest import (
    in_parallel,
    login,
    login_admin,
    make_course,
    make_enrollment,
    make_module,
    make_option,
    make_question,
    make_quiz,
    meet_at,
    seed,
    user_id,
)

TEACHER_PHONE = "+7 (707) 123-45-67"
STUB = {
    "title": "Тест модуля 5",
    "time_required_min": 15,
    "is_final": False,
    "pass_score": 70,
}
# Вопрос из примера контракта — он же тело POST /admin/quizzes/{id}/questions.
QUESTION = {
    "type": "single",
    "text": "Что описывает дескриптор?",
    "explanation": "Дескриптор — про наблюдаемое действие.",
    "points": 1,
    "options": [
        {"text": "Наблюдаемое действие ученика", "is_correct": True},
        {"text": "Отношение ученика к предмету", "is_correct": False},
    ],
}


def make_editable_quiz():
    """Тест из примера контракта вместе с курсом и модулем: их названия —
    хлебные крошки шапки редактора, и в ответе они целиком."""
    course = make_course(title="Критериальное оценивание в начальной школе", category_id=3)
    module = make_module(course.id, title="Модуль 1. Зачем менять оценивание")
    quiz = make_quiz(
        module.id,
        title="Тест модуля 1",
        pass_score=70,
        time_limit_min=15,
        shuffle=True,
        time_required_min=15,
    )
    return course, module, quiz


def make_single(quiz, **kw):
    """Вопрос с одним правильным и двумя вариантами — минимальный законный
    состав: правила зачёта проверяются на паре «тип — варианты»."""
    question = make_question(quiz.id, text="Что описывает дескриптор?", **kw)
    make_option(question.id, text="Наблюдаемое действие ученика", is_correct=True,
                order_index=0)
    make_option(question.id, text="Отношение ученика к предмету", order_index=1)
    return question


def attempted(quiz, questions, user):
    """Попытка сырым объектом: снимок состава — это и есть «вопрос показывали»,
    и проходить тест по HTTP ради него лишний шум."""
    return seed(
        QuizAttempt(
            user_id=user,
            quiz_id=quiz.id,
            question_order=[question.id for question in questions],
            finished_at=now_utc(),
            score=1,
            passed=True,
        )
    )


def rows(table: str, where: str) -> int:
    """Заглянуть в базу там, где ответа уже нет: после удаления."""
    with get_engine().begin() as conn:
        return conn.scalar(text(f"SELECT count(*) FROM {table} WHERE {where}"))


def updated_at(client, course_id: int) -> str:
    return client.get(f"/admin/courses/{course_id}").json()["updated_at"]


# -- права -------------------------------------------------------------


def test_401_without_login(client):
    _, module, quiz = make_editable_quiz()
    question = make_single(quiz)
    assert client.post(f"/admin/modules/{module.id}/quizzes", json=STUB).status_code == 401
    assert client.get(f"/admin/quizzes/{quiz.id}").status_code == 401
    assert client.patch(f"/admin/quizzes/{quiz.id}", json={"title": "Т"}).status_code == 401
    assert client.delete(f"/admin/quizzes/{quiz.id}").status_code == 401
    assert client.post(f"/admin/quizzes/{quiz.id}/questions",
                       json=QUESTION).status_code == 401
    assert client.patch(f"/admin/quiz_questions/{question.id}",
                        json={"is_hidden": True}).status_code == 401
    assert client.delete(f"/admin/quiz_questions/{question.id}").status_code == 401


def test_403_for_teacher(client, sms):
    course, module, quiz = make_editable_quiz()
    question = make_single(quiz)
    login(client, sms)
    # Доступ к курсу правки не открывает: редактор — не экран учителя
    make_enrollment(user_id(client), course.id)

    resp = client.get(f"/admin/quizzes/{quiz.id}")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert client.post(f"/admin/modules/{module.id}/quizzes", json=STUB).status_code == 403
    assert client.patch(f"/admin/quizzes/{quiz.id}", json={"title": "Т"}).status_code == 403
    assert client.delete(f"/admin/quizzes/{quiz.id}").status_code == 403
    assert client.post(f"/admin/quizzes/{quiz.id}/questions",
                       json=QUESTION).status_code == 403
    assert client.patch(f"/admin/quiz_questions/{question.id}",
                        json={"is_hidden": True}).status_code == 403
    assert client.delete(f"/admin/quiz_questions/{question.id}").status_code == 403


def test_404_for_missing_quiz_module_and_question(client, sms):
    login_admin(client, sms)
    assert client.post("/admin/modules/999999/quizzes", json=STUB).status_code == 404
    resp = client.get("/admin/quizzes/999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert client.patch("/admin/quizzes/999999", json={"title": "Т"}).status_code == 404
    assert client.delete("/admin/quizzes/999999").status_code == 404
    assert client.post("/admin/quizzes/999999/questions", json=QUESTION).status_code == 404
    assert client.patch("/admin/quiz_questions/999999",
                        json={"is_hidden": True}).status_code == 404
    assert client.delete("/admin/quiz_questions/999999").status_code == 404


# -- заготовка ---------------------------------------------------------


def test_stub_answers_with_the_editor(client, sms):
    course, module, _ = make_editable_quiz()
    login_admin(client, sms)

    resp = client.post(f"/admin/modules/{module.id}/quizzes", json=STUB)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "id": body["id"],
        "module_id": module.id,
        "course": {
            "id": course.id,
            "lang": "ru",
            "title": "Критериальное оценивание в начальной школе",
        },
        "module": {"id": module.id, "title": "Модуль 1. Зачем менять оценивание"},
        "title": "Тест модуля 5",
        "is_final": False,
        "pass_score": 70,
        # Таймера у заготовки нет: его включают переключателем в редакторе
        "time_limit_min": None,
        "shuffle": False,
        "show_review": True,
        "retakable": False,
        "time_required_min": 15,
        # Заготовка заводится скрытой: пустой тест в открытом курсе входит
        # в условия сертификата и отдаёт 409 на попытку
        "is_hidden": True,
        "has_attempts": False,
        "max_score": 0,
        "questions": [],
    }


def test_stub_goes_last_in_the_module(client, sms):
    """Порядок в модуле общий на все три вида: свой максимум у каждой таблицы
    поставил бы новый тест в середину дерева."""
    course, module, quiz = make_editable_quiz()
    login_admin(client, sms)

    created = client.post(f"/admin/modules/{module.id}/quizzes", json=STUB).json()
    program = client.get(f"/admin/courses/{course.id}").json()["program"]
    assert [item["id"] for item in program[0]["items"]] == [quiz.id, created["id"]]


def test_stub_does_not_close_the_publish_button(client, sms):
    """То же у теста: заготовка без вопросов скрыта, и в `empty_quizzes`
    её нет — иначе новый тест гасил бы кнопку «Открыть набор» у курса,
    с которым всё в порядке."""
    course = make_course(cover="https://cdn.example.kz/covers/assessment_ru.jpg")
    module = make_module(course.id, title="Модуль 1")
    make_single(make_quiz(module.id, title="Тест модуля 1"))
    login_admin(client, sms)
    assert client.get(f"/admin/courses/{course.id}").json()["readiness"]["can_open"] is True

    created = client.post(f"/admin/modules/{module.id}/quizzes", json=STUB).json()
    assert created["is_hidden"] is True

    body = client.get(f"/admin/courses/{course.id}").json()
    checks = {item["code"]: item for item in body["readiness"]["items"]}
    assert checks["empty_quizzes"] == {
        "code": "empty_quizzes",
        "ok": True,
        "text": "Во всех тестах есть вопросы",
        "items": [],
    }
    assert body["readiness"]["can_open"] is True


def test_stub_validates_fields(client, sms):
    _, module, _ = make_editable_quiz()
    login_admin(client, sms)

    def field_of(payload):
        resp = client.post(f"/admin/modules/{module.id}/quizzes", json=payload)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        return resp.json()["error"]["details"]["fields"][0]["field"]

    assert field_of({**STUB, "title": "   "}) == "title"
    assert field_of({**STUB, "time_required_min": 601}) == "time_required_min"
    assert field_of({**STUB, "pass_score": 0}) == "pass_score"
    assert field_of({**STUB, "pass_score": 101}) == "pass_score"


# -- редактор теста ----------------------------------------------------


def test_editor_shows_the_quiz_whole(client, client2, sms):
    course, module, quiz = make_editable_quiz()
    single = make_single(
        quiz,
        explanation="Дескриптор — про наблюдаемое действие, а не про отношение.",
        order_index=0,
    )
    multi = make_question(quiz.id, type="multi", text="Что относится к формирующему?",
                          points=2, order_index=1)
    first = make_option(multi.id, text="Обратная связь по критериям", is_correct=True,
                        order_index=0)
    second = make_option(multi.id, text="Взаимооценивание", is_correct=True, order_index=1)
    third = make_option(multi.id, text="Итоговая контрольная", order_index=2)

    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    attempted(quiz, [single], teacher)

    login_admin(client2, sms)
    body = client2.get(f"/admin/quizzes/{quiz.id}").json()
    options = body["questions"][0]["options"]
    assert body == {
        "id": quiz.id,
        "module_id": module.id,
        "course": {
            "id": course.id,
            "lang": "ru",
            "title": "Критериальное оценивание в начальной школе",
        },
        "module": {"id": module.id, "title": "Модуль 1. Зачем менять оценивание"},
        "title": "Тест модуля 1",
        "is_final": False,
        "pass_score": 70,
        "time_limit_min": 15,
        "shuffle": True,
        "show_review": True,
        "retakable": False,
        "time_required_min": 15,
        "is_hidden": False,
        "has_attempts": True,
        "max_score": 3,
        "questions": [
            {
                "id": single.id,
                "type": "single",
                "text": "Что описывает дескриптор?",
                "explanation": "Дескриптор — про наблюдаемое действие, а не про отношение.",
                "points": 1,
                "is_hidden": False,
                # Этот вопрос был в составе попытки, соседний — нет
                "has_attempts": True,
                "options": [
                    {"id": options[0]["id"], "text": "Наблюдаемое действие ученика",
                     "is_correct": True},
                    {"id": options[1]["id"], "text": "Отношение ученика к предмету",
                     "is_correct": False},
                ],
            },
            {
                "id": multi.id,
                "type": "multi",
                "text": "Что относится к формирующему?",
                "explanation": None,
                "points": 2,
                "is_hidden": False,
                "has_attempts": False,
                "options": [
                    {"id": first.id, "text": "Обратная связь по критериям",
                     "is_correct": True},
                    {"id": second.id, "text": "Взаимооценивание", "is_correct": True},
                    {"id": third.id, "text": "Итоговая контрольная", "is_correct": False},
                ],
            },
        ],
    }


def test_correct_answers_reach_the_admin_only(client, client2, sms):
    """Единственное место во всём контракте, где верные ответы уходят наружу.
    Учительский GET /quizzes/{id} их по-прежнему не отдаёт."""
    course, _, quiz = make_editable_quiz()
    make_single(quiz)
    login(client, sms, TEACHER_PHONE)
    make_enrollment(user_id(client), course.id)

    teacher_view = client.get(f"/quizzes/{quiz.id}")
    assert teacher_view.status_code == 200
    # У учителя до старта вопросов нет вовсе — ни текста, ни вариантов
    assert "questions" not in teacher_view.json()
    assert "is_correct" not in teacher_view.text

    login_admin(client2, sms)
    admin_view = client2.get(f"/admin/quizzes/{quiz.id}").json()
    assert [option["is_correct"] for option in admin_view["questions"][0]["options"]] == [
        True,
        False,
    ]


def test_editor_shows_hidden_quiz_of_a_draft_course(client, sms):
    """Админ видит то, чего нет на площадке: фильтр видимости каталога
    к редактору не применяется вовсе."""
    course = make_course(status="draft")
    module = make_module(course.id)
    quiz = make_quiz(module.id, is_hidden=True)

    login_admin(client, sms)
    assert client.get(f"/admin/quizzes/{quiz.id}").json()["is_hidden"] is True
    # Тот же тест учителю не существует: он скрыт, а курс — черновик
    assert client.get(f"/quizzes/{quiz.id}").status_code == 404


def test_hidden_question_is_shown_but_not_counted(client, sms):
    """Скрытый вопрос приходит с is_hidden: true — админ должен видеть,
    что спрятал. В max_score он не считается, как не считается и у учителя."""
    _, _, quiz = make_editable_quiz()
    make_single(quiz, order_index=0)
    hidden = make_single(quiz, is_hidden=True, order_index=1)
    login_admin(client, sms)

    body = client.get(f"/admin/quizzes/{quiz.id}").json()
    assert [question["id"] for question in body["questions"]][-1] == hidden.id
    assert body["questions"][-1]["is_hidden"] is True
    assert body["max_score"] == 1


# -- настройки теста ---------------------------------------------------


def test_patch_returns_the_saved_settings(client, sms):
    _, _, quiz = make_editable_quiz()
    login_admin(client, sms)

    body = client.patch(
        f"/admin/quizzes/{quiz.id}",
        json={"title": "  Тест модуля 1. Оценивание  ", "pass_score": 80,
              "shuffle": False, "show_review": False, "retakable": True,
              "time_required_min": 20, "is_hidden": True},
    ).json()
    assert body["title"] == "Тест модуля 1. Оценивание"
    assert body["pass_score"] == 80
    assert body["shuffle"] is False
    assert body["show_review"] is False
    assert body["retakable"] is True
    assert body["time_required_min"] == 20
    assert body["is_hidden"] is True
    assert client.patch(f"/admin/quizzes/{quiz.id}",
                        json={"title": "   "}).status_code == 422


def test_timer_is_the_choice_between_null_and_a_number(client, sms):
    """Переключатель «Таймер» и есть выбор между null и числом: null здесь
    значение, а не «не прислано»."""
    _, _, quiz = make_editable_quiz()
    login_admin(client, sms)

    assert client.patch(f"/admin/quizzes/{quiz.id}",
                        json={"time_limit_min": None}).json()["time_limit_min"] is None
    assert client.patch(f"/admin/quizzes/{quiz.id}",
                        json={"time_limit_min": 30}).json()["time_limit_min"] == 30
    resp = client.patch(f"/admin/quizzes/{quiz.id}", json={"time_limit_min": 601})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "time_limit_min"


def test_settings_of_a_quiz_with_attempts_are_editable(client, client2, sms):
    """Запрет раздела 10 касается вопросов, а не правил: настройки правятся
    и у теста, который уже проходили."""
    course, _, quiz = make_editable_quiz()
    question = make_single(quiz)
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    attempted(quiz, [question], teacher)

    login_admin(client2, sms)
    body = client2.patch(f"/admin/quizzes/{quiz.id}", json={"pass_score": 90}).json()
    assert body["pass_score"] == 90
    assert body["has_attempts"] is True


def test_raising_pass_score_does_not_take_away_a_finished_pass(client, client2, sms):
    """passed и score завершённых попыток посчитаны на старом проходном балле
    и такими остаются: иначе поднятие проходного отобрало бы у людей уже
    полученный зачёт, а вместе с ним и выданный сертификат."""
    course, _, quiz = make_editable_quiz()
    question = make_single(quiz)
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    attempt = attempted(quiz, [question], teacher)

    login_admin(client2, sms)
    assert client2.patch(f"/admin/quizzes/{quiz.id}",
                         json={"pass_score": 100}).status_code == 200

    with get_engine().begin() as conn:
        stored = conn.execute(
            text("SELECT score, passed FROM quiz_attempt WHERE id = :id"),
            {"id": attempt.id},
        ).one()
    assert stored == (1, True)
    # И на экране учителя зачёт тоже остался
    result = client.get(f"/quizzes/{quiz.id}").json()["state"]["result"]
    assert result["passed"] is True
    # Проходной при этом показан новый: он свойство теста, а не попытки
    assert result["pass_score"] == 100


def test_second_final_quiz_in_a_course_is_409(client, sms):
    """Итоговый тест в курсе один: отчёт берёт первый попавшийся, а чек-лист
    сертификата пишет «Сдать итоговый тест» в единственном числе."""
    course, module, quiz = make_editable_quiz()
    final = make_quiz(module.id, title="Итоговый тест", is_final=True)
    login_admin(client, sms)

    resp = client.patch(f"/admin/quizzes/{quiz.id}", json={"is_final": True})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "final_quiz_exists"
    assert resp.json()["error"]["message"] == "Итоговый тест в курсе уже есть: «Итоговый тест»"
    assert resp.json()["error"]["details"] == {"quiz_id": final.id}
    # Отбитый PATCH не меняет ничего
    assert client.get(f"/admin/quizzes/{quiz.id}").json()["is_final"] is False

    # Заготовка проверяется тем же правилом
    resp = client.post(f"/admin/modules/{module.id}/quizzes",
                       json={**STUB, "is_final": True})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "final_quiz_exists"
    assert rows("quiz", f"module_id = {module.id}") == 2

    # Итоговый в соседнем курсе этому не мешает: правило про курс, а не про базу
    other = make_module(make_course().id)
    assert client.post(f"/admin/modules/{other.id}/quizzes",
                       json={**STUB, "is_final": True}).status_code == 200
    # И сам итоговый остаётся итоговым: себя он не запирает
    assert client.patch(f"/admin/quizzes/{final.id}",
                        json={"is_final": True}).status_code == 200
    assert course.id != other.id


def test_two_final_quizzes_at_once_leave_one(client, client2, sms, monkeypatch):
    """Единственность итогового живёт в коде, а не в индексе: `course_id`
    у теста нет, он в двух джойнах. Значит два одновременных PATCH обязаны
    сойтись на строке курса — иначе оба прочитают «итогового нет»."""
    _, module, quiz = make_editable_quiz()
    other = make_quiz(module.id, title="Тест модуля 2")
    login_admin(client, sms)
    login_admin(client2, sms, phone="+7 (700) 000-00-98")
    meet_at(monkeypatch, QuizAdminRepo, "with_course_and_module")

    first, second = in_parallel(
        lambda: client.patch(f"/admin/quizzes/{quiz.id}", json={"is_final": True}),
        lambda: client2.patch(f"/admin/quizzes/{other.id}", json={"is_final": True}),
    )
    winner, loser = sorted([first, second], key=lambda resp: resp.status_code)
    assert winner.status_code == 200
    assert loser.status_code == 409
    assert loser.json()["error"]["code"] == "final_quiz_exists"
    assert loser.json()["error"]["details"] == {"quiz_id": winner.json()["id"]}
    assert rows("quiz", f"module_id = {module.id} AND is_final IS TRUE") == 1


# -- удаление теста ----------------------------------------------------


def test_delete_takes_questions_and_options_with_it(client, sms):
    _, module, quiz = make_editable_quiz()
    question = make_single(quiz)
    survivor = make_quiz(module.id, title="Соседний тест")
    login_admin(client, sms)

    assert client.delete(f"/admin/quizzes/{quiz.id}").status_code == 204
    assert rows("quiz", f"id = {quiz.id}") == 0
    assert rows("question", f"quiz_id = {quiz.id}") == 0
    assert rows("option", f"question_id = {question.id}") == 0
    assert rows("quiz", f"id = {survivor.id}") == 1


def test_delete_of_a_quiz_with_attempts_is_409_and_changes_nothing(client, client2, sms):
    course, _, quiz = make_editable_quiz()
    question = make_single(quiz)
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    attempted(quiz, [question], teacher)

    login_admin(client2, sms)
    resp = client2.delete(f"/admin/quizzes/{quiz.id}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "has_attempts"
    assert resp.json()["error"]["details"] == {"attempts_count": 1}
    assert resp.json()["error"]["message"] == (
        "У теста 1 попытка — его можно скрыть, но не удалить"
    )
    assert rows("quiz", f"id = {quiz.id}") == 1
    assert rows("quiz_attempt", f"quiz_id = {quiz.id}") == 1

    # Дальше его прячут — это и предлагает текст ошибки
    assert client2.patch(f"/admin/quizzes/{quiz.id}",
                         json={"is_hidden": True}).json()["is_hidden"] is True


# -- вопросы -----------------------------------------------------------


def test_question_is_created_with_its_options(client, sms):
    _, _, quiz = make_editable_quiz()
    login_admin(client, sms)

    resp = client.post(f"/admin/quizzes/{quiz.id}/questions", json=QUESTION)
    assert resp.status_code == 200
    body = resp.json()
    options = body["options"]
    assert body == {
        "id": body["id"],
        "type": "single",
        "text": "Что описывает дескриптор?",
        "explanation": "Дескриптор — про наблюдаемое действие.",
        "points": 1,
        "is_hidden": False,
        "has_attempts": False,
        "options": [
            {"id": options[0]["id"], "text": "Наблюдаемое действие ученика",
             "is_correct": True},
            {"id": options[1]["id"], "text": "Отношение ученика к предмету",
             "is_correct": False},
        ],
    }
    # Вопрос встаёт последним, и max_score теста вырос на его баллы
    second = client.post(f"/admin/quizzes/{quiz.id}/questions",
                         json={**QUESTION, "text": "Второй вопрос", "points": 2}).json()
    card = client.get(f"/admin/quizzes/{quiz.id}").json()
    assert [question["id"] for question in card["questions"]] == [body["id"], second["id"]]
    assert card["max_score"] == 3


def test_options_rules_are_checked_on_the_pair_of_type_and_options(client, sms):
    """Правила зачёта: вариантов 2..10, single — ровно один правильный,
    multi — хотя бы один правильный и не меньше трёх вариантов,
    bool — ровно два варианта и ровно один правильный."""
    _, _, quiz = make_editable_quiz()
    login_admin(client, sms)

    def message_of(payload):
        resp = client.post(f"/admin/quizzes/{quiz.id}/questions", json=payload)
        assert resp.status_code == 422, resp.text
        fields = resp.json()["error"]["details"]["fields"]
        assert len(fields) == 1
        return fields[0]["field"], fields[0]["message"]

    yes = {"text": "Да", "is_correct": True}
    no = {"text": "Нет", "is_correct": False}

    assert message_of({**QUESTION, "options": [yes]}) == (
        "options", "Вариантов должно быть от 2 до 10"
    )
    assert message_of({**QUESTION, "options": [dict(no, text=str(n)) for n in range(11)]}) == (
        "options", "Вариантов должно быть от 2 до 10"
    )
    assert message_of({**QUESTION, "options": [yes, {"text": "  ", "is_correct": False}]}) == (
        "options", "Вариант без текста не сохранить"
    )
    assert message_of({**QUESTION, "options": [yes, dict(no, is_correct=True)]}) == (
        "options", "В вопросе с одним ответом правильный ровно один"
    )
    assert message_of({**QUESTION, "options": [no, no]}) == (
        "options", "В вопросе с одним ответом правильный ровно один"
    )
    assert message_of({**QUESTION, "type": "multi", "options": [no, no, no]}) == (
        "options", "В вопросе с несколькими ответами нужен правильный"
    )
    # «Несколько правильных» из двух вариантов — это вопрос с одним ответом наоборот
    assert message_of({**QUESTION, "type": "multi", "options": [yes, no]}) == (
        "options", "В вопросе с несколькими ответами вариантов не меньше 3"
    )
    assert message_of({**QUESTION, "type": "bool", "options": [yes, no, no]}) == (
        "options", "У вопроса «да/нет» два варианта и один правильный"
    )
    assert message_of(
        {**QUESTION, "type": "bool", "options": [yes, dict(no, is_correct=True)]}
    ) == ("options", "У вопроса «да/нет» два варианта и один правильный")
    assert message_of({**QUESTION, "text": "   "}) == (
        "text", "Без текста вопрос не сохранить"
    )
    # Ни один отбитый запрос не завёл строки
    assert rows("question", f"quiz_id = {quiz.id}") == 0

    # А законные составы проходят
    assert client.post(f"/admin/quizzes/{quiz.id}/questions",
                       json={**QUESTION, "type": "multi",
                             "options": [yes, dict(no, is_correct=True), no]}).status_code == 200
    assert client.post(f"/admin/quizzes/{quiz.id}/questions",
                       json={**QUESTION, "type": "bool",
                             "options": [yes, no]}).status_code == 200


def test_options_are_replaced_by_the_whole_list(client, sms):
    """Варианты приходят полным списком и заменяют прежние: частичной правки
    одного варианта нет."""
    _, _, quiz = make_editable_quiz()
    question = make_single(quiz)
    login_admin(client, sms)

    body = client.patch(
        f"/admin/quiz_questions/{question.id}",
        json={"type": "multi", "text": "Что относится к формирующему?",
              "explanation": None, "points": 2,
              "options": [
                  {"text": "Обратная связь по критериям", "is_correct": True},
                  {"text": "Взаимооценивание", "is_correct": True},
                  {"text": "Итоговая контрольная", "is_correct": False},
              ]},
    ).json()
    assert body["type"] == "multi"
    assert body["points"] == 2
    assert body["explanation"] is None
    assert [option["text"] for option in body["options"]] == [
        "Обратная связь по критериям",
        "Взаимооценивание",
        "Итоговая контрольная",
    ]
    # Прежних вариантов в базе не осталось — их ровно три, а не пять
    assert rows("option", f"question_id = {question.id}") == 3


def test_type_switch_alone_is_checked_against_the_stored_options(client, sms):
    """Проверяется то, что получится, а не то, что было: смена типа
    в одиночку проверяется на тех вариантах, что уже лежат."""
    _, _, quiz = make_editable_quiz()
    question = make_single(quiz)
    login_admin(client, sms)

    resp = client.patch(f"/admin/quiz_questions/{question.id}", json={"type": "multi"})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["message"] == (
        "В вопросе с несколькими ответами вариантов не меньше 3"
    )
    # Отбитый PATCH не меняет ничего
    assert client.get(f"/admin/quizzes/{quiz.id}").json()["questions"][0]["type"] == "single"

    # А вместе с новыми вариантами тот же тип проходит одним запросом
    assert client.patch(
        f"/admin/quiz_questions/{question.id}",
        json={"type": "multi",
              "options": [{"text": "Раз", "is_correct": True},
                          {"text": "Два", "is_correct": True},
                          {"text": "Три", "is_correct": False}]},
    ).status_code == 200


def test_question_with_attempts_is_not_edited_but_hiding_passes(client, client2, sms):
    """Вопрос с попытками не редактируется: разбор ответов покажет человеку
    то, чего он не видел. Запрос с одним is_hidden проходит всегда — иначе
    выполнить совет из текста ошибки было бы нечем."""
    course, _, quiz = make_editable_quiz()
    question = make_single(quiz)
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    attempted(quiz, [question], teacher)

    login_admin(client2, sms)
    resp = client2.patch(f"/admin/quiz_questions/{question.id}",
                         json={"text": "Другой вопрос"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "has_attempts"
    assert resp.json()["error"]["details"] == {"attempts_count": 1}
    assert resp.json()["error"]["message"] == (
        "Вопрос был в 1 попытке — создайте новый, а этот скройте"
    )

    # Варианты не подменились: проверка попыток идёт до замены, а не после
    body = client2.get(f"/admin/quizzes/{quiz.id}").json()["questions"][0]
    assert body["text"] == "Что описывает дескриптор?"
    assert len(body["options"]) == 2
    # Тот же запрос с одним лишь is_hidden проходит
    hidden = client2.patch(f"/admin/quiz_questions/{question.id}",
                           json={"is_hidden": True}).json()
    assert hidden["is_hidden"] is True
    assert hidden["has_attempts"] is True
    # А is_hidden вместе с содержимым — это уже правка содержимого
    assert client2.patch(f"/admin/quiz_questions/{question.id}",
                         json={"is_hidden": False, "points": 2}).status_code == 409


def test_skipped_question_still_counts_as_shown(client, client2, sms):
    """«Есть попытки» — это question_order, а не ответы: вопрос показан
    и тогда, когда человек его пропустил."""
    course, _, quiz = make_editable_quiz()
    skipped = make_single(quiz)
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    # Попытка состава из этого вопроса, но без единого ответа на него
    attempted(quiz, [skipped], teacher)
    assert rows("answer", f"question_id = {skipped.id}") == 0

    login_admin(client2, sms)
    assert client2.get(f"/admin/quizzes/{quiz.id}").json()["questions"][0][
        "has_attempts"
    ] is True
    assert client2.delete(f"/admin/quiz_questions/{skipped.id}").status_code == 409


def test_question_outside_any_attempt_is_edited_and_deleted(client, client2, sms):
    """Соседний вопрос, не попавший в состав попытки, правится и удаляется
    как обычно: замок висит на вопросе, а не на тесте."""
    course, _, quiz = make_editable_quiz()
    shown = make_single(quiz, order_index=0)
    fresh = make_single(quiz, order_index=1)
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    attempted(quiz, [shown], teacher)

    login_admin(client2, sms)
    assert client2.patch(f"/admin/quiz_questions/{fresh.id}",
                         json={"points": 3}).json()["points"] == 3
    assert client2.delete(f"/admin/quiz_questions/{fresh.id}").status_code == 204
    assert rows("question", f"id = {fresh.id}") == 0
    assert rows("option", f"question_id = {fresh.id}") == 0
    assert rows("question", f"id = {shown.id}") == 1


# -- «Изменён» у курса -------------------------------------------------


def test_every_edit_moves_the_course_updated_at(client, sms):
    """Правка теста — это правка курса: столбец «Изменён» в списке курсов
    без этого врал бы."""
    course, module, quiz = make_editable_quiz()
    login_admin(client, sms)

    was = updated_at(client, course.id)
    created = client.post(f"/admin/modules/{module.id}/quizzes", json=STUB).json()
    after_create = updated_at(client, course.id)
    assert after_create > was

    client.patch(f"/admin/quizzes/{quiz.id}", json={"time_required_min": 30})
    after_patch = updated_at(client, course.id)
    assert after_patch > after_create

    question = client.post(f"/admin/quizzes/{quiz.id}/questions", json=QUESTION).json()
    after_question = updated_at(client, course.id)
    assert after_question > after_patch

    client.patch(f"/admin/quiz_questions/{question['id']}", json={"points": 2})
    after_question_patch = updated_at(client, course.id)
    assert after_question_patch > after_question

    client.delete(f"/admin/quiz_questions/{question['id']}")
    after_question_delete = updated_at(client, course.id)
    assert after_question_delete > after_question_patch

    client.delete(f"/admin/quizzes/{created['id']}")
    assert updated_at(client, course.id) > after_question_delete
