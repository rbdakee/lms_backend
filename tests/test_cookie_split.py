"""Разные куки у кабинета учителя и у админки.

Владелец работает с двух сторон одновременно: админом с одного номера
и учителем с другого, в одном браузере. Пока кука была одна на оба домена,
вход в админку выбивал сессию в кабинете — и наоборот.

Приложение узнаётся по `Origin`: только он приходит от браузера на каждый
запрос фронта и уже проверен CORS. `ADMIN_BASE_URL` для этого не годится —
локально он нарочно `127.0.0.1`, а браузер ходит на `localhost`.
"""

import pytest

from app.api.deps import ADMIN_COOKIE_NAME, COOKIE_NAME
from app.config import Settings, check_admin_origins, get_settings
from tests.conftest import ADMIN_PHONE, PHONE, age_codes, make_admin, request_code

CFG = get_settings()
ADMIN_ORIGIN = CFG.admin_origins[0]
WEB_ORIGIN = next(o for o in CFG.cors_origins if o not in CFG.admin_origins)


def login_from(client, sms, origin, phone):
    """Вход с указанием, из какого приложения пришли, — как это делает браузер."""
    headers = {"Origin": origin}
    assert (
        client.post(
            "/auth/request_code", json={"phone": phone, "consent": True}, headers=headers
        ).status_code
        == 200
    )
    _, code = sms.sent[-1]
    resp = client.post("/auth/verify_code", json={"phone": phone, "code": code}, headers=headers)
    assert resp.status_code == 200, resp.text
    age_codes(2)
    return resp


def test_admin_origin_gets_its_own_cookie(client, sms):
    login_from(client, sms, ADMIN_ORIGIN, ADMIN_PHONE)
    assert ADMIN_COOKIE_NAME in client.cookies
    assert COOKIE_NAME not in client.cookies


def test_teacher_origin_keeps_the_old_cookie(client, sms):
    login_from(client, sms, WEB_ORIGIN, PHONE)
    assert COOKIE_NAME in client.cookies
    assert ADMIN_COOKIE_NAME not in client.cookies


def test_one_browser_holds_both_sessions(client, sms):
    """Ради чего всё и затевалось: админ одним номером, учитель другим,
    и вход во вторую сторону не трогает первую."""
    admin_in = login_from(client, sms, ADMIN_ORIGIN, ADMIN_PHONE)
    # Права выдаются сырым SQL: PATCH /me их нарочно не меняет
    make_admin(admin_in.json()["phone"])
    login_from(client, sms, WEB_ORIGIN, PHONE)

    admin = client.get("/me", headers={"Origin": ADMIN_ORIGIN})
    teacher = client.get("/me", headers={"Origin": WEB_ORIGIN})
    assert admin.status_code == 200 and teacher.status_code == 200
    assert admin.json()["is_admin"] is True
    assert teacher.json()["is_admin"] is False
    assert admin.json()["id"] != teacher.json()["id"]


def test_logout_closes_only_its_own_side(client, sms):
    login_from(client, sms, ADMIN_ORIGIN, ADMIN_PHONE)
    login_from(client, sms, WEB_ORIGIN, PHONE)

    assert client.post("/auth/logout", headers={"Origin": ADMIN_ORIGIN}).status_code == 204
    assert client.get("/me", headers={"Origin": ADMIN_ORIGIN}).status_code == 401
    assert client.get("/me", headers={"Origin": WEB_ORIGIN}).status_code == 200


def test_request_without_origin_stays_on_the_teacher_cookie(client, sms):
    """Так ходят curl и тесты: имя куки для них не меняется."""
    resp = request_code(client)
    assert resp.status_code == 200
    _, code = sms.sent[-1]
    assert client.post("/auth/verify_code", json={"phone": PHONE, "code": code}).status_code == 200
    assert COOKIE_NAME in client.cookies


def test_admin_origin_outside_cors_stops_the_service_at_start():
    """Источник, которому не разрешён CORS, до API не дойдёт вовсе — значит
    разделение молча не сработает, а выяснится это только руками."""
    cfg = Settings(
        cors_origins=["https://lms.kz"],
        admin_origins=["https://admin.lms.kz"],
    )
    with pytest.raises(RuntimeError) as failed:
        check_admin_origins(cfg)
    assert "ADMIN_ORIGINS" in str(failed.value)


def test_empty_admin_origins_stops_prod_but_not_dev():
    """Пустой список — это общая кука обратно. В бою так молчать нельзя."""
    prod = Settings(env="prod", cors_origins=["https://lms.kz"], admin_origins=[])
    with pytest.raises(RuntimeError):
        check_admin_origins(prod)
    check_admin_origins(Settings(env="dev", cors_origins=["https://lms.kz"], admin_origins=[]))


def test_the_shipped_defaults_pass_the_check():
    check_admin_origins(get_settings())
