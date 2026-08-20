"""Привязка Telegram-бота: код, вебхук, тестовое сообщение, отвязка.

`chat_id` вписать руками нельзя, и это не придирка к интерфейсу: чужой чат,
указанный по ошибке, получает заявки с телефонами учителей. Поэтому привязка
идёт от бота — админ берёт код в настройках, отправляет боту `/start <код>`,
и `chat_id` записывает сервер, приняв сообщение (CONTRACT, «Telegram-бот»).

Код лежит отдельной строкой настроек, а не рядом с привязкой: строку
`telegram` пишет PATCH настроек, и общая строка ловила бы гонку между
сохранением флагов и выдачей кода.
"""

import logging
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta

from app.adapters.db.repos import SettingRepo, now_utc
from app.application.ports import TelegramPort
from app.application.settings import TELEGRAM_KEY
from app.config import Settings
from app.domain.errors import (
    BotNotConfiguredError,
    TelegramFailedError,
    TelegramNotConnectedError,
)

log = logging.getLogger("telegram")

BIND_KEY = "telegram_bind"

# Код диктуют по телефону, поэтому в алфавите нет 0 O 1 I: на слух и на глаз
# они неразличимы, а ошибка стоит ещё одного захода в настройки.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6

# Промахи по коду считаются в той же строке настроек. Потолок — десяток:
# код диктуют по телефону вслух, и пара опечаток при наборе это норма.
# Дальше бот на промахи просто МОЛЧИТ — но код не гасит.
#
# Гасить код промахами нельзя, хотя сначала было сделано именно так: бот
# публичный, его имя знает кто угодно, и десять сообщений от прохожего
# оставляли админа без привязки — повторяемо, на каждый новый код.
# Подбор при этом не был угрозой и без потолка: 32 знака алфавита в шести
# позициях — миллиард вариантов на 10 минут жизни кода. Потолок здесь
# про другое: каждая попытка стоит одного исходящего сообщения.
ATTEMPTS_FIELD = "attempts"
MAX_ATTEMPTS = 10

# Поля привязки. Флаги типов сообщений лежат в той же строке и отвязкой
# не стираются: отвязали чат — не значит передумали получать заявки.
BOUND_FIELDS = ("chat_id", "chat_title", "connected_at")

START = "/start"
CONNECTED = "Готово: чат подключён. Сюда будут приходить заявки и работы на проверку."
WRONG_CODE = "Код не подошёл — он живёт 10 минут. Возьмите новый в настройках платформы."
# Голый /start — самый частый первый шаг: человек нажал ссылку бота и не
# знает, что дальше. Код можно прислать и так, и командой /start <код> —
# отвечаем на оба.
START_HINT = (
    "Чтобы привязать чат, возьмите код в настройках платформы (вкладка "
    "«Telegram-бот») и пришлите его сюда — командой /start <код> или просто кодом."
)
# Персональных данных в тестовом сообщении нет — как и в настоящих
TEST_MESSAGE = "Проверка связи: платформа видит этот чат."


class TelegramBindService:
    def __init__(
        self,
        settings: SettingRepo,
        telegram: TelegramPort,
        cfg: Settings,
        commit: Callable[[], None],
    ):
        self.settings = settings
        self.telegram = telegram
        self.cfg = cfg
        # Запись в настройки коммитится до похода в Telegram: иначе строка
        # стоит занятой весь сетевой вызов, а прислать боту сообщение может
        # кто угодно — и все они выстраиваются в очередь на одной строке,
        # каждый со своим занятым соединением базы
        self.commit = commit

    # -- POST /admin/settings/telegram/bind_code ---------------------------

    def bind_code(self) -> dict:
        """Код привязки и адрес бота.

        Повторный вызов до истечения отдаёт тот же код и тот же срок: админ
        мог закрыть окно, не дойдя до телефона, — а новый код обесценил бы
        уже продиктованный.
        """
        if (
            not self.cfg.telegram_bot_token
            or not self.cfg.telegram_bot_username
            # Без username не собрать deep_link, а он — половина этого ответа.
            # Пустой секрет вебхука отвергает всё, что присылает Telegram:
            # код бы выдался, а `/start` с ним ушёл бы в 403, и админ не понял
            # бы, почему после похода в бот ничего не произошло
            or not self.cfg.telegram_webhook_secret
        ):
            raise BotNotConfiguredError()

        stored = self.settings.get(BIND_KEY)
        code = stored.get("code")
        expires_at = stored.get("expires_at")
        if not code or _expired(expires_at, now_utc()):
            code = _new_code()
            expires_at = _iso(now_utc() + timedelta(minutes=self.cfg.telegram_bind_code_min))
            self.settings.put(BIND_KEY, {"code": code, "expires_at": expires_at})

        username = self.cfg.telegram_bot_username
        return {
            "code": code,
            "bot_username": username,
            "deep_link": f"https://t.me/{username}?start={code}",
            "expires_at": expires_at,
        }

    # -- POST /telegram/webhook --------------------------------------------

    def handle_update(self, update: dict) -> None:
        """Разбор сообщения от бота: `/start <код>`, голый код или голый `/start`.

        Результат разбора на ответ вебхука не влияет: Telegram на любой
        не-200 повторяет доставку по нарастающей. Отказ по коду виден
        человеку в самом боте, а не в коде ответа.
        """
        message = _dict(update.get("message"))
        chat = _dict(message.get("chat"))
        chat_id = chat.get("id")
        text = _text(message.get("text"))
        if chat_id is None or not text:
            return

        command, _, argument = text.partition(" ")
        # В группе клиент дописывает к команде имя бота — «/start@lms_kz_bot K7M2XB»
        if command.split("@", 1)[0] == START:
            # Код набирают и руками, с продиктованного по телефону: алфавит
            # заглавный, поэтому регистр присланного значения не важен
            code = argument.strip().upper()
            if not code:
                # Нажали ссылку бота или просто написали /start — код ещё
                # не присылали, это не промах мимо него, а первый шаг
                self.telegram.send_to(str(chat_id), START_HINT)
                return
        elif _looks_like_code(text):
            # Код без команды — тот же ввод, что и после /start
            code = text.strip().upper()
        else:
            # Не команда и не похоже на код — молчим, как и на всё остальное
            return

        if self._burn_code(code):
            # Слиянием, а не целой строкой: флаги уведомлений лежат в ней же,
            # и админ мог переключить их, пока код шёл до бота
            self.settings.merge(
                TELEGRAM_KEY,
                {
                    "chat_id": str(chat_id),
                    "chat_title": _chat_title(chat, chat_id),
                    # ISO-строка UTC: в JSONB нет своего типа под время,
                    # и такой же лежит в примерах контракта
                    "connected_at": _iso(now_utc()),
                },
            )
            reply = CONNECTED
        else:
            reply = WRONG_CODE if self._miss_deserves_an_answer(code) else None

        # Запись закончена — отпускаем строку до сетевого вызова
        self.commit()
        if reply is not None:
            self.telegram.send_to(str(chat_id), reply)

    # -- POST /admin/settings/telegram/test --------------------------------

    def send_test(self) -> None:
        """Единственное место, где сбой доставки виден админу как ошибка:
        он сам нажал «отправить тестовое»."""
        chat_id = self.settings.get(TELEGRAM_KEY).get("chat_id")
        if not chat_id:
            raise TelegramNotConnectedError()
        # Чтение открыло транзакцию, а дальше сетевой вызов на пять секунд
        self.commit()
        try:
            self.telegram.send_to(chat_id, TEST_MESSAGE)
        except Exception as err:
            # Домен поднимает сценарий: адаптер про 502 и коды ошибок
            # не знает, а в лог не уходит ни текст сообщения, ни ответ бота
            log.warning("Тестовое сообщение в Telegram не ушло")
            raise TelegramFailedError() from err

    # -- POST /admin/settings/telegram/unbind ------------------------------

    def unbind(self) -> None:
        """Стирает привязку; флаги типов сообщений остаются как были.

        Непривязанный бот отвязывается тем же 204: для нажавшего «Отвязать»
        результат один и тот же — чата нет.

        Убираются ровно поля привязки, а не строка целиком: «Отвязать»
        и переключатели уведомлений живут на одной вкладке экрана, и чтение
        всей строки в питон отменяло бы соседнюю правку.

        Заодно гаснет и невыданный код: продиктованный по телефону, но ещё
        не использованный, он иначе прожил бы свои 10 минут и привязал чат
        обратно — уже после того, как админ передумал.
        """
        self.settings.unset(TELEGRAM_KEY, BOUND_FIELDS)
        self.settings.put(BIND_KEY, {})

    # -- код привязки -------------------------------------------------------

    def _burn_code(self, code: str) -> bool:
        """Код одноразовый: сработал — гаснет. Не подошёл или просрочен —
        в настройках не меняется ничего, и админ берёт новый код.

        Гасит код условная запись в базе, а не «прочитали, сравнили,
        записали»: между чтением и записью успевает пройти второй `/start`
        с тем же кодом, и одноразовый код привязывал два чата — второй
        перезаписывал `chat_id` первого, а первый об этом не узнавал.
        """
        stored = self.settings.get(BIND_KEY)
        if not code or code != stored.get("code"):
            return False
        if _expired(stored.get("expires_at"), now_utc()):
            return False
        return self.settings.clear_if(BIND_KEY, "code", code)

    def _miss_deserves_an_answer(self, code: str) -> bool:
        """Отвечать ли на промах. Код при этом не трогаем — см. MAX_ATTEMPTS.

        Голый `/start` сюда не доходит вовсе — на него отвечает `START_HINT`
        ещё в `handle_update`, до всякого разбора кода. Счётчик обнуляется
        вместе с выдачей нового кода — строка настроек переписывается целиком.
        """
        return self.settings.bump(BIND_KEY, ATTEMPTS_FIELD) <= MAX_ATTEMPTS


def _new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _looks_like_code(text: str) -> bool:
    """Похоже ли сообщение на код, присланный без команды `/start`.

    Форма, не совпадение с настоящим кодом — тот же промах и то же
    ограничение попыток проверяет `_burn_code` дальше. Проверка по алфавиту
    и длине, а не regex-догадка: обычная фраза длиной ровно шесть знаков
    из этого алфавита — редкость, которую можно принять на себя.
    """
    candidate = text.strip().upper()
    return len(candidate) == CODE_LENGTH and all(ch in CODE_ALPHABET for ch in candidate)


def _expired(expires_at: str | None, now: datetime) -> bool:
    """Срок кода. Нечитаемое значение считаем истёкшим: выдать новый код
    дешевле, чем оставить привязку открытой на непонятной строке."""
    if not expires_at:
        return True
    try:
        moment = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    return moment <= now


def _iso(moment: datetime) -> str:
    """Время как его читает `SettingsService` и схема ответа. Без микросекунд:
    значение показывается человеку, а не сравнивается с точностью до тика."""
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dict(value: object) -> dict:
    """В вебхук приходит что угодно — вплоть до чужого запроса с верным
    секретом: разбор не должен падать на неожиданном типе."""
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _chat_title(chat: dict, chat_id: object) -> str:
    """Как чат называется на экране админа. У группы это `title`, у личного
    чата его нет вовсе — поэтому падаем на username и на имя с фамилией.
    Пустым поле остаться не может: по нему админ и узнаёт, куда привязано."""
    name = " ".join(
        part for part in (_text(chat.get("first_name")), _text(chat.get("last_name"))) if part
    )
    return _text(chat.get("title")) or _text(chat.get("username")) or name or f"Чат {chat_id}"
