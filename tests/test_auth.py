from app.config import get_settings
from tests.conftest import NORM, PHONE, age_codes, login, request_code

CFG = get_settings()


def test_request_code_sends_sms_and_normalizes(client, sms):
    resp = request_code(client, phone="8 (707) 123-45-67")
    assert resp.status_code == 200
    assert resp.json() == {"retry_after_sec": 60}
    phone, code = sms.sent[-1]
    assert phone == NORM
    assert len(code) == 4 and code.isdigit()


def test_request_code_requires_consent(client, sms):
    resp = request_code(client, consent=False)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert sms.sent == []


def test_request_code_rejects_bad_phone(client, sms):
    resp = request_code(client, phone="12345")
    assert resp.status_code == 422
    assert sms.sent == []


def test_resend_too_fast(client, sms):
    assert request_code(client).status_code == 200
    resp = request_code(client)
    assert resp.status_code == 429
    error = resp.json()["error"]
    assert error["code"] == "rate_limited"
    assert 0 < error["details"]["retry_after_sec"] <= 60


def test_verify_happy_path(client, sms):
    resp = login(client, sms)
    body = resp.json()
    assert body["phone"] == NORM
    assert body["onboarding_done"] is False
    assert body["is_admin"] is False
    assert client.cookies.get("sid")

    me = client.get("/me")
    assert me.status_code == 200
    assert me.json()["id"] == body["id"]


def test_verify_wrong_code_then_block(client, sms):
    request_code(client)
    _, real = sms.sent[-1]
    wrong = "0000" if real != "0000" else "1111"

    first = client.post("/auth/verify_code", json={"phone": PHONE, "code": wrong})
    assert first.status_code == 400
    assert first.json()["error"]["code"] == "wrong_code"
    assert first.json()["error"]["details"]["attempts_left"] == 2

    second = client.post("/auth/verify_code", json={"phone": PHONE, "code": wrong})
    assert second.json()["error"]["details"]["attempts_left"] == 1

    third = client.post("/auth/verify_code", json={"phone": PHONE, "code": wrong})
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "too_many_attempts"

    # Пока действует блок, не работает ни ввод, ни запрос нового кода
    blocked = client.post("/auth/verify_code", json={"phone": PHONE, "code": real})
    assert blocked.status_code == 429
    age_codes(2)
    resend = request_code(client)
    assert resend.status_code == 429
    assert resend.json()["error"]["code"] == "too_many_attempts"


def test_code_expired(client, sms):
    request_code(client)
    _, code = sms.sent[-1]
    age_codes(6)  # TTL кода — 5 минут
    resp = client.post("/auth/verify_code", json={"phone": PHONE, "code": code})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "code_expired"


def test_code_is_single_use(client, sms):
    login(client, sms)
    _, code = sms.sent[-1]
    resp = client.post("/auth/verify_code", json={"phone": PHONE, "code": code})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "code_expired"


def test_second_login_is_same_user(client, client2, sms):
    first = login(client, sms).json()
    second = login(client2, sms).json()
    assert first["id"] == second["id"]

    sessions = client.get("/me/sessions").json()["items"]
    assert len(sessions) == 2


def test_daily_phone_limit(client, sms):
    for _ in range(10):
        assert request_code(client).status_code == 200
        age_codes(2)
    resp = request_code(client)
    assert resp.status_code == 429
    error = resp.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["details"]["retry_after_sec"] > 0


def test_blocked_user(client, sms):
    from sqlalchemy import text

    from app.adapters.db.base import get_engine

    login(client, sms)
    with get_engine().begin() as conn:
        conn.execute(text('UPDATE "user" SET is_blocked = true'))

    # Старая сессия перестаёт работать сразу
    me = client.get("/me")
    assert me.status_code == 403
    assert me.json()["error"]["code"] == "blocked"

    # Новый код заблокированному не отправляется
    resp = request_code(client)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "blocked"


# -- Провайдер не принял код -------------------------------------------
# Заглушка не отказывала никогда, настоящий провайдер отказывает: сеть,
# протухший токен, снятый с публикации шаблон.


def test_a_code_that_did_not_go_out_says_so(client, sms, monkeypatch):
    """Отдельный код ошибки, а не `internal_error`: «что-то пошло не так»
    человек читает как «сломалось насовсем», а здесь помогает вторая
    попытка."""

    def refuse(phone, code):
        raise RuntimeError("провайдер недоступен")

    monkeypatch.setattr(sms, "send_code", refuse)

    resp = request_code(client)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "send_failed"


def test_a_code_that_did_not_go_out_does_not_hold_up_the_next_try(client, sms, monkeypatch):
    """Минуту до повтора держит строка кода в базе. Не ушедший код такой
    строки не оставляет — иначе человек ждёт из-за сообщения, которого
    не получал."""
    attempts = []

    def flaky(phone, code):
        attempts.append(code)
        if len(attempts) == 1:
            raise RuntimeError("провайдер недоступен")
        sms.sent.append((phone, code))

    monkeypatch.setattr(sms, "send_code", flaky)

    assert request_code(client).status_code == 502
    assert request_code(client).status_code == 200
    assert len(sms.sent) == 1


def test_a_code_that_did_not_go_out_does_not_eat_the_daily_cap(client, sms, monkeypatch):
    """Суточный потолок считается по строкам той же таблицы. Не ушедший код
    в него попадать не должен: SMS не отправлена и денег не стоила,
    а человек остался бы без входа на сутки."""

    def refuse(phone, code):
        raise RuntimeError("провайдер недоступен")

    monkeypatch.setattr(sms, "send_code", refuse)

    for attempt in range(CFG.phone_codes_per_day + 1):
        assert request_code(client).status_code == 502, attempt
