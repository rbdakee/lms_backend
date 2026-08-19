from sqlalchemy import text

from app.adapters.db.base import get_engine
from tests.conftest import login, make_notification, user_id


def make_bell(client, uid):
    """Три уведомления одного учителя: выдача доступа, вердикт по работе
    и ответ на вопрос. Возвращает их в порядке создания, снизу вверх."""
    granted = make_notification(
        uid, params={"course_id": 1, "course_title": "Критериальное оценивание"}
    )
    reviewed = make_notification(
        uid,
        type="submission_reviewed",
        params={
            "task_id": 1,
            "task_title": "Составьте дескрипторы к своему уроку",
            "course_id": 1,
            "verdict": "accepted",
        },
    )
    answered = make_notification(
        uid,
        type="answer_posted",
        params={
            "lesson_id": 2,
            "lesson_title": "Критерии и дескрипторы",
            "course_id": 1,
            "message_id": 18,
        },
    )
    return granted, reviewed, answered


def read_at_rows():
    """Состояние читается сырым SQL: read_at наружу отдаётся, но проверять
    «не переписано» надо именно в базе."""
    with get_engine().begin() as conn:
        return dict(
            conn.execute(text("SELECT id, read_at FROM notification ORDER BY id")).all()
        )


# -- доступ ------------------------------------------------------------


def test_notifications_require_auth(client):
    assert client.get("/notifications").status_code == 401
    assert client.post("/notifications/read", json={"all": True}).status_code == 401


def test_empty_bell(client, sms):
    login(client, sms)
    body = client.get("/notifications").json()
    assert body == {"items": [], "unread_count": 0, "total": 0, "page": 1, "per_page": 20}


# -- текст собирается при чтении ---------------------------------------


def test_list_is_newest_first_with_ready_text(client, sms):
    login(client, sms)
    make_bell(client, user_id(client))

    body = client.get("/notifications").json()
    assert body["total"] == 3
    assert body["unread_count"] == 3
    texts = [item["text"] for item in body["items"]]
    assert texts == [
        "На ваш вопрос к уроку «Критерии и дескрипторы» ответили",
        "Ваше задание «Составьте дескрипторы к своему уроку» зачтено",
        "Открыт доступ к курсу «Критериальное оценивание»",
    ]
    # params уходят рядом с текстом: из них фронт строит адрес перехода
    assert body["items"][0]["params"]["message_id"] == 18
    assert body["items"][0]["read_at"] is None


def test_same_type_reads_in_teacher_lang(client, sms):
    login(client, sms)
    uid = user_id(client)
    make_notification(
        uid,
        type="submission_reviewed",
        params={"task_id": 1, "task_title": "Дескрипторы", "course_id": 1, "verdict": "rework"},
    )
    ru = client.get("/notifications").json()["items"][0]["text"]
    assert ru == "Ваше задание «Дескрипторы» отправлено на доработку"

    # Тот же самый type и те же params — язык поменялся, текст обязан тоже
    client.patch("/me", json={"lang": "kz"})
    kz = client.get("/notifications").json()["items"][0]["text"]
    assert kz == "«Дескрипторы» тапсырмаңыз пысықтауға жіберілді"


def test_old_row_without_title_does_not_break_answer(client, sms):
    login(client, sms)
    uid = user_id(client)
    # Так писали до сессии 6: course_title в params не было
    make_notification(uid, params={"course_id": 7})

    body = client.get("/notifications").json()
    assert body["items"][0]["text"] == "Открыт доступ к курсу"
    client.patch("/me", json={"lang": "kz"})
    assert client.get("/notifications").json()["items"][0]["text"] == (
        "Курсқа қолжетімділік ашылды"
    )


# -- счётчик и страница ------------------------------------------------


def test_unread_count_comes_over_the_page(client, sms):
    login(client, sms)
    uid = user_id(client)
    granted, _, _ = make_bell(client, uid)

    client.post("/notifications/read", json={"ids": [granted.id]})
    # Панель колокольчика берёт per_page=4 и получает и список, и счётчик
    body = client.get("/notifications", params={"per_page": 2}).json()
    assert len(body["items"]) == 2
    assert (body["total"], body["unread_count"]) == (3, 2)


# -- отметка «прочитано» -----------------------------------------------


def test_read_one_then_list_then_all(client, sms):
    login(client, sms)
    uid = user_id(client)
    granted, reviewed, answered = make_bell(client, uid)

    assert client.post("/notifications/read", json={"ids": [granted.id]}).status_code == 204
    rows = read_at_rows()
    assert rows[granted.id] is not None
    assert rows[reviewed.id] is None

    client.post("/notifications/read", json={"ids": [reviewed.id, answered.id]})
    assert all(value is not None for value in read_at_rows().values())
    assert client.get("/notifications").json()["unread_count"] == 0

    make_notification(uid, type="certificate_issued",
                      params={"course_id": 1, "course_title": "Курс", "certificate_id": 3})
    assert client.post("/notifications/read", json={"all": True}).status_code == 204
    assert client.get("/notifications").json()["unread_count"] == 0


def test_foreign_id_is_skipped_silently(client, client2, sms):
    login(client, sms)
    login(client2, sms, "+7 (707) 765-43-21")
    mine = make_notification(user_id(client))
    stranger = make_notification(user_id(client2))

    # Список у учителя мог устареть — отметка «прочитано» не место для ошибки
    resp = client.post("/notifications/read", json={"ids": [stranger.id, mine.id, 999999]})
    assert resp.status_code == 204
    rows = read_at_rows()
    assert rows[mine.id] is not None
    assert rows[stranger.id] is None


def test_second_read_keeps_first_time(client, sms):
    login(client, sms)
    notification = make_notification(user_id(client))

    client.post("/notifications/read", json={"ids": [notification.id]})
    first = read_at_rows()[notification.id]
    client.post("/notifications/read", json={"all": True})
    assert read_at_rows()[notification.id] == first


def test_read_requires_exactly_one_field(client, sms):
    login(client, sms)
    notification = make_notification(user_id(client))

    both = client.post("/notifications/read", json={"ids": [notification.id], "all": True})
    assert both.status_code == 422
    assert both.json()["error"]["details"]["fields"][0]["field"] == "ids"

    neither = client.post("/notifications/read", json={})
    assert neither.status_code == 422
    # Ни одно поле не сработало — уведомление осталось непрочитанным
    assert read_at_rows()[notification.id] is None
