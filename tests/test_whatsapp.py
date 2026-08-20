"""Адаптер WhatsApp Cloud API.

Настоящий Meta тестам не нужен: проверяется наше — форма запроса,
которую ждёт согласованный шаблон, и то, что молчаливого успеха
не бывает. Подмена `urlopen` — та же, что у адаптера Telegram.

Чего заглушка не проверяет: что Meta принимает именно этот шаблон.
Это сверяется отправкой на живой номер (список изменений сессии).
"""

import io
import json
import urllib.error

import pytest

from app.adapters.sms.whatsapp import WhatsAppError, WhatsAppSms
from app.config import get_settings

PHONE = "+77071234567"
CODE = "1234"
SENT = {"messages": [{"id": "wamid.HBgL"}]}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(code: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://graph.facebook.com/v21.0/1/messages",
        code,
        "Bad Request",
        {},
        io.BytesIO(json.dumps(payload).encode()),
    )


@pytest.fixture
def whatsapp(monkeypatch):
    """Настройки боевого вида, кроме секретов: их у теста нет и не нужно."""
    cfg = get_settings()
    monkeypatch.setattr(cfg, "sms_provider", "whatsapp")
    monkeypatch.setattr(cfg, "whatsapp_phone_number_id", "1035760399630606")
    monkeypatch.setattr(cfg, "whatsapp_access_token", "fake-token")
    return WhatsAppSms(cfg)


def sending(monkeypatch, response=None, raises=None):
    """Подменяет поход в сеть и возвращает список ушедших запросов."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        if raises is not None:
            raise raises
        return FakeResponse(SENT if response is None else response)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


# -- Форма запроса ------------------------------------------------------


def test_the_code_goes_as_an_approved_template(whatsapp, monkeypatch):
    """Свободный текст WhatsApp примет только внутри 24-часового окна,
    а на входе его нет никогда. Поэтому type=template, а не text."""
    calls = sending(monkeypatch)

    whatsapp.send_code(PHONE, CODE)

    body = json.loads(calls[0].data)
    assert body["messaging_product"] == "whatsapp"
    assert body["type"] == "template"
    assert body["template"]["name"] == "otp_ru"
    assert body["template"]["language"] == {"code": "ru"}


def test_the_recipient_is_digits_only(whatsapp, monkeypatch):
    """Получатель у WhatsApp — wa_id: `+77071234567` → `77071234567`."""
    calls = sending(monkeypatch)

    whatsapp.send_code(PHONE, CODE)

    assert json.loads(calls[0].data)["to"] == "77071234567"


def test_the_code_goes_twice_with_a_button(whatsapp, monkeypatch):
    """Шаблон Authentication у Meta по умолчанию с кнопкой «Скопировать
    код», и тогда API требует код и в теле, и в кнопке. Индекс кнопки —
    строка, так требует сам API."""
    calls = sending(monkeypatch)

    whatsapp.send_code(PHONE, CODE)

    components = json.loads(calls[0].data)["template"]["components"]
    assert components == [
        {"type": "body", "parameters": [{"type": "text", "text": CODE}]},
        {
            "type": "button",
            "sub_type": "url",
            "index": "0",
            "parameters": [{"type": "text", "text": CODE}],
        },
    ]


def test_a_template_without_a_button_gets_the_code_once(whatsapp, monkeypatch):
    """Шаблон без кнопки существует, и лишний параметр он не примет."""
    monkeypatch.setattr(get_settings(), "whatsapp_template_has_button", False)
    calls = sending(monkeypatch)

    WhatsAppSms(get_settings()).send_code(PHONE, CODE)

    components = json.loads(calls[0].data)["template"]["components"]
    assert [c["type"] for c in components] == ["body"]


def test_the_token_goes_in_the_header(whatsapp, monkeypatch):
    calls = sending(monkeypatch)

    whatsapp.send_code(PHONE, CODE)

    assert calls[0].headers["Authorization"] == "Bearer fake-token"
    assert calls[0].full_url.endswith("/v21.0/1035760399630606/messages")


# -- Молчаливого успеха не бывает ---------------------------------------


def test_a_refusal_is_not_success(whatsapp, monkeypatch):
    """131047 — «нет окна», 132001 — «нет такого шаблона», 190 — протух
    токен. Для сценария входа все они одно: код не ушёл."""
    sending(monkeypatch, raises=http_error(400, {"error": {"code": 131047}}))

    with pytest.raises(WhatsAppError):
        whatsapp.send_code(PHONE, CODE)


def test_an_unreachable_provider_is_not_success(whatsapp, monkeypatch):
    sending(monkeypatch, raises=TimeoutError("таймаут"))

    with pytest.raises(WhatsAppError):
        whatsapp.send_code(PHONE, CODE)


def test_two_hundred_without_a_message_id_is_not_success(whatsapp, monkeypatch):
    """Двухсотый ответ ещё ничего не значит: без идентификатора сообщения
    ничего не отправлено, и считать это успехом — значит показать человеку
    экран ввода кода, которого он не получит."""
    sending(monkeypatch, response={"messaging_product": "whatsapp"})

    with pytest.raises(WhatsAppError):
        whatsapp.send_code(PHONE, CODE)


# -- Персональные данные и секреты --------------------------------------


def test_neither_the_code_nor_the_token_nor_the_phone_reach_the_log(
    whatsapp, monkeypatch, caplog
):
    """Правило ПД: телефон в лог целиком не попадает. Код входа и токен —
    тем более: лог сервиса читают при разборе, а это ключи от аккаунта
    и от чужого входа."""
    sending(monkeypatch)

    with caplog.at_level("INFO"):
        whatsapp.send_code(PHONE, CODE)

    written = "\n".join(record.getMessage() for record in caplog.records)
    assert "***4567" in written
    assert PHONE not in written
    assert "7071234567" not in written
    assert CODE not in written
    assert "fake-token" not in written
