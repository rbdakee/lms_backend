"""Настоящий бот: HTTP к Bot API поверх stdlib.

Своего HTTP-клиента в зависимостях нет и заводить его ради двух запросов
незачем: `urllib` умеет ровно то, что нужно — POST с JSON и таймаут.

В лог отсюда не уходит ни текст сообщения, ни ответ Telegram: там переписка
людей. Остаётся код ответа и факт неудачи.
"""

import html
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from app.application.ports import NotificationCard

log = logging.getLogger("telegram")

API_BASE = "https://api.telegram.org"
# Без таймаута у сокета его нет вовсе: зависший Telegram подвесил бы запрос,
# внутри которого отправка случилась, — а это заявка учителя.
#
# Покрывает он не весь поход к Telegram: `urlopen` отдаёт таймаут сокету,
# а имя хоста разрешается раньше, чем сокет вообще создан, —
# `socket.create_connection` первой строкой зовёт `getaddrinfo` и только
# потом ставит таймаут на сокет. Ограничить `getaddrinfo` средствами stdlib
# нечем: параметра времени у него нет, `signal.alarm` работает только
# в главном потоке, а отправка идёт из threadpool. То есть зависший
# DNS-резолвер здесь не ограничен ничем, и лечится это в системе
# (таймаут и число попыток в resolv.conf), а не в этом файле.
TIMEOUT_SEC = 5

# Повторы обещаны разделом 11 BACKEND_NOTES именно уведомлениям админу —
# новой заявке и работе на проверку. Поэтому они живут в `notify_admins`,
# а не в `send_to`: ответ боту на `/start` и «отправить тестовое» админ ждёт
# вживую, и растянутое на секунды молчание там во вред. Фоновой очереди
# в проекте нет (CLAUDE.md), значит повтор идёт внутри того же запроса.
ATTEMPTS = 3
PAUSE_SEC = 0.5


class TelegramError(RuntimeError):
    """Сообщение не ушло. Подробностей от Telegram в тексте нет: его читает
    лог, а не человек."""


class TelegramUnreachableError(TelegramError):
    """Сообщение не доставлено: сеть, таймаут или сбой на стороне Telegram.

    Отдельно от отказа, потому что повторять имеет смысл только это:
    отклонённое сообщение не примут и со второй попытки.
    """


class TelegramBot:
    def __init__(self, token: str, chat_id: str | None):
        self.token = token
        # Куда слать «админам». Приходит только от вебхука привязки:
        # вписанный руками чужой chat_id — это заявки с телефонами учителей,
        # ушедшие незнакомому человеку.
        self.chat_id = chat_id

    def notify_admins(self, card: NotificationCard) -> None:
        """Уведомление админу карточкой — с повторами на недоставку.

        Худший случай по времени: ATTEMPTS × TIMEOUT_SEC + паузы между
        попытками = 3 × 5 + 2 × 0,5 ≈ 16 секунд. Ждёт их учитель, нажавший
        «Оставить заявку», — ответ ему уходит после отправки. Цена известная
        и выбранная: сама заявка к этому моменту уже в базе, и провал всех
        попыток отменяет только уведомление.
        """
        if not self.chat_id:
            # Бот не привязан — уведомлять некуда. Это не сбой доставки:
            # заявка уже в админке, и падать сценарию не на чем
            return
        text = _render(card)
        keyboard = _keyboard(card)
        for attempt in range(ATTEMPTS):
            try:
                self._post(self.chat_id, text, parse_mode="HTML", reply_markup=keyboard)
                return
            except TelegramUnreachableError:
                if attempt == ATTEMPTS - 1:
                    raise
                time.sleep(PAUSE_SEC)

    def send_to(self, chat_id: str, text: str) -> None:
        self._post(chat_id, text)

    def _post(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> None:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        request = urllib.request.Request(
            f"{API_BASE}/bot{self.token}/sendMessage",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as err:
            # Код ответа — всё, что уходит в лог: тело Telegram повторяет
            # присланное сообщение
            log.warning("Telegram ответил %s", err.code)
            if err.code >= 500:
                # Сбой на той стороне: сообщение не отвергнуто, оно
                # не доставлено, — такое повторяют
                raise TelegramUnreachableError("Telegram недоступен") from err
            # 4xx — осмысленный отказ, в том числе 429: то же сообщение
            # не примут и со второй попытки
            raise TelegramError("Telegram не принял сообщение") from err
        except Exception as err:
            log.warning("Запрос к Telegram не удался")
            raise TelegramUnreachableError("Telegram недоступен") from err
        # Двухсотый ответ ещё ничего не значит: отказ Bot API приезжает
        # с кодом 200 и `ok: false` в теле
        if not (isinstance(body, dict) and body.get("ok")):
            log.warning("Telegram не принял сообщение")
            raise TelegramError("Telegram не принял сообщение")


def _render(card: NotificationCard) -> str:
    """Заголовок жирным, дальше — строки деталей. Экранирование обязательно:
    в строки попадают название курса и задания, а `parse_mode=HTML` на
    неэкранированном `<` или `&` не рисует текст как есть — валит всё
    сообщение целиком, и уведомление не уходит вообще."""
    lines = [f"<b>{html.escape(card.title)}</b>"]
    lines.extend(html.escape(line) for line in card.lines)
    return "\n".join(lines)


def _keyboard(card: NotificationCard) -> dict | None:
    """Кнопка-ссылка — или её отсутствие, если ссылка нерабочая.

    Bot API проверяет URL кнопки и отказывает сообщению целиком, а не только
    кнопке: `sendMessage` с `http://localhost:3001/...` возвращает 400 —
    хотя тот же адрес по IP (`127.0.0.1`) Telegram принимает. Локальный
    `ADMIN_BASE_URL` уже смотрит на `127.0.0.1` (см. `.env.example`), но
    опечатка в конфигурации не должна убивать заявку и уведомление целиком —
    молча остаётся без кнопки, а не без сообщения.
    """
    if not card.link_url or urllib.parse.urlparse(card.link_url).hostname == "localhost":
        return None
    return {"inline_keyboard": [[{"text": card.link_text, "url": card.link_url}]]}
