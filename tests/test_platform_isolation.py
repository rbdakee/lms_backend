"""Площадка — граница учёбы, и держит её сервер.

Курс у площадок общий, а доступ, прогресс, попытка теста и сертификат —
свои: человек, купивший общий курс дважды, учится дважды и получает два
документа (PLATFORMS_BRIEF, решение 2). Отсюда и опасность: между двумя
площадками нет ни отдельной базы, ни отдельного сервиса — есть только
колонка `platform` и то, что её всюду спрашивают. Забыть её в одном
запросе значит открыть платный курс тому, кто купил соседний.

Поэтому здесь не «работает ли ручка», а «отказывает ли она чужой
площадке»: у каждой проверки есть и обратная половина — на своей площадке
то же самое отвечает 200, иначе тест зеленел бы и на сломанном доступе.

Сам `platform_of` проверяет `tests/test_cookie_split.py`; отсюда площадка
задаётся так же — заголовком `Origin`, как её присылает браузер.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.adapters.db.base import get_engine
from app.adapters.db.models import LessonFile
from app.config import get_settings
from tests.conftest import (
    login,
    login_admin,
    login_named,
    make_certificate,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_option,
    make_question,
    make_quiz,
    make_task,
    seed,
    user_id,
)

# Свои источники, а не из `.env`: у разработчика там localhost с портами,
# и тест не должен зависеть от того, дописал ли он себе вторую площадку.
P1_ORIGIN = "https://first.example.kz"
P2_ORIGIN = "https://second.example.kz"
P1 = {"Origin": P1_ORIGIN}
P2 = {"Origin": P2_ORIGIN}


@pytest.fixture(autouse=True)
def platform_origins(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "platform_origins", {P1_ORIGIN: "p1", P2_ORIGIN: "p2"}
    )


def common_course(**kw):
    """Курс, который живёт на обеих площадках: урок с материалом, тест
    и задание — по одному экрану на каждую учебную ручку.

    Публикация каталога платформу пока не знает (её включает сессия 2),
    поэтому «общий курс» здесь — просто курс: он один на две площадки,
    а расходятся уже доступ и учёба.
    """
    course = make_course(**kw)
    module = make_module(course.id)
    lesson = make_lesson(module.id, order_index=1)
    file = seed(
        LessonFile(
            lesson_id=lesson.id,
            name="Памятка по критериям.pdf",
            url="uploads/2026/09/03/9f3c1a7e4b2d8c05.pdf",
            size_bytes=21,
            mime="application/pdf",
        )
    )
    quiz = make_quiz(module.id, order_index=2)
    task = make_task(module.id, order_index=3)
    return course, lesson, file, quiz, task


def one_question(quiz_id):
    """Вопрос на один балл и его верный вариант: попытку надо не только
    начать, но и сдать — иначе «зачётная» в тесте ничего не значит."""
    question = make_question(quiz_id, text="Что фиксирует дескриптор?")
    correct = make_option(
        question.id, text="Наблюдаемое действие", is_correct=True, order_index=1
    )
    make_option(question.id, text="Отметку в журнале", order_index=2)
    return question.id, correct.id


def pass_the_quiz(client, quiz_id, question_id, option_id, headers):
    """Полный проход теста по HTTP на указанной площадке: старт, ответ,
    завершение. Возвращает попытку и её результат."""
    attempt = client.post(f"/quizzes/{quiz_id}/quiz_attempts", headers=headers)
    assert attempt.status_code == 200, attempt.text
    attempt_id = attempt.json()["id"]
    assert (
        client.post(
            f"/quiz_attempts/{attempt_id}/answers",
            json={"question_id": question_id, "option_ids": [option_id]},
            headers=headers,
        ).status_code
        == 204
    )
    result = client.post(f"/quiz_attempts/{attempt_id}/finish", headers=headers)
    assert result.status_code == 200, result.text
    return attempt_id, result.json()


def rows(sql, **params):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params).all()


def issue(admin, certificate_id):
    """Выдача админом: документ у человека появляется только после неё,
    а без документа половину этих проверок не разыграть.

    Площадки у админки нет вовсе — она одна на обе, — и площадку каждый
    документ приносит свою: здесь это и проверяется.
    """
    resp = admin.post(
        f"/admin/certificates/{certificate_id}/issue",
        json={"registration_number": "АКД-2026/117"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["certificate"]


# -- доступ ------------------------------------------------------------


def test_a_p1_enrollment_does_not_open_the_course_on_p2(client, sms):
    """Главный замок: за курс на второй площадке платят отдельно, и доступ,
    выданный на первой, её каталога не открывает. Проверяется каждая дверь
    в курс — урок, тест, задание и материал урока, — потому что достаточно
    одной забытой, чтобы платный курс достался бесплатно."""
    course, lesson, file, quiz, task = common_course()
    login(client, sms)
    make_enrollment(user_id(client), course.id)

    doors = [
        f"/lessons/{lesson.id}",
        f"/quizzes/{quiz.id}",
        f"/tasks/{task.id}",
        f"/files/{file.id}",
    ]
    for door in doors:
        # Своя площадка пускает: иначе отказ на второй означал бы сломанную
        # сцену, а не изоляцию
        assert client.get(door, headers=P1).status_code == 200, door
        denied = client.get(door, headers=P2)
        assert denied.status_code == 403, door
        assert denied.json()["error"]["code"] == "forbidden", door

    # Отметка «пройден» и ссылка для плеера — тоже двери, только на запись
    assert client.post(f"/lessons/{lesson.id}/complete", headers=P2).status_code == 403
    assert client.get(f"/lessons/{lesson.id}/playback", headers=P2).status_code == 403

    assert client.get("/me/courses", headers=P1).json()["items"] != []
    assert client.get("/me/courses", headers=P2).json()["items"] == []


# -- попытка теста -----------------------------------------------------


def test_a_counted_attempt_on_p1_does_not_spend_the_attempt_on_p2(client, sms):
    """Попытка одна и не возвращается, поэтому цена ошибки здесь — курс
    человека. Купивший общий курс дважды получает две попытки одного теста:
    это следствие раздельной учёбы, принятое владельцем, а не дефект."""
    course, _, _, quiz, _ = common_course()
    question_id, option_id = one_question(quiz.id)
    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_enrollment(uid, course.id, platform="p2")

    first, result = pass_the_quiz(client, quiz.id, question_id, option_id, P1)
    assert result["passed"] is True

    # На своей площадке попытка потрачена — с этого отказа тест и начинается
    used = client.post(f"/quizzes/{quiz.id}/quiz_attempts", headers=P1)
    assert used.status_code == 409
    assert used.json()["error"]["code"] == "attempt_used"

    second, _ = pass_the_quiz(client, quiz.id, question_id, option_id, P2)
    assert second != first
    assert sorted(rows("SELECT id, platform FROM quiz_attempt ORDER BY id")) == [
        (first, "p1"),
        (second, "p2"),
    ]


def test_an_attempt_started_on_p1_is_not_reachable_from_p2(client, sms):
    """Площадка проверяется не только на старте: id попытки приходит с фронта
    сам по себе, и подставить в него чужой — самый дешёвый способ сдать тест
    там, где попытка ещё цела."""
    course, _, _, quiz, _ = common_course()
    question_id, option_id = one_question(quiz.id)
    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_enrollment(uid, course.id, platform="p2")

    attempt = client.post(f"/quizzes/{quiz.id}/quiz_attempts", headers=P1).json()["id"]
    stolen = client.post(
        f"/quiz_attempts/{attempt}/answers",
        json={"question_id": question_id, "option_ids": [option_id]},
        headers=P2,
    )
    assert stolen.status_code == 404
    assert client.post(f"/quiz_attempts/{attempt}/finish", headers=P2).status_code == 404


# -- сертификат --------------------------------------------------------


def test_a_request_on_p1_does_not_close_the_one_on_p2(client, client2, sms):
    """Два документа за один курс — тоже следствие раздельной учёбы: человек
    прошёл его дважды и заплатил дважды. Единственность держит частичный
    индекс, и если площадка в него не вошла, вторая заявка упрётся в базу —
    отказом на экране «Курс пройден».

    Выдача разводится по площадкам следом за заявкой: номера у двух бумаг
    разные, потому что и строки разные."""
    course = make_course()
    login_named(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_enrollment(uid, course.id, platform="p2")

    first = client.post(f"/courses/{course.id}/certificate", headers=P1)
    assert first.status_code == 200, first.text
    second = client.post(f"/courses/{course.id}/certificate", headers=P2)
    assert second.status_code == 200, second.text
    assert second.json()["id"] != first.json()["id"]

    assert rows("SELECT platform FROM certificate ORDER BY platform") == [("p1",), ("p2",)]

    login_admin(client2, sms)
    on_p1 = issue(client2, first.json()["id"])
    on_p2 = issue(client2, second.json()["id"])
    assert (on_p1["platform"], on_p2["platform"]) == ("p1", "p2")
    assert on_p1["number"] != on_p2["number"]


def test_my_certificates_shows_only_the_documents_of_its_own_platform(client, client2, sms):
    """Кабинет у площадок разный: чужой документ в списке — это ссылка
    на бумагу другого бренда."""
    course = make_course()
    login_named(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_enrollment(uid, course.id, platform="p2")

    first = client.post(f"/courses/{course.id}/certificate", headers=P1).json()
    second = client.post(f"/courses/{course.id}/certificate", headers=P2).json()
    # Пока это заявки, кабинет пуст на обеих площадках
    assert client.get("/me/certificates", headers=P1).json() == {"items": []}
    assert client.get("/me/certificates", headers=P2).json() == {"items": []}

    login_admin(client2, sms)
    on_p1 = issue(client2, first["id"])
    on_p2 = issue(client2, second["id"])

    assert [
        item["number"] for item in client.get("/me/certificates", headers=P1).json()["items"]
    ] == [on_p1["number"]]
    assert [
        item["number"] for item in client.get("/me/certificates", headers=P2).json()["items"]
    ] == [on_p2["number"]]


def test_a_number_from_another_platform_answers_exactly_like_a_missing_one(
    client, client2, sms
):
    """Проверка публична и без входа, значит номера по ней перебирают.
    Отличайся отказ чужому номеру от отказа несуществующему хоть текстом,
    хоть статусом — и проверка сама подсказывала бы, что документ где-то
    есть: реестр становится публичным, а в нём ФИО."""
    course = make_course()
    login_named(client, sms)
    make_enrollment(user_id(client), course.id)
    requested = client.post(f"/courses/{course.id}/certificate", headers=P1).json()
    login_admin(client2, sms)
    number = issue(client2, requested["id"])["number"]

    own = client.get(f"/verify/{number}", headers=P1)
    assert own.status_code == 200
    assert own.json()["number"] == number

    stranger = client.get(f"/verify/{number}", headers=P2)
    # Номер той же формы, которого в реестре нет вовсе
    missing = client.get("/verify/KZ-2026-ZZZZZZ", headers=P2)
    assert stranger.status_code == missing.status_code == 404
    assert stranger.json() == missing.json()


def test_the_certificate_number_stays_unique_across_the_whole_database(client, sms):
    """Нумерация у площадок общая: номер диктуют по телефону, и совпади он
    у двух документов — проверка перестала бы быть однозначной. Держит это
    база, а не сценарий выдачи."""
    login(client, sms)
    uid = user_id(client)
    number = "KZ-2026-XB7K2M"
    make_certificate(uid, make_course().id, number=number)

    with pytest.raises(IntegrityError):
        # Другой курс и другая площадка: упереться тут можно только в номер
        make_certificate(uid, make_course().id, number=number, platform="p2")


# -- прогресс ----------------------------------------------------------


def test_a_lesson_completed_on_p1_stays_unfinished_on_p2(client, sms):
    """Прогресс — это то, за что человек заплатил на своей площадке. Утекай
    он на соседнюю, курс закрывался бы сам собой, а вместе с ним открывался
    бы и сертификат."""
    course, lesson, _, _, _ = common_course()
    second_lesson = make_lesson(make_module(course.id).id, title="Второй урок")
    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_enrollment(uid, course.id, platform="p2")

    done = client.post(f"/lessons/{lesson.id}/complete", headers=P1)
    assert done.status_code == 200
    assert done.json()["is_completed"] is True

    assert client.get(f"/lessons/{lesson.id}", headers=P1).json()["is_completed"] is True
    assert client.get(f"/lessons/{lesson.id}", headers=P2).json()["is_completed"] is False

    # Тот же урок отмечается на второй площадке заново, а не «уже пройден»
    assert client.post(f"/lessons/{second_lesson.id}/complete", headers=P2).status_code == 200
    assert rows(
        "SELECT lesson_id, platform FROM lesson_progress ORDER BY platform"
    ) == [(lesson.id, "p1"), (second_lesson.id, "p2")]


def test_the_course_percentage_counts_only_its_own_platform(client, sms):
    """Проценты в кабинете считаются по своей площадке: на второй курс
    начинают с нуля, сколько бы ни было пройдено на первой."""
    course, lesson, _, quiz, task = common_course()
    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_enrollment(uid, course.id, platform="p2")

    client.post(f"/lessons/{lesson.id}/complete", headers=P1)

    on_p1 = client.get("/me/courses", headers=P1).json()["items"][0]
    on_p2 = client.get("/me/courses", headers=P2).json()["items"][0]
    assert on_p1["done_count"] == 1
    assert on_p1["progress_percent"] == 33
    assert on_p2["done_count"] == 0
    assert on_p2["progress_percent"] == 0
    # Кнопка «Продолжить» на второй площадке ведёт в начало курса
    assert on_p2["next_lesson"]["id"] == lesson.id


# -- запись ------------------------------------------------------------


def test_a_lead_is_filed_separately_on_each_platform(client, sms, telegram):
    """Заявка на общий курс подаётся на каждой площадке своя: оплата
    и доступ у них разные, и одна заявка на две означала бы, что человек
    оплатил один курс, а получил другой. Здесь же единственная проверка,
    что запись вообще не сломана: колонка `platform` у заявки NOT NULL,
    и забытая площадка не сохранила бы строку вовсе."""
    # Курс выложен на обеих: заявку на курс чужой площадки подать нельзя
    course = make_course(title="Оценивание для учителей", platforms={"p1": 45000, "p2": 60000})
    login_named(client, sms)

    first = client.post(f"/courses/{course.id}/lead", headers=P1)
    second = client.post(f"/courses/{course.id}/lead", headers=P2)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["id"] != first.json()["id"]

    assert rows("SELECT id, platform FROM lead ORDER BY id") == [
        (first.json()["id"], "p1"),
        (second.json()["id"], "p2"),
    ]
    # Повторная кнопка на своей площадке остаётся напоминанием, а не дублем
    assert client.post(f"/courses/{course.id}/lead", headers=P1).json()["id"] == (
        first.json()["id"]
    )


def test_a_review_is_left_separately_on_each_platform(client, sms):
    """Прошёл курс дважды — оставит два отзыва, каждый в своём каталоге
    (решение 15). Рейтинг по площадкам разводит сессия 2; сейчас проверяем,
    что отзыв вообще пишется и запоминает, откуда пришёл."""
    course = make_course()
    login_named(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_enrollment(uid, course.id, platform="p2")

    first = client.post(
        f"/courses/{course.id}/reviews",
        json={"rating": 5, "text": "Наконец-то понятно, как объяснять оценки родителям."},
        headers=P1,
    )
    second = client.post(
        f"/courses/{course.id}/reviews",
        json={"rating": 4, "text": "Второй раз — ради разбора работ, не пожалела."},
        headers=P2,
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    assert rows("SELECT rating, platform FROM review ORDER BY id") == [(5, "p1"), (4, "p2")]


def test_a_submission_is_filed_on_the_platform_it_was_sent_from(client, sms):
    """Работа уходит на проверку своей площадки: у админа они в одной
    очереди, и метка платформы — единственное, чем они там различаются."""
    course, _, _, _, task = common_course()
    login_named(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_enrollment(uid, course.id, platform="p2")

    assert (
        client.post(
            f"/tasks/{task.id}/submissions",
            json={"text": "Мой урок по критериальному оцениванию.", "files": []},
            headers=P2,
        ).status_code
        == 200
    )
    assert rows("SELECT platform FROM submission") == [("p2",)]
    # На своей площадке задание всё ещё ждёт работу
    assert client.get(f"/tasks/{task.id}", headers=P1).json()["status"] == "none"
    assert client.get(f"/tasks/{task.id}", headers=P2).json()["status"] == "pending"


# -- целостность ответа ------------------------------------------------


def test_my_courses_does_not_mix_halves_of_the_answer(client, sms, telegram):
    """`/me/courses` — один экран из двух половин: доступы и открытые заявки.
    Отсеки площадку только у одной — и человек увидит на второй площадке
    заявку на курс, которого в его «Моих курсах» нет."""
    granted = make_course(title="Оценивание для учителей")
    wanted = make_course(title="Цифровые инструменты урока")
    login_named(client, sms)
    make_enrollment(user_id(client), granted.id)
    assert client.post(f"/courses/{wanted.id}/lead", headers=P1).status_code == 200

    on_p1 = client.get("/me/courses", headers=P1).json()
    assert [item["id"] for item in on_p1["items"]] == [granted.id]
    assert [lead["course"]["id"] for lead in on_p1["leads"]] == [wanted.id]

    on_p2 = client.get("/me/courses", headers=P2).json()
    assert on_p2 == {"items": [], "leads": []}
