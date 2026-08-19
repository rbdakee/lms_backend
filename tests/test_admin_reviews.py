from sqlalchemy import text

from app.adapters.db.base import get_engine
from tests.conftest import (
    login,
    login_admin,
    make_course,
    make_enrollment,
    make_review,
    make_user,
    user_id,
)


def teacher(number: int, **kw):
    """Автор отзыва с вымышленным номером: настоящих в тестах не бывает."""
    fields = {
        "last_name": "Нурланова",
        "first_name": "Айгуль",
        "middle_name": "Сериковна",
        "school": "КГУ «Школа-лицей №27»",
        "city": "Алматы",
    }
    fields.update(kw)
    return make_user(f"+7703000{number:04d}", **fields)


def deleted_row(review_id: int):
    """Отметка мягкого удаления: в ответах её нет, а проверить нужно и то,
    кто удалил, и что повторный вызов не двигает время."""
    with get_engine().begin() as conn:
        return conn.execute(
            text("SELECT deleted_at, deleted_by FROM review WHERE id = :id"),
            {"id": review_id},
        ).one()


# -- права ---------------------------------------------------------------


def test_reviews_require_admin(client, sms):
    course = make_course()
    review = make_review(teacher(1).id, course.id)

    def statuses() -> list[int]:
        return [
            client.get("/admin/reviews").status_code,
            client.post(f"/admin/reviews/{review.id}/reply", json={"text": "Спасибо"}).status_code,
            client.delete(f"/admin/reviews/{review.id}").status_code,
        ]

    assert statuses() == [401] * 3

    login(client, sms)
    assert statuses() == [403] * 3
    assert client.get("/admin/reviews").json()["error"]["code"] == "forbidden"


def test_404_for_unknown_review(client, sms):
    login_admin(client, sms)
    resp = client.post("/admin/reviews/999999/reply", json={"text": "Спасибо"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert client.delete("/admin/reviews/999999").status_code == 404


# -- лента ---------------------------------------------------------------


def test_empty_feed_is_not_an_error(client, sms):
    login_admin(client, sms)
    assert client.get("/admin/reviews").json() == {
        "items": [],
        "total": 0,
        "page": 1,
        "per_page": 20,
    }


def test_feed_row_has_course_author_and_reply(client, sms):
    login_admin(client, sms)
    course = make_course(title="Критериальное оценивание в начальной школе")
    author = teacher(1)
    review = make_review(author.id, course.id)
    client.post(
        f"/admin/reviews/{review.id}/reply",
        json={"text": "Спасибо! Второй модуль как раз про разговор с родителями."},
    )

    body = client.get("/admin/reviews").json()
    assert body["items"][0].pop("created_at")
    assert body["items"][0]["reply"].pop("created_at")
    assert body == {
        "items": [
            {
                "id": review.id,
                "rating": 5,
                "text": "Наконец-то понятно, как объяснять оценки родителям.",
                # Правки отзыва учителем в продукте нет — поле остаётся пустым
                "updated_at": None,
                "course": {
                    "id": course.id,
                    "lang": "ru",
                    "title": "Критериальное оценивание в начальной школе",
                },
                "teacher": {
                    "id": author.id,
                    "last_name": "Нурланова",
                    "first_name": "Айгуль",
                    "middle_name": "Сериковна",
                    "school": "КГУ «Школа-лицей №27»",
                    "city": "Алматы",
                },
                "reply": {
                    "text": "Спасибо! Второй модуль как раз про разговор с родителями."
                },
            }
        ],
        "total": 1,
        "page": 1,
        "per_page": 20,
    }


def test_feed_filters_by_course_and_rating(client, sms):
    login_admin(client, sms)
    first, second = make_course(title="Оценивание"), make_course(title="Цифровые инструменты")
    author = teacher(1)
    good = make_review(author.id, first.id, rating=5)
    bad = make_review(author.id, first.id, rating=2, text="Мало практики")
    other = make_review(author.id, second.id, rating=2, text="Не про мой предмет")

    def found(query: str) -> list[int]:
        return [item["id"] for item in client.get(f"/admin/reviews?{query}").json()["items"]]

    assert found(f"course_id={first.id}") == [bad.id, good.id]
    assert found("rating=2") == [other.id, bad.id]
    assert found(f"course_id={first.id}&rating=2") == [bad.id]


def test_feed_shows_reviews_of_a_hidden_course(client, sms):
    """Видимость курса лента не проверяет: отзыв разбирают и по курсу,
    который со страницы уже снят."""
    login_admin(client, sms)
    course = make_course(status="hidden")
    review = make_review(teacher(1).id, course.id)

    assert [item["id"] for item in client.get("/admin/reviews").json()["items"]] == [review.id]


# -- ответ администратора -------------------------------------------------


def test_reply_is_visible_on_the_course_page(client, client2, sms):
    login_admin(client, sms)
    course = make_course()
    review = make_review(teacher(1).id, course.id)

    resp = client.post(f"/admin/reviews/{review.id}/reply", json={"text": "Спасибо!"})
    assert resp.status_code == 200
    assert resp.json()["reply"]["text"] == "Спасибо!"

    # Ответ виден всем: страница курса публичная
    public = client2.get(f"/courses/{course.id}/reviews").json()["items"][0]
    assert public["reply"]["created_at"] == resp.json()["reply"]["created_at"]
    assert public["reply"]["text"] == "Спасибо!"


def test_second_reply_replaces_the_first(client, sms):
    """Отдельной ручки правки нет: на экране это кнопка «Изменить ответ»,
    и ответ у отзыва остаётся один."""
    login_admin(client, sms)
    course = make_course()
    review = make_review(teacher(1).id, course.id)

    client.post(f"/admin/reviews/{review.id}/reply", json={"text": "Первый ответ"})
    resp = client.post(f"/admin/reviews/{review.id}/reply", json={"text": "Второй ответ"})
    assert resp.json()["reply"]["text"] == "Второй ответ"

    items = client.get(f"/courses/{course.id}/reviews").json()["items"]
    assert len(items) == 1
    assert items[0]["reply"]["text"] == "Второй ответ"


def test_reply_rejects_blank_and_too_long_text(client, sms):
    login_admin(client, sms)
    review = make_review(teacher(1).id, make_course().id)

    blank = client.post(f"/admin/reviews/{review.id}/reply", json={"text": "   "})
    assert blank.status_code == 422
    assert blank.json()["error"]["details"]["fields"][0]["field"] == "text"

    long = client.post(f"/admin/reviews/{review.id}/reply", json={"text": "я" * 2001})
    assert long.status_code == 422
    assert long.json()["error"]["details"]["fields"][0]["field"] == "text"


def test_reply_to_deleted_review_404(client, sms):
    login_admin(client, sms)
    review = make_review(teacher(1).id, make_course().id)
    assert client.delete(f"/admin/reviews/{review.id}").status_code == 204

    resp = client.post(f"/admin/reviews/{review.id}/reply", json={"text": "Спасибо"})
    assert resp.status_code == 404


# -- удаление -------------------------------------------------------------


def test_delete_removes_review_from_feed_page_and_rating(client, client2, sms):
    """Числитель и знаменатель считают по одним строкам: удалённый отзыв
    уходит и из ленты, и со страницы курса, и из средней с разбивкой,
    и из карточки каталога."""
    login_admin(client, sms)
    course = make_course()
    kept = make_review(teacher(1).id, course.id, rating=5)
    removed = make_review(teacher(2).id, course.id, rating=1, text="Не понравилось")

    before = client2.get(f"/courses/{course.id}/reviews").json()
    assert before["total"] == 2
    assert before["rating"] == 3.0

    assert client.delete(f"/admin/reviews/{removed.id}").status_code == 204

    assert [item["id"] for item in client.get("/admin/reviews").json()["items"]] == [kept.id]
    after = client2.get(f"/courses/{course.id}/reviews").json()
    assert [item["id"] for item in after["items"]] == [kept.id]
    assert after["total"] == 1
    assert after["rating"] == 5.0
    assert after["breakdown"] == {"5": 1, "4": 0, "3": 0, "2": 0, "1": 0}

    group = client2.get("/courses").json()["items"][0]
    assert group["rating"] == 5.0
    assert group["reviews_count"] == 1


def test_deleted_review_does_not_come_back_by_filter(client, sms):
    login_admin(client, sms)
    course = make_course()
    review = make_review(teacher(1).id, course.id, rating=1)
    client.delete(f"/admin/reviews/{review.id}")

    for query in ("", f"?course_id={course.id}", "?rating=1"):
        assert client.get(f"/admin/reviews{query}").json() == {
            "items": [],
            "total": 0,
            "page": 1,
            "per_page": 20,
        }


def test_repeated_delete_keeps_the_first_time(client, sms):
    login_admin(client, sms)
    review = make_review(teacher(1).id, make_course().id)

    assert client.delete(f"/admin/reviews/{review.id}").status_code == 204
    first = deleted_row(review.id)
    assert first.deleted_by == user_id(client)

    assert client.delete(f"/admin/reviews/{review.id}").status_code == 204
    assert deleted_row(review.id) == first


def test_teacher_can_leave_a_new_review_after_deletion(client, client2, sms):
    """Удаление — это модерация одной строки, а не запрет человеку:
    доступ к курсу у него остался, и новый отзыв он оставит."""
    login_admin(client, sms)
    course = make_course()
    login(client2, sms)
    author = user_id(client2)
    make_enrollment(author, course.id)
    review = make_review(author, course.id)

    client.delete(f"/admin/reviews/{review.id}")
    resp = client2.post(f"/courses/{course.id}/reviews", json={"rating": 4, "text": "Ещё раз"})
    assert resp.status_code == 200
    assert client.get("/admin/reviews").json()["total"] == 1
