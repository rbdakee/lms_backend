"""Приёмка сессии 8: лимиты и защита под нагрузкой.

Числа проверяются поведением, а не чтением конфигурации: где именно
срабатывает потолок, по какому ключу он считается, переживает ли перезапуск
процесса и что от него остаётся, когда воркеров больше одного.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.adapters.db.repos import AuthCodeRepo, SessionRepo, UserRepo
from app.api.deps import get_playback_limiter, get_thread_limiter, get_verify_limiter
from app.application import auth as auth_module
from app.application.ratelimit import SlidingWindowLimiter
from app.config import get_settings
from app.main import app
from tests.conftest import (
    NORM,
    PHONE,
    FakeClock,
    age_codes,
    in_parallel,
    login,
    make_certificate,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_user,
    meet_at,
    request_code,
    user_id,
)

CFG = get_settings()

# Сколько воркеров uvicorn обсуждается в отчёте: словарь лимитера у каждого
# процесса свой, и потолок умножается ровно на их число.
WORKERS = 4


def phone_no(number: int) -> str:
    """Вымышленный номер в нормализованном виде: потолок на адрес общий,
    и упереться в него можно только десятками разных номеров."""
    return f"+7777{number:03d}0011"


def seed_codes(count: int, *, ip: str) -> None:
    """Строки auth_code мимо приложения: так их видит соседний воркер и так
    они выглядят после перезапуска процесса."""
    with get_engine().begin() as conn:
        for number in range(count):
            conn.execute(
                text(
                    "INSERT INTO auth_code (phone, code_hash, ip, expires_at)"
                    " VALUES (:phone, 'seeded', :ip, now() + interval '5 minutes')"
                ),
                {"phone": phone_no(number), "ip": ip},
            )


def codes_for(phone: str = NORM) -> int:
    with get_engine().begin() as conn:
        return conn.execute(
            text("SELECT count(*) FROM auth_code WHERE phone = :phone"), {"phone": phone}
        ).scalar()


def code_ips() -> set:
    with get_engine().begin() as conn:
        return {row[0] for row in conn.execute(text("SELECT DISTINCT ip FROM auth_code"))}


def thread_messages() -> int:
    with get_engine().begin() as conn:
        return conn.execute(text("SELECT count(*) FROM thread_message")).scalar()


def attempts_on_last_code(phone: str = NORM) -> int:
    """Попытки у свежего кода: действует всегда последний отправленный,
    а у погашенных предшественников счётчик остаётся своим."""
    with get_engine().begin() as conn:
        return conn.execute(
            text(
                "SELECT attempts FROM auth_code WHERE phone = :phone"
                " ORDER BY created_at DESC LIMIT 1"
            ),
            {"phone": phone},
        ).scalar()


def expire_block() -> None:
    """Сдвигает блок ввода в прошлое: age_codes трогает created_at
    и expires_at, а blocked_until — нет, и десять минут в тесте не переждёшь."""
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE auth_code SET blocked_until = now() - interval '1 minute'"
                " WHERE blocked_until IS NOT NULL"
            )
        )


def wrong_code(sms) -> str:
    _, real = sms.sent[-1]
    return "0000" if real != "0000" else "1111"


def guess_wrong(client, sms, times: int) -> list:
    """Подбор кода: заведомо неверные попытки подряд, ответы — списком."""
    wrong = wrong_code(sms)
    return [
        client.post("/auth/verify_code", json={"phone": PHONE, "code": wrong})
        for _ in range(times)
    ]


def with_deadline(job, seconds: float = 20):
    """`in_parallel` ждёт результата без срока, а прошлая сессия нашла ровно
    зависание процесса под одновременными запросами: без этого обёртки
    прогон встал бы навсегда вместо того, чтобы упасть тестом.

    Поток демонский — зависший, он не помешает прогону дойти до конца.
    """
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = job()
        except Exception as exc:
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        pytest.fail(f"одновременные запросы не ответили за {seconds} с — зависание")
    if "error" in box:
        raise box["error"]
    return box["value"]


def all_in_parallel(jobs: list):
    """`in_parallel` из conftest сводит двоих, а здесь нужна пачка: потерянное
    обновление счётчика попыток тем крупнее, чем шире залп."""
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        return [job.result() for job in [pool.submit(one) for one in jobs]]


def worker_limiter(limit: int):
    """Соседний воркер uvicorn: свой процесс — свой пустой словарь и свои часы."""
    limiter = SlidingWindowLimiter(limit, 60, FakeClock())
    return lambda: limiter


def certificate_in_registry(number: str = "KZ-2026-XB7K2M") -> str:
    """Документ в реестре: проверяющий никуда не входит — на то она
    и публичная проверка."""
    holder = make_user(phone_no(500))
    make_certificate(holder.id, make_course().id, number=number)
    return number


def video_lesson():
    course = make_course()
    return course, make_lesson(make_module(course.id).id)


DAY_LIMIT_MESSAGE = "Лимит SMS на сегодня исчерпан — попробуйте позже"


# -- 1. SMS: потолок на номер ------------------------------------------


def test_phone_cap_trips_on_the_eleventh_and_stays_shut(client, sms):
    """Потолок ровно `phone_codes_per_day`: десятый код уходит, одиннадцатый
    уже нет, и второго окна из десяти за ним не открывается."""
    for number in range(CFG.phone_codes_per_day):
        assert request_code(client).status_code == 200, number
        # Иначе упрёмся в повтор раз в минуту, а проверяем суточный потолок
        age_codes(2)
    assert len(sms.sent) == 10

    for _ in range(3):
        resp = request_code(client)
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "rate_limited"
        # Именно суточный потолок, а не «код уже отправлен»: сообщения разные
        assert resp.json()["error"]["message"] == DAY_LIMIT_MESSAGE
    assert len(sms.sent) == 10, "отказ не должен стоить ни одной SMS"


def test_phone_day_window_slides_and_ignores_the_calendar(client, sms):
    """«В сутки» — скользящие 24 часа, а не календарный день: коды, которым
    по календарю уже «вчера», держат потолок, пока им не исполнится сутки."""
    for _ in range(CFG.phone_codes_per_day):
        assert request_code(client).status_code == 200
        age_codes(2)
    assert request_code(client).status_code == 429

    age_codes(23 * 60)  # по календарю это вчерашняя ночь
    assert request_code(client).json()["error"]["message"] == DAY_LIMIT_MESSAGE

    age_codes(70)  # а теперь самому свежему коду больше 24 часов
    assert request_code(client).status_code == 200


# -- 1. SMS: потолок на адрес ------------------------------------------


def test_ip_cap_is_one_bucket_for_every_phone_behind_the_address(client, sms):
    """Потолок на адрес общий на все номера: тридцать чужих кодов с того же
    адреса закрывают вход тридцать первому, у которого своих кодов ноль.

    Ключ — get_client_ip, а при `trust_forwarded_for=False` за nginx этот
    адрес у всей страны один. Тридцать SMS в сутки на всех — см. отчёт.
    """
    for number in range(CFG.ip_codes_per_day):
        assert request_code(client, phone=phone_no(number)).status_code == 200, number
    assert len(sms.sent) == 30
    assert code_ips() == {"testclient"}, "все тридцать легли в одно ведро"

    victim = request_code(client, phone=phone_no(900))
    assert victim.status_code == 429
    assert victim.json()["error"]["message"] == DAY_LIMIT_MESSAGE
    assert len(sms.sent) == 30


def test_sms_counter_lives_in_the_database_not_in_the_process(client, sms):
    """Счётчик SMS — строки auth_code, а не память процесса: коды, положенные
    в базу мимо приложения (так их кладёт соседний воркер и так они лежат
    после перезапуска), считаются наравне с отправленными этим процессом."""
    seed_codes(CFG.ip_codes_per_day - 1, ip="testclient")

    assert request_code(client, phone=phone_no(900)).status_code == 200  # тридцатый
    over = request_code(client, phone=phone_no(901))
    assert over.status_code == 429
    assert over.json()["error"]["message"] == DAY_LIMIT_MESSAGE


def test_ip_cap_is_not_counted_without_a_client_address(sms):
    """`request.client is None` (бывает за некоторыми прокси) — и потолок
    на адрес не считается вовсе: `ip` уходит в базу пустым, а проверка
    пропускается целиком, сколько бы кодов до этого ни ушло."""
    seed_codes(CFG.ip_codes_per_day, ip="testclient")

    with TestClient(app, client=None) as anonymous:
        for number in range(900, 906):
            assert request_code(anonymous, phone=phone_no(number)).status_code == 200, number

    assert len(sms.sent) == 6
    assert code_ips() == {"testclient", None}


# -- 2. Подбор кода входа ----------------------------------------------


def test_block_is_not_bypassed_by_a_new_code(client, sms):
    """Блокировку ввода новым кодом не обойти: пока она держится, отказывает
    и `verify_code`, и `request_code`. Но держится она десять минут, а после
    неё новый код приходит с нулём попыток — блок не стоит ничего, кроме
    времени, и подбор продолжается с чистого счётчика."""
    assert request_code(client).status_code == 200
    assert [r.status_code for r in guess_wrong(client, sms, CFG.code_max_attempts)] == [
        400,
        400,
        429,
    ]

    age_codes(2)  # повтор раз в минуту тут уже ни при чём
    blocked = request_code(client)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "too_many_attempts"

    expire_block()
    assert request_code(client).status_code == 200
    assert attempts_on_last_code() == 0, "у нового кода свой счётчик попыток"
    assert [r.status_code for r in guess_wrong(client, sms, CFG.code_max_attempts)] == [
        400,
        400,
        429,
    ]


def test_thirty_guesses_a_day_for_one_number(client, sms):
    """Сколько подборов четырёхзначного кода реально доступно за сутки.

    Кодов в сутки десять, у каждого три попытки ввода — тридцать подборов.
    Блок на десять минут числа не уменьшает: он лишь растягивает сутки,
    а `code_resend_sec` (60 секунд) при десяти кодах в сутки не связывает
    вовсе. Тридцать из 10000 вариантов — 0.3% за сутки на один номер.
    """
    guesses = 0
    for _ in range(CFG.phone_codes_per_day):
        assert request_code(client).status_code == 200
        answers = guess_wrong(client, sms, CFG.code_max_attempts)
        assert [r.status_code for r in answers] == [400, 400, 429]
        guesses += len(answers)
        expire_block()
        age_codes(2)

    assert guesses == 30
    exhausted = request_code(client)
    assert exhausted.status_code == 429
    assert exhausted.json()["error"]["message"] == DAY_LIMIT_MESSAGE


def test_two_wrong_codes_at_once_cost_two_attempts(client, client2, sms, monkeypatch):
    """Счётчик попыток считает база под занятой строкой: два одновременных
    неверных кода стоят двух попыток, и «осталось» у них разное.

    Потерянное обновление было бы лишним подбором, и оно масштабируется
    параллелизмом: сколько подборов уместится в одну пачку, столько
    и обошлось бы в одну попытку.
    """
    assert request_code(client).status_code == 200
    wrong = wrong_code(sms)
    meet_at(monkeypatch, AuthCodeRepo, "active_for_phone")

    first, second = with_deadline(
        lambda: in_parallel(
            lambda: client.post("/auth/verify_code", json={"phone": PHONE, "code": wrong}),
            lambda: client2.post("/auth/verify_code", json={"phone": PHONE, "code": wrong}),
        )
    )
    monkeypatch.undo()  # барьер на двоих: дальше запросы идут по одному

    assert [first.status_code, second.status_code] == [400, 400]
    # Кто из двоих занял строку первым — дело случая, но ответы разные:
    # «осталось 2» обоим означало бы, что вторая попытка ничего не стоила
    left = sorted(r.json()["error"]["details"]["attempts_left"] for r in (first, second))
    assert left == [1, 2]
    assert attempts_on_last_code() == 2

    # И третий подбор у этого кода последний, а не четвёртый
    (third,) = guess_wrong(client, sms, 1)
    assert third.json()["error"]["code"] == "too_many_attempts"


def test_a_volley_of_guesses_gets_no_more_tries_than_the_limit(client, sms, monkeypatch):
    """Сколько подборов сервер на самом деле сверит с кодом, если прислать их
    пачкой. Считаем сверки, а не ответы: 429 приходит и от исчерпанных попыток,
    и от уже поставленного блока, а по коду ответа их не различить.

    Восемь одновременных запросов — три сверки, ровно столько, сколько
    настроено попыток: строка кода занята до сверки, и опоздавшим сверять
    уже нечего. Барьер для этого не нужен: гонка ловилась сама, без всякой
    помощи со стороны теста.
    """
    assert request_code(client).status_code == 200
    wrong = wrong_code(sms)
    compared: list[str] = []
    hash_code = auth_module._hash_code

    def counting(phone: str, code: str) -> str:
        compared.append(code)
        return hash_code(phone, code)

    monkeypatch.setattr(auth_module, "_hash_code", counting)

    volley = [TestClient(app) for _ in range(8)]
    answers = with_deadline(
        lambda: all_in_parallel(
            [
                (lambda c=one: c.post("/auth/verify_code", json={"phone": PHONE, "code": wrong}))
                for one in volley
            ]
        )
    )

    assert len(answers) == 8
    assert len(compared) == CFG.code_max_attempts, (
        f"сверок с кодом {len(compared)} при потолке в {CFG.code_max_attempts} попытки"
    )
    assert attempts_on_last_code() == len(compared), "счётчик считает ровно сверки"
    # Две попытки ответили «неверный код», третья закрыла ввод, остальные
    # пятеро до сверки не дошли вовсе
    assert sorted(r.status_code for r in answers) == [400, 400] + [429] * 6


# -- 3. Лимитеры в памяти процесса: ключ -------------------------------


def test_playback_cap_is_per_user_not_per_address(client, client2, sms):
    """Ключ playback — user_id: исчерпавший потолок сосед по адресу
    не мешает второму человеку, хотя адрес у обоих один."""
    course, lesson = video_lesson()
    login(client, sms)
    login(client2, sms, phone=phone_no(1))
    make_enrollment(user_id(client), course.id)
    make_enrollment(user_id(client2), course.id)

    for _ in range(CFG.playback_per_min):
        assert client.get(f"/lessons/{lesson.id}/playback").status_code == 200
    assert client.get(f"/lessons/{lesson.id}/playback").status_code == 429
    assert client2.get(f"/lessons/{lesson.id}/playback").status_code == 200


def test_question_cap_is_per_user_not_per_address(client, client2, sms):
    """Ключ формы вопроса — тоже user_id, и по той же причине: потолок ставят
    на человека, а не на класс, сидящий за одним школьным роутером."""
    course, lesson = video_lesson()
    login(client, sms)
    login(client2, sms, phone=phone_no(1))
    make_enrollment(user_id(client), course.id)
    make_enrollment(user_id(client2), course.id)
    ask = {"text": "Дескрипторы на 34 человека реально успеть?", "parent_id": None}

    for _ in range(CFG.thread_messages_per_min):
        assert client.post(f"/lessons/{lesson.id}/questions", json=ask).status_code == 201
    assert client.post(f"/lessons/{lesson.id}/questions", json=ask).status_code == 429
    assert client2.post(f"/lessons/{lesson.id}/questions", json=ask).status_code == 201


def test_verify_cap_is_per_address_not_per_person(client, client2, sms):
    """Ключ публичной проверки — адрес: два разных браузера с одного адреса
    делят одно ведро на двадцать проверок в минуту."""
    number = certificate_in_registry()

    for _ in range(10):
        assert client.get(f"/verify/{number}").status_code == 200
    for _ in range(10):
        assert client2.get(f"/verify/{number}").status_code == 200

    assert client.get(f"/verify/{number}").status_code == 429
    assert client2.get(f"/verify/{number}").status_code == 429


# -- 1 и 3. Откуда берётся адрес: X-Forwarded-For -----------------------


def test_forwarded_header_does_not_split_the_bucket_by_default(client):
    """`trust_forwarded_for=False` — заголовок не смотрят вовсе: два разных
    `X-Forwarded-For` делят одно ведро, потому что ключ берётся из сокета.

    Это шов сессии 6, и он держится. Обратная сторона у него та же самая:
    за nginx сокет один на всю страну.
    """
    number = certificate_in_registry()

    for half in ("9.9.9.9", "8.8.8.8"):
        for _ in range(CFG.verify_per_min // 2):
            resp = client.get(f"/verify/{number}", headers={"x-forwarded-for": half})
            assert resp.status_code == 200
    third = client.get(f"/verify/{number}", headers={"x-forwarded-for": "7.7.7.7"})
    assert third.status_code == 429


def test_trusted_forwarded_header_lets_the_client_pick_its_own_bucket(client, monkeypatch):
    """А включённый `trust_forwarded_for` отдаёт ключ клиенту: берётся первый
    элемент `X-Forwarded-For`, и новый выдуманный адрес открывает новое ведро.

    Первый элемент — ровно тот, который приписал сам клиент: стандартный
    `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for` у nginx
    не переписывает заголовок, а дописывает свой адрес в конец.
    """
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", True)
    number = certificate_in_registry()

    for _ in range(CFG.verify_per_min):
        resp = client.get(f"/verify/{number}", headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1"})
        assert resp.status_code == 200
    assert client.get(
        f"/verify/{number}", headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1"}
    ).status_code == 429

    # Другой первый элемент — другое ведро, и так сколько угодно раз
    assert client.get(
        f"/verify/{number}", headers={"x-forwarded-for": "9.9.9.10, 10.0.0.1"}
    ).status_code == 200


def test_trusted_forwarded_header_also_unlocks_the_daily_sms_cap(client, sms, monkeypatch):
    """Тот же ключ — у суточного потолка SMS: с включённым доверием заголовку
    тридцать кодов с выдуманного адреса не мешают тридцать первому, если
    подписаться другим адресом. Потолок на номер при этом остаётся."""
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", True)

    for number in range(CFG.ip_codes_per_day):
        resp = client.post(
            "/auth/request_code",
            json={"phone": phone_no(number), "consent": True},
            headers={"x-forwarded-for": "9.9.9.9"},
        )
        assert resp.status_code == 200, number
    same = client.post(
        "/auth/request_code",
        json={"phone": phone_no(900), "consent": True},
        headers={"x-forwarded-for": "9.9.9.9"},
    )
    assert same.status_code == 429

    other = client.post(
        "/auth/request_code",
        json={"phone": phone_no(900), "consent": True},
        headers={"x-forwarded-for": "9.9.9.10"},
    )
    assert other.status_code == 200
    assert len(sms.sent) == 31


# -- 1 и 3. Когда адреса нет вовсе -------------------------------------


def test_verify_cap_is_one_bucket_for_everyone_without_an_address():
    """Ключ — `ip or "unknown"`. Нет адреса у запроса (за некоторыми прокси
    и в тестах) — и все анонимы делят одно ведро на двадцать проверок:
    комиссия, проверяющая бумагу, отбивается чужими запросами."""
    number = certificate_in_registry()

    with TestClient(app, client=None) as one, TestClient(app, client=None) as two:
        for _ in range(10):
            assert one.get(f"/verify/{number}").status_code == 200
        for _ in range(10):
            assert two.get(f"/verify/{number}").status_code == 200
        assert one.get(f"/verify/{number}").status_code == 429
        assert two.get(f"/verify/{number}").status_code == 429


# -- 3. Лимитеры в памяти процесса: несколько воркеров ------------------


def test_four_workers_multiply_the_playback_cap(client, sms):
    """Словарь лимитера у каждого процесса свой. Четыре воркера uvicorn —
    четыре независимых счётчика, и один и тот же человек получает сорок
    ссылок в минуту там, где настроено десять."""
    course, lesson = video_lesson()
    login(client, sms)
    make_enrollment(user_id(client), course.id)

    granted = 0
    for _ in range(WORKERS):
        app.dependency_overrides[get_playback_limiter] = worker_limiter(CFG.playback_per_min)
        for _ in range(CFG.playback_per_min):
            granted += client.get(f"/lessons/{lesson.id}/playback").status_code == 200

    assert granted == WORKERS * CFG.playback_per_min == 40


def test_four_workers_multiply_the_verify_cap(client):
    """То же и у публичной проверки: восемьдесят проверок в минуту с адреса
    при настроенных двадцати — перебор реестра идёт вчетверо быстрее."""
    number = certificate_in_registry()

    granted = 0
    for _ in range(WORKERS):
        app.dependency_overrides[get_verify_limiter] = worker_limiter(CFG.verify_per_min)
        for _ in range(CFG.verify_per_min):
            granted += client.get(f"/verify/{number}").status_code == 200

    assert granted == WORKERS * CFG.verify_per_min == 80


def test_four_workers_multiply_the_question_cap(client, sms):
    """И у формы вопроса: двенадцать сообщений в минуту при настроенных трёх."""
    course, lesson = video_lesson()
    login(client, sms)
    make_enrollment(user_id(client), course.id)
    ask = {"text": "Вопрос по уроку", "parent_id": None}

    granted = 0
    for _ in range(WORKERS):
        app.dependency_overrides[get_thread_limiter] = worker_limiter(
            CFG.thread_messages_per_min
        )
        for _ in range(CFG.thread_messages_per_min):
            granted += client.post(f"/lessons/{lesson.id}/questions", json=ask).status_code == 201

    assert granted == WORKERS * CFG.thread_messages_per_min == 12


# -- 3. Лимитеры в памяти процесса: словарь под потоком ключей ----------


def test_forget_cold_costs_what_it_swept_not_what_it_holds():
    """Уборка холодных ключей идёт по-прежнему на каждом новом ключе, но
    стоит вынесенными ключами, а не всеми: словарь держится по времени
    последнего запроса, остывшие лежат подряд в голове, и на первом живом
    ключе обход кончается.

    Ключ публичной проверки задаёт клиент, значит эту работу заказывает
    посторонний, а идёт она в потоке запроса под GIL — то есть эти секунды
    стоят в очереди у всех остальных. Обход всего словаря на каждом ключе
    стоил здесь 14.5 секунды чистого CPU на двадцати тысячах адресов
    за окно; бюджет ниже дан с запасом на медленную машину.
    """
    limiter = SlidingWindowLimiter(CFG.verify_per_min, 60, FakeClock())
    sweeps = 0
    sweep = SlidingWindowLimiter._forget_cold

    def counting(self, now):
        nonlocal sweeps
        sweeps += 1
        sweep(self, now)

    limiter._forget_cold = counting.__get__(limiter, SlidingWindowLimiter)

    keys = [f"10.{number // 256}.{number % 256}.1" for number in range(20000)]
    started = time.process_time()
    for key in keys:
        assert limiter.hit(key) == 0
    spent = time.process_time() - started

    assert sweeps == len(keys), "уборка идёт на каждом новом ключе"
    assert len(limiter.hits) == len(keys), "живые ключи уборка не трогает"
    assert spent < 2, f"уборка съела {spent:.1f} с CPU на {len(keys)} ключах"


def test_cold_keys_are_dropped_only_when_a_cold_key_asks():
    """Память отдаётся не по времени, а по случаю: пока приходят только
    «горячие» ключи, `_forget_cold` не вызывается вовсе и двести остывших
    адресов лежат в словаре сколько угодно долго."""
    clock = FakeClock()
    limiter = SlidingWindowLimiter(CFG.verify_per_min, 60, clock)
    limiter.hit("hot")
    for number in range(200):
        limiter.hit(f"10.0.0.{number}")
    assert len(limiter.hits) == 201

    # Час подряд ходит только горячий ключ: его очередь не пустеет никогда,
    # а уборка стоит именно на пустой очереди
    for _ in range(120):
        clock.shift(30)
        assert limiter.hit("hot") == 0
    assert len(limiter.hits) == 201, "остывшие адреса никто не вынес"

    # Зато первый же новый адрес схлопывает словарь разом
    limiter.hit("10.0.1.1")
    assert set(limiter.hits) == {"hot", "10.0.1.1"}


def test_a_sweep_from_a_neighbour_cannot_erase_a_key_in_flight():
    """Уборка соседа больше не выносит чужой ключ вместе с его запросом:
    `hit` целиком идёт под замком, а ключ заводится уже после уборки.

    Раньше между `setdefault` (очередь пуста) и `append` стояло окно, и сосед,
    зашедший в это время в `_forget_cold`, ключ удалял — запрос обслуживался,
    но не считался. Окно было не мгновенное: внутри него шла собственная
    уборка запроса, тем дольше, чем больше адресов, то есть потолок протекал
    ровно под нагрузкой, ради которой он и заведён.
    """
    limiter = SlidingWindowLimiter(2, 60, FakeClock())
    sweep = SlidingWindowLimiter._forget_cold
    inside = threading.Event()
    let_go = threading.Event()

    def waiting(self, now):
        # Первый вошедший держит уборку открытой: будь `hit` без замка,
        # сосед успел бы за это время завести и потерять свой ключ
        if not inside.is_set():
            inside.set()
            assert let_go.wait(timeout=10)
        sweep(self, now)

    limiter._forget_cold = waiting.__get__(limiter, SlidingWindowLimiter)

    answers: dict[str, int] = {}
    first = threading.Thread(target=lambda: answers.update(a=limiter.hit("a")), daemon=True)
    first.start()
    assert inside.wait(timeout=10), "первый поток не дошёл до уборки"

    neighbour = threading.Thread(target=lambda: answers.update(b=limiter.hit("b")), daemon=True)
    neighbour.start()
    neighbour.join(0.5)
    waited_outside = neighbour.is_alive()  # проверяем после того, как отпустим

    let_go.set()
    first.join(10)
    neighbour.join(10)
    assert waited_outside, "сосед вошёл в лимитер, пока в нём стоял другой поток"
    assert answers == {"a": 0, "b": 0}
    assert set(limiter.hits) == {"a", "b"}, "ключ пережил уборку соседа"

    # И запрос «a» посчитан: при потолке в два третий — уже отказ
    del limiter._forget_cold
    assert limiter.hit("a") == 0
    assert limiter.hit("a") > 0


# -- 4. Одновременные запросы ------------------------------------------


def test_two_request_codes_at_once_send_one_sms(client, client2, sms, monkeypatch):
    """Повтор «не чаще раза в минуту» — это чтение `last_sent_at` и вставка
    строки следом, и оба запроса делают это по очереди: первый отправляет,
    второй получает отказ. Две SMS за секунду на один номер оплачивала бы
    площадка, поэтому считаем именно отправленное.

    Барьер сведён раньше, чем прежде, — на `UserRepo.by_phone`: за ним идёт
    блокировка номера, и второй запрос ждал бы первого уже не в тесте,
    а в базе.
    """
    meet_at(monkeypatch, UserRepo, "by_phone")

    first, second = with_deadline(
        lambda: in_parallel(lambda: request_code(client), lambda: request_code(client2))
    )
    monkeypatch.undo()

    assert sorted([first.status_code, second.status_code]) == [200, 429]
    refused = first if first.status_code == 429 else second
    assert refused.json()["error"]["code"] == "rate_limited"
    assert refused.json()["error"]["details"]["retry_after_sec"] > 0
    assert len(sms.sent) == 1
    # Живой код у номера один: иначе набравший код из первой SMS получил бы
    # «неверный код» — active_for_phone признаёт только последний
    assert codes_for() == 1


def test_two_request_codes_at_once_do_not_overrun_the_daily_cap(
    client, client2, sms, monkeypatch
):
    """Суточный потолок — «прочитать счётчик, потом вставить строку»: два
    запроса, пришедшие вместе на двадцать девятом коде, оба видели двадцать
    девять и оба отправляли. Тридцать первой SMS за сутки не будет.

    Номера у запросов разные, а потолок — на адрес: на одном номере отказ
    пришёл бы от повтора раз в минуту, и потолок остался бы непроверенным.
    """
    seed_codes(CFG.ip_codes_per_day - 1, ip="testclient")

    meet_at(monkeypatch, UserRepo, "by_phone")
    first, second = with_deadline(
        lambda: in_parallel(
            lambda: request_code(client, phone=phone_no(900)),
            lambda: request_code(client2, phone=phone_no(901)),
        )
    )
    monkeypatch.undo()

    assert sorted([first.status_code, second.status_code]) == [200, 429]
    refused = first if first.status_code == 429 else second
    assert refused.json()["error"]["message"] == DAY_LIMIT_MESSAGE
    assert len(sms.sent) == 1
    assert codes_for(phone_no(900)) + codes_for(phone_no(901)) == 1


def test_parallel_playback_from_one_person_keeps_the_count(client, client2, sms, monkeypatch):
    """Два одновременных playback от одного человека спорят за одну очередь
    в словаре. Проверяем два свойства: процесс не встаёт и счётчик сходится —
    после восьми последовательных и двух одновременных потолок закрыт."""
    course, lesson = video_lesson()
    login(client, sms)
    make_enrollment(user_id(client), course.id)
    login(client2, sms)  # второе устройство того же человека: ключ лимитера один

    for _ in range(CFG.playback_per_min - 2):
        assert client.get(f"/lessons/{lesson.id}/playback").status_code == 200

    meet_at(monkeypatch, SessionRepo, "by_token_hash")
    first, second = with_deadline(
        lambda: in_parallel(
            lambda: client.get(f"/lessons/{lesson.id}/playback"),
            lambda: client2.get(f"/lessons/{lesson.id}/playback"),
        )
    )
    monkeypatch.undo()

    assert [first.status_code, second.status_code] == [200, 200]
    assert client.get(f"/lessons/{lesson.id}/playback").status_code == 429


def test_parallel_questions_from_one_person_keep_the_count(client, client2, sms, monkeypatch):
    """То же для формы вопроса, где потолок низкий (три в минуту) и цена
    ошибки — спам в треде урока: два одновременных сообщения на втором
    и третьем месте обязаны закрыть окно, а не оставить его открытым."""
    course, lesson = video_lesson()
    login(client, sms)
    make_enrollment(user_id(client), course.id)
    login(client2, sms)
    ask = {"text": "Вопрос по уроку", "parent_id": None}

    assert client.post(f"/lessons/{lesson.id}/questions", json=ask).status_code == 201

    meet_at(monkeypatch, SessionRepo, "by_token_hash")
    first, second = with_deadline(
        lambda: in_parallel(
            lambda: client.post(f"/lessons/{lesson.id}/questions", json=ask),
            lambda: client2.post(f"/lessons/{lesson.id}/questions", json=ask),
        )
    )
    monkeypatch.undo()

    assert [first.status_code, second.status_code] == [201, 201]
    assert client.post(f"/lessons/{lesson.id}/questions", json=ask).status_code == 429
    # Ни одно сообщение при этом не потерялось и не задвоилось
    assert thread_messages() == 3
