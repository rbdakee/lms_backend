import os

# До импорта приложения: настройки читаются один раз при старте
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://lms:lms@localhost:5445/lms_test"
)

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.adapters.db.models import Base
from app.api.deps import get_sms
from app.main import app

PHONE = "+7 (707) 123-45-67"
NORM = "+77071234567"


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
