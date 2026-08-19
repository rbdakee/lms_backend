from tests.conftest import (
    login,
    login_admin,
    make_course,
    make_enrollment,
    make_review,
    make_user,
    user_id,
)


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


def test_reply_comes_as_an_object(client, client2, sms):
    """Ответ админа лежит у самого отзыва и приходит объектом
    {text, created_at}: до сессии 7б в этом поле всегда стоял null,
    потому что хранить ответ было негде. Имени отвечающего в нём нет —
    на экране он подписан просто «Администратор»."""
    course = make_course()
    author = make_user(
        "+77030000001",
        last_name="Нурланова",
        first_name="Айгуль",
        middle_name="Сериковна",
        school="КГУ «Школа-лицей №27»",
        city="Алматы",
    )
    review = make_review(author.id, course.id, rating=5, text="Стало понятнее")
    login_admin(client2, sms)
    client2.post(f"/admin/reviews/{review.id}/reply", json={"text": "Спасибо!"})

    body = client.get(f"/courses/{course.id}/reviews").json()
    assert body["items"][0].pop("created_at")
    assert body["items"][0]["reply"].pop("created_at")
    assert body["items"][0] == {
        "id": review.id,
        "author_name": "Нурланова Айгуль Сериковна",
        "school": "КГУ «Школа-лицей №27»",
        "city": "Алматы",
        "rating": 5,
        "text": "Стало понятнее",
        "reply": {"text": "Спасибо!"},
    }


def test_deleted_review_leaves_page_rating_and_breakdown(client, client2, sms):
    """Удалённый админом отзыв не приходит наружу нигде: ни строкой, ни
    в total, ни в средней, ни в разбивке по звёздам — числитель и знаменатель
    считают по одним и тем же строкам."""
    course = make_course()
    first = make_user("+77030000002")
    second = make_user("+77030000003")
    make_review(first.id, course.id, rating=5)
    removed = make_review(second.id, course.id, rating=1, text="Мало практики")

    login_admin(client2, sms)
    assert client2.delete(f"/admin/reviews/{removed.id}").status_code == 204

    body = client.get(f"/courses/{course.id}/reviews").json()
    assert [item["rating"] for item in body["items"]] == [5]
    assert body["total"] == 1
    assert body["rating"] == 5.0
    assert body["breakdown"] == {"5": 1, "4": 0, "3": 0, "2": 0, "1": 0}
