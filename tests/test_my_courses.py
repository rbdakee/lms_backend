from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.adapters.db.repos import now_utc
from tests.conftest import (
    login,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_progress,
    user_id,
)


def test_my_courses_requires_auth(client):
    assert client.get("/me/courses").status_code == 401


def test_my_courses_empty(client, sms):
    login(client, sms)
    assert client.get("/me/courses").json() == {"items": [], "leads": []}


def test_my_courses_with_progress(client, sms):
    course = make_course(title="Мой курс")
    module = make_module(course.id)
    first = make_lesson(module.id, title="Первый", order_index=1)
    second = make_lesson(module.id, title="Второй", order_index=2)
    make_lesson(module.id, title="Третий", order_index=3)

    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_progress(uid, first.id)

    body = client.get("/me/courses").json()
    assert len(body["items"]) == 1
    card = body["items"][0]
    assert card["title"] == "Мой курс"
    assert card["done_count"] == 1
    assert card["total_count"] == 3
    assert card["progress_percent"] == 33
    assert card["next_lesson"] == {"id": second.id, "title": "Второй", "kind": "video"}
    assert card["completed_at"] is None


def test_my_courses_hides_revoked(client, sms):
    course = make_course()
    login(client, sms)
    make_enrollment(user_id(client), course.id, revoked_at=now_utc())
    assert client.get("/me/courses").json()["items"] == []


def test_my_leads_keep_price_snapshot(client, sms):
    course = make_course(title="Курс с ценой", price=45000)
    login(client, sms)
    client.post(f"/courses/{course.id}/lead")

    # Цену курса подняли — заявка помнит свою
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE course SET price = 60000"))
        conn.execute(
            text("UPDATE lead SET created_at = created_at - make_interval(days => 4)")
        )

    leads = client.get("/me/courses").json()["leads"]
    assert len(leads) == 1
    lead = leads[0]
    assert lead["status"] == "new"
    assert lead["waiting_days"] == 4
    assert lead["course"]["title"] == "Курс с ценой"
    assert lead["course"]["price"] == 45000
