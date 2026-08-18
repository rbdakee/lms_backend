from app.adapters.db.models import Question
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


def test_catalog_empty(client):
    resp = client.get("/courses")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_catalog_groups_versions_and_hides_draft_hidden(client):
    make_course(group_id=100, lang="ru", title="Оценивание (рус)", status="open")
    make_course(group_id=100, lang="kz", title="Бағалау (қаз)", status="planned")
    make_course(status="draft")
    make_course(status="hidden")

    items = client.get("/courses").json()["items"]
    assert len(items) == 1
    group = items[0]
    assert group["group_id"] == 100
    assert group["langs"] == ["ru", "kz"]
    assert group["rating"] is None
    assert group["reviews_count"] == 0
    assert [v["lang"] for v in group["versions"]] == ["ru", "kz"]
    assert group["versions"][1]["status"] == "planned"


def test_catalog_counts_lessons_and_students(client, client2, sms):
    course = make_course()
    module = make_module(course.id)
    make_lesson(module.id)
    make_lesson(module.id)
    make_lesson(module.id, is_hidden=True)

    login(client, sms)
    login(client2, sms, "+7 (707) 765-43-21")
    make_enrollment(user_id(client), course.id)
    # Отозванный доступ в счётчик участников не входит
    make_enrollment(user_id(client2), course.id, revoked_at=now_utc())

    card = client.get("/courses").json()["items"][0]["versions"][0]
    assert card["lessons_count"] == 2
    assert card["students_count"] == 1


def test_course_page_program_and_chips(client):
    course = make_course(group_id=200, lang="ru", title="РУ", short="Коротко", full="Подробно")
    make_course(group_id=200, lang="kz", title="ҚАЗ", status="closed")
    make_course(status="draft")
    module = make_module(course.id, title="Модуль 1", order_index=1)
    lesson = make_lesson(module.id, title="Первый урок", order_index=1, duration_label="12:34")
    make_lesson(module.id, title="Скрытый", order_index=2, is_hidden=True)
    quiz = make_quiz(module.id, title="Тест модуля", order_index=3, time_limit_min=20)
    make_task(module.id, title="Практика", order_index=2, submit_format="both")
    empty_module = make_module(course.id, title="Модуль 2", order_index=2)

    seed(Question(quiz_id=quiz.id, type="single", text="Вопрос 1"))
    seed(Question(quiz_id=quiz.id, type="single", text="Скрытый", is_hidden=True))

    body = client.get(f"/courses/{course.id}").json()
    assert body["short"] == "Коротко"
    assert body["full"] == "Подробно"
    assert body["access"] == {"state": "none"}
    # Чипы — только версии, видимые в каталоге
    chips = [(v["lang"], v["status"]) for v in body["versions"]]
    assert chips == [("ru", "open"), ("kz", "closed")]

    program = body["program"]
    assert [m["title"] for m in program] == ["Модуль 1", "Модуль 2"]
    items = program[0]["items"]
    # Скрытый урок не показан, порядок по order_index, типы вперемешку
    assert [i["kind"] for i in items] == ["video", "task", "quiz"]
    assert items[0]["duration_label"] == "12:34"
    assert items[0]["id"] == lesson.id
    assert items[1]["submit_format"] == "both"
    quiz_item = items[2]
    assert quiz_item["questions_count"] == 1
    assert quiz_item["pass_score"] == 70
    assert quiz_item["time_limit_min"] == 20
    assert quiz_item["is_final"] is False
    assert program[1]["items"] == []
    assert empty_module.id == program[1]["id"]


def test_course_page_404_for_draft_hidden_even_for_admin(client, sms):
    draft = make_course(status="draft")
    hidden = make_course(status="hidden")
    assert client.get(f"/courses/{draft.id}").status_code == 404
    assert client.get("/courses/999999").status_code == 404

    login_admin(client, sms)
    resp = client.get(f"/courses/{hidden.id}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_course_page_access_states(client, sms):
    course = make_course()
    login(client, sms)

    # Заявки нет — none
    assert client.get(f"/courses/{course.id}").json()["access"] == {"state": "none"}

    client.post(f"/courses/{course.id}/lead")
    access = client.get(f"/courses/{course.id}").json()["access"]
    assert access == {"state": "requested", "waiting_days": 0}

    module = make_module(course.id)
    first = make_lesson(module.id, title="Первый", order_index=1)
    second = make_lesson(module.id, title="Второй", order_index=2)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_progress(uid, first.id)

    access = client.get(f"/courses/{course.id}").json()["access"]
    assert access["state"] == "granted"
    assert access["done_count"] == 1
    assert access["total_count"] == 2
    assert access["progress_percent"] == 50
    assert access["next_lesson"] == {"id": second.id, "title": "Второй", "kind": "video"}

    make_progress(uid, second.id)
    access = client.get(f"/courses/{course.id}").json()["access"]
    assert access["progress_percent"] == 100
    assert access["next_lesson"] is None


def test_group_rating_shared_between_versions(client, sms):
    ru = make_course(group_id=300, lang="ru")
    kz = make_course(group_id=300, lang="kz")
    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, ru.id)
    client.post(f"/courses/{ru.id}/reviews", json={"rating": 4, "text": "Хороший курс"})

    # Оценка русской версии видна и на казахской странице — рейтинг групповой
    body = client.get(f"/courses/{kz.id}").json()
    assert body["rating"] == 4.0
    assert body["reviews_count"] == 1

    group = client.get("/courses").json()["items"][0]
    assert group["rating"] == 4.0
    assert group["reviews_count"] == 1
