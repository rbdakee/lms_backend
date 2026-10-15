import os

# До импорта приложения: настройки читаются один раз при старте
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://lms:lms@localhost:5445/lms_test"
)
# Локальный `.env` разработчика тестам не указ: с заведённой парой
# AUTH_BOOTSTRAP_* вход получает фиксированный код, и проверки обычного входа
# начинают падать на чужой машине. Переменные окружения главнее `.env`,
# поэтому обе половины гасятся здесь.
os.environ["AUTH_BOOTSTRAP_PHONE"] = ""
os.environ["AUTH_BOOTSTRAP_CODE"] = ""
# Ключ подставной: настоящий клиент провайдера подменяется фикстурой `ai`,
# и наружу не ходит никто. Без ключа «Жоспар» отвечал бы `lesson_ai_off`.
os.environ["OPENAI_API_KEY"] = "test-key-not-a-real-one"
# «Жоспар» в тестах включён, но без фоновых воркеров: TestClient поднимает
# lifespan приложения, и воркер, запущенный в нём, брал бы задания раньше
# теста — и шёл бы с ними в настоящий OpenAI. Задания тесты берут руками
# (`JobRunner.run_next`), подставив свой генератор или подставной ИИ.
os.environ["LESSON_AI_ENABLED"] = "true"
os.environ["LESSON_AI_WORKERS"] = "0"
# MCP в тестах включён: выключатель по умолчанию закрыт, а ручки нужны всем
# тестам в этой папке. Выключенный вариант проверяет `test_mcp_off.py` в отдельном процессе.
os.environ["MCP_ENABLED"] = "true"

import itertools
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

# Импорт ради побочного действия: декораторы `@tool` наполняют реестр при загрузке
# пакета. Сервис грузит его сам при MCP_ENABLED; тестам он нужен независимо от флага.
import app.assistant.tools  # noqa: F401
from app.adapters.ai.openai_chat import AiUnavailableError
from app.adapters.db.base import get_engine
from app.adapters.db.models import (
    Base,
    Category,
    Certificate,
    Course,
    CoursePlatform,
    Enrollment,
    Lead,
    Lesson,
    LessonProgress,
    Module,
    Notification,
    Option,
    Question,
    Quiz,
    Review,
    Submission,
    Task,
    ThreadMessage,
    User,
)
from app.api.deps import (
    get_lesson_block_ai,
    get_lesson_chat_limiter,
    get_lesson_kits_limiter,
    get_playback_limiter,
    get_sms,
    get_storage,
    get_telegram,
    get_thread_limiter,
    get_verify_limiter,
    require_lesson_ai_access,
)
from app.application.ports import AiReply, AiToolCall, NotificationCard
from app.application.ratelimit import SlidingWindowLimiter
from app.assistant import registry
from app.assistant.registry import ToolCtx, ToolError
from app.config import get_settings
from app.domain.errors import AppError
from app.domain.platform import DEFAULT_PLATFORM
from app.main import app

# Каждый тест начинается с TRUNCATE всех таблиц. Направить это на базу
# разработки — значит стереть данные ручного QA владельца, поэтому имя базы
# проверяется до того, как что-нибудь успеет выполниться.
_DB_NAME = os.environ["DATABASE_URL"].rsplit("/", 1)[-1].split("?", 1)[0]
if not _DB_NAME.endswith("_test"):
    raise RuntimeError(
        f"Тесты чистят базу целиком и запускаются только на тестовой: «{_DB_NAME}» "
        "на неё не похожа. Проверьте DATABASE_URL и TEST_DATABASE_URL."
    )

PHONE = "+7 (707) 123-45-67"
NORM = "+77071234567"
ADMIN_PHONE = "+7 (700) 000-00-99"
ADMIN_NORM = "+77000000099"


@pytest.fixture(scope="session", autouse=True)
def _migrated():
    command.upgrade(Config("alembic.ini"), "head")


# Шесть категорий приезжают миграцией и в проде существуют всегда: на них
# ссылается category_id у курса. TRUNCATE их сносит, поэтому каждый тест
# начинается с них заново — иначе редактор курса отбивал бы category_id
# у любой тестовой сцены, а GET /dictionaries отдавал пустой список.
BASE_CATEGORIES = [
    (1, "Цифровые навыки"),
    (2, "Методика преподавания"),
    (3, "Оценивание"),
    (4, "Инклюзивное образование"),
    (5, "Классное руководство"),
    (6, "Предметные курсы"),
]


@pytest.fixture(autouse=True)
def _clean_db(_migrated):
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        for number, (category_id, title) in enumerate(BASE_CATEGORIES, start=1):
            conn.execute(
                text(
                    "INSERT INTO category (id, title, order_index)"
                    " VALUES (:id, :title, :order_index)"
                ),
                {"id": category_id, "title": title, "order_index": number},
            )
        # Строки вставлены с явными id: без сдвига последовательности первая
        # созданная в тесте категория упрётся в id = 1
        conn.execute(text("SELECT setval('category_id_seq', (SELECT max(id) FROM category))"))
    yield


class FakeSms:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_code(self, phone: str, code: str) -> None:
        self.sent.append((phone, code))


@pytest.fixture
def sms():
    fake = FakeSms()
    app.dependency_overrides[get_sms] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_sms, None)


class FakeTelegram:
    def __init__(self):
        self.sent: list[NotificationCard] = []
        # Ответы в конкретный чат: привязка бота отвечает туда, откуда пришла
        # команда, — это не то же самое, что уведомление админам
        self.to_chat: list[tuple[str, str]] = []

    def notify_admins(self, card: NotificationCard) -> None:
        self.sent.append(card)

    def send_to(self, chat_id: str, text: str) -> None:
        self.to_chat.append((chat_id, text))


@pytest.fixture
def telegram():
    fake = FakeTelegram()
    app.dependency_overrides[get_telegram] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_telegram, None)


class FakeStorage:
    """Хранилище в памяти: тесту не нужен ни диск, ни уборка за собой."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def save(self, key, chunks) -> int:
        # Исключение из итератора (превышен лимит) объект не создаёт —
        # как и локальный адаптер, который удаляет недописанный файл
        data = b"".join(chunks)
        self.objects[key] = data
        return len(data)

    def size(self, key):
        data = self.objects.get(key)
        return len(data) if data is not None else None

    def read(self, key):
        return iter([self.objects[key]])

    def delete(self, key):
        self.objects.pop(key, None)


@pytest.fixture
def storage():
    fake = FakeStorage()
    app.dependency_overrides[get_storage] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_storage, None)


class FakeClock:
    """Часы лимитера: минуту в тесте не переждёшь."""

    def __init__(self):
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def shift(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture(autouse=True)
def limiter_clock():
    """Лимитер playback живёт в памяти процесса, то есть переживает тест.
    Свой на каждый тест — иначе счётчик течёт из одного теста в другой."""
    clock = FakeClock()
    limiter = SlidingWindowLimiter(get_settings().playback_per_min, 60, clock)
    app.dependency_overrides[get_playback_limiter] = lambda: limiter
    yield clock
    app.dependency_overrides.pop(get_playback_limiter, None)


@pytest.fixture(autouse=True)
def verify_limiter_clock():
    """Лимитер публичной проверки сертификата тоже живёт в памяти процесса:
    без своего на каждый тест счётчик течёт из одного теста в другой."""
    clock = FakeClock()
    limiter = SlidingWindowLimiter(get_settings().verify_per_min, 60, clock)
    app.dependency_overrides[get_verify_limiter] = lambda: limiter
    yield clock
    app.dependency_overrides.pop(get_verify_limiter, None)


@pytest.fixture(autouse=True)
def thread_limiter_clock():
    """Лимитер вопросов под уроком тоже в памяти процесса, а потолок низкий:
    без своего на каждый тест счётчик течёт из одного теста в другой."""
    clock = FakeClock()
    limiter = SlidingWindowLimiter(get_settings().thread_messages_per_min, 60, clock)
    app.dependency_overrides[get_thread_limiter] = lambda: limiter
    yield clock
    app.dependency_overrides.pop(get_thread_limiter, None)


class FakeAi:
    """Подставной провайдер: отдаёт заранее разложенные ответы по очереди.

    Живого хода в OpenAI в тестах нет и быть не должно — он стоит денег
    и требует сети. Зато видно всё, чего не видно снаружи: что ушло
    в системном промпте, какие инструменты предложены модели и в каком
    виде вернулся результат вызова.
    """

    def __init__(self):
        self.replies: list[AiReply] = []
        # Каждый запрос целиком: system, переписка и список инструментов
        self.calls: list[dict] = []
        # Запросы с файлом (`describe_media`): разбора вложений «Жоспара»
        self.media_calls: list[dict] = []
        self.media_replies: list = []

    def describes(self, text: str = "На фото страница учебника", **kw) -> "FakeAi":
        """Ответ разбора вложения: выжимка файла."""
        self.media_replies.append(AiReply(text=text, **kw))
        return self

    def media_fails(self, exc: Exception) -> "FakeAi":
        """Разбор вложения упал: сеть, неподдерживаемый файл, битый документ."""
        self.media_replies.append(exc)
        return self

    def says(self, text: str = "Готово", **kw) -> "FakeAi":
        self.replies.append(AiReply(text=text, **kw))
        return self

    def wants(self, name: str, call_id: str = "call_1", **arguments) -> "FakeAi":
        """Ход, в котором модель просит вызвать инструмент."""
        self.replies.append(
            AiReply(text="", tool_calls=(AiToolCall(id=call_id, name=name, arguments=arguments),))
        )
        return self

    def complete(self, *, system, messages, tools=None) -> AiReply:
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        if not self.replies:
            # Реплик не разложили — значит тесту важен сам факт вызова
            return AiReply(text="Готово", tokens_in=7, tokens_out=11)
        return self.replies.pop(0)

    def returns_json(self, data, tokens_in: int = 900, tokens_out: int = 1400) -> "FakeAi":
        """Ответ генерации «Жоспара» по схеме. `data` — словарь (уйдёт JSON)
        или строка как есть: кривой ответ модели тоже надо уметь подсунуть."""
        text_ = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        self.replies.append(AiReply(text=text_, tokens_in=tokens_in, tokens_out=tokens_out))
        return self

    def fails(self) -> "FakeAi":
        """Провайдер не ответил: сеть, таймаут, отказ по лимиту."""
        self.replies.append(AiUnavailableError("Модель не ответила"))
        return self

    def complete_json(self, *, system, messages, name, schema) -> AiReply:
        self.calls.append(
            {"system": system, "messages": list(messages), "name": name, "schema": schema}
        )
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def describe_media(self, *, system, media, instruction) -> AiReply:
        """Мультимодальный запрос: файл виден целиком — тест проверяет,
        что модель позвали один раз и с тем файлом."""
        self.media_calls.append(
            {"system": system, "media": media, "instruction": instruction}
        )
        if not self.media_replies:
            return AiReply(text="Выжимка файла", tokens_in=50, tokens_out=20)
        reply = self.media_replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def system(self) -> str:
        """Системный промпт последнего запроса."""
        return self.calls[-1]["system"]

    @property
    def tool_names(self) -> list[str]:
        return [item["function"]["name"] for item in self.calls[-1]["tools"] or []]


@pytest.fixture
def ai():
    # Подставной ИИ для перегенерации блока «Жоспара»: у неё своя модель и свой таймаут
    fake = FakeAi()
    app.dependency_overrides[get_lesson_block_ai] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_lesson_block_ai, None)


@pytest.fixture(autouse=True)
def lesson_chat_limiter_clock():
    """Потолок реплик чату «Жоспара» — тот же случай: счётчик в памяти
    процесса, и без своего на каждый тест он течёт из теста в тест."""
    clock = FakeClock()
    limiter = SlidingWindowLimiter(get_settings().lesson_ai_chat_messages_per_hour, 3600, clock)
    app.dependency_overrides[get_lesson_chat_limiter] = lambda: limiter
    yield clock
    app.dependency_overrides.pop(get_lesson_chat_limiter, None)


@pytest.fixture(autouse=True)
def lesson_kits_limiter_clock():
    """Суточный лимит комплектов «Жоспара» (L7) — тоже в памяти процесса."""
    clock = FakeClock()
    limiter = SlidingWindowLimiter(get_settings().lesson_ai_kits_per_day, 86400, clock)
    app.dependency_overrides[get_lesson_kits_limiter] = lambda: limiter
    yield clock
    app.dependency_overrides.pop(get_lesson_kits_limiter, None)


@pytest.fixture
def lesson_ai_open():
    """Доступ к «Жоспару» открыт любому вошедшему — для тестов самого продукта.

    Не автоматом: по умолчанию проверка доступа настоящая, и новый тест,
    забывший про неё, получает честный 403, а не молча проходящую проверку.
    Сама проверка доступа тестируется без этой фикстуры.
    """
    app.dependency_overrides[require_lesson_ai_access] = lambda: None
    yield
    app.dependency_overrides.pop(require_lesson_ai_access, None)


@pytest.fixture
def tool_ctx():
    """Контекст вызова инструмента реестра мимо HTTP.

    Реестр не предполагает, что зовёт его наш собственный чат
    (`ASSISTANT_BRIEF`, раздел 15): `ctx` собирается из админа и сессии базы,
    и тесту он собирается так же.

    Сессия закрывается фикстурой, а не тестом: оставленная открытой, она
    держит транзакцию, и следующий тест насмерть виснет на TRUNCATE.
    """
    sessions: list[OrmSession] = []

    def make(admin_id: int, pd_allowed: bool = False) -> ToolCtx:
        db = OrmSession(get_engine())
        sessions.append(db)
        return ToolCtx(
            db=db,
            admin=db.get(User, admin_id),
            now=datetime.now(UTC),
            pd_allowed=pd_allowed,
        )

    yield make
    for db in sessions:
        db.close()


class Plan(dict):
    """Опасное действие до исполнения: «что произойдёт» словами и всё нужное,
    чтобы потом исполнить. Реестр исполняет `confirm` сразу, поэтому тест,
    который сначала сверяет текст последствий, а потом само действие, берёт
    текст из `@preview`, а исполнение откладывает до `execute`."""

    def __init__(self, name: str, args: dict, ctx: ToolCtx):
        spec = registry.get(name)
        self.name, self.args, self.ctx = name, args, ctx
        what = spec.preview(ctx, **registry.check_args(spec, args)) if spec.preview else ""
        super().__init__(what_happens=what)


def plan(name: str, args: dict, ctx: ToolCtx) -> Plan:
    return Plan(name, args, ctx)


def execute(planned: Plan) -> dict:
    result = registry.call(planned.name, planned.args, planned.ctx)
    planned.ctx.db.commit()
    return result


def refusal(planned: Plan) -> str:
    """Код, которым действие отказало. Отказом бывает и `ToolError`
    инструмента, и ошибка сценария под ним."""
    try:
        registry.call(planned.name, planned.args, planned.ctx)
    except (ToolError, AppError) as exc:
        planned.ctx.db.rollback()
        return exc.code
    raise AssertionError(f"{planned.name} должен был отказать, а исполнился")


def as_model_sees(ctx: ToolCtx, name: str, **args) -> dict:
    """Результат вызова глазами вызывающего: отказ инструмента или сценария —
    обычный результат, а не исключение."""
    try:
        return registry.call(name, args, ctx)
    except ToolError as exc:
        return exc.as_result()
    except AppError as exc:
        return {"error": {"code": exc.code, "message": exc.message}}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client2():
    """Второе «устройство»: своя куки-банка."""
    with TestClient(app) as c:
        yield c


def request_code(client, phone=PHONE, consent=True):
    return client.post("/auth/request_code", json={"phone": phone, "consent": consent})


def age_codes(minutes: float) -> None:
    """Сдвигает коды в прошлое: тестам не ждать лимит «раз в минуту»."""
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE auth_code SET"
                " created_at = created_at - make_interval(mins => :m),"
                " expires_at = expires_at - make_interval(mins => :m)"
            ),
            {"m": minutes},
        )


def login(client, sms, phone=PHONE):
    resp = request_code(client, phone)
    assert resp.status_code == 200, resp.text
    _, code = sms.sent[-1]
    resp = client.post("/auth/verify_code", json={"phone": phone, "code": code})
    assert resp.status_code == 200, resp.text
    # Следующему входу в этом же тесте не должен мешать лимит повторной отправки
    age_codes(2)
    return resp


def login_named(client, sms, phone=PHONE, **profile):
    """Вход и сразу заполненный профиль.

    ФИО печатается на сертификате и остаётся снимком, а без ИИН заявку
    на сертификат не принимают — вход же по SMS заводит человека с пустыми
    полями и нулевым ИИН.

    Нужен человек с незаполненным ИИН — `login_named(..., iin=None)`:
    поле просто не уйдёт в PATCH, и заглушка останется на месте.
    """
    resp = login(client, sms, phone=phone)
    fields = {"last_name": "Нурланова", "first_name": "Айгуль", "iin": fake_iin()}
    fields.update(profile)
    fields = {name: value for name, value in fields.items() if value is not None}
    assert client.patch("/me", json=fields).status_code == 200
    return resp


def make_admin(phone=NORM):
    """Флаг админа сырым SQL — по образцу age_codes: PATCH /me его нарочно не меняет."""
    with get_engine().begin() as conn:
        conn.execute(
            text('UPDATE "user" SET is_admin = true WHERE phone = :phone'), {"phone": phone}
        )


def login_admin(client, sms, phone=ADMIN_PHONE):
    resp = login(client, sms, phone)
    make_admin(resp.json()["phone"])
    return resp


def meet_at(monkeypatch, cls, method):
    """Сводит два запроса в одной точке: из `method` обе стороны выходят
    одновременно, а дальше их разводит уже база.

    Без этого гонку не воспроизвести: два запроса, пущенные просто рядом,
    почти всегда успевают разойтись по времени, и тест ничего не ловит.
    """
    barrier = threading.Barrier(2, timeout=10)
    real = getattr(cls, method)

    def waiting(self, *args, **kw):
        result = real(self, *args, **kw)
        barrier.wait()
        return result

    monkeypatch.setattr(cls, method, waiting)


def in_parallel(first, second):
    """Два запроса в двух соединениях: каждый своим потоком, ответы —
    в порядке аргументов. Соединение на запрос своё, потому что сессию БД
    заводит get_db, а не тест."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(first), pool.submit(second)]
        return [job.result() for job in jobs]


def seed(obj):
    """Кладёт ORM-объект в базу напрямую: админских редакторов курсов ещё нет,
    тестовые данные готовятся мимо HTTP."""
    with OrmSession(get_engine()) as db:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        db.expunge(obj)
    return obj


# group_id сквозной на прогон: таблицы чистятся между тестами, а уникальность
# (group_id, lang) важна только внутри одного теста
_group_seq = itertools.count(1)

# ИИН сквозной по той же причине: он уникален в базе, и два человека в одной
# сцене обязаны получить разные номера.
_iin_seq = itertools.count(1)


def fake_iin() -> str:
    """Вымышленный ИИН: 12 цифр, свой у каждого вызова.

    Настоящим быть не может по построению: третья и четвёртая цифры ИИН —
    месяц рождения, а здесь на их месте стоит 99. Так номер в тестах и в логе
    прогона нельзя спутать с чьим-то живым (общий CLAUDE.md, «Персональные
    данные»).
    """
    return f"9099{next(_iin_seq):08d}"


def make_course(platforms=None, price=45000, **kw):
    """Курс, по умолчанию выложенный на первой площадке по своей цене.

    Публикация и цена живут строкой `course_platform`, и без неё курса нет
    ни в одном каталоге. `platforms` — где он выложен и почём:
    `{"p1": 45000, "p2": 60000}` — на обеих, `{}` — нигде. `price` — цена
    на площадке по умолчанию: колонки `course.price` больше нет.
    """
    kw.setdefault("group_id", next(_group_seq))
    fields = {"lang": "ru", "title": "Курс", "category_id": 1, "hours": 72,
              "status": "open"}
    fields.update(kw)
    course = seed(Course(**fields))
    if platforms is None:
        platforms = {DEFAULT_PLATFORM: price}
    for platform, price in platforms.items():
        seed(CoursePlatform(course_id=course.id, platform=platform, price=price))
    return course


def make_module(course_id, **kw):
    fields = {"course_id": course_id, "title": "Модуль"}
    fields.update(kw)
    return seed(Module(**fields))


def make_lesson(module_id, **kw):
    fields = {"module_id": module_id, "title": "Урок", "kind": "video",
              "video_url": "https://youtube.com/watch?v=demo", "time_required_min": 15}
    fields.update(kw)
    return seed(Lesson(**fields))


def make_quiz(module_id, **kw):
    fields = {"module_id": module_id, "title": "Тест", "pass_score": 70}
    fields.update(kw)
    return seed(Quiz(**fields))


def make_question(quiz_id, **kw):
    fields = {"quiz_id": quiz_id, "type": "single", "text": "Вопрос", "points": 1}
    fields.update(kw)
    return seed(Question(**fields))


def make_option(question_id, **kw):
    fields = {"question_id": question_id, "text": "Вариант", "is_correct": False}
    fields.update(kw)
    return seed(Option(**fields))


def make_task(module_id, **kw):
    fields = {"module_id": module_id, "title": "Задание",
              "statement": {"text": "Опишите свой урок"}}
    fields.update(kw)
    return seed(Task(**fields))


def make_submission(user_id, task_id, **kw):
    fields = {"user_id": user_id, "task_id": task_id, "text": "Первый вариант",
              "platform": DEFAULT_PLATFORM}
    fields.update(kw)
    return seed(Submission(**fields))


def make_enrollment(user_id, course_id, **kw):
    fields = {"user_id": user_id, "course_id": course_id, "granted_by": user_id,
              "platform": DEFAULT_PLATFORM}
    fields.update(kw)
    return seed(Enrollment(**fields))


def make_certificate(user_id, course_id, **kw):
    """Выданный документ: номер и дата выдачи проставлены.

    Дата обязательна явно — `server_default` у `issued_at` снят, чтобы заявка
    не получала её на вставке, а номер без даты отбивает `ck_certificate_issued`.
    """
    fields = {"user_id": user_id, "course_id": course_id, "number": "KZ-2026-XB7K2M",
              "holder_name": "Смагулова Гульмира Токтарбековна", "course_title": "Курс",
              "hours": 72, "lang": "ru", "platform": DEFAULT_PLATFORM,
              "issued_at": datetime.now(UTC)}
    fields.update(kw)
    return seed(Certificate(**fields))


def make_certificate_request(user_id, course_id, **kw):
    """Заявка: та же строка до выдачи — ни номера, ни даты выдачи."""
    return make_certificate(user_id, course_id, number=None, issued_at=None, **kw)


def make_progress(user_id, lesson_id, **kw):
    fields = {"user_id": user_id, "lesson_id": lesson_id, "platform": DEFAULT_PLATFORM}
    fields.update(kw)
    return seed(LessonProgress(**fields))


def user_id(client) -> int:
    return client.get("/me").json()["id"]


def make_notification(user_id, **kw):
    fields = {"user_id": user_id, "type": "access_granted",
              "params": {"course_id": 1, "course_title": "Курс"},
              "platform": DEFAULT_PLATFORM}
    fields.update(kw)
    return seed(Notification(**fields))


def make_user(phone, **kw):
    """Учитель мимо входа: дашборду и отчёту нужны десятки людей, и заводить
    каждого через SMS — лишний шум. Номера вымышленные."""
    fields = {"phone": phone, "last_name": "Смагулова", "first_name": "Гульмира",
              "middle_name": "Токтарбековна", "school": "КГУ «Средняя школа №27»",
              "region": "Алматы", "iin": fake_iin()}
    fields.update(kw)
    return seed(User(**fields))


def make_lead(user_id, course_id, **kw):
    fields = {"user_id": user_id, "course_id": course_id, "price_snapshot": 45000,
              "status": "new", "platform": DEFAULT_PLATFORM}
    fields.update(kw)
    return seed(Lead(**fields))


def make_review(user_id, course_id, **kw):
    """Отзыв мимо HTTP: лента админа и модерация проверяют разбор отзыва,
    а не право его оставить — доступ к курсу для этого не нужен."""
    fields = {"user_id": user_id, "course_id": course_id, "rating": 5,
              "text": "Наконец-то понятно, как объяснять оценки родителям.",
              "platform": DEFAULT_PLATFORM}
    fields.update(kw)
    return seed(Review(**fields))


def make_category(title, **kw):
    """Седьмая категория сверх базовых шести: нужна там, где проверяют
    сам справочник, а не курс на нём."""
    fields = {"title": title, "order_index": len(BASE_CATEGORIES) + 1}
    fields.update(kw)
    return seed(Category(**fields))


def make_thread_message(lesson_id, course_id, user_id, **kw):
    fields = {"lesson_id": lesson_id, "course_id": course_id, "user_id": user_id,
              "text": "Вопрос по уроку", "platform": DEFAULT_PLATFORM}
    fields.update(kw)
    return seed(ThreadMessage(**fields))
