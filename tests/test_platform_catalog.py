"""Каталог, цена и отзывы — у каждой площадки свои.

Курс один, а галочек публикации две: строка `course_platform` решает, есть ли
курс в каталоге площадки и сколько он там стоит (PLATFORMS_BRIEF, решения 1
и 3). Отсюда и проверки: курс соседней площадки в этом каталоге не «скрыт»,
его нет — и отвечает он так же, как несуществующий.

Отдельно — обратная сторона решения 12: снятая галочка убирает курс из
каталога, но не отбирает доступ. Тот, кто уже учится, доучивается и получает
сертификат, и ни один из этих экранов не имеет права закрыться.

Площадка задаётся так же, как её присылает браузер, — заголовком `Origin`.
"""

import pytest
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.repos import CoursePlatformRepo
from app.config import get_settings
from app.domain.platform import DEFAULT_PLATFORM
from tests.conftest import (
    ADMIN_PHONE,
    login_admin,
    login_named,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_review,
    user_id,
)

# Свои источники, а не из `.env`: у разработчика там localhost с портами
P1_ORIGIN = "https://first.example.kz"
P2_ORIGIN = "https://second.example.kz"
P1 = {"Origin": P1_ORIGIN}
P2 = {"Origin": P2_ORIGIN}


@pytest.fixture(autouse=True)
def platform_origins(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "platform_origins", {P1_ORIGIN: "p1", P2_ORIGIN: "p2"}
    )


def platforms_repo(db):
    return CoursePlatformRepo(db)


def set_platforms(course_id, rows):
    """Галочки публикации курса, как их поставит редактор: набор заменяется
    целиком, чего нет в списке — снято."""
    with OrmSession(get_engine()) as db:
        platforms_repo(db).set_for(course_id, rows)
        db.commit()


def catalog_ids(client, headers):
    resp = client.get("/courses", headers=headers)
    assert resp.status_code == 200, resp.text
    return [version["id"] for group in resp.json()["items"] for version in group["versions"]]


def catalog_price(client, headers, course_id):
    resp = client.get("/courses", headers=headers)
    return next(
        version["price"]
        for group in resp.json()["items"]
        for version in group["versions"]
        if version["id"] == course_id
    )


# -- каталог -----------------------------------------------------------


def test_the_catalog_shows_only_the_courses_of_its_own_platform(client):
    """Курс, выложенный на первой площадке, во втором каталоге не появляется
    и по прямой ссылке отвечает как несуществующий."""
    only_p1 = make_course(title="Критериальное оценивание")
    only_p2 = make_course(title="Цифровые инструменты", platforms={"p2": 60000})

    assert catalog_ids(client, P1) == [only_p1.id]
    assert catalog_ids(client, P2) == [only_p2.id]

    assert client.get(f"/courses/{only_p2.id}", headers=P1).status_code == 404
    assert client.get(f"/courses/{only_p1.id}", headers=P2).status_code == 404
    # Отзывы чужого курса — тот же ответ: ленту открывают с его же страницы
    assert client.get(f"/courses/{only_p1.id}/reviews", headers=P2).status_code == 404


def test_a_course_on_both_platforms_keeps_its_own_price_in_each(client):
    """Цена — свойство пары «курс и площадка»: один и тот же курс на второй
    площадке продают за свои деньги."""
    course = make_course(platforms={"p1": 45000, "p2": 60000})

    assert catalog_ids(client, P1) == [course.id]
    assert catalog_ids(client, P2) == [course.id]
    assert catalog_price(client, P1, course.id) == 45000
    assert catalog_price(client, P2, course.id) == 60000
    assert client.get(f"/courses/{course.id}", headers=P1).json()["price"] == 45000
    assert client.get(f"/courses/{course.id}", headers=P2).json()["price"] == 60000


def test_the_language_chips_show_only_the_versions_of_this_platform(client):
    """Чипы RU|ҚАЗ на странице курса — версии этой площадки: казахская,
    выложенная только на соседней, переключателем отсюда не открывается."""
    ru = make_course(lang="ru", title="Критериальное оценивание")
    kz = make_course(lang="kz", title="Критериалды бағалау", group_id=ru.group_id,
                     platforms={"p2": 60000})

    page = client.get(f"/courses/{ru.id}", headers=P1).json()
    assert [version["id"] for version in page["versions"]] == [ru.id]
    assert client.get(f"/courses/{kz.id}", headers=P1).status_code == 404


def test_a_course_published_nowhere_is_missing_from_every_catalog(client):
    """Ни одной галочки — курса нет ни в одном каталоге: цена и площадка
    у него ещё не назначены, показывать нечего."""
    course = make_course(platforms={})

    assert catalog_ids(client, P1) == []
    assert catalog_ids(client, P2) == []
    assert client.get(f"/courses/{course.id}", headers=P1).status_code == 404


# -- снятая галочка ----------------------------------------------------


def test_an_unpublished_course_keeps_every_door_open_for_whoever_studies_it(client, sms):
    """Решение 12: снятие галочки — не действие над чужой учёбой.

    Курс уходит из каталога, а у того, кто уже учится, остаются открытыми
    урок, программа, страница курса и выдача сертификата. Цена на странице
    при этом пустая: цены на этой площадке у курса больше нет.
    """
    course = make_course()
    module = make_module(course.id)
    lesson = make_lesson(module.id)
    login_named(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)

    set_platforms(course.id, [])

    assert catalog_ids(client, P1) == []
    page = client.get(f"/courses/{course.id}", headers=P1)
    assert page.status_code == 200, page.text
    assert page.json()["price"] is None
    # Открытая версия обязана остаться в чипах — иначе экрану нечего подсветить
    assert [version["id"] for version in page.json()["versions"]] == [course.id]
    assert page.json()["access"]["state"] == "granted"

    assert client.get(f"/courses/{course.id}/program", headers=P1).status_code == 200
    assert client.get(f"/lessons/{lesson.id}", headers=P1).status_code == 200
    certificate = client.post(f"/courses/{course.id}/certificate", headers=P1)
    assert certificate.status_code == 200, certificate.text

    # В «Моих курсах» курс остаётся, но уже без цены
    my = client.get("/me/courses", headers=P1).json()
    assert [item["id"] for item in my["items"]] == [course.id]
    assert my["items"][0]["price"] is None


def test_a_course_without_a_row_is_not_a_course_for_a_stranger(client, client2, sms):
    """Обратная половина той же проверки: поблажка про доступ — про доступ,
    а не про всех. Тому, у кого доступа нет, снятого курса не существует."""
    course = make_course()
    login_named(client, sms)
    make_enrollment(user_id(client), course.id)
    login_named(client2, sms, phone="+7 (701) 765-43-21")

    set_platforms(course.id, [])

    assert client.get(f"/courses/{course.id}", headers=P1).status_code == 200
    assert client2.get(f"/courses/{course.id}", headers=P1).status_code == 404


# -- отзывы ------------------------------------------------------------


def test_a_review_from_one_platform_is_not_in_the_feed_of_the_other(client, sms):
    """Отзыв, вопрос и рейтинг считаются по своей площадке (решение 3):
    прошедший курс дважды оставил два отзыва, и каждый каталог видит свой."""
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    login_named(client, sms)
    uid = user_id(client)
    make_review(uid, course.id, rating=5)
    make_review(uid, course.id, rating=2, platform="p2", text="Второй раз брала зря.")

    first = client.get(f"/courses/{course.id}/reviews", headers=P1).json()
    second = client.get(f"/courses/{course.id}/reviews", headers=P2).json()

    assert [review["rating"] for review in first["items"]] == [5]
    assert [review["rating"] for review in second["items"]] == [2]
    assert (first["total"], second["total"]) == (1, 1)
    assert (first["rating"], second["rating"]) == (5.0, 2.0)
    assert first["breakdown"]["5"] == 1 and first["breakdown"]["2"] == 0
    assert second["breakdown"]["2"] == 1 and second["breakdown"]["5"] == 0

    # Звёзды на карточке каталога — те же, что под лентой
    page = client.get(f"/courses/{course.id}", headers=P2).json()
    assert (page["rating"], page["reviews_count"]) == (2.0, 1)


# -- заявка ------------------------------------------------------------


def test_a_lead_on_a_course_of_another_platform_is_not_found(client, sms, telegram):
    """Заявку подаёт тот, у кого доступа ещё нет, поэтому поблажки про доступ
    здесь нет вовсе: курса на этой площадке не продают."""
    course = make_course()
    login_named(client, sms)

    assert client.post(f"/courses/{course.id}/lead", headers=P2).status_code == 404
    assert telegram.sent == []
    assert client.post(f"/courses/{course.id}/lead", headers=P1).status_code == 200


def test_the_lead_snapshot_is_the_price_of_its_own_platform(client, sms, telegram):
    """Снимок цены — цена того каталога, откуда пришла заявка: человеку
    показывали именно её."""
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    login_named(client, sms)

    assert client.post(f"/courses/{course.id}/lead", headers=P2).status_code == 200
    leads = client.get("/me/courses", headers=P2).json()["leads"]
    assert [lead["course"]["price"] for lead in leads] == [60000]
    assert "60 000 ₸" in "\n".join(telegram.sent[-1].lines)


# -- сводка админа -----------------------------------------------------


def test_the_dashboard_counts_a_course_on_both_platforms_once(client, sms):
    """`/admin/overview` считает обе площадки: фильтра у него пока нет.
    Курс, выложенный дважды, остаётся одной версией в списке админа."""
    make_course(platforms={"p1": 45000, "p2": 60000})
    make_course(platforms={"p2": 60000})
    make_course(platforms={})
    login_admin(client, sms, phone=ADMIN_PHONE)

    totals = client.get("/admin/overview").json()["totals"]
    assert totals["courses_published"] == 2


# -- строки публикации -------------------------------------------------


def test_set_for_replaces_the_whole_set_of_platforms():
    """`set_for` — это набор галочек целиком: чего нет в списке, то снято,
    что осталось — обновлено по цене. Так его позовёт PATCH курса."""
    course = make_course(platforms={"p1": 45000})

    set_platforms(course.id, [("p1", 50000), ("p2", 60000)])
    with OrmSession(get_engine()) as db:
        rows = platforms_repo(db).list_for(course.id)
        # Порядок — как в PLATFORMS, а не как в присланном списке
        assert [(row.platform, row.price) for row in rows] == [("p1", 50000), ("p2", 60000)]
        assert [row.platform for row in rows] == ["p1", "p2"]
        assert platforms_repo(db).prices([course.id], "p2") == {course.id: 60000}

    set_platforms(course.id, [("p2", None)])
    with OrmSession(get_engine()) as db:
        repo = platforms_repo(db)
        assert [(row.platform, row.price) for row in repo.list_for(course.id)] == [
            ("p2", None)
        ]
        assert DEFAULT_PLATFORM not in [row.platform for row in repo.list_for(course.id)]
        # Цена «по запросу» — это строка с пустой ценой, а не отсутствие строки
        assert repo.prices([course.id], "p2") == {course.id: None}

    set_platforms(course.id, [])
    with OrmSession(get_engine()) as db:
        assert platforms_repo(db).list_for(course.id) == []
