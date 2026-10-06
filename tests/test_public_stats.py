"""Цифры лендинга: курсы в каталоге, учителя и выданные сертификаты.

Числа стоят на витрине без входа, поэтому проверяется не только «сколько»,
но и «чьё»: каждая площадка видит свои числа, а админ не входит ни в одно
(общее правило статистики, `not_admin`).

Курс считается так, как его видит посетитель каталога: русская и казахская
версии одной группы — одна карточка, а значит и одна единица в цифре.
"""

from datetime import UTC, datetime

import pytest

from app.config import get_settings
from tests.conftest import (
    login,
    login_admin,
    make_certificate,
    make_certificate_request,
    make_course,
    make_enrollment,
    user_id,
)

# Свои источники, а не из `.env`: у разработчика там localhost с портами
P1_ORIGIN = "https://first.example.kz"
P2_ORIGIN = "https://second.example.kz"
P1 = {"Origin": P1_ORIGIN}
P2 = {"Origin": P2_ORIGIN}

# Вымышленные номера: настоящих телефонов в тестах нет (CLAUDE.md)
TEACHER_A = "+7 (707) 000-00-11"
TEACHER_B = "+7 (707) 000-00-22"


@pytest.fixture(autouse=True)
def platform_origins(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "platform_origins", {P1_ORIGIN: "p1", P2_ORIGIN: "p2"}
    )


def stats(client, headers):
    resp = client.get("/stats", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def teacher(client, sms, phone):
    login(client, sms, phone=phone)
    return user_id(client)


def test_an_empty_platform_shows_zeros_without_login(client):
    """Пустая площадка — нули, а не ошибка: лендинг открыт до любого курса,
    и посетитель без входа получает ответ."""
    assert stats(client, P1) == {"courses": 0, "teachers": 0, "certificates": 0}


def test_courses_are_counted_as_catalog_cards(client):
    """Русская и казахская версии — одна карточка каталога и одна единица
    в цифре; черновик в каталоге не стоит и не считается; курс соседней
    площадки сюда не попадает, а выложенный на обеих — считается на обеих."""
    ru = make_course(lang="ru", group_id=901)
    make_course(lang="kz", group_id=ru.group_id)
    make_course(status="draft")
    make_course(platforms={"p2": 30000})
    make_course(platforms={"p1": 45000, "p2": 60000})

    assert stats(client, P1)["courses"] == 2
    assert stats(client, P2)["courses"] == 2


def test_teachers_are_people_with_open_access_on_the_platform(client, sms):
    """Учитель с двумя курсами — один учитель. Отозванный доступ, доступ
    на соседней площадке и доступ админа в цифру не входят."""
    first = make_course()
    second = make_course()
    a = teacher(client, sms, TEACHER_A)
    b = teacher(client, sms, TEACHER_B)
    login_admin(client, sms)
    admin = user_id(client)

    make_enrollment(a, first.id)
    make_enrollment(a, second.id)
    make_enrollment(b, first.id, revoked_at=datetime.now(UTC))
    make_enrollment(b, second.id, platform="p2")
    make_enrollment(admin, first.id)

    assert stats(client, P1)["teachers"] == 1
    assert stats(client, P2)["teachers"] == 1


def test_certificates_are_issued_and_not_revoked(client, sms):
    """Считается выданный документ своей площадки. Заявка ещё не документ,
    отозванный больше не документ, а сертификат админа не входит ни в один
    показатель."""
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    a = teacher(client, sms, TEACHER_A)
    b = teacher(client, sms, TEACHER_B)
    login_admin(client, sms)
    admin = user_id(client)

    make_certificate(a, course.id, number="KZ-2026-AAAA01")
    make_certificate(a, course.id, number="KZ-2026-AAAA02", platform="p2")
    make_certificate(b, course.id, number="KZ-2026-AAAA03", revoked_at=datetime.now(UTC))
    make_certificate_request(b, course.id, platform="p2")
    make_certificate(admin, course.id, number="KZ-2026-AAAA04")

    assert stats(client, P1)["certificates"] == 1
    assert stats(client, P2)["certificates"] == 1
