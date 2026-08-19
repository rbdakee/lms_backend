import os

# До импорта приложения: настройки читаются один раз при старте
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://lms:lms@localhost:5445/lms_test"
)

import itertools

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import (
    Base,
    Certificate,
    Course,
    Enrollment,
    Lead,
    Lesson,
    LessonProgress,
    Module,
    Notification,
    Option,
    Question,
    Quiz,
    Submission,
    Task,
    ThreadMessage,
    User,
)
from app.api.deps import (
    get_playback_limiter,
    get_sms,
    get_storage,
    get_telegram,
    get_thread_limiter,
    get_verify_limiter,
)
from app.application.ratelimit import SlidingWindowLimiter
from app.config import get_settings
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


@pytest.fixture(autouse=True)
def _clean_db(_migrated):
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
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
        self.sent: list[str] = []

    def notify_admins(self, text: str) -> None:
        self.sent.append(text)


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

    ФИО печатается на сертификате и остаётся снимком, поэтому выдача без имени
    отбивается — а вход по SMS заводит человека с пустыми полями.
    """
    resp = login(client, sms, phone=phone)
    fields = {"last_name": "Нурланова", "first_name": "Айгуль"}
    fields.update(profile)
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


def make_course(**kw):
    kw.setdefault("group_id", next(_group_seq))
    fields = {"lang": "ru", "title": "Курс", "category_id": 1, "hours": 72,
              "price": 45000, "status": "open"}
    fields.update(kw)
    return seed(Course(**fields))


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
    fields = {"user_id": user_id, "task_id": task_id, "text": "Первый вариант"}
    fields.update(kw)
    return seed(Submission(**fields))


def make_enrollment(user_id, course_id, **kw):
    fields = {"user_id": user_id, "course_id": course_id, "granted_by": user_id}
    fields.update(kw)
    return seed(Enrollment(**fields))


def make_certificate(user_id, course_id, **kw):
    fields = {"user_id": user_id, "course_id": course_id, "number": "KZ-2026-XB7K2M",
              "holder_name": "Смагулова Гульмира Токтарбековна", "course_title": "Курс",
              "hours": 72, "lang": "ru"}
    fields.update(kw)
    return seed(Certificate(**fields))


def make_progress(user_id, lesson_id):
    return seed(LessonProgress(user_id=user_id, lesson_id=lesson_id))


def user_id(client) -> int:
    return client.get("/me").json()["id"]


def make_notification(user_id, **kw):
    fields = {"user_id": user_id, "type": "access_granted",
              "params": {"course_id": 1, "course_title": "Курс"}}
    fields.update(kw)
    return seed(Notification(**fields))


def make_user(phone, **kw):
    """Учитель мимо входа: дашборду и отчёту нужны десятки людей, и заводить
    каждого через SMS — лишний шум. Номера вымышленные."""
    fields = {"phone": phone, "last_name": "Смагулова", "first_name": "Гульмира",
              "middle_name": "Токтарбековна", "school": "КГУ «Средняя школа №27»",
              "region": "Алматы"}
    fields.update(kw)
    return seed(User(**fields))


def make_lead(user_id, course_id, **kw):
    fields = {"user_id": user_id, "course_id": course_id, "price_snapshot": 45000,
              "status": "new"}
    fields.update(kw)
    return seed(Lead(**fields))


def make_thread_message(lesson_id, course_id, user_id, **kw):
    fields = {"lesson_id": lesson_id, "course_id": course_id, "user_id": user_id,
              "text": "Вопрос по уроку"}
    fields.update(kw)
    return seed(ThreadMessage(**fields))
