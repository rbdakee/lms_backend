from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.adapters.db.repos import now_utc
from tests.conftest import (
    login,
    login_admin,
    make_course,
    make_enrollment,
    user_id,
)

TEACHER_PHONE = "+7 (707) 123-45-67"


def _teacher_with_lead(client, sms, course):
    """Учитель с заполненным профилем и заявкой на курс."""
    login(client, sms, TEACHER_PHONE)
    client.patch("/me", json={"last_name": "Нурланова", "first_name": "Айгуль",
                              "region": "Алматы", "school": "Школа №1",
                              "subject": "Математика"})
    lead = client.post(f"/courses/{course.id}/lead").json()
    return user_id(client), lead


def test_admin_leads_forbidden_for_teacher(client, sms):
    login(client, sms)
    resp = client.get("/admin/leads")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert client.patch("/admin/leads/1", json={"note": "x"}).status_code == 403
    assert client.post(
        "/admin/enrollments", json={"user_id": 1, "course_id": 1, "paid": True}
    ).status_code == 403


def test_admin_leads_list_and_filters(client, client2, sms):
    course = make_course(title="Курс А", price=45000)
    other = make_course(title="Курс Б")
    _teacher_with_lead(client, sms, course)
    client.post(f"/courses/{other.id}/lead")

    login_admin(client2, sms)
    body = client2.get("/admin/leads").json()
    assert body["total"] == 2
    assert body["page"] == 1

    # Новые сверху: вторая заявка первой в списке
    item = body["items"][1]
    assert item["status"] == "new"
    assert item["price_snapshot"] == 45000
    assert item["waiting_days"] == 0
    assert item["reminded_at"] is None
    assert item["teacher"]["last_name"] == "Нурланова"
    assert item["teacher"]["phone"] == "+77071234567"
    assert item["teacher"]["region"] == "Алматы"
    assert item["course"] == {"id": course.id, "lang": "ru", "title": "Курс А",
                              "price": 45000}

    assert client2.get(f"/admin/leads?course_id={course.id}").json()["total"] == 1
    assert client2.get("/admin/leads?status=open").json()["total"] == 2
    assert client2.get("/admin/leads?status=declined").json()["total"] == 0
    # Поиск по ФИО и по телефону в любом виде — «8 707…» находит +7707…
    assert client2.get("/admin/leads?q=Нурланова").json()["total"] == 2
    assert client2.get("/admin/leads?q=8 (707) 123").json()["total"] == 2
    assert client2.get("/admin/leads?q=Иванов").json()["total"] == 0


def test_admin_patch_lead(client, client2, sms):
    course = make_course()
    _, lead = _teacher_with_lead(client, sms, course)

    login_admin(client2, sms)
    resp = client2.patch(f"/admin/leads/{lead['id']}",
                         json={"status": "declined", "note": "Не дозвонились"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "declined"
    assert body["note"] == "Не дозвонились"
    # Закрытая заявка не ждёт
    assert body["waiting_days"] == 0

    assert client2.patch("/admin/leads/999999", json={"note": "x"}).status_code == 404


def test_admin_patch_granted_rejected(client, client2, sms):
    course = make_course()
    _, lead = _teacher_with_lead(client, sms, course)

    login_admin(client2, sms)
    resp = client2.patch(f"/admin/leads/{lead['id']}", json={"status": "granted"})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert "POST /admin/enrollments" in error["message"]
    # Статус не поменялся
    assert client2.get("/admin/leads").json()["items"][0]["status"] == "new"


def test_grant_enrollment(client, client2, sms):
    course = make_course(title="Курс")
    teacher_id, lead = _teacher_with_lead(client, sms, course)

    login_admin(client2, sms)
    resp = client2.post("/admin/enrollments", json={
        "user_id": teacher_id, "course_id": course.id,
        "paid": True, "note": "Kaspi, перевод от 16.08",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == teacher_id
    assert body["course_id"] == course.id
    assert body["paid"] is True
    assert body["note"] == "Kaspi, перевод от 16.08"

    # Заявка закрыта выдачей
    assert client2.get("/admin/leads").json()["items"][0]["status"] == "granted"
    # Учителю — колокольчик со ссылкой на курс, без хранимого текста
    with get_engine().connect() as conn:
        n_type, params, n_user = conn.execute(
            text("SELECT type, params, user_id FROM notification")
        ).one()
    assert n_type == "access_granted"
    assert params == {"course_id": course.id}
    assert n_user == teacher_id
    # Курс появился в кабинете учителя
    assert client.get("/me/courses").json()["items"][0]["id"] == course.id
    assert client.get("/me/courses").json()["leads"] == []


def test_grant_unpaid_note_goes_to_lead(client, client2, sms):
    course = make_course()
    teacher_id, lead = _teacher_with_lead(client, sms, course)

    login_admin(client2, sms)
    body = client2.post("/admin/enrollments", json={
        "user_id": teacher_id, "course_id": course.id,
        "paid": False, "note": "Оплатит после зарплаты",
    }).json()
    assert body["paid"] is False
    assert body["note"] is None

    item = client2.get("/admin/leads").json()["items"][0]
    assert item["status"] == "granted"
    assert item["note"] == "Оплатит после зарплаты"


def test_grant_twice_409(client, client2, sms):
    course = make_course()
    teacher_id, _ = _teacher_with_lead(client, sms, course)

    login_admin(client2, sms)
    payload = {"user_id": teacher_id, "course_id": course.id, "paid": True}
    assert client2.post("/admin/enrollments", json=payload).status_code == 200
    resp = client2.post("/admin/enrollments", json=payload)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "already_enrolled"


def test_grant_after_revoke_reuses_row(client, client2, sms):
    course = make_course()
    login(client, sms, TEACHER_PHONE)
    teacher_id = user_id(client)
    enrollment = make_enrollment(teacher_id, course.id, revoked_at=now_utc())

    login_admin(client2, sms)
    resp = client2.post("/admin/enrollments", json={
        "user_id": teacher_id, "course_id": course.id, "paid": True,
    })
    assert resp.status_code == 200
    assert resp.json()["id"] == enrollment.id

    with get_engine().connect() as conn:
        revoked_at, paid_note = conn.execute(
            text("SELECT revoked_at, paid_note FROM enrollment")
        ).one()
    assert revoked_at is None
    assert paid_note == ""


def test_grant_404_for_missing_user_or_hidden_course(client2, sms):
    hidden = make_course(status="hidden")
    login_admin(client2, sms)
    assert client2.post("/admin/enrollments", json={
        "user_id": 999999, "course_id": hidden.id, "paid": True,
    }).status_code == 404

    course = make_course()
    admin_id = user_id(client2)
    assert client2.post("/admin/enrollments", json={
        "user_id": admin_id, "course_id": hidden.id, "paid": True,
    }).status_code == 404
    assert client2.post("/admin/enrollments", json={
        "user_id": 999999, "course_id": course.id, "paid": True,
    }).status_code == 404
