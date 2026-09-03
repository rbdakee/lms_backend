"""Вопросы под уроком и колокольчик — ленты своей площадки.

Курс у площадок общий, а разговор под уроком и уведомления — нет
(PLATFORMS_BRIEF, решение 3 и раздел «База»): человек, проходящий курс
на обеих, задаёт вопросы в двух лентах, и одна другую не видит.
У уведомления площадка решает, на какой домен ведёт ссылка, поэтому
на первом сайте нечего показывать про второй.

Опасность та же, что и во всей учебной части: отдельной базы у площадок нет,
есть колонка `platform` и то, что её всюду спрашивают. Поэтому у каждой
проверки есть обратная половина — на своей площадке та же строка видна,
иначе тест зеленел бы и на пустой выдаче.

Площадка задаётся так же, как её присылает браузер, — заголовком `Origin`.
"""

import pytest

from app.config import get_settings
from tests.conftest import (
    login,
    login_admin,
    login_named,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_notification,
    user_id,
)

# Свои источники, а не из `.env`: у разработчика там localhost с портами
P1_ORIGIN = "https://first.example.kz"
P2_ORIGIN = "https://second.example.kz"
P1 = {"Origin": P1_ORIGIN}
P2 = {"Origin": P2_ORIGIN}


@pytest.fixture(autouse=True)
def platform_origins(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "platform_origins", {P1_ORIGIN: "p1", P2_ORIGIN: "p2"}
    )


def common_lesson(client, sms):
    """Урок общего курса и доступ к нему на обеих площадках: вопросы под ним
    задаются из двух каталогов, а урок один."""
    course = make_course(title="Критериальное оценивание", platforms={"p1": 45000, "p2": 60000})
    lesson = make_lesson(make_module(course.id).id, title="Критерии и дескрипторы")
    login_named(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_enrollment(uid, course.id, platform="p2")
    return uid, lesson


def ask(client, lesson_id, text_, headers, parent_id=None):
    resp = client.post(
        f"/lessons/{lesson_id}/questions",
        json={"text": text_, "parent_id": parent_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def feed(client, lesson_id, headers):
    resp = client.get(f"/lessons/{lesson_id}/questions", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def bell(client, headers):
    resp = client.get("/notifications", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# -- вопросы под уроком ------------------------------------------------


def test_a_question_from_one_platform_is_not_in_the_feed_of_the_other(client, sms):
    """Две ленты под одним уроком: у каждой свои строки и свой `total` —
    иначе пагинация обещает страницу, которой на этой площадке нет."""
    _, lesson = common_lesson(client, sms)
    first = ask(client, lesson.id, "Дескрипторы на 34 человека реально успеть?", P1)
    second = ask(client, lesson.id, "Где взять примеры по естествознанию?", P2)

    on_p1, on_p2 = feed(client, lesson.id, P1), feed(client, lesson.id, P2)
    assert [item["id"] for item in on_p1["items"]] == [first["id"]]
    assert [item["id"] for item in on_p2["items"]] == [second["id"]]
    assert (on_p1["total"], on_p2["total"]) == (1, 1)


def test_an_admin_reply_lands_in_the_thread_where_the_question_was_asked(
    client, client2, sms
):
    """Админ отвечает из общей очереди, и его `Origin` площадки не несёт:
    ответ ложится на площадку своего вопроса, а не запроса. Иначе тред
    разъехался бы по двум лентам, и спросивший ответа не увидел бы."""
    _, lesson = common_lesson(client, sms)
    question = ask(client, lesson.id, "Где взять примеры по естествознанию?", P2)

    login_admin(client2, sms)
    # Без Origin — так ходит админка: площадка запроса у неё первая
    reply = ask(client2, lesson.id, "В методичке к модулю 2.", {}, parent_id=question["id"])

    on_p2 = feed(client, lesson.id, P2)
    assert [item["id"] for item in on_p2["items"]] == [question["id"]]
    assert [answer["id"] for answer in on_p2["items"][0]["replies"]] == [reply["id"]]
    assert feed(client, lesson.id, P1) == {
        "items": [],
        "total": 0,
        "page": 1,
        "per_page": 20,
    }

    # Уведомление об ответе уходит туда же: ссылка из него ведёт на тот домен,
    # где вопрос задан
    assert [item["type"] for item in bell(client, P2)["items"]] == ["answer_posted"]
    assert bell(client, P1)["items"] == []


# -- колокольчик -------------------------------------------------------


def test_a_notification_of_one_platform_stays_out_of_the_other_bell(client, sms):
    login(client, sms)
    uid = user_id(client)
    on_p1 = make_notification(uid)
    on_p2 = make_notification(
        uid,
        platform="p2",
        params={"course_id": 2, "course_title": "Цифровые инструменты урока"},
    )

    first, second = bell(client, P1), bell(client, P2)
    assert [item["id"] for item in first["items"]] == [on_p1.id]
    assert [item["id"] for item in second["items"]] == [on_p2.id]
    assert (first["total"], second["total"]) == (1, 1)
    # Счётчик считается по тем же строкам, что и список: иначе на колокольчике
    # горит число, а в списке под ним пусто
    assert (first["unread_count"], second["unread_count"]) == (1, 1)


def test_read_all_on_one_platform_leaves_the_other_unread(client, sms):
    """«Отметить все как прочитанные» — кнопка своего сайта: соседний
    колокольчик она не гасит."""
    login(client, sms)
    uid = user_id(client)
    make_notification(uid)
    make_notification(uid, platform="p2")

    assert (
        client.post("/notifications/read", json={"all": True}, headers=P1).status_code == 204
    )

    assert bell(client, P1)["unread_count"] == 0
    on_p2 = bell(client, P2)
    assert on_p2["unread_count"] == 1
    assert on_p2["items"][0]["read_at"] is None


def test_an_id_from_another_platform_is_not_marked_read(client, sms):
    """Присланный id соседней площадки — то же самое, что чужой или
    несуществующий: 204 и ничего не изменилось. Список у человека мог
    устареть, и отметка «прочитано» — не место для ошибки."""
    login(client, sms)
    uid = user_id(client)
    on_p2 = make_notification(uid, platform="p2")

    resp = client.post("/notifications/read", json={"ids": [on_p2.id]}, headers=P1)
    assert resp.status_code == 204

    unread = bell(client, P2)
    assert unread["items"][0]["read_at"] is None
    assert unread["unread_count"] == 1
    # На своей площадке тот же id гасится — иначе проверка зеленела бы
    # на сломанной отметке
    assert (
        client.post("/notifications/read", json={"ids": [on_p2.id]}, headers=P2).status_code
        == 204
    )
    assert bell(client, P2)["unread_count"] == 0
