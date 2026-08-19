import json
import logging
import time
import urllib.error
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.repos import SessionRepo, SettingRepo
from app.adapters.telegram import bot as bot_module
from app.adapters.telegram.bot import ATTEMPTS, PAUSE_SEC, TelegramBot, TelegramError
from app.adapters.telegram.log_telegram import LogTelegram
from app.api.deps import get_telegram
from app.api.routers.telegram import MAX_BODY_BYTES
from app.application.settings import TELEGRAM_KEY
from app.application.telegram_bind import (
    CODE_ALPHABET,
    CONNECTED,
    MAX_ATTEMPTS,
    TEST_MESSAGE,
    WRONG_CODE,
)
from app.config import get_settings
from app.main import app
from tests.conftest import (
    in_parallel,
    login,
    login_admin,
    make_course,
    make_enrollment,
    make_module,
    make_task,
    meet_at,
    user_id,
)

SECRET = "webhook-secret"
USERNAME = "lms_kz_bot"
# Чат вымышленный, как и всё в тестовых данных
CHAT_ID = -1001234567890
CHAT = {"id": CHAT_ID, "title": "LMS — заявки"}

BIND_CODE = "/admin/settings/telegram/bind_code"
TEST = "/admin/settings/telegram/test"
UNBIND = "/admin/settings/telegram/unbind"
WEBHOOK = "/telegram/webhook"


@pytest.fixture
def bot(monkeypatch):
    """Токен, имя бота и секрет вебхука живут в конфигурации сервиса, а не
    в настройках площадки: это ключи доступа, их место рядом с паролем базы."""
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "123456:AA-fake-token")
    monkeypatch.setattr(get_settings(), "telegram_bot_username", USERNAME)
    monkeypatch.setattr(get_settings(), "telegram_webhook_secret", SECRET)


class BoomTelegram:
    """Бот, до которого сообщение не доходит: адаптер поднимает исключение,
    а домашнюю ошибку из него делает уже сценарий."""

    def notify_admins(self, text: str) -> None:
        raise RuntimeError("бот недоступен")

    def send_to(self, chat_id: str, text: str) -> None:
        raise RuntimeError("бот недоступен")


def webhook(client, update, secret=SECRET, raw=None):
    headers = {} if secret is None else {"X-Telegram-Bot-Api-Secret-Token": secret}
    if raw is not None:
        return client.post(WEBHOOK, content=raw, headers=headers)
    return client.post(WEBHOOK, json=update, headers=headers)


def start(code, **chat):
    return {"message": {"chat": {**CHAT, **chat}, "text": f"/start {code}"}}


def bind(client, telegram, **chat):
    """Привязка как она происходит на самом деле: админ берёт код,
    а chat_id записывает сервер, приняв сообщение бота."""
    code = client.post(BIND_CODE).json()["code"]
    assert webhook(client, start(code, **chat)).status_code == 200
    telegram.to_chat.clear()
    return code


def stored_telegram() -> dict:
    """Строка настройки как она лежит в базе — вместе с chat_id, которого
    в ответе экрана нет."""
    with get_engine().connect() as conn:
        value = conn.execute(text("SELECT value FROM setting WHERE key = 'telegram'")).scalar()
    return value or {}


def expire_bind_code():
    """Сдвигает код в прошлое: десяти минут в тесте не переждёшь."""
    _set_bind_expiry("2026-01-01T00:00:00Z")


def garble_bind_expiry():
    """Портит срок кода до неразбираемого: так выглядит строка, пережившая
    смену формата или правку руками."""
    _set_bind_expiry("позавчера")


def _set_bind_expiry(value: str):
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE setting SET value = jsonb_set(value, '{expires_at}',"
                " to_jsonb(CAST(:value AS text))) WHERE key = 'telegram_bind'"
            ),
            {"value": value},
        )


def stored_bind() -> dict:
    with get_engine().connect() as conn:
        value = conn.execute(
            text("SELECT value FROM setting WHERE key = 'telegram_bind'")
        ).scalar()
    return value or {}


# -- права ---------------------------------------------------------------


def test_telegram_endpoints_require_admin(client, sms, bot):
    def statuses() -> list[int]:
        return [
            client.post(BIND_CODE).status_code,
            client.post(TEST).status_code,
            client.post(UNBIND).status_code,
        ]

    assert statuses() == [401, 401, 401]

    login(client, sms)
    assert statuses() == [403, 403, 403]
    assert client.post(BIND_CODE).json()["error"]["code"] == "forbidden"


# -- POST /admin/settings/telegram/bind_code ------------------------------


def test_bind_code_is_the_same_until_it_expires(client, sms, bot):
    """Админ мог закрыть окно, не дойдя до телефона: новый код обесценил бы
    уже продиктованный."""
    login_admin(client, sms)

    resp = client.post(BIND_CODE)
    assert resp.status_code == 200, resp.text
    first = resp.json()
    assert len(first["code"]) == 6
    # Код диктуют по телефону: 0 O 1 I в алфавите нет
    assert set(first["code"]) <= set(CODE_ALPHABET)
    assert first["bot_username"] == USERNAME
    assert first["deep_link"] == f"https://t.me/{USERNAME}?start={first['code']}"

    second = client.post(BIND_CODE).json()
    assert second == first

    expire_bind_code()
    third = client.post(BIND_CODE).json()
    assert third["code"] != first["code"]


def test_bind_code_409_without_bot_in_config(client, sms, bot, monkeypatch):
    login_admin(client, sms)

    monkeypatch.setattr(get_settings(), "telegram_bot_token", "")
    resp = client.post(BIND_CODE)
    assert resp.status_code == 409
    assert resp.json()["error"] == {
        "code": "bot_not_configured",
        "message": "Бот не настроен на сервере — задайте токен в конфигурации",
    }

    # Без username не собрать deep_link, который эта же ручка обязана вернуть
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "123456:AA-fake-token")
    monkeypatch.setattr(get_settings(), "telegram_bot_username", "")
    assert client.post(BIND_CODE).json()["error"]["code"] == "bot_not_configured"


def test_bind_code_409_without_a_webhook_secret(client, client2, sms, bot, monkeypatch, telegram):
    """Пустой секрет вебхука отвергает всё, что присылает Telegram: код
    выдался бы, админ дошёл бы до бота, а `/start` с ним ушёл бы в 403 —
    и человек не понял бы, почему ничего не произошло."""
    login_admin(client, sms)
    monkeypatch.setattr(get_settings(), "telegram_webhook_secret", "")

    resp = client.post(BIND_CODE)
    assert resp.status_code == 409
    assert resp.json()["error"] == {
        "code": "bot_not_configured",
        "message": "Бот не настроен на сервере — задайте токен в конфигурации",
    }
    # Кода нет вовсе: выдавать заведомо нерабочий незачем
    assert stored_bind() == {}


def test_an_unreadable_expiry_counts_as_expired(client, client2, sms, bot, telegram):
    """Выдать новый код дешевле, чем оставить привязку открытой
    на непонятной строке."""
    login_admin(client, sms)
    first = client.post(BIND_CODE).json()["code"]
    garble_bind_expiry()

    # Кодом уже не привязаться
    assert webhook(client2, start(first)).status_code == 200
    assert telegram.to_chat == [(str(CHAT_ID), WRONG_CODE)]
    assert stored_telegram() == {}

    # А ручка выдаёт новый вместо непонятного
    assert client.post(BIND_CODE).json()["code"] != first


# -- POST /telegram/webhook -----------------------------------------------


def test_webhook_403_on_a_wrong_secret(client, client2, sms, bot, telegram):
    login_admin(client, sms)
    code = client.post(BIND_CODE).json()["code"]

    for secret in (None, "", "wrong-secret", SECRET + "x"):
        resp = webhook(client2, start(code), secret=secret)
        assert resp.status_code == 403, secret
        assert resp.json()["error"]["code"] == "forbidden"

    # Ни привязки, ни погашенного кода: код всё ещё живой
    assert stored_telegram() == {}
    assert telegram.to_chat == []
    assert client.post(BIND_CODE).json()["code"] == code


def test_webhook_403_when_the_secret_is_not_configured(client, monkeypatch, telegram):
    """Пустой секрет в конфигурации означает «отвергать всё», а не «пускать
    всех»: иначе привязка чужого чата открыта всему интернету."""
    monkeypatch.setattr(get_settings(), "telegram_webhook_secret", "")

    for secret in (None, "", "any-secret"):
        resp = webhook(client, start("K7M2XB"), secret=secret)
        assert resp.status_code == 403, secret

    assert stored_telegram() == {}
    assert telegram.to_chat == []


def test_webhook_binds_the_chat_but_never_shows_its_id(client, client2, sms, bot, telegram):
    login_admin(client, sms)
    code = client.post(BIND_CODE).json()["code"]

    resp = webhook(client2, start(code))
    assert resp.status_code == 200
    assert resp.json() == {}
    # Бот отвечает туда, откуда пришла команда
    assert telegram.to_chat == [(str(CHAT_ID), CONNECTED)]

    body = client.get("/admin/settings").json()
    assert body["telegram"]["connected"] is True
    assert body["telegram"]["chat_title"] == "LMS — заявки"
    assert body["telegram"]["connected_at"] is not None
    # chat_id наружу не уходит ни под каким именем
    assert "chat_id" not in json.dumps(body)
    assert str(CHAT_ID) not in json.dumps(body)
    # В базе он при этом есть — уведомлениям нужно, куда слать
    assert stored_telegram()["chat_id"] == str(CHAT_ID)

    # Код одноразовый: сработал — погашен
    telegram.to_chat.clear()
    assert webhook(client2, start(code)).status_code == 200
    assert telegram.to_chat == [(str(CHAT_ID), WRONG_CODE)]


def test_webhook_rejects_a_wrong_or_expired_code(client, client2, sms, bot, telegram):
    login_admin(client, sms)
    code = client.post(BIND_CODE).json()["code"]

    assert webhook(client2, start("ZZZZZZ")).status_code == 200
    assert telegram.to_chat == [(str(CHAT_ID), WRONG_CODE)]
    assert stored_telegram() == {}

    expire_bind_code()
    telegram.to_chat.clear()
    assert webhook(client2, start(code)).status_code == 200
    assert telegram.to_chat == [(str(CHAT_ID), WRONG_CODE)]
    # В настройках не поменялось ничего
    assert stored_telegram() == {}
    assert client.get("/admin/settings").json()["telegram"]["connected"] is False


def test_webhook_answers_200_to_anything(client, sms, bot, telegram):
    """Telegram на любой не-200 повторяет доставку по нарастающей: один
    неудачный разбор превратился бы в бесконечный поток."""
    login_admin(client, sms)
    code = client.post(BIND_CODE).json()["code"]

    updates = [
        {},
        {"message": {}},
        {"message": {"chat": {"id": CHAT_ID}}},
        # Не /start: бот понимает одну команду
        {"message": {"chat": CHAT, "text": "Здравствуйте"}},
        # Мусор вместо ожидаемых типов
        {"message": {"chat": "не объект", "text": ["/start", code]}},
        {"message": [1, 2, 3]},
    ]
    for update in updates:
        resp = webhook(client, update)
        assert resp.status_code == 200, update
        assert resp.json() == {}

    # Тело, которое вообще не JSON
    resp = webhook(client, None, raw=b"\x00 not json at all")
    assert resp.status_code == 200
    assert resp.json() == {}

    # Ни привязки, ни ответов боту, а код цел
    assert stored_telegram() == {}
    assert telegram.to_chat == []
    assert client.post(BIND_CODE).json()["code"] == code


def test_webhook_names_a_private_chat_by_its_owner(client, client2, sms, bot, telegram):
    """У личного чата нет title — иначе на экране админа поле осталось бы
    пустым, и он не понял бы, куда привязано."""
    login_admin(client, sms)
    bind(client, telegram, id=555001, title=None, first_name="Аскар", last_name="Дуйсенов")

    assert client.get("/admin/settings").json()["telegram"]["chat_title"] == "Аскар Дуйсенов"


def test_webhook_names_a_chat_without_a_name_at_all(client, sms, bot, telegram):
    login_admin(client, sms)
    bind(client, telegram, id=555002, title=None)

    assert client.get("/admin/settings").json()["telegram"]["chat_title"] == "Чат 555002"


# Медленная отправка в Telegram — не выдумка теста: вебхук переотправляется
# именно тогда, когда бот отвечает долго.
SLOW_SEC = 1.5


def test_webhook_does_not_hold_up_other_requests(client, sms, bot, telegram, monkeypatch):
    """Тело вебхука работает вне цикла событий.

    На цикле событий синхронная отправка в Telegram встаёт поперёк всех
    запросов сразу, а несколько одновременных апдейтов с одним кодом вешают
    процесс насмерть: INSERT берёт блокировку строки, второй такой же
    блокирует сам поток цикла, и коммит первого некому запланировать.
    """
    login_admin(client, sms)
    code = client.post(BIND_CODE).json()["code"]

    def slow_send(chat_id: str, text: str) -> None:
        time.sleep(SLOW_SEC)

    monkeypatch.setattr(telegram, "send_to", slow_send)

    def health_while_the_hook_works():
        # Дать вебхуку дойти до медленной отправки
        time.sleep(SLOW_SEC / 5)
        started = time.monotonic()
        resp = client.get("/health")
        return resp.status_code, time.monotonic() - started

    hook, (status, elapsed) = in_parallel(
        lambda: webhook(client, start(code)),
        health_while_the_hook_works,
    )

    assert hook.status_code == 200
    assert status == 200
    assert elapsed < SLOW_SEC / 3, f"посторонний запрос ждал {elapsed:.2f} с"


# -- POST /admin/settings/telegram/test -----------------------------------


def test_test_message_409_without_binding(client, sms, bot, telegram):
    login_admin(client, sms)

    resp = client.post(TEST)
    assert resp.status_code == 409
    assert resp.json()["error"] == {
        "code": "telegram_not_connected",
        "message": "Бот не подключён — сначала привяжите чат",
    }
    assert telegram.to_chat == []


def test_test_message_204_and_without_personal_data(client, sms, bot, telegram):
    login_admin(client, sms)
    bind(client, telegram)

    assert client.post(TEST).status_code == 204
    chat_id, message = telegram.to_chat[0]
    assert chat_id == str(CHAT_ID)
    assert message == TEST_MESSAGE
    # В бот не уходят ни ФИО, ни телефоны — ни здесь, ни в настоящих сообщениях
    assert "Нурланова" not in message
    assert "7000000099" not in message


def test_test_message_502_when_telegram_refuses(client, sms, bot, telegram):
    login_admin(client, sms)
    bind(client, telegram)

    app.dependency_overrides[get_telegram] = BoomTelegram
    try:
        resp = client.post(TEST)
    finally:
        app.dependency_overrides[get_telegram] = lambda: telegram
    assert resp.status_code == 502
    assert resp.json()["error"] == {
        "code": "telegram_failed",
        "message": "Telegram не принял сообщение — попробуйте позже",
    }
    # Привязка на месте: не принятое сообщение её не отменяет
    assert client.get("/admin/settings").json()["telegram"]["connected"] is True


# -- POST /admin/settings/telegram/unbind ---------------------------------


def test_unbind_erases_the_chat_and_keeps_the_flags(client, sms, bot, telegram):
    login_admin(client, sms)
    bind(client, telegram)
    client.patch("/admin/settings", json={"telegram": {"notify_leads": False}})

    assert client.post(UNBIND).status_code == 204

    assert client.get("/admin/settings").json()["telegram"] == {
        "connected": False,
        "chat_title": None,
        "connected_at": None,
        # Отвязали чат — не значит передумали получать заявки
        "notify_leads": False,
        "notify_submissions": True,
    }
    assert "chat_id" not in stored_telegram()
    # Уведомлять больше некуда, но и ошибкой это не становится
    assert client.post(TEST).status_code == 409


def test_unbind_on_a_bot_that_was_never_bound(client, sms, bot):
    login_admin(client, sms)

    assert client.post(UNBIND).status_code == 204
    assert client.post(UNBIND).status_code == 204
    assert client.get("/admin/settings").json()["telegram"]["connected"] is False


def test_unbind_kills_the_code_it_had_already_issued(client, client2, sms, bot, telegram):
    """Код, продиктованный по телефону и не использованный, после «Отвязать»
    прожил бы свои 10 минут и привязал чат обратно — уже после того, как
    админ передумал."""
    login_admin(client, sms)
    code = client.post(BIND_CODE).json()["code"]

    assert client.post(UNBIND).status_code == 204

    assert webhook(client2, start(code)).status_code == 200
    assert telegram.to_chat == [(str(CHAT_ID), WRONG_CODE)]
    assert stored_telegram() == {}
    assert client.get("/admin/settings").json()["telegram"]["connected"] is False


def test_unbind_survives_a_flag_saved_at_the_same_time(
    client, client2, sms, bot, telegram, monkeypatch
):
    """«Отвязать» и переключатели уведомлений живут на одной вкладке экрана,
    и чтение-изменение-запись всей строки настройки отменяло одно другим:
    экран показывал «не подключён», а chat_id оставался в базе — и заявки
    с телефонами учителей продолжали уходить в чат, который считается
    отвязанным.

    Победитель тут не важен: слияние на стороне базы даёт один и тот же
    итог при любом порядке — чата нет, флаг сохранён.
    """
    login_admin(client, sms)
    login_admin(client2, sms, phone="+7 (700) 000-00-98")
    bind(client, telegram)
    meet_at(monkeypatch, SessionRepo, "by_token_hash")

    unbind, patch = in_parallel(
        lambda: client.post(UNBIND),
        lambda: client2.patch("/admin/settings", json={"telegram": {"notify_leads": False}}),
    )

    assert unbind.status_code == 204
    assert patch.status_code == 200, patch.text
    # Строка настройки целиком, а не через ответ экрана: там chat_id не виден,
    # а болит именно он — заявки уходят по нему. Спрашиваем базу напрямую
    # ещё и потому, что барьер стоит до конца теста и следующий HTTP-запрос
    # ждать было бы некому.
    assert stored_telegram() == {"notify_leads": False}


# -- флаги типов сообщений ------------------------------------------------


def test_notify_leads_off_silences_the_message_not_the_lead(client, client2, sms, telegram):
    """Выключенный переключатель на экране обязан что-то значить — но заявка
    складывается в админке в любом случае."""
    login_admin(client, sms)
    client.patch("/admin/settings", json={"telegram": {"notify_leads": False}})
    course = make_course()

    login(client2, sms)
    resp = client2.post(f"/courses/{course.id}/lead")

    assert resp.status_code == 200, resp.text
    assert telegram.sent == []
    assert client.get("/admin/leads").json()["total"] == 1


def test_notify_submissions_off_silences_the_message_not_the_work(
    client, client2, sms, storage, telegram
):
    login_admin(client, sms)
    client.patch("/admin/settings", json={"telegram": {"notify_submissions": False}})
    course = make_course()
    task = make_task(make_module(course.id).id, submit_format="text")

    login(client2, sms)
    make_enrollment(user_id(client2), course.id)
    resp = client2.post(f"/tasks/{task.id}/submissions", json={"text": "Дескрипторы"})

    assert resp.status_code == 200, resp.text
    assert telegram.sent == []
    # Работа всё равно в очереди проверки: Telegram — доставка, не хранилище
    assert client.get("/admin/submissions").json()["total"] == 1


def test_the_other_flag_stays_on(client, client2, sms, telegram):
    """Флаги независимы: выключенные заявки не глушат работы на проверку."""
    login_admin(client, sms)
    client.patch("/admin/settings", json={"telegram": {"notify_submissions": False}})
    course = make_course(title="Оценивание для учителей")

    login(client2, sms)
    client2.post(f"/courses/{course.id}/lead")

    assert len(telegram.sent) == 1
    assert "Оценивание для учителей" in telegram.sent[0]


# -- настоящий адаптер ----------------------------------------------------


class FakeResponse:
    """Ответ Bot API: адаптеру от него нужно только тело."""

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_bot_adapter_sends_and_notices_a_refusal(monkeypatch):
    """Отказ Bot API приезжает с кодом 200 и `ok: false` в теле: «сообщение
    не ушло» обязано отличаться от «ушло»."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append((request.full_url, json.loads(request.data), timeout))
        return FakeResponse({"ok": len(calls) == 1})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    adapter = TelegramBot("123456:AA-fake-token", str(CHAT_ID))

    adapter.notify_admins("Новая заявка №1 на курс „Оценивание“")
    url, payload, timeout = calls[0]
    assert url == "https://api.telegram.org/bot123456:AA-fake-token/sendMessage"
    assert payload == {"chat_id": str(CHAT_ID), "text": "Новая заявка №1 на курс „Оценивание“"}
    # Без таймаута зависший Telegram вешает запрос, внутри которого отправка
    assert timeout == 5

    with pytest.raises(TelegramError):
        adapter.send_to(str(CHAT_ID), TEST_MESSAGE)


def test_bot_adapter_notices_an_error_code(monkeypatch, caplog):
    """Bot API отвечает и кодами: 429 на потолок, 403 на выгнанного из чата
    бота. В лог уходит код — ни текста сообщения, ни тела ответа."""

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING, logger="telegram"):
        with pytest.raises(TelegramError):
            TelegramBot("123456:AA-fake-token", str(CHAT_ID)).notify_admins("Новая заявка №1")
    assert "429" in caplog.text
    assert "Новая заявка" not in caplog.text


def test_bot_adapter_notices_an_unreachable_telegram(monkeypatch, caplog):
    """Сеть отвалилась или вышел таймаут: сообщение не ушло, и это обязано
    отличаться от «ушло»."""

    def fake_urlopen(request, timeout=None):
        raise TimeoutError("сокет молчит")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING, logger="telegram"):
        with pytest.raises(TelegramError):
            TelegramBot("123456:AA-fake-token", str(CHAT_ID)).send_to(str(CHAT_ID), TEST_MESSAGE)
    assert TEST_MESSAGE not in caplog.text


def test_log_stub_keeps_the_chat_id_out_of_the_log(caplog):
    """Бот публичный: любой, кто написал ему `/start`, оставил бы в логе свой
    Telegram-идентификатор. Из тела апдейта в лог не уходит ничего."""
    with caplog.at_level(logging.INFO, logger="telegram"):
        LogTelegram().send_to(str(CHAT_ID), CONNECTED)

    # Сам текст ответа в логе нужен: без него заглушку не отладить
    assert CONNECTED in caplog.text
    assert str(CHAT_ID) not in caplog.text


def test_bot_without_a_bound_chat_says_nothing(monkeypatch):
    """Бот не привязан — уведомлять некуда. Это не сбой доставки: заявка
    уже в админке, и падать сценарию не на чем."""

    def fail(*args, **kw):
        raise AssertionError("запроса к Telegram быть не должно")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    TelegramBot("123456:AA-fake-token", None).notify_admins("Новая заявка №1")


def test_the_provider_is_chosen_by_configuration(monkeypatch):
    """Выбор адаптера — строчкой конфигурации, а не `if` в месте вызова.
    Настоящему боту чат достаётся из настроек площадки, заглушке он не нужен."""
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "123456:AA-fake-token")
    with OrmSession(get_engine()) as db:
        SettingRepo(db).put(TELEGRAM_KEY, {"chat_id": str(CHAT_ID)})
        db.commit()

        monkeypatch.setattr(get_settings(), "telegram_provider", "bot")
        adapter = get_telegram(db)
        assert isinstance(adapter, TelegramBot)
        assert adapter.chat_id == str(CHAT_ID)

        monkeypatch.setattr(get_settings(), "telegram_provider", "log")
        assert isinstance(get_telegram(db), LogTelegram)


# -- сессия 8: одноразовость кода, потолок попыток, повторы, размер тела ----


def test_two_starts_with_one_code_bind_a_single_chat(client, sms, telegram, bot, monkeypatch):
    """Код одноразовый и под одновременными запросами тоже.

    Раньше гашение шло чтением и записью: оба `/start` успевали прочитать
    живой код, и побеждал последний — второй чат перезаписывал `chat_id`
    первого, а первый об этом не узнавал. Теперь сравнивает база, и второму
    достаётся ноль изменённых строк.
    """
    login_admin(client, sms)
    code = client.post(BIND_CODE).json()["code"]
    # Обе стороны выходят из чтения строки настроек одновременно — без этого
    # запросы почти всегда успевают разойтись по времени
    meet_at(monkeypatch, SettingRepo, "get")

    first, second = in_parallel(
        lambda: webhook(client, start(code, id=-100111, title="Первый чат")),
        lambda: webhook(client, start(code, id=-100222, title="Второй чат")),
    )
    monkeypatch.undo()

    # Вебхук отвечает 200 обоим: свой код ответа означал бы переотправку
    assert [first.status_code, second.status_code] == [200, 200]
    # А чат привязан ровно один, и отказ ушёл ровно один
    connected = [text for _, text in telegram.to_chat if text == CONNECTED]
    refused = [text for _, text in telegram.to_chat if text == WRONG_CODE]
    assert len(connected) == 1, telegram.to_chat
    assert len(refused) == 1, telegram.to_chat
    assert stored_telegram()["chat_id"] in ("-100111", "-100222")


def test_after_ten_misses_the_bot_goes_quiet_but_the_code_lives(client, sms, telegram, bot):
    """Промахи гасят ОТВЕТ бота, а не код.

    Сначала было сделано наоборот — десять промахов гасили код, — и это
    оказалось хуже дыры, которую закрывало: бот публичный, и любой прохожий
    десятью сообщениями оставлял админа без привязки, повторяемо, на каждый
    новый код. Подбор угрозой не был и без потолка: миллиард вариантов
    на 10 минут. Потолок остаётся про исходящие сообщения — они стоят денег
    и лимитов Telegram.
    """
    login_admin(client, sms)
    code = client.post(BIND_CODE).json()["code"]
    wrong = "ZZZZZZ" if code != "ZZZZZZ" else "YYYYYYY"

    # Десять промахов из ЧУЖОГО чата — ровно то, чем ломали привязку
    for number in range(MAX_ATTEMPTS):
        assert webhook(client, start(wrong, id=-900_000 - number)).status_code == 200
    assert [text for _, text in telegram.to_chat] == [WRONG_CODE] * MAX_ATTEMPTS

    # Одиннадцатый промах остаётся без ответа: исходящие кончились
    telegram.to_chat.clear()
    assert webhook(client, start(wrong, id=-900_999)).status_code == 200
    assert telegram.to_chat == []

    # А код цел, и админ по нему привязывается
    assert webhook(client, start(code)).status_code == 200
    assert telegram.to_chat[-1][1] == CONNECTED
    assert stored_telegram()["chat_id"] == str(CHAT_ID)


def test_a_new_code_gives_the_bot_its_voice_back(client, sms, telegram, bot):
    """Счётчик промахов живёт вместе с кодом: выдали новый — считаем заново.
    Иначе замолчавший однажды бот молчал бы навсегда."""
    login_admin(client, sms)
    wrong = "ZZZZZZ"
    for _ in range(MAX_ATTEMPTS + 1):
        webhook(client, start(wrong))

    telegram.to_chat.clear()
    # Новый код переписывает строку настроек целиком — вместе со счётчиком
    client.post(UNBIND)
    client.post(BIND_CODE)
    assert webhook(client, start(wrong)).status_code == 200
    assert telegram.to_chat[-1][1] == WRONG_CODE


def test_a_bare_start_does_not_spend_an_attempt(client, sms, telegram, bot):
    """`/start` без кода — любопытный посетитель публичного бота, а не
    промах: тратить на него потолок попыток незачем."""
    login_admin(client, sms)
    code = client.post(BIND_CODE).json()["code"]

    for _ in range(MAX_ATTEMPTS + 5):
        assert webhook(client, {"message": {"chat": CHAT, "text": "/start"}}).status_code == 200

    # Код цел: продиктованный админу по телефону, он пережил чужое любопытство
    assert webhook(client, start(code)).status_code == 200
    assert telegram.to_chat[-1][1] == CONNECTED


def test_a_fat_webhook_body_is_not_parsed(client, sms, telegram, bot, caplog):
    """Тело больше потолка не разбирается вовсе, а ответ тот же 200: свой
    код ответа заставил бы Telegram переотправлять то же тело по нарастающей."""
    login_admin(client, sms)
    code = client.post(BIND_CODE).json()["code"]

    # Настоящая команда, утопленная в мусоре: разберись сервер с телом —
    # чат бы привязался
    fat = json.dumps(
        {**start(code)["message"], "padding": "x" * MAX_BODY_BYTES}
    ).encode()
    assert len(fat) > MAX_BODY_BYTES

    with caplog.at_level(logging.WARNING, logger="telegram"):
        assert webhook(client, None, raw=b'{"message": ' + fat + b"}").status_code == 200

    assert stored_telegram().get("chat_id") is None
    assert telegram.to_chat == []
    # В логе остаётся факт и потолок, самого тела там быть не должно
    assert str(MAX_BODY_BYTES) in caplog.text
    assert code not in caplog.text


def test_admin_notification_is_retried_when_telegram_is_unreachable(monkeypatch, caplog):
    """Раздел 11 BACKEND_NOTES обещает отправку с повторами. Недоставку
    повторяем — сеть моргает чаще, чем Telegram отказывает."""
    calls = []
    pauses = []

    def fake_urlopen(request, timeout=None):
        calls.append(timeout)
        raise TimeoutError("сокет молчит")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    # Подменяем модуль целиком, а не `time.sleep` в нём: `bot.time` — это сам
    # stdlib-модуль, и правка его атрибута отменяла бы паузы всему процессу.
    # Паузы не выжидаем, а записываем: полторы секунды на тест — это дорого
    monkeypatch.setattr(bot_module, "time", SimpleNamespace(sleep=pauses.append))

    with caplog.at_level(logging.WARNING, logger="telegram"):
        with pytest.raises(TelegramError):
            TelegramBot("123456:AA-fake-token", str(CHAT_ID)).notify_admins("Новая заявка №1")

    assert len(calls) == ATTEMPTS
    assert pauses == [PAUSE_SEC] * (ATTEMPTS - 1)
    assert "Новая заявка" not in caplog.text


def test_admin_notification_is_not_retried_when_telegram_refuses(monkeypatch):
    """Осмысленный отказ повторять бессмысленно: то же сообщение не примут
    и со второй попытки, а заявка учителя ждёт ответа всё это время."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(bot_module, "time", SimpleNamespace(sleep=_no_sleep))

    with pytest.raises(TelegramError):
        TelegramBot("123456:AA-fake-token", str(CHAT_ID)).notify_admins("Новая заявка №1")
    assert len(calls) == 1


def test_a_five_hundred_from_telegram_is_retried(monkeypatch):
    """Пятисотый — это сбой на той стороне: сообщение не отвергнуто,
    оно не доставлено, и такое повторяют."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(bot_module, "time", SimpleNamespace(sleep=_no_sleep))

    with pytest.raises(TelegramError):
        TelegramBot("123456:AA-fake-token", str(CHAT_ID)).notify_admins("Новая заявка №1")
    assert len(calls) == ATTEMPTS


def _no_sleep(_seconds):
    """Повторы проверяются числом попыток, а не тем, сколько тест простоял."""
