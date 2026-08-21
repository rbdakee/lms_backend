from datetime import timedelta

from app.adapters.db.models import QuizAttempt
from app.adapters.db.repos import now_utc
from tests.conftest import (
    login,
    login_admin,
    make_admin,
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
    """Участник с вымышленным номером: в отчёте их нужно много."""
    return make_user(f"+7702000{number:04d}", **kw)


def days_ago(days: int):
    return now_utc() - timedelta(days=days)


def report_course(**course_kw):
    """Курс из двух модулей: два урока, задание и тест модуля в первом,
    итоговый тест во втором. Пять видимых элементов программы — процент
    прогресса считается пятыми долями."""
    fields = {"title": "Критериальное оценивание", "cert_require_lessons": True}
    fields.update(course_kw)
    course = make_course(**fields)
    first = make_module(course.id, title="Модуль 1", order_index=1)
    second = make_module(course.id, title="Модуль 2", order_index=2)
    lesson_one = make_lesson(first.id, title="Урок 1", order_index=1)
    lesson_two = make_lesson(first.id, title="Урок 2", order_index=2)
    task = make_task(first.id, title="Задание", order_index=3)
    quiz = make_quiz(first.id, title="Тест модуля", order_index=4)
    final = make_quiz(second.id, title="Итоговый тест", order_index=1, is_final=True)
    return course, lesson_one, lesson_two, task, quiz, final


def two_questions(quiz):
    """Два вопроса по баллу: процент попытки считается половинами."""
    return [make_question(quiz.id, text="Вопрос 1"), make_question(quiz.id, text="Вопрос 2")]


def attempt(uid, quiz, questions, *, score, passed=True, finished=True, counted=True):
    """Попытка сырым объектом: проходить тест по HTTP ради одной клетки
    таблицы — лишний шум."""
    return seed(
        QuizAttempt(
            user_id=uid,
            quiz_id=quiz.id,
            question_order=[question.id for question in questions],
            finished_at=now_utc() if finished else None,
            score=score if finished else None,
            passed=passed if finished else None,
            is_counted=counted,
        )
    )


def by_user(body) -> dict[int, dict]:
    return {item["user_id"]: item for item in body["participants"]["items"]}


# -- права --------------------------------------------------------------


def test_report_requires_admin(client, sms):
    course = make_course()
    assert client.get(f"/admin/reports/{course.id}").status_code == 401

    login(client, sms)
    resp = client.get(f"/admin/reports/{course.id}")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_report_404_for_unknown_course(client, sms):
    login_admin(client, sms)
    resp = client.get("/admin/reports/999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_draft_course_report_opens(client, sms):
    """Черновик и скрытый курс админу видны: отчёт открывают из редактора."""
    login_admin(client, sms)
    draft = make_course(status="draft")
    hidden = make_course(status="hidden")

    assert client.get(f"/admin/reports/{draft.id}").status_code == 200
    assert client.get(f"/admin/reports/{hidden.id}").status_code == 200


# -- пустой отчёт --------------------------------------------------------


def test_report_without_participants(client, sms):
    login_admin(client, sms)
    course = make_course(title="Новый курс")

    body = client.get(f"/admin/reports/{course.id}").json()

    assert body["course"] == {"id": course.id, "lang": "ru", "title": "Новый курс"}
    assert body["generated_at"] is not None
    assert body["summary"] == {
        "granted": 0,
        "started": 0,
        "completed": 0,
        "avg_progress_percent": None,
        "avg_final_score": None,
        "certificates": 0,
        "avg_days_to_complete": None,
    }
    assert body["funnel"] == []
    assert body["participants"] == {"items": [], "total": 0, "page": 1, "per_page": 20}


# -- прогресс участника --------------------------------------------------


def test_participant_progress_matches_program_screen(client, client2, sms):
    course, lesson_one, _, task, _, _ = report_course()
    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_progress(uid, lesson_one.id)
    make_submission(uid, task.id, status="accepted")

    program = client.get(f"/courses/{course.id}/program").json()["program"]
    items = [item for module in program for item in module["items"]]
    done = sum(1 for item in items if item["status"] == "done")

    login_admin(client2, sms)
    body = client2.get(f"/admin/reports/{course.id}").json()

    # Одно и то же число на двух экранах: разойдись оно — это прочитают как ошибку
    assert by_user(body)[uid]["progress_percent"] == round(done * 100 / len(items))
    assert by_user(body)[uid]["progress_percent"] == 40


def test_revoked_access_is_not_a_participant(client, sms):
    login_admin(client, sms)
    course, lesson_one, *_ = report_course()
    active = teacher(1, last_name="Абдиева")
    revoked = teacher(2, last_name="Ярова")
    make_enrollment(active.id, course.id)
    make_enrollment(revoked.id, course.id, revoked_at=now_utc())
    make_progress(active.id, lesson_one.id)
    make_progress(revoked.id, lesson_one.id)

    body = client.get(f"/admin/reports/{course.id}").json()

    assert body["summary"]["granted"] == 1
    assert body["summary"]["started"] == 1
    assert [item["user_id"] for item in body["participants"]["items"]] == [active.id]
    # Отозванный не считается и в воронке — иначе она выше числа участников
    assert body["funnel"][0]["reached"] == 1


# -- воронка -------------------------------------------------------------


def test_funnel_lists_every_visible_item_in_order(client, sms):
    login_admin(client, sms)
    course, lesson_one, lesson_two, task, quiz, final = report_course()
    hidden = make_lesson(
        lesson_one.module_id, title="Скрытый", order_index=5, is_hidden=True
    )
    hidden_task = make_task(
        lesson_one.module_id, title="Скрытое задание", order_index=6, is_hidden=True
    )
    hidden_quiz = make_quiz(
        lesson_one.module_id, title="Скрытый тест", order_index=7, is_hidden=True
    )
    first, second = teacher(1), teacher(2)
    for person in (first, second):
        make_enrollment(person.id, course.id)
        make_progress(person.id, lesson_one.id)
        make_progress(person.id, hidden.id)
        make_submission(person.id, hidden_task.id, status="accepted")
    make_progress(first.id, lesson_two.id)
    make_submission(first.id, task.id, status="accepted")
    attempt(first.id, quiz, two_questions(quiz), score=2)
    attempt(first.id, hidden_quiz, two_questions(hidden_quiz), score=2)

    body = client.get(f"/admin/reports/{course.id}").json()

    # Скрытых элементов в воронке нет, хотя отметки, сдачи и попытки по ним
    # в базе есть; номера сквозные — по всем элементам, а не по одним урокам
    assert body["funnel"] == [
        {"kind": "video", "id": lesson_one.id, "number": 1, "title": "Урок 1", "reached": 2},
        {"kind": "video", "id": lesson_two.id, "number": 2, "title": "Урок 2", "reached": 1},
        {"kind": "task", "id": task.id, "number": 3, "title": "Задание", "reached": 1},
        {"kind": "quiz", "id": quiz.id, "number": 4, "title": "Тест модуля", "reached": 1},
        {"kind": "quiz", "id": final.id, "number": 5, "title": "Итоговый тест", "reached": 0},
    ]
    # Числитель и знаменатель сходятся: пройденное скрытое не идёт ни туда,
    # ни сюда — иначе прогресс перевалил бы за сотню
    assert by_user(body)[first.id]["progress_percent"] == 80
    assert by_user(body)[second.id]["progress_percent"] == 20


# -- сводка --------------------------------------------------------------


def test_summary_counts_and_averages(client, sms):
    login_admin(client, sms)
    course, lesson_one, lesson_two, task, quiz, final = report_course()
    questions = two_questions(final)
    finisher, starter, idler = teacher(1), teacher(2), teacher(3)

    make_enrollment(
        finisher.id, course.id, granted_at=days_ago(10), completed_at=days_ago(4)
    )
    make_progress(finisher.id, lesson_one.id)
    make_progress(finisher.id, lesson_two.id)
    make_submission(finisher.id, task.id, status="accepted")
    attempt(finisher.id, quiz, two_questions(quiz), score=2)
    attempt(finisher.id, final, questions, score=2)
    make_certificate(finisher.id, course.id)

    make_enrollment(starter.id, course.id)
    make_progress(starter.id, lesson_one.id)
    # Провалил итоговый: в средний балл идёт, в прогресс — нет
    attempt(starter.id, final, questions, score=1, passed=False)

    make_enrollment(idler.id, course.id)

    body = client.get(f"/admin/reports/{course.id}").json()

    assert body["summary"] == {
        "granted": 3,
        "started": 2,
        "completed": 1,
        # (100 + 20 + 0) / 3 — не начавшие входят в среднее нулями
        "avg_progress_percent": 40,
        # (100 + 50) / 2 — процентами, как на экране результата теста
        "avg_final_score": 75,
        "certificates": 1,
        "avg_days_to_complete": 6,
    }


def test_admin_is_a_ghost_in_the_report(client, sms):
    """Админ проходит курс наравне с учителями, но ни в одно число отчёта
    не входит: в списке учителей и в счётчике дашборда его нет, и отчёт,
    считающий его наравне со всеми, расходился бы с ними (владелец,
    21.08.2026)."""
    login_admin(client, sms)
    course, lesson_one, lesson_two, task, quiz, final = report_course()
    questions = two_questions(final)

    person = teacher(1)
    make_enrollment(person.id, course.id)
    make_progress(person.id, lesson_one.id)

    # Тот же курс целиком проходит второй админ — с сертификатом и сданным
    # итоговым тестом, то есть по всем показателям сразу
    ghost = teacher(2)
    make_admin(ghost.phone)
    make_enrollment(ghost.id, course.id, granted_at=days_ago(10), completed_at=days_ago(4))
    make_progress(ghost.id, lesson_one.id)
    make_progress(ghost.id, lesson_two.id)
    make_submission(ghost.id, task.id, status="accepted")
    attempt(ghost.id, quiz, two_questions(quiz), score=2)
    attempt(ghost.id, final, questions, score=2)
    make_certificate(ghost.id, course.id)

    body = client.get(f"/admin/reports/{course.id}").json()

    assert body["summary"] == {
        "granted": 1,
        "started": 1,
        "completed": 0,
        "avg_progress_percent": 20,
        "avg_final_score": None,
        "certificates": 0,
        "avg_days_to_complete": None,
    }
    # Воронка считает только учителя: пройденное админом в ней не отражается
    assert [item["reached"] for item in body["funnel"]] == [1, 0, 0, 0, 0]
    assert body["participants"]["total"] == 1
    assert list(by_user(body)) == [person.id]


def test_revoked_certificate_is_not_counted(client, sms):
    login_admin(client, sms)
    course, *_ = report_course()
    person = teacher(1)
    make_enrollment(person.id, course.id)
    make_certificate(person.id, course.id, revoked_at=now_utc())

    body = client.get(f"/admin/reports/{course.id}").json()

    assert body["summary"]["certificates"] == 0
    assert by_user(body)[person.id]["certificate"] != "issued"


# -- таблица участников --------------------------------------------------


def test_participants_paginate_and_search_by_name(client, sms):
    login_admin(client, sms)
    course, *_ = report_course()
    people = [
        teacher(1, last_name="Ярова", first_name="Ольга"),
        teacher(2, last_name="Абдиева", first_name="Сауле"),
        teacher(3, last_name="Смагулова", first_name="Гульмира"),
    ]
    for person in people:
        make_enrollment(person.id, course.id)

    first_page = client.get(f"/admin/reports/{course.id}?page=1&per_page=2").json()
    second_page = client.get(f"/admin/reports/{course.id}?page=2&per_page=2").json()

    # По алфавиту: в отчёте на восемь сотен строк человека ищут по фамилии
    assert [item["last_name"] for item in first_page["participants"]["items"]] == [
        "Абдиева",
        "Смагулова",
    ]
    assert [item["last_name"] for item in second_page["participants"]["items"]] == ["Ярова"]
    assert second_page["participants"]["total"] == 3
    assert (second_page["participants"]["page"], second_page["participants"]["per_page"]) == (
        2,
        2,
    )
    # Сводка от страницы не зависит: она про весь курс
    assert first_page["summary"]["granted"] == second_page["summary"]["granted"] == 3

    found = client.get(f"/admin/reports/{course.id}?q=смаг").json()["participants"]
    assert found["total"] == 1
    assert found["items"][0]["last_name"] == "Смагулова"

    assert client.get(f"/admin/reports/{course.id}?q=Мукашев").json()["participants"] == {
        "items": [],
        "total": 0,
        "page": 1,
        "per_page": 20,
    }


def test_participant_row_carries_school_region_and_quiz_scores(client, sms):
    login_admin(client, sms)
    course, lesson_one, lesson_two, _, quiz, final = report_course()
    quiz_questions = two_questions(quiz)
    final_questions = two_questions(final)
    passed, failed, running, idle = (teacher(n) for n in range(1, 5))
    for person in (passed, failed, running, idle):
        make_enrollment(person.id, course.id)

    attempt(passed.id, quiz, quiz_questions, score=2)
    attempt(passed.id, final, final_questions, score=2)
    attempt(failed.id, quiz, quiz_questions, score=1, passed=False)
    attempt(failed.id, final, final_questions, score=1, passed=False)
    # Незавершённая попытка зачётной не помечена — такой её заводит пересдача
    attempt(running.id, final, final_questions, score=None, finished=False, counted=False)

    rows = by_user(client.get(f"/admin/reports/{course.id}").json())

    assert rows[passed.id]["school"] == "КГУ «Средняя школа №27»"
    assert rows[passed.id]["region"] == "Алматы"
    assert rows[passed.id]["module_quizzes"] == [
        {"quiz_id": quiz.id, "title": "Тест модуля", "score": 100}
    ]
    assert rows[passed.id]["final_quiz"] == {"state": "passed", "score": 100}
    # Провал балл показывает: «не сдавал» — это null, а не ноль
    assert rows[failed.id]["module_quizzes"][0]["score"] == 50
    assert rows[failed.id]["final_quiz"] == {"state": "failed", "score": 50}
    assert rows[running.id]["final_quiz"] == {"state": "in_progress", "score": None}
    assert rows[idle.id]["final_quiz"] == {"state": "not_started", "score": None}
    assert rows[idle.id]["module_quizzes"][0]["score"] is None


def test_certificate_state_repeats_the_checklist(client, sms):
    login_admin(client, sms)
    # Условие одно — пройти все уроки: чек-лист сертификата задаётся курсом
    course, lesson_one, lesson_two, *_ = report_course(cert_require_lessons=True)
    issued, ready, in_progress = (teacher(n) for n in range(1, 4))
    for person in (issued, ready, in_progress):
        make_enrollment(person.id, course.id)
        make_progress(person.id, lesson_one.id)
    make_progress(issued.id, lesson_two.id)
    make_progress(ready.id, lesson_two.id)
    make_certificate(issued.id, course.id)

    rows = by_user(client.get(f"/admin/reports/{course.id}").json())

    assert rows[issued.id]["certificate"] == "issued"
    assert rows[ready.id]["certificate"] == "ready"
    assert rows[in_progress.id]["certificate"] == "in_progress"


def test_hidden_pass_keeps_the_report_row_ready(client, sms):
    """Отчёт читает тот же чек-лист, что и учитель на экране завершения:
    единственный урок курса прошли и потом сняли с программы — зачёт
    остаётся, и «готов к выдаче» не гаснет. Второй копии правил быть
    не должно: разойдётся — и админ прочитает это как ошибку."""
    login_admin(client, sms)
    course = make_course(title="Критериальное оценивание", cert_require_lessons=True)
    module = make_module(course.id, title="Модуль 1", order_index=1)
    removed = make_lesson(module.id, title="Снят с программы", order_index=1, is_hidden=True)
    person = teacher(1)
    make_enrollment(person.id, course.id)
    make_progress(person.id, removed.id)

    rows = by_user(client.get(f"/admin/reports/{course.id}").json())

    assert rows[person.id]["certificate"] == "ready"
    # Прогресс при этом считается по видимой программе, а в ней пусто
    assert rows[person.id]["progress_percent"] == 0


def test_report_page_does_not_scale_with_participants(client, sms):
    """Отчёт — самый тяжёлый экран админки, и участников на странице до сотни.
    Число запросов должно зависеть от размера курса, а не от числа строк:
    условия сертификата считаются на каждую строку, и поход в базу на строку
    превращает страницу в шесть сотен запросов."""
    from sqlalchemy import event

    from app.adapters.db.base import get_engine

    def queries_for(participants: int, first_number: int) -> int:
        course, lesson, *_ = report_course()
        for number in range(first_number, first_number + participants):
            member = teacher(number)
            make_enrollment(member.id, course.id)
            make_progress(member.id, lesson.id)

        counted = []
        engine = get_engine()

        def count(*args):
            counted.append(1)

        event.listen(engine, "before_cursor_execute", count)
        try:
            resp = client.get(f"/admin/reports/{course.id}")
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert resp.status_code == 200
        assert resp.json()["participants"]["total"] == participants
        return len(counted)

    login_admin(client, sms)
    few = queries_for(3, first_number=0)
    many = queries_for(13, first_number=100)
    # Десять лишних участников не должны стоить ни одного лишнего запроса
    assert many == few, f"{few} запросов на 3 участника, {many} на 13"
