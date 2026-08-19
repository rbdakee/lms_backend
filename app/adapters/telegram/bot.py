"""Настоящий бот: HTTP к Bot API поверх stdlib.

Своего HTTP-клиента в зависимостях нет и заводить его ради двух запросов
незачем: `urllib` умеет ровно то, что нужно — POST с JSON и таймаут.

В лог отсюда не уходит ни текст сообщения, ни ответ Telegram: там переписка
людей. Остаётся код ответа и факт неудачи.
"""

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("telegram")

API_BASE = "https://api.telegram.org"
# Без таймаута у сокета его нет вовсе: зависший Telegram подвесил бы запрос,
# внутри которого отправка случилась, — а это заявка учителя.
TIMEOUT_SEC = 5


class TelegramError(RuntimeError):
    """Сообщение не ушло. Подробностей от Telegram в тексте нет: его читает
    лог, а не человек."""


class TelegramBot:
    def __init__(self, token: str, chat_id: str | None):
        self.token = token
        # Куда слать «админам». Приходит только от вебхука привязки:
        # вписанный руками чужой chat_id — это заявки с телефонами учителей,
        # ушедшие незнакомому человеку.
        self.chat_id = chat_id

    def notify_admins(self, text: str) -> None:
        if not self.chat_id:
            # Бот не привязан — уведомлять некуда. Это не сбой доставки:
            # заявка уже в админке, и падать сценарию не на чем
            return
        self.send_to(self.chat_id, text)

    def send_to(self, chat_id: str, text: str) -> None:
        request = urllib.request.Request(
            f"{API_BASE}/bot{self.token}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as err:
            # Код ответа — всё, что уходит в лог: тело Telegram повторяет
            # присланное сообщение
            log.warning("Telegram ответил %s", err.code)
            raise TelegramError("Telegram не принял сообщение") from err
        except Exception as err:
            log.warning("Запрос к Telegram не удался")
            raise TelegramError("Telegram недоступен") from err
        # Двухсотый ответ ещё ничего не значит: отказ Bot API приезжает
        # с кодом 200 и `ok: false` в теле
        if not (isinstance(body, dict) and body.get("ok")):
            log.warning("Telegram не принял сообщение")
            raise TelegramError("Telegram не принял сообщение")
