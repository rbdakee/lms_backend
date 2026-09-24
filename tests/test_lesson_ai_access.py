"""Доступ к «Жоспару»: проверка на входе в продукт и выдача в админке.

Фикстуры `lesson_ai_open` здесь нет нарочно — проверка доступа настоящая.
Главное в файле — два обещания брифа («Что решено», п. 4): доступ к помощнику
и доступ к курсам — разные вещи, и ни один не открывает другого; и каждая
ручка продукта закрыта. Второе проверяется перебором маршрутов приложения,
а не списком в тесте: ручка, которую завтра допишут мимо проверки, провалит
этот файл сама.
"""

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import Enrollment, LessonAiAccess, LessonAiUsage, LessonKit
from app.application import lesson_ai_access
from app.application.lesson_ai_access import today_kz
from app.config import get_settings
from app.main import app
from tests.conftest import (
    ADMIN_NORM,
    ADMIN_PHONE,
    login,
    login_admin,
    login_named,
    make_admin,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_user,
    seed,
    user_id,
)
from tests.test_lesson_ai_kits import LESSON_AI_OFF, Scene, invalid

NO_ACCESS = {
    "error": {
        "code": "lesson_ai_no_access",
        "message": "Доступа к помощнику нет — его выдаёт администратор после оплаты",
    }
}
NO_ACCESS_YET = {"has_access": False, "expires_on": None}


def product_routes() -> list[tuple[str, str]]:
    """Все ручки продукта — из схемы OpenAPI самого приложения, а не из памяти
    теста. Схема, а не `app.routes`: подключённые роутеры FastAPI держит
    обёртками без `path` (так же в `test_lesson_ai_boundary.py`). Параметры
    пути заменяются числом: отказ в доступе обязан прийти раньше, чем ручка
    начнёт искать комплект или разбирать вид материала."""
    found = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/lesson_ai/") or path == "/lesson_ai/access":
            continue
        concrete = re.sub(r"\{[^}]+\}", "1", path)
        found += [(method.upper(), concrete) for method in sorted(operations)]
    return found


def call(client, method: str, path: str):
    if method in ("POST", "PUT", "PATCH"):
        return client.request(method, path, json={})
    return client.request(method, path)


def admin_client(client2, sms):
    """Админ с ФИО: имя выдавшего показывается в карточке доступа."""
    login_named(client2, sms, phone=ADMIN_PHONE, last_name="Ахметова", first_name="Динара")
    make_admin(ADMIN_NORM)
    return user_id(client2)


def teacher(number: int, **kw):
    return make_user(f"+7702000{number:04d}", **kw)


def seed_access(user_id_: int, **kw) -> LessonAiAccess:
    """Строка доступа мимо ручки: у ручки выдачи дата в прошлом — `422`,
    а истёкший доступ надо как-то получить."""
    fields = {"user_id": user_id_, "granted_by": user_id_}
    fields.update(kw)
    return seed(LessonAiAccess(**fields))


def count(model) -> int:
    with OrmSession(get_engine()) as db:
        return db.scalar(select(func.count()).select_from(model))


def today() -> date:
    return today_kz(datetime.now(UTC))


def freeze(monkeypatch, moment: datetime) -> None:
    """Часы сервиса доступа: и проверка, и выдача берут «сегодня» отсюда."""
    monkeypatch.setattr(lesson_ai_access, "now_utc", lambda: moment)


# -- каждая ручка продукта закрыта ----------------------------------------


def test_route_list_is_not_empty():
    """Перебор ниже обязан что-то перебирать: пустой список проходил бы
    любую проверку."""
    paths = {path for _, path in product_routes()}
    assert len(product_routes()) >= 14
    assert "/lesson_ai/kits" in paths
    assert "/lesson_ai/chats/1/messages" in paths
    assert "/lesson_ai/chats" in paths
    assert "/lesson_ai/objectives" in paths


def test_every_product_route_refuses_without_access(client, sms):
    login(client, sms)

    for method, path in product_routes():
        resp = call(client, method, path)
        assert (resp.status_code, resp.json()) == (403, NO_ACCESS), f"{method} {path}"


def test_chat_stream_is_refused_before_it_starts(client, sms):
    """Реплика чата стоит денег: отказ — обычным ответом JSON, а не событием
    внутри начавшегося стрима, и модель не вызывается вовсе."""
    login(client, sms)

    resp = client.post("/lesson_ai/chats/1/messages", json={"text": "урок во 2 классе"})

    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == NO_ACCESS


@pytest.mark.parametrize(
    "state", ["none", "revoked", "expired"], ids=["never", "revoked", "expired"]
)
def test_every_route_refuses_for_each_kind_of_no_access(client, sms, state):
    login(client, sms)
    uid = user_id(client)
    if state == "revoked":
        seed_access(uid, revoked_at=datetime.now(UTC), revoked_by=uid)
    elif state == "expired":
        seed_access(uid, expires_on=today() - timedelta(days=1))

    for method, path in product_routes():
        assert call(client, method, path).status_code == 403, f"{method} {path}"


def test_anonymous_gets_401_not_403(client):
    assert client.post("/lesson_ai/chats").status_code == 401
    assert client.get("/lesson_ai/access").status_code == 401


def test_switched_off_product_answers_before_access(client, sms, monkeypatch):
    """Выключенный продукт отвечает `lesson_ai_off` раньше проверки доступа:
    по этому коду фронт прячет вход, и отказ «нет доступа» здесь был бы
    неправдой — доступа нет ни у кого."""
    login(client, sms)
    monkeypatch.setattr(get_settings(), "lesson_ai_enabled", False)

    for method, path in [*product_routes(), ("GET", "/lesson_ai/access")]:
        resp = call(client, method, path)
        assert (resp.status_code, resp.json()) == (503, LESSON_AI_OFF), f"{method} {path}"


# -- GET /lesson_ai/access -------------------------------------------------


def test_status_never_granted(client, sms):
    login(client, sms)
    assert client.get("/lesson_ai/access").json() == NO_ACCESS_YET


def test_status_unlimited_lets_in(client, sms):
    login(client, sms)
    seed_access(user_id(client))

    assert client.get("/lesson_ai/access").json() == {"has_access": True, "expires_on": None}
    assert client.post("/lesson_ai/chats").status_code == 201


def test_status_until_date_lets_in(client, sms):
    login(client, sms)
    until = today() + timedelta(days=30)
    seed_access(user_id(client), expires_on=until)

    assert client.get("/lesson_ai/access").json() == {
        "has_access": True,
        "expires_on": until.isoformat(),
    }
    assert client.post("/lesson_ai/chats").status_code == 201


def test_status_last_day_still_lets_in(client, sms):
    """По этот день включительно: доступ «до сегодня» сегодня ещё действует."""
    login(client, sms)
    seed_access(user_id(client), expires_on=today())

    assert client.get("/lesson_ai/access").json()["has_access"] is True
    assert client.post("/lesson_ai/chats").status_code == 201


def test_status_expired_yesterday(client, sms):
    login(client, sms)
    seed_access(user_id(client), expires_on=today() - timedelta(days=1))

    assert client.get("/lesson_ai/access").json() == NO_ACCESS_YET
    assert client.post("/lesson_ai/chats").json() == NO_ACCESS


def test_status_revoked(client, sms):
    """Срок у снятого не показывается: он ничего не значит."""
    login(client, sms)
    uid = user_id(client)
    seed_access(uid, expires_on=today() + timedelta(days=30), revoked_at=datetime.now(UTC),
                revoked_by=uid)

    assert client.get("/lesson_ai/access").json() == NO_ACCESS_YET
    assert client.post("/lesson_ai/chats").json() == NO_ACCESS


@pytest.mark.parametrize(
    ("utc_now", "has_access"),
    [
        # 23:59 по Алматы 31 декабря — последний день ещё идёт
        (datetime(2026, 12, 31, 18, 59, tzinfo=UTC), True),
        # 00:00 по Алматы 1 января, а по UTC всё ещё 31 декабря — уже нет
        (datetime(2026, 12, 31, 19, 0, tzinfo=UTC), False),
    ],
    ids=["last-minute-kz", "next-day-kz-same-day-utc"],
)
def test_day_boundary_is_almaty(client, sms, monkeypatch, utc_now, has_access):
    """Доступ «до 31 декабря» кончается в полночь по Алматы, а не в 19:00
    по UTC и не в полночь по UTC пятью часами позже."""
    login(client, sms)
    seed_access(user_id(client), expires_on=date(2026, 12, 31))
    freeze(monkeypatch, utc_now)

    assert client.get("/lesson_ai/access").json()["has_access"] is has_access
    assert (client.post("/lesson_ai/chats").status_code == 201) is has_access


def test_grant_accepts_today_by_almaty_when_utc_is_still_yesterday(
    client, client2, sms, monkeypatch
):
    """В 02:00 по Алматы по UTC ещё вчера: дата «сегодня» — не прошлое."""
    admin_client(client2, sms)
    person = teacher(1)
    freeze(monkeypatch, datetime(2026, 12, 31, 21, 0, tzinfo=UTC))

    ok = client2.put(f"/admin/lesson_ai/access/{person.id}", json={"expires_on": "2027-01-01"})
    past = client2.put(f"/admin/lesson_ai/access/{person.id}", json={"expires_on": "2026-12-31"})

    assert ok.status_code == 200, ok.text
    assert ok.json()["access"]["status"] == "active"
    assert past.status_code == 422


# -- «Жоспар» и курсы — разные доступы ------------------------------------


def course_with_lesson():
    course = make_course(title="Критериальное оценивание")
    lesson = make_lesson(make_module(course.id).id)
    return course, lesson


def test_course_enrollment_does_not_open_the_assistant(client, sms):
    """Купивший курс помощника не получает."""
    course, lesson = course_with_lesson()
    login(client, sms)
    make_enrollment(user_id(client), course.id)

    assert client.get(f"/lessons/{lesson.id}").status_code == 200
    assert client.get("/lesson_ai/access").json() == NO_ACCESS_YET
    assert client.post("/lesson_ai/chats").json() == NO_ACCESS


def test_assistant_access_does_not_open_a_course(client, client2, sms):
    """Пользующийся помощником курс не получает: ни урока, ни видео,
    ни признака доступа на странице курса."""
    course, lesson = course_with_lesson()
    login(client, sms)
    admin_client(client2, sms)
    assert client2.put(f"/admin/lesson_ai/access/{user_id(client)}", json={}).status_code == 200

    assert client.post("/lesson_ai/chats").status_code == 201
    resp = client.get(f"/lessons/{lesson.id}")
    assert (resp.status_code, resp.json()["error"]["code"]) == (403, "forbidden")
    assert client.get(f"/lessons/{lesson.id}/playback").status_code == 403
    assert client.get(f"/courses/{course.id}").json()["access"] == {"state": "none"}


def test_grant_and_revoke_do_not_touch_the_other_access(client, client2, sms):
    """Выдача и снятие одного доступа не трогают другой — в обе стороны."""
    course, lesson = course_with_lesson()
    login(client, sms)
    uid = user_id(client)
    admin_client(client2, sms)

    client2.put(f"/admin/lesson_ai/access/{uid}", json={"paid": True})
    assert count(Enrollment) == 0

    enrollment = client2.post(
        "/admin/enrollments",
        json={"user_id": uid, "course_id": course.id, "platform": "p1", "paid": True},
    ).json()
    assert count(LessonAiAccess) == 1

    assert client2.delete(f"/admin/lesson_ai/access/{uid}").status_code == 204
    assert client.get(f"/lessons/{lesson.id}").status_code == 200

    client2.put(f"/admin/lesson_ai/access/{uid}", json={})
    assert client2.delete(f"/admin/enrollments/{enrollment['id']}").status_code == 204
    assert client.get(f"/lessons/{lesson.id}").status_code == 403
    assert client.get("/lesson_ai/access").json()["has_access"] is True
    assert client.post("/lesson_ai/chats").status_code == 201


# -- админ проверяется так же, как учитель ---------------------------------


def test_admin_without_access_is_refused_like_anyone(client2, sms):
    """Роль не открывает продукт: доступ проверяется по строке (L7, запись 2)."""
    admin_id = admin_client(client2, sms)

    assert client2.post("/lesson_ai/chats").json() == NO_ACCESS

    assert client2.put(f"/admin/lesson_ai/access/{admin_id}", json={}).status_code == 200
    assert client2.post("/lesson_ai/chats").status_code == 201


# -- админские ручки: права ------------------------------------------------


def admin_routes(person_id: int) -> list[tuple[str, str]]:
    base = f"/admin/lesson_ai/access/{person_id}"
    return [
        ("GET", "/admin/lesson_ai/access"),
        ("GET", base),
        ("PUT", base),
        ("DELETE", base),
    ]


def test_admin_routes_require_admin(client, sms):
    person = teacher(1)
    for method, path in admin_routes(person.id):
        assert call(client, method, path).status_code == 401, f"{method} {path}"

    login(client, sms)
    for method, path in admin_routes(person.id):
        resp = call(client, method, path)
        assert (resp.status_code, resp.json()["error"]["code"]) == (403, "forbidden"), path
    assert count(LessonAiAccess) == 0


def test_admin_routes_work_with_the_product_switched_off(client2, sms, monkeypatch):
    """Доступ выдают после оплаты, и выключенный продукт её не ждёт."""
    admin_client(client2, sms)
    person = teacher(1)
    monkeypatch.setattr(get_settings(), "lesson_ai_enabled", False)

    assert client2.put(f"/admin/lesson_ai/access/{person.id}", json={}).status_code == 200
    assert client2.get("/admin/lesson_ai/access").json()["total"] == 1


# -- выдача, продление, снятие ---------------------------------------------


def test_card_of_never_granted(client2, sms):
    admin_client(client2, sms)
    person = teacher(1)

    resp = client2.get(f"/admin/lesson_ai/access/{person.id}")

    assert resp.status_code == 200
    assert resp.json() == {"user_id": person.id, "access": None}


def test_unknown_user_is_404(client2, sms):
    admin_client(client2, sms)

    for method, path in admin_routes(999999)[1:]:
        resp = call(client2, method, path)
        assert (resp.status_code, resp.json()["error"]["code"]) == (404, "not_found"), method


def test_grant_answers_with_the_card(client2, sms):
    admin_id = admin_client(client2, sms)
    person = teacher(1)
    until = today() + timedelta(days=90)

    resp = client2.put(
        f"/admin/lesson_ai/access/{person.id}",
        json={"expires_on": until.isoformat(), "paid": True, "note": "Kaspi, перевод от 22.09"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    granted_at = body["access"].pop("granted_at")
    assert body == {
        "user_id": person.id,
        "access": {
            "status": "active",
            "expires_on": until.isoformat(),
            "paid": True,
            "paid_note": "Kaspi, перевод от 22.09",
            "granted_by": {"id": admin_id, "name": "Ахметова Динара"},
            "revoked_at": None,
        },
    }
    assert datetime.fromisoformat(granted_at)
    assert client2.get(f"/admin/lesson_ai/access/{person.id}").json()["access"]["status"] == (
        "active"
    )


def test_grant_records_the_admin_who_did_it(client, client2, sms):
    """Админов несколько: выдача и снятие помнят, кто именно (actor)."""
    first_id = admin_client(client2, sms)
    login_admin(client, sms, phone="+7 (700) 000-00-98")
    second_id = user_id(client)
    assert first_id != second_id
    person = teacher(1)

    client2.put(f"/admin/lesson_ai/access/{person.id}", json={})
    by_second = client.put(f"/admin/lesson_ai/access/{person.id}", json={}).json()
    client2.delete(f"/admin/lesson_ai/access/{person.id}")

    assert by_second["access"]["granted_by"]["id"] == second_id
    with OrmSession(get_engine()) as db:
        row = db.scalar(select(LessonAiAccess))
        assert (row.granted_by, row.revoked_by) == (second_id, first_id)


def test_paid_false_drops_the_note(client2, sms):
    admin_client(client2, sms)
    person = teacher(1)

    unpaid = client2.put(
        f"/admin/lesson_ai/access/{person.id}", json={"paid": False, "note": "обещала в пятницу"}
    ).json()["access"]
    paid = client2.put(f"/admin/lesson_ai/access/{person.id}", json={"paid": True}).json()[
        "access"
    ]

    assert (unpaid["paid"], unpaid["paid_note"]) == (False, None)
    # Отметка без комментария — всё равно отметка
    assert (paid["paid"], paid["paid_note"]) == (True, "")


def test_extend_changes_the_date(client2, sms):
    admin_client(client2, sms)
    person = teacher(1)
    url = f"/admin/lesson_ai/access/{person.id}"
    first = today() + timedelta(days=30)
    second = today() + timedelta(days=365)

    client2.put(url, json={"expires_on": first.isoformat()})
    resp = client2.put(url, json={"expires_on": second.isoformat()})
    unlimited = client2.put(url, json={"expires_on": None})

    assert resp.json()["access"]["expires_on"] == second.isoformat()
    assert unlimited.json()["access"]["expires_on"] is None
    assert count(LessonAiAccess) == 1


def test_extend_revives_expired(client, client2, sms):
    login(client, sms)
    uid = user_id(client)
    seed_access(uid, expires_on=today() - timedelta(days=1))
    admin_client(client2, sms)

    assert client2.get(f"/admin/lesson_ai/access/{uid}").json()["access"]["status"] == "expired"
    resp = client2.put(f"/admin/lesson_ai/access/{uid}", json={"expires_on": today().isoformat()})

    assert resp.json()["access"]["status"] == "active"
    assert client.post("/lesson_ai/chats").status_code == 201


def test_date_in_the_past_is_422(client2, sms):
    admin_client(client2, sms)
    person = teacher(1)
    yesterday = today() - timedelta(days=1)

    resp = client2.put(
        f"/admin/lesson_ai/access/{person.id}", json={"expires_on": yesterday.isoformat()}
    )

    assert resp.status_code == 422
    assert resp.json() == invalid("expires_on", "Дата окончания уже прошла")
    assert count(LessonAiAccess) == 0


def test_revoke_closes_the_product_and_is_idempotent(client, client2, sms):
    login(client, sms)
    uid = user_id(client)
    admin_id = admin_client(client2, sms)
    url = f"/admin/lesson_ai/access/{uid}"
    client2.put(url, json={})
    assert client.post("/lesson_ai/chats").status_code == 201

    first = client2.delete(url)
    card = client2.get(url).json()["access"]
    again = client2.delete(url)

    assert (first.status_code, again.status_code) == (204, 204)
    assert first.content == b""
    assert card["status"] == "revoked"
    assert card["revoked_at"] is not None
    assert client2.get(url).json()["access"]["revoked_at"] == card["revoked_at"]
    with OrmSession(get_engine()) as db:
        assert db.scalar(select(LessonAiAccess.revoked_by)) == admin_id
    assert client.post("/lesson_ai/chats").json() == NO_ACCESS
    assert client.get("/lesson_ai/access").json() == NO_ACCESS_YET


def test_grant_after_revoke_is_active_again(client, client2, sms):
    login(client, sms)
    uid = user_id(client)
    admin_client(client2, sms)
    url = f"/admin/lesson_ai/access/{uid}"
    client2.put(url, json={})
    client2.delete(url)

    resp = client2.put(url, json={})

    assert resp.json()["access"]["status"] == "active"
    assert resp.json()["access"]["revoked_at"] is None
    assert client.post("/lesson_ai/chats").status_code == 201
    assert count(LessonAiAccess) == 1


def test_revoke_never_granted_is_404(client2, sms):
    admin_client(client2, sms)
    person = teacher(1)

    resp = client2.delete(f"/admin/lesson_ai/access/{person.id}")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# -- список ----------------------------------------------------------------


def make_kit(user_id_: int, objective_id: int) -> LessonKit:
    return seed(
        LessonKit(user_id=user_id_, objective_id=objective_id, topic="Урок", grade=2, lang="ru")
    )


def spend(user_id_: int, *, tokens_in: int, tokens_out: int, cost: str | None, **kw):
    fields = {"user_id": user_id_, "source": "chat", "model": "gpt-4.1",
              "tokens_in": tokens_in, "tokens_out": tokens_out,
              "cost_usd": Decimal(cost) if cost is not None else None}
    fields.update(kw)
    return seed(LessonAiUsage(**fields))


class ListScene:
    """Три учителя с тремя состояниями доступа, выданными в разные дни,
    и четвёртый, которому доступ не выдавали, — со своим расходом, чтобы
    было видно, что к чужим строкам он не прилипает."""

    def __init__(self):
        objective_id = Scene().objective.id
        now = datetime.now(UTC)
        self.active = teacher(1, last_name="Абенова", first_name="Сауле")
        self.expired = teacher(2, last_name="Байжанова", first_name="Гаухар")
        self.revoked = teacher(3, last_name="Жумабекова", first_name="Алия")
        self.stranger = teacher(4, last_name="Абенова", first_name="Динара")
        seed_access(self.active.id, granted_at=now - timedelta(days=1))
        seed_access(self.expired.id, granted_at=now - timedelta(days=40),
                    expires_on=today() - timedelta(days=10))
        seed_access(self.revoked.id, granted_at=now - timedelta(days=2),
                    revoked_at=now, revoked_by=self.revoked.id)

        for _ in range(2):
            make_kit(self.active.id, objective_id)
        spend(self.active.id, tokens_in=1000, tokens_out=200, cost="0.003600")
        spend(self.active.id, tokens_in=500, tokens_out=100, cost="0.001800",
              source="material", kind="lesson_plan")
        # Модели нет в прайсе: токены считаются, стоимость неизвестна
        spend(self.expired.id, tokens_in=300, tokens_out=50, cost=None, model="gpt-x")

        make_kit(self.stranger.id, objective_id)
        spend(self.stranger.id, tokens_in=9999, tokens_out=9999, cost="9.000000")


def ids(resp) -> list[int]:
    return [item["teacher"]["id"] for item in resp.json()["items"]]


def test_list_shape_order_and_usage(client2, sms):
    admin_client(client2, sms)
    scene = ListScene()

    resp = client2.get("/admin/lesson_ai/access")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["total"], body["page"], body["per_page"]) == (3, 1, 20)
    # Свежие выдачи сверху; кому не выдавали — в списке нет
    assert ids(resp) == [scene.active.id, scene.revoked.id, scene.expired.id]

    first = body["items"][0]
    assert first["teacher"] == {
        "id": scene.active.id,
        "last_name": "Абенова",
        "first_name": "Сауле",
        "middle_name": "Токтарбековна",
        "phone": "+77020000001",
        "school": "КГУ «Средняя школа №27»",
        "region": "Алматы",
        "city": "",
        "subject": "",
        "is_blocked": False,
    }
    assert first["access"]["status"] == "active"
    usage = {item["teacher"]["id"]: item["usage"] for item in body["items"]}
    assert usage == {
        scene.active.id: {"kits": 2, "tokens_in": 1500, "tokens_out": 300, "cost_usd": 0.0054},
        scene.expired.id: {"kits": 0, "tokens_in": 300, "tokens_out": 50, "cost_usd": None},
        scene.revoked.id: {"kits": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": None},
    }
    statuses = {item["teacher"]["id"]: item["access"]["status"] for item in body["items"]}
    assert statuses[scene.expired.id] == "expired"
    assert statuses[scene.revoked.id] == "revoked"


@pytest.mark.parametrize("status", ["active", "expired", "revoked"])
def test_list_status_filter(client2, sms, status):
    admin_client(client2, sms)
    scene = ListScene()

    resp = client2.get("/admin/lesson_ai/access", params={"status": status})

    assert ids(resp) == [getattr(scene, status).id]
    assert resp.json()["total"] == 1


def test_list_active_filter_counts_the_last_day(client2, sms):
    """«Действует» по той же дате, что вход в продукт: последний день — да."""
    admin_client(client2, sms)
    person = teacher(1)
    seed_access(person.id, expires_on=today())

    assert ids(client2.get("/admin/lesson_ai/access", params={"status": "active"})) == [
        person.id
    ]
    assert client2.get("/admin/lesson_ai/access", params={"status": "expired"}).json() == {
        "items": [], "total": 0, "page": 1, "per_page": 20,
    }


def test_list_unknown_status_is_422(client2, sms):
    admin_client(client2, sms)
    assert client2.get("/admin/lesson_ai/access", params={"status": "paid"}).status_code == 422


def test_list_search_by_name_and_phone(client2, sms):
    admin_client(client2, sms)
    scene = ListScene()

    by_name = client2.get("/admin/lesson_ai/access", params={"q": "абенова"})
    # Номер так, как его диктуют: с восьмёркой и пробелами
    by_phone = client2.get("/admin/lesson_ai/access", params={"q": "8 702 000 0002"})
    nobody = client2.get("/admin/lesson_ai/access", params={"q": "Сейткали"})

    # Однофамилица без доступа в выдачу не попадает
    assert ids(by_name) == [scene.active.id]
    assert ids(by_phone) == [scene.expired.id]
    assert nobody.json() == {"items": [], "total": 0, "page": 1, "per_page": 20}


def test_list_pagination(client2, sms):
    admin_client(client2, sms)
    scene = ListScene()

    first = client2.get("/admin/lesson_ai/access", params={"per_page": 2})
    second = client2.get("/admin/lesson_ai/access", params={"per_page": 2, "page": 2})
    beyond = client2.get("/admin/lesson_ai/access", params={"per_page": 2, "page": 3})

    assert ids(first) == [scene.active.id, scene.revoked.id]
    assert ids(second) == [scene.expired.id]
    assert beyond.json() == {"items": [], "total": 3, "page": 3, "per_page": 2}
    assert first.json()["total"] == second.json()["total"] == 3


def test_list_empty(client2, sms):
    admin_client(client2, sms)

    resp = client2.get("/admin/lesson_ai/access")

    assert resp.json() == {"items": [], "total": 0, "page": 1, "per_page": 20}


def test_list_shows_admin_own_access(client2, sms):
    """Админ, выдавший доступ себе, в списке виден: строка — такая же выдача."""
    admin_id = admin_client(client2, sms)
    client2.put(f"/admin/lesson_ai/access/{admin_id}", json={})

    assert ids(client2.get("/admin/lesson_ai/access")) == [admin_id]

