from datetime import UTC, datetime

from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.api.deps import get_telegram
from app.domain.kz_time import waiting_days
from app.main import app
from tests.conftest import login, make_course, make_enrollment, user_id


def test_create_lead(client, sms, telegram):
    course = make_course(title="Оценивание для учителей", price=45000)
    login(client, sms)
    client.patch("/me", json={"last_name": "Нурланова", "first_name": "Айгуль"})

    resp = client.post(f"/courses/{course.id}/lead")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "new"
    assert body["waiting_days"] == 0

    # Уведомление без ПД: только номер заявки и курс
    assert len(telegram.sent) == 1
    message = telegram.sent[0]
    assert f"№{body['id']}" in message
    assert "Оценивание для учителей" in message
    assert "Нурланова" not in message
    assert "7071234567" not in message


def test_repeat_lead_is_reminder_not_duplicate(client, sms, telegram):
    course = make_course()
    login(client, sms)
    first = client.post(f"/courses/{course.id}/lead").json()
    second = client.post(f"/courses/{course.id}/lead").json()
    assert second["id"] == first["id"]

    with get_engine().connect() as conn:
        count, reminded_at = conn.execute(
            text("SELECT count(*), max(reminded_at) FROM lead")
        ).one()
    assert count == 1
    assert reminded_at is not None
    # Напоминание — не новая заявка: Telegram молчит
    assert len(telegram.sent) == 1


def test_lead_closed_course_409(client, sms):
    course = make_course(status="closed")
    login(client, sms)
    resp = client.post(f"/courses/{course.id}/lead")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "enrollment_closed"


def test_lead_when_enrolled_409(client, sms):
    course = make_course()
    login(client, sms)
    make_enrollment(user_id(client), course.id)
    resp = client.post(f"/courses/{course.id}/lead")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "already_enrolled"


def test_lead_hidden_course_404(client, sms):
    course = make_course(status="hidden")
    login(client, sms)
    assert client.post(f"/courses/{course.id}/lead").status_code == 404


def test_lead_requires_auth(client):
    course = make_course()
    assert client.post(f"/courses/{course.id}/lead").status_code == 401


class BoomTelegram:
    def notify_admins(self, text: str) -> None:
        raise RuntimeError("бот недоступен")


def test_telegram_failure_does_not_break_lead(client, sms):
    course = make_course()
    login(client, sms)
    app.dependency_overrides[get_telegram] = BoomTelegram
    try:
        resp = client.post(f"/courses/{course.id}/lead")
    finally:
        app.dependency_overrides.pop(get_telegram, None)
    # Заявка сначала пишется в базу: бот упал — заявка всё равно есть
    assert resp.status_code == 200
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM lead")).scalar() == 1


def test_waiting_days_by_almaty_calendar():
    # 20:30 UTC 15-го — это уже 01:30 16-го по Алматы: свежая заявка не «вчерашняя»
    now = datetime(2026, 8, 15, 21, 0, tzinfo=UTC)  # 02:00 16.08 по Алматы
    assert waiting_days(datetime(2026, 8, 15, 20, 30, tzinfo=UTC), now) == 0
    # 18:00 UTC 15-го — ещё 23:00 15-го по Алматы: прошёл один календарный день
    assert waiting_days(datetime(2026, 8, 15, 18, 0, tzinfo=UTC), now) == 1
    assert waiting_days(datetime(2026, 8, 13, 12, 0, tzinfo=UTC), now) == 3
