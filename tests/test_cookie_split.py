"""Разные куки у кабинета учителя и у админки.

Владелец работает с двух сторон одновременно: админом с одного номера
и учителем с другого, в одном браузере. Пока кука была одна на оба домена,
вход в админку выбивал сессию в кабинете — и наоборот.

Приложение узнаётся по `Origin`: только он приходит от браузера на каждый
запрос фронта и уже проверен CORS. `ADMIN_BASE_URL` для этого не годится —
локально он нарочно `127.0.0.1`, а браузер ходит на `localhost`.

Тем же способом и по той же причине узнаётся площадка, поэтому `platform_of`
проверяется здесь же. А домена у куки больше нет вовсе: у площадок разные
домены, общего родителя у них не осталось.
"""

import logging

from fastapi import Request

from app.api.deps import ADMIN_COOKIE_NAME, COOKIE_NAME, platform_of
from app.config import Settings, check_admin_origins, get_settings
from app.domain.platform import DEFAULT_PLATFORM
from tests.conftest import ADMIN_PHONE, PHONE, age_codes, make_admin, request_code

CFG = get_settings()
# Локальный `.env` источники задаёт, а голые умолчания — нет: без настройки
# кука общая, и тогда проверять нечего (см. `check_admin_origins`)
ADMIN_ORIGIN = CFG.admin_origins[0] if CFG.admin_origins else "http://localhost:3001"
WEB_ORIGIN = next(
    (o for o in CFG.cors_origins if o not in CFG.admin_origins), "http://localhost:3000"
)


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


def test_admin_origin_outside_cors_warns_but_lets_the_service_start(caplog):
    """Источник, которому не разрешён CORS, до API не дойдёт вовсе — кука
    останется общей. Сервис при этом поднимается: цена ошибки — поведение,
    с которым площадка жила до разделения, и гасить из-за неё API дороже."""
    cfg = Settings(cors_origins=["https://lms.kz"], admin_origins=["https://admin.lms.kz"])
    with caplog.at_level(logging.WARNING):
        check_admin_origins(cfg)
    assert "ADMIN_ORIGINS" in caplog.text
    assert "https://admin.lms.kz" in caplog.text


def test_missing_admin_origins_warns_loudly(caplog):
    """Забытая переменная 21.08.2026 уронила прод: сервис уходил в цикл
    перезапуска. Теперь она говорит в лог, а не гасит API."""
    cfg = Settings(env="prod", cors_origins=["https://lms.kz"], admin_origins=[])
    with caplog.at_level(logging.WARNING):
        check_admin_origins(cfg)
    assert "ADMIN_ORIGINS" in caplog.text


def test_the_shipped_defaults_do_not_raise():
    """Голые умолчания — рабочая конфигурация: проверка не мешает старту."""
    check_admin_origins(Settings())
    check_admin_origins(get_settings())


def test_the_session_cookie_is_host_only(client, sms):
    """Общего родительского домена у площадок больше нет: `Domain=` привязал
    бы куку к одному из них, и на втором домене сессия не завелась бы вовсе.
    Проверяем и выдачу, и гашение — `delete_cookie` без того же домена
    старую куку не тронул бы."""
    login = login_from(client, sms, WEB_ORIGIN, PHONE)
    assert "domain=" not in login.headers.get_list("set-cookie")[0].lower()

    logout = client.post("/auth/logout", headers={"Origin": WEB_ORIGIN})
    assert "domain=" not in logout.headers.get_list("set-cookie")[0].lower()


# -- Площадка запроса ---------------------------------------------------
# Тот же `Origin` и по той же причине: браузер присылает его на каждый запрос
# фронта, и он уже проверен CORS.


def request_from(origin):
    """Голый запрос: `platform_of` смотрит только на заголовок `Origin`."""
    headers = [(b"origin", origin.encode())] if origin else []
    return Request({"type": "http", "headers": headers})


def test_platform_comes_from_the_origin(monkeypatch):
    monkeypatch.setattr(
        get_settings(),
        "platform_origins",
        {"https://lms.kz": "p1", "https://second.kz": "p2"},
    )
    assert platform_of(request_from("https://lms.kz")) == "p1"
    assert platform_of(request_from("https://second.kz")) == "p2"


def test_an_unknown_origin_and_the_admin_are_the_first_platform(monkeypatch):
    """Так ходят curl, тесты и админка: площадки у неё нет вовсе — она одна
    на обе, и в `PLATFORM_ORIGINS` её источника не бывает."""
    monkeypatch.setattr(get_settings(), "platform_origins", {"https://second.kz": "p2"})
    assert platform_of(request_from(None)) == DEFAULT_PLATFORM
    assert platform_of(request_from("https://elsewhere.kz")) == DEFAULT_PLATFORM
    assert platform_of(request_from(ADMIN_ORIGIN)) == DEFAULT_PLATFORM


def test_without_the_setting_every_request_is_the_first_platform(monkeypatch):
    """Забытая настройка не отказывает в обслуживании — она возвращает
    поведение до разделения (см. `check_platform_origins`)."""
    monkeypatch.setattr(get_settings(), "platform_origins", {})
    assert platform_of(request_from(WEB_ORIGIN)) == DEFAULT_PLATFORM
