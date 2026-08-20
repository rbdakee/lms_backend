"""Код входа через WhatsApp Cloud API.

Своего HTTP-клиента в зависимостях нет и заводить его ради одного запроса
незачем: `urllib` умеет ровно то, что нужно, — POST с JSON и таймаут.
Так же сделан адаптер Telegram.

Код уходит **согласованным шаблоном** категории Authentication, а не
обычным текстом. Свободное сообщение WhatsApp принимает только внутри
24-часового окна — то есть если человек сам написал на этот номер за
последние сутки; на входе это никогда не так, и Meta отвечает 131047.
Шаблон окно не спрашивает.

Шаблон Authentication у Meta по умолчанию идёт с кнопкой «Скопировать
код», и тогда код передаётся ДВАЖДЫ — в теле и в кнопке. Шаблон без кнопки
существует, поэтому это переключатель, а не константа.

В лог отсюда не уходит ни код, ни токен, ни телефон целиком — только
последние четыре цифры, как у заглушки.
"""

import json
import logging
import re
import urllib.error
import urllib.request

from app.config import Settings

log = logging.getLogger("sms")

API_BASE = "https://graph.facebook.com"
# Без таймаута у сокета его нет вовсе, а этого ответа ждёт человек на экране
# входа. Про то, что таймаут не покрывает разрешение имени хоста, написано
# у адаптера Telegram — здесь ровно то же самое.
TIMEOUT_SEC = 5


class WhatsAppError(RuntimeError):
    """Код не ушёл. Подробностей в тексте нет: их читает лог, а не человек."""


class WhatsAppSms:
    def __init__(self, cfg: Settings):
        self.endpoint = (
            f"{API_BASE}/{cfg.whatsapp_api_version}/{cfg.whatsapp_phone_number_id}/messages"
        )
        self.token = cfg.whatsapp_access_token
        self.template = cfg.whatsapp_template_name
        self.language = cfg.whatsapp_template_language
        self.has_button = cfg.whatsapp_template_has_button

    def send_code(self, phone: str, code: str) -> None:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(self._body(phone, code)).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as err:
            # Разбор ответа — только ради кода ошибки Meta: он один говорит,
            # что чинить (131047 — окно, 132001 — нет такого шаблона,
            # 190 — протух токен). Тело целиком в лог не уходит: там номер
            # получателя.
            log.warning(
                "WhatsApp ответил %s на код для ***%s%s",
                err.code,
                phone[-4:],
                _meta_code(err),
            )
            raise WhatsAppError("WhatsApp не принял сообщение") from err
        except Exception as err:
            log.warning("Запрос к WhatsApp не удался для кода на ***%s", phone[-4:])
            raise WhatsAppError("WhatsApp недоступен") from err

        # Двухсотый ответ ещё ничего не значит: без идентификатора сообщения
        # ничего не отправлено, и молча считать это успехом нельзя
        message_id = _message_id(body)
        if message_id is None:
            log.warning("WhatsApp ответил без идентификатора сообщения (***%s)", phone[-4:])
            raise WhatsAppError("WhatsApp не принял сообщение")
        log.info("WhatsApp: код на ***%s, сообщение %s", phone[-4:], message_id)

    def _body(self, phone: str, code: str) -> dict:
        parameter = {"type": "text", "text": code}
        components: list[dict] = [{"type": "body", "parameters": [parameter]}]
        if self.has_button:
            # Кнопка «Скопировать код» у Authentication-шаблона. Индекс —
            # строка, так требует сам API.
            components.append(
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [parameter],
                }
            )
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": _wa_id(phone),
            "type": "template",
            "template": {
                "name": self.template,
                "language": {"code": self.language},
                "components": components,
            },
        }


def _wa_id(phone: str) -> str:
    """Получатель у WhatsApp — одни цифры: `+77071234567` → `77071234567`.
    Номер к этому месту уже нормализован (`app/domain/phone.py`)."""
    return re.sub(r"\D", "", phone)


def _meta_code(err: urllib.error.HTTPError) -> str:
    """Код ошибки Meta из тела отказа. Не разобралось — значит его нет,
    и отказ всё равно попадёт в лог статусом."""
    try:
        error = json.loads(err.read()).get("error", {})
    except Exception:
        return ""
    code = error.get("code")
    return f" (код Meta {code})" if code is not None else ""


def _message_id(body: object) -> str | None:
    if not isinstance(body, dict):
        return None
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    first = messages[0]
    return first.get("id") if isinstance(first, dict) else None
