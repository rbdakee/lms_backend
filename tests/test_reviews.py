from tests.conftest import login, make_course, make_enrollment, user_id


def test_reviews_empty(client):
    course = make_course()
    body = client.get(f"/courses/{course.id}/reviews").json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["rating"] is None
    assert body["breakdown"] == {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0}


def test_review_requires_enrollment(client, sms):
    course = make_course()
    login(client, sms)
    resp = client.post(f"/courses/{course.id}/reviews", json={"rating": 5, "text": "Отлично"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_review_requires_auth(client):
    course = make_course()
    resp = client.post(f"/courses/{course.id}/reviews", json={"rating": 5})
    assert resp.status_code == 401


def test_review_visible_immediately_with_author(client, sms):
    course = make_course()
    login(client, sms)
    client.patch("/me", json={"last_name": "Нурланова", "first_name": "Айгуль",
                              "school": "Школа №1", "city": "Алматы"})
    make_enrollment(user_id(client), course.id)

    resp = client.post(f"/courses/{course.id}/reviews", json={"rating": 5, "text": "Спасибо"})
    assert resp.status_code == 200
    created = resp.json()
    assert created["author_name"] == "Нурланова Айгуль"
    assert created["school"] == "Школа №1"
    assert created["city"] == "Алматы"
    assert created["reply"] is None

    # Премодерации нет — отзыв виден сразу
    body = client.get(f"/courses/{course.id}/reviews").json()
    assert body["total"] == 1
    assert body["items"][0]["text"] == "Спасибо"


def test_rating_counts_last_review_per_author(client, client2, sms):
    course = make_course()
    login(client, sms)
    login(client2, sms, "+7 (707) 765-43-21")
    make_enrollment(user_id(client), course.id)
    make_enrollment(user_id(client2), course.id)

    # Автор передумал: в среднюю идёт только его последний отзыв (5, а не 2)
    client.post(f"/courses/{course.id}/reviews", json={"rating": 2, "text": "Сначала не то"})
    client.post(f"/courses/{course.id}/reviews", json={"rating": 5, "text": "Разобрался"})
    client2.post(f"/courses/{course.id}/reviews", json={"rating": 4})

    body = client.get(f"/courses/{course.id}/reviews").json()
    assert body["total"] == 3  # в ленте все строки
    assert body["rating"] == 4.5  # (5 + 4) / 2
    assert body["breakdown"] == {"5": 1, "4": 1, "3": 0, "2": 0, "1": 0}

    group = client.get("/courses").json()["items"][0]
    assert group["rating"] == 4.5
    assert group["reviews_count"] == 2


def test_reviews_of_hidden_course_404(client):
    course = make_course(status="hidden")
    assert client.get(f"/courses/{course.id}/reviews").status_code == 404


def test_review_validates_rating(client, sms):
    course = make_course()
    login(client, sms)
    make_enrollment(user_id(client), course.id)
    resp = client.post(f"/courses/{course.id}/reviews", json={"rating": 6})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
