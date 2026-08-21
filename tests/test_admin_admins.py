"""Список администраторов и добавление нового — `/admin/admins`."""

from sqlalchemy import text

from app.adapters.db.base import get_engine
from tests.conftest import (
    ADMIN_NORM,
    login,
    login_admin,
    make_user,
    request_code,
)


def phones(client) -> list[str]:
    return [item["phone"] for item in client.get("/admin/admins").json()["items"]]


# -- права ---------------------------------------------------------------


def test_admins_require_admin(client, sms):
    """Телефоны админов — персональные данные: без прав их не видно
    и не добавить."""

    def statuses() -> list[int]:
        return [
            client.get("/admin/admins").status_code,
            client.post("/admin/admins", json={"phone": "+77012223344"}).status_code,
        ]

    assert statuses() == [401, 401]

    login(client, sms)
    assert statuses() == [403, 403]
    assert client.get("/admin/admins").json()["error"]["code"] == "forbidden"


# -- список ---------------------------------------------------------------


def test_list_shows_admins_only_and_marks_self(client, sms):
    login_admin(client, sms)
    make_user("+77015550001")  # учитель — в списке админов его быть не должно
    client.post("/admin/admins", json={"phone": "+7 (701) 555-00-02"})

    items = client.get("/admin/admins").json()["items"]
    assert [item["phone"] for item in items] == ["+77015550002", ADMIN_NORM]
    # Свежие сверху, «это вы» — ровно у одного
    assert [item["is_current"] for item in items] == [False, True]


def test_list_shows_empty_name_until_profile_filled(client, sms):
    """Добавляют по одному телефону: ФИО человек пишет себе сам."""
    login_admin(client, sms)
    client.post("/admin/admins", json={"phone": "+77015550003"})

    added = client.get("/admin/admins").json()["items"][0]
    assert (added["last_name"], added["first_name"], added["middle_name"]) == ("", "", "")


# -- добавление ------------------------------------------------------------


def test_add_unknown_phone_creates_user(client, sms):
    """Незнакомый номер заводится пользователем сразу — тем же, каким войдёт:
    второго человека с тем же телефоном база не примет."""
    login_admin(client, sms)
    resp = client.post("/admin/admins", json={"phone": "8 701 555 00 04"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"] == "+77015550004"
    assert resp.json()["is_current"] is False

    # Тот же человек, а не второй: вход по этому номеру нового не создаёт
    assert request_code(client, "+77015550004").status_code == 200
    _, code = sms.sent[-1]
    login_resp = client.post("/auth/verify_code", json={"phone": "+77015550004", "code": code})
    assert login_resp.json()["id"] == resp.json()["id"]
    assert login_resp.json()["is_admin"] is True


def test_add_existing_teacher_keeps_profile(client, sms):
    """Учителю выдают права, а не заводят его заново: ФИО остаётся."""
    login_admin(client, sms)
    teacher = make_user("+77015550005", last_name="Абишева", first_name="Асель")

    resp = client.post("/admin/admins", json={"phone": "+77015550005"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == teacher.id
    assert resp.json()["last_name"] == "Абишева"


def test_add_rejects_bad_phone(client, sms):
    login_admin(client, sms)
    resp = client.post("/admin/admins", json={"phone": "+7 999"})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "phone"
    # Номер не завёлся: в списке только тот, кто вошёл
    assert phones(client) == [ADMIN_NORM]


def test_add_rejects_existing_admin(client, sms):
    """Повторное добавление — это опечатка в номере: молчаливое «ок» её
    бы скрыло."""
    login_admin(client, sms)
    client.post("/admin/admins", json={"phone": "+77015550006"})

    resp = client.post("/admin/admins", json={"phone": "+77015550006"})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["message"] == (
        "Этот номер уже администратор"
    )
    assert phones(client) == ["+77015550006", ADMIN_NORM]


def test_add_rejects_blocked_phone(client, sms):
    login_admin(client, sms)
    blocked = make_user("+77015550007")
    with get_engine().begin() as conn:
        conn.execute(text('UPDATE "user" SET is_blocked = true WHERE id = :id'), {"id": blocked.id})

    resp = client.post("/admin/admins", json={"phone": "+77015550007"})
    assert resp.status_code == 422
    assert "заблокирован" in resp.json()["error"]["details"]["fields"][0]["message"]
    assert phones(client) == [ADMIN_NORM]


def test_add_forbids_extra_fields(client, sms):
    """`extra: forbid` — лишнее поле роняет запрос, а не молча теряется."""
    login_admin(client, sms)
    resp = client.post("/admin/admins", json={"phone": "+77015550008", "last_name": "Абишева"})
    assert resp.status_code == 422
    assert phones(client) == [ADMIN_NORM]
