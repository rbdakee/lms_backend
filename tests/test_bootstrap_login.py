"""Вход без SMS для одного номера и первый админ на чистой базе.

Настоящего SMS-провайдера нет: заглушка печатает код входа в лог сервиса,
и в бою войти может только тот, кто читает логи. Пара `AUTH_BOOTSTRAP_PHONE`
+ `AUTH_BOOTSTRAP_CODE` держит вход первого админа, пока провайдера нет.

Это бэкдор, и здесь проверяется ровно то, что делает его терпимым: он
живёт в окружении, а не в коде, и не выводит номер из-под общих правил —
код так же истекает, попытки так же считаются, номер так же блокируется.
Файл удаляется целиком вместе с `app/application/bootstrap.py`, когда
появится настоящий SMS.

Номер здесь вымышленный и намеренно не тот, что стоит в бою: боевой
в репозитории не лежит — иначе от бэкдора остаются одни четыре цифры.
"""

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from tests.conftest import age_codes, request_code

PHONE = "+7 (700) 000-00-77"
NORM = "+77000000077"
CODE = "0909"


def enable(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "auth_bootstrap_phone", PHONE)
    monkeypatch.setattr(get_settings(), "auth_bootstrap_code", CODE)


def verify(client, code=CODE, phone=PHONE):
    return client.post("/auth/verify_code", json={"phone": phone, "code": code})


# -- Код входа ----------------------------------------------------------


def test_the_bootstrap_number_gets_the_code_from_the_environment(client, sms, monkeypatch):
    enable(monkeypatch)
    assert request_code(client, phone=PHONE).status_code == 200

    phone, code = sms.sent[-1]
    assert (phone, code) == (NORM, CODE)


def test_everyone_else_still_gets_a_random_code(client, sms, monkeypatch):
    """Фиксированный код — ровно у одного номера. Случайность здесь
    подменена, иначе «не совпало» значило бы «повезло»."""
    enable(monkeypatch)
    monkeypatch.setattr("app.application.auth.secrets.choice", lambda alphabet: "7")

    assert request_code(client, phone="+7 (701) 000-00-11").status_code == 200
    assert sms.sent[-1][1] == "7777"

    assert request_code(client, phone=PHONE).status_code == 200
    assert sms.sent[-1][1] == CODE


def test_the_bootstrap_number_logs_in_with_the_fixed_code(client, sms, monkeypatch):
    enable(monkeypatch)
    request_code(client, phone=PHONE)

    resp = verify(client)
    assert resp.status_code == 200
    assert resp.json()["phone"] == NORM
    assert client.cookies.get("sid")


def test_the_number_is_off_without_the_pair(client, sms, monkeypatch):
    """Выключенный бэкдор — это обычный номер: снятые переменные снимают
    фиксированный код, и войти по нему больше нельзя."""
    monkeypatch.setattr("app.application.auth.secrets.choice", lambda alphabet: "7")
    request_code(client, phone=PHONE)

    assert sms.sent[-1][1] == "7777"
    assert verify(client).status_code == 400


# -- Общие правила входа на него распространяются -----------------------


def test_the_fixed_code_still_expires(client, sms, monkeypatch):
    """Пять минут — и код недействителен. Фиксированный код не значит
    вечный: висящая строка в базе так же гасится по времени."""
    enable(monkeypatch)
    request_code(client, phone=PHONE)
    age_codes(minutes=6)

    resp = verify(client)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "code_expired"


def test_three_wrong_tries_block_the_bootstrap_number_too(client, sms, monkeypatch):
    """Подбор четырёх цифр стоит того же: три попытки и блок на десять
    минут. Известный код не открывает номеру дорогу мимо счётчика."""
    enable(monkeypatch)
    request_code(client, phone=PHONE)

    for _ in range(3):
        assert verify(client, code="1234").status_code in (400, 429)

    blocked = verify(client)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "too_many_attempts"


# -- Первый админ на чистой базе ----------------------------------------


def test_startup_makes_the_bootstrap_number_an_admin(sms, monkeypatch):
    """Прод поднимается с пустой базой, и выдать первый доступ там некому.
    Номер заводится админом при старте — без `INSERT` в инструкции
    по деплою."""
    enable(monkeypatch)

    with TestClient(app) as client:
        request_code(client, phone=PHONE)
        assert verify(client).json()["is_admin"] is True


def test_a_second_start_does_not_make_a_second_admin(sms, monkeypatch):
    """Старт идемпотентен: перезапуск сервиса не заводит второго
    пользователя с тем же номером и не трогает существующего."""
    enable(monkeypatch)

    ids = []
    for _ in range(2):
        with TestClient(app) as client:
            request_code(client, phone=PHONE)
            ids.append(verify(client).json()["id"])
        age_codes(minutes=6)

    assert ids[0] == ids[1]
