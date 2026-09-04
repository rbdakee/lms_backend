"""Админка одна на обе площадки: метка площадки у строки и фильтр по ней.

Учительская половина площадку уже фильтрует — там она рамка: чужого просто
нет. В админке всё наоборот: она видит обе площадки разом, и потому у каждой
строки обязана быть метка «откуда», а у каждого списка — фильтр
(PLATFORMS_BRIEF, раздел «Админка»). Числа при этом общие, с фильтром,
а не двумя колонками (решение 9).

Проверки парные: рядом с «чужого нет» стоит «своё есть». Иначе тест зеленел
бы и на пустой выдаче, а фильтр, отрезающий вообще всё, выглядел бы рабочим.

Отдельно здесь закрыт открытый вопрос 3 сессии 1: строка отчёта — это доступ,
а не человек, и у купившего общий курс дважды две строки со своими процентами.

Телефоны и ФИО в тестовых данных вымышленные.
"""

import pytest

from app.adapters.db.models import QuizAttempt
from app.adapters.db.repos import now_utc
from app.config import get_settings
from app.domain.brands import brand
from tests.conftest import (
    login,
    login_admin,
    login_named,
    make_certificate,
    make_course,
    make_enrollment,
    make_lead,
    make_lesson,
    make_module,
    make_progress,
    make_question,
    make_quiz,
    make_review,
    make_submission,
    make_task,
    make_thread_message,
    make_user,
    seed,
    user_id,
)

# Свои источники, а не из `.env`: у разработчика там localhost с портами
P1_ORIGIN = "https://first.example.kz"
P2_ORIGIN = "https://second.example.kz"
P1 = {"Origin": P1_ORIGIN}
P2 = {"Origin": P2_ORIGIN}

# Списки, у которых метка и фильтр одинаковы: проверяются одним прогоном
LIST_URLS = ["/admin/leads", "/admin/submissions", "/admin/questions", "/admin/reviews"]
# Плюс сводка и отчёт — у них фильтр тот же, а формы ответа другие
FILTERED_URLS = [*LIST_URLS, "/admin/overview"]


@pytest.fixture(autouse=True)
def platform_origins(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "platform_origins", {P1_ORIGIN: "p1", P2_ORIGIN: "p2"}
    )


def teacher(number: int, **kw):
    """Учитель с вымышленным номером: заявки и работы нужны от кого-то."""
    return make_user(f"+7702000{number:04d}", **kw)


def both_queues(client, sms):
    """По строке каждого вида на каждой площадке.

    Курс один и тот же — в этом вся опасность: отдельной базы у площадок нет,
    есть колонка `platform`, и по курсу площадку не вычислить.
    """
    login_admin(client, sms)
    course = make_course(
        title="Критериальное оценивание", platforms={"p1": 45000, "p2": 60000}
    )
    module = make_module(course.id)
    lesson = make_lesson(module.id, title="Критерии и дескрипторы")
    task = make_task(module.id, title="Составьте дескрипторы")
    author = teacher(1)
    rows = {}
    for platform in ("p1", "p2"):
        rows[platform] = {
            "lead": make_lead(author.id, course.id, platform=platform),
            "submission": make_submission(author.id, task.id, platform=platform),
            "question": make_thread_message(
                lesson.id, course.id, author.id, platform=platform
            ),
            "review": make_review(author.id, course.id, platform=platform),
        }
    return course, rows


def items(client, url, **params):
    resp = client.get(url, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# -- метка площадки в списках ------------------------------------------


def test_every_admin_list_marks_the_platform_of_its_row(client, sms):
    """Метка у каждой строки — и она кодом, а не именем бренда: имя админка
    берёт из справочника `GET /admin/settings`."""
    _, rows = both_queues(client, sms)

    for url, key in zip(LIST_URLS, ("lead", "submission", "question", "review"), strict=True):
        body = items(client, url)
        by_id = {item["id"]: item["platform"] for item in body["items"]}
        assert by_id[rows["p1"][key].id] == "p1", url
        assert by_id[rows["p2"][key].id] == "p2", url


def test_lead_row_shows_the_price_of_its_own_platform(client, sms):
    """Цена рядом с заявкой — цена того каталога, откуда она пришла. Читается
    одним запросом на площадку, а не запросом на строку."""
    _, rows = both_queues(client, sms)

    body = items(client, "/admin/leads")
    prices = {item["platform"]: item["course"]["price"] for item in body["items"]}
    assert prices == {"p1": 45000, "p2": 60000}


def test_card_of_a_row_keeps_its_platform(client, sms):
    """Ручка, отдающая карточку той же строки, отдаёт и метку: иначе после
    сохранения строка на экране её теряла бы."""
    _, rows = both_queues(client, sms)

    lead = client.patch(
        f"/admin/leads/{rows['p2']['lead'].id}", json={"status": "contacted"}
    )
    assert lead.status_code == 200, lead.text
    assert lead.json()["platform"] == "p2"

    card = client.get(f"/admin/submissions/{rows['p2']['submission'].id}")
    assert card.status_code == 200, card.text
    assert card.json()["platform"] == "p2"

    reply = client.post(
        f"/admin/reviews/{rows['p2']['review'].id}/reply", json={"text": "Спасибо!"}
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["platform"] == "p2"


def test_dashboard_lists_mark_the_platform_too(client, sms):
    """Те же строки на соседнем экране: без метки админ видел бы одну и ту же
    заявку с площадкой в очереди и без неё на дашборде."""
    _, rows = both_queues(client, sms)

    body = client.get("/admin/overview").json()
    for key, field in (("lead", "leads"), ("submission", "submissions"), ("question", "questions")):
        marks = {item["id"]: item["platform"] for item in body[field]}
        assert marks[rows["p1"][key].id] == "p1", field
        assert marks[rows["p2"][key].id] == "p2", field


# -- фильтр ------------------------------------------------------------


@pytest.mark.parametrize("url", LIST_URLS)
def test_platform_filter_cuts_items_and_total(client, sms, url):
    """Фильтр отрезает и страницу, и число под ней: `total`, посчитанный
    мимо фильтра, разошёлся бы со списком на том же экране."""
    both_queues(client, sms)

    whole = items(client, url)
    assert whole["total"] == 2
    assert {item["platform"] for item in whole["items"]} == {"p1", "p2"}

    only_first = items(client, url, platform="p1")
    assert only_first["total"] == 1
    assert [item["platform"] for item in only_first["items"]] == ["p1"]

    only_second = items(client, url, platform="p2")
    assert only_second["total"] == 1
    assert [item["platform"] for item in only_second["items"]] == ["p2"]


def test_platform_filter_combines_with_status(client, sms):
    """Фильтр площадки сочетается с уже существующими, а не заменяет их."""
    _, rows = both_queues(client, sms)
    assert (
        client.patch(
            f"/admin/leads/{rows['p1']['lead'].id}", json={"status": "contacted"}
        ).status_code
        == 200
    )

    assert items(client, "/admin/leads", status="new")["total"] == 1
    assert items(client, "/admin/leads", platform="p1")["total"] == 1
    # Новая заявка есть только на второй площадке — на первой она contacted
    assert items(client, "/admin/leads", status="new", platform="p1")["total"] == 0
    assert items(client, "/admin/leads", status="new", platform="p2")["total"] == 1
    assert items(client, "/admin/leads", status="contacted", platform="p1")["total"] == 1


@pytest.mark.parametrize("url", FILTERED_URLS)
def test_unknown_platform_is_422_in_russian(client, sms, url):
    """Отказ читает админ, поэтому текст русский: `Literal` в сигнатуре дал бы
    английский ответ валидатора."""
    both_queues(client, sms)

    resp = client.get(url, params={"platform": "px"})
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"] == "Неизвестная площадка: px"


def test_report_rejects_unknown_platform_too(client, sms):
    course, _ = both_queues(client, sms)

    resp = client.get(f"/admin/reports/{course.id}", params={"platform": "px"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["message"] == "Неизвестная площадка: px"


# -- сводка ------------------------------------------------------------


def test_overview_filter_reaches_counters_lists_and_totals(client, sms):
    """Общие числа плюс фильтр, а не две колонки у показателя
    (PLATFORMS_BRIEF, решение 9)."""
    course, _ = both_queues(client, sms)
    author = teacher(2)
    make_certificate(author.id, course.id, number="KZ-2026-P1", platform="p1")
    make_certificate(author.id, course.id, number="KZ-2026-P2", platform="p2")
    # Второй курс — только на первой площадке: у неё каталог из двух версий
    make_course(title="Только на первой", platforms={"p1": 30000})

    whole = client.get("/admin/overview").json()
    assert whole["leads_count"] == 2
    assert whole["submissions_count"] == 2
    assert whole["questions_count"] == 2
    assert whole["totals"]["courses_published"] == 2
    assert whole["totals"]["certificates"] == 2

    second = client.get("/admin/overview", params={"platform": "p2"}).json()
    assert second["leads_count"] == 1
    assert second["submissions_count"] == 1
    assert second["questions_count"] == 1
    assert [item["platform"] for item in second["leads"]] == ["p2"]
    assert [item["platform"] for item in second["submissions"]] == ["p2"]
    assert [item["platform"] for item in second["questions"]] == ["p2"]
    assert second["totals"]["courses_published"] == 1
    assert second["totals"]["certificates"] == 1
    # Аккаунт один на обе площадки — учителей фильтр не делит
    assert second["totals"]["teachers"] == whole["totals"]["teachers"]


# -- отчёт по курсу ----------------------------------------------------


def two_platform_course(client, sms):
    """Курс на обеих площадках и человек, купивший его дважды.

    Программа из двух уроков и итогового теста — три видимых элемента,
    процент считается третями. На первой площадке пройдено всё и сдан тест,
    на второй — только первый урок.
    """
    login_admin(client, sms)
    course = make_course(
        title="Критериальное оценивание",
        platforms={"p1": 45000, "p2": 60000},
        cert_require_lessons=True,
    )
    module = make_module(course.id)
    first = make_lesson(module.id, title="Урок 1", order_index=1)
    second = make_lesson(module.id, title="Урок 2", order_index=2)
    final = make_quiz(module.id, title="Итоговый тест", order_index=3, is_final=True)
    questions = [make_question(final.id, text="Вопрос 1", points=1)]

    both = teacher(3)
    make_enrollment(both.id, course.id, platform="p1")
    make_enrollment(both.id, course.id, platform="p2")

    make_progress(both.id, first.id, platform="p1")
    make_progress(both.id, second.id, platform="p1")
    # На второй площадке курс начат заново: пройден только первый урок
    make_progress(both.id, first.id, platform="p2")
    seed(
        QuizAttempt(
            user_id=both.id,
            quiz_id=final.id,
            question_order=[question.id for question in questions],
            finished_at=now_utc(),
            score=1,
            passed=True,
            is_counted=True,
            platform="p1",
        )
    )
    make_certificate(both.id, course.id, number="KZ-2026-ONLY-P1", platform="p1")
    return course, both, (first, second, final)


def test_report_row_is_an_access_not_a_person(client, sms):
    """Регресс открытого вопроса 3 сессии 1: человек с доступом на обеих
    площадках — две строки, и проценты у каждой свои.

    Раньше `participants_page` соединял `User` с `Enrollment` без площадки,
    и прогресс двух доступов складывался: 3 пройденных из 3 элементов на две
    строки — 100% там, где на второй площадке пройдена треть.
    """
    course, both, (first, second, final) = two_platform_course(client, sms)

    body = client.get(f"/admin/reports/{course.id}").json()

    assert body["summary"]["granted"] == 2
    assert body["summary"]["started"] == 2
    # Среднее по доступам: 100% и 33%
    assert body["summary"]["avg_progress_percent"] == 66
    # Сертификат выдан только на первой
    assert body["summary"]["certificates"] == 1

    rows = {item["platform"]: item for item in body["participants"]["items"]}
    assert body["participants"]["total"] == 2
    assert set(rows) == {"p1", "p2"}
    assert all(row["user_id"] == both.id for row in rows.values())
    assert rows["p1"]["progress_percent"] == 100
    assert rows["p2"]["progress_percent"] == 33
    # Попытка теста лежит у доступа, а не у человека
    assert rows["p1"]["final_quiz"] == {"state": "passed", "score": 100}
    assert rows["p2"]["final_quiz"] == {"state": "not_started", "score": None}
    assert rows["p1"]["certificate"] == "issued"
    assert rows["p2"]["certificate"] == "in_progress"

    # Воронка меряет то же, что и granted: первый урок пройден по двум
    # доступам, второй — по одному
    reached = {(item["kind"], item["id"]): item["reached"] for item in body["funnel"]}
    assert reached[("video", first.id)] == 2
    assert reached[("video", second.id)] == 1
    assert reached[("quiz", final.id)] == 1


def test_report_filter_leaves_one_row_with_its_own_numbers(client, sms):
    """С фильтром остаётся одна строка — и все числа сводки становятся
    числами этой площадки, а не половиной общих."""
    course, both, (first, second, final) = two_platform_course(client, sms)

    second_platform = client.get(
        f"/admin/reports/{course.id}", params={"platform": "p2"}
    ).json()

    assert second_platform["summary"] == {
        "granted": 1,
        "started": 1,
        "completed": 0,
        "avg_progress_percent": 33,
        # Зачётная попытка была только на первой площадке
        "avg_final_score": None,
        "certificates": 0,
        "avg_days_to_complete": None,
    }
    assert second_platform["participants"]["total"] == 1
    row = second_platform["participants"]["items"][0]
    assert row["platform"] == "p2"
    assert row["progress_percent"] == 33
    reached = {(item["kind"], item["id"]): item["reached"] for item in second_platform["funnel"]}
    assert reached[("video", first.id)] == 1
    assert reached[("video", second.id)] == 0

    first_platform = client.get(
        f"/admin/reports/{course.id}", params={"platform": "p1"}
    ).json()
    assert first_platform["summary"]["granted"] == 1
    assert first_platform["summary"]["avg_progress_percent"] == 100
    assert first_platform["summary"]["avg_final_score"] == 100
    assert first_platform["summary"]["certificates"] == 1
    assert [row["platform"] for row in first_platform["participants"]["items"]] == ["p1"]


# -- площадка в тексте Telegram ----------------------------------------


def test_lead_message_names_the_source_platform(client, sms, telegram):
    """Чат один на обе площадки, поэтому площадка-источник — строкой в тексте,
    первой и человеческим именем (PLATFORMS_BRIEF, решение 6)."""
    course = make_course(
        title="Критериальное оценивание", platforms={"p1": 45000, "p2": 60000}
    )
    login_named(client, sms)

    assert client.post(f"/courses/{course.id}/lead", headers=P1).status_code == 200
    assert telegram.sent[-1].lines[0] == f"Площадка: {brand('p1').platform_name}"

    assert client.post(f"/courses/{course.id}/lead", headers=P2).status_code == 200
    # У второй площадки строка другая — иначе метка была бы бесполезной
    assert telegram.sent[-1].lines[0] == f"Площадка: {brand('p2').platform_name}"
    # Имя, а не код: «Площадка: p2» админу ничего не говорит
    assert "p2" not in telegram.sent[-1].lines[0]
    assert telegram.sent[-1].lines[1] == "Курс: Критериальное оценивание"


def test_submission_message_names_the_platform_of_the_work(client, sms, storage, telegram):
    """Площадка берётся у сдачи, а не у запроса: работу админ смотрит
    из своего чата, где площадки нет вовсе."""
    course = make_course(
        title="Критериальное оценивание", platforms={"p1": 45000, "p2": 60000}
    )
    task = make_task(make_module(course.id).id, title="Составьте дескрипторы")
    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id, platform="p2")

    resp = client.post(
        f"/tasks/{task.id}/submissions", json={"text": "Готово"}, headers=P2
    )
    assert resp.status_code == 200, resp.text
    assert telegram.sent[-1].lines == [
        f"Площадка: {brand('p2').platform_name}",
        "Задание: Составьте дескрипторы",
        "Курс: Критериальное оценивание",
    ]
