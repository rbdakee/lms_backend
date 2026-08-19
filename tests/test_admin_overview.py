from datetime import timedelta

from app.adapters.db.repos import now_utc
from tests.conftest import (
    login,
    login_admin,
    make_certificate,
    make_course,
    make_lead,
    make_lesson,
    make_module,
    make_submission,
    make_task,
    make_thread_message,
    make_user,
)


def teacher(number: int, **kw):
    """Учитель с вымышленным номером: дашборду их нужно много, и заводить
    каждого через SMS — лишний шум."""
    return make_user(f"+7701000{number:04d}", **kw)


def days_ago(days: int):
    return now_utc() - timedelta(days=days)


# -- права --------------------------------------------------------------


def test_overview_requires_admin(client, sms):
    assert client.get("/admin/overview").status_code == 401

    login(client, sms)
    resp = client.get("/admin/overview")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


# -- пустой дашборд ------------------------------------------------------


def test_empty_overview_is_zeros_not_error(client, sms):
    login_admin(client, sms)

    assert client.get("/admin/overview").json() == {
        "leads_count": 0,
        "submissions_count": 0,
        "questions_count": 0,
        "leads": [],
        "submissions": [],
        "questions": [],
        # Сам админ учителем не считается
        "totals": {"teachers": 0, "courses_published": 0, "certificates": 0},
    }


# -- счётчики и списки ---------------------------------------------------


def test_counters_repeat_queue_totals(client, sms):
    login_admin(client, sms)
    course = make_course(title="Критериальное оценивание")
    module = make_module(course.id)
    lesson = make_lesson(module.id, title="Критерии и дескрипторы", order_index=1)
    task = make_task(module.id, title="Составьте дескрипторы", order_index=2)
    first, second = teacher(1), teacher(2)

    make_lead(first.id, course.id, created_at=days_ago(4))
    # В работе, а не новая: плитка считает только новые
    make_lead(second.id, course.id, status="contacted")
    make_submission(first.id, task.id, created_at=days_ago(3))
    make_submission(second.id, task.id, status="accepted", reviewed_at=now_utc())
    make_thread_message(lesson.id, course.id, first.id, created_at=days_ago(2))
    answered = make_thread_message(lesson.id, course.id, second.id, text="Уже ответили")
    make_thread_message(lesson.id, course.id, first.id, text="Ответ", parent_id=answered.id)

    body = client.get("/admin/overview").json()

    assert body["leads_count"] == 1
    assert body["submissions_count"] == 1
    assert body["questions_count"] == 1
    # Те же числа, что админка тянула отдельными запросами за total
    assert body["leads_count"] == client.get("/admin/leads?status=new").json()["total"]
    assert (
        body["submissions_count"]
        == client.get("/admin/submissions?status=pending").json()["total"]
    )
    assert (
        body["questions_count"] == client.get("/admin/questions?answered=false").json()["total"]
    )

    lead = body["leads"][0]
    assert lead["waiting_days"] == 4
    assert lead["price_snapshot"] == 45000
    assert lead["teacher"]["last_name"] == "Смагулова"
    assert lead["course"] == {"id": course.id, "title": course.title}

    submission = body["submissions"][0]
    assert submission["waiting_days"] == 3
    assert submission["task"] == {"id": task.id, "title": task.title}

    question = body["questions"][0]
    assert question["text"] == "Вопрос по уроку"
    assert question["lesson"] == {
        "id": lesson.id,
        "number": 1,
        "title": "Критерии и дескрипторы",
    }


def test_lists_are_five_newest(client, sms):
    login_admin(client, sms)
    course = make_course()
    module = make_module(course.id)
    lesson = make_lesson(module.id)
    task = make_task(module.id)
    leads, submissions, questions = [], [], []
    for day in range(7):
        # Работа у каждого своя: вторую «на проверке» по той же паре
        # учитель+задание не пускает частичный уникальный индекс
        person = teacher(day)
        leads.append(make_lead(person.id, course.id, created_at=days_ago(day)).id)
        submissions.append(make_submission(person.id, task.id, created_at=days_ago(day)).id)
        questions.append(
            make_thread_message(lesson.id, course.id, person.id, created_at=days_ago(day)).id
        )

    body = client.get("/admin/overview").json()

    assert body["leads_count"] == 7
    assert body["submissions_count"] == 7
    assert body["questions_count"] == 7
    # Свежие сверху и ровно пять — одинаково у всех трёх списков
    assert [item["id"] for item in body["leads"]] == leads[:5]
    assert [item["id"] for item in body["submissions"]] == submissions[:5]
    assert [item["id"] for item in body["questions"]] == questions[:5]
    assert [item["waiting_days"] for item in body["leads"]] == [0, 1, 2, 3, 4]
    assert [item["waiting_days"] for item in body["submissions"]] == [0, 1, 2, 3, 4]


def test_hidden_lesson_has_no_number_in_question(client, sms):
    login_admin(client, sms)
    course = make_course()
    module = make_module(course.id)
    make_lesson(module.id, title="Урок 1", order_index=1)
    hidden = make_lesson(module.id, title="Скрытый", order_index=2, is_hidden=True)
    visible = make_lesson(module.id, title="Урок 2", order_index=3)
    person = teacher(1)
    make_thread_message(hidden.id, course.id, person.id, created_at=days_ago(1))
    make_thread_message(visible.id, course.id, person.id)

    body = client.get("/admin/overview").json()

    # Скрытый урок в нумерации не участвует: у него номера нет, а следующий
    # видимый идёт вторым, а не третьим
    assert [item["lesson"]["number"] for item in body["questions"]] == [2, 0]


# -- справочные числа ----------------------------------------------------


def test_totals_count_teachers_published_courses_and_certificates(client, sms):
    login_admin(client, sms)
    published = make_course(status="open")
    make_course(status="planned")
    make_course(status="draft")
    make_course(status="hidden")
    blocked = teacher(1, is_blocked=True)
    active = teacher(2)
    make_certificate(active.id, published.id)
    make_certificate(
        blocked.id, published.id, number="KZ-2026-AAAAAA", revoked_at=now_utc()
    )

    totals = client.get("/admin/overview").json()["totals"]

    # Заблокированный учителем быть не перестал; отозванный сертификат
    # не считается; черновик и скрытый курс в каталоге не видны
    assert totals == {"teachers": 2, "courses_published": 2, "certificates": 1}
