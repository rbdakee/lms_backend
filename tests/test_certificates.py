import re

from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import QuizAttempt
from app.adapters.db.repos import CertificateRepo, now_utc
from tests.conftest import (
    fake_iin,
    login,
    login_admin,
    login_named,
    make_certificate,
    make_certificate_request,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_progress,
    make_quiz,
    make_submission,
    make_task,
    seed,
    user_id,
)

# Номер печатается на бумаге и диктуется по телефону: формат проверяем целиком,
# включая алфавит без похожих начертаний.
NUMBER_RE = re.compile(r"^KZ-\d{4}-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{6}$")

# Номер академии: мы его не придумываем, админ берёт его из своей нумерации.
REGISTRATION_NUMBER = "АКД-2026/117"


def make_cert_course(uid=None, **course_kw):
    """Курс из урока, задания, теста модуля и итогового теста.

    Условия задаются флагами `cert_require_*`; по умолчанию включены уроки
    и итоговый тест — набор с экрана курса. С `uid` доступ уже выдан.
    Возвращает курс и четыре его элемента.
    """
    fields = {"cert_require_lessons": True, "cert_require_final_quiz": True}
    fields.update(course_kw)
    course = make_course(**fields)
    module = make_module(course.id)
    lesson = make_lesson(module.id, order_index=1)
    task = make_task(module.id, order_index=2)
    quiz = make_quiz(module.id, title="Тест модуля", order_index=3)
    final = make_quiz(module.id, title="Итоговый тест", is_final=True, order_index=4)
    if uid is not None:
        make_enrollment(uid, course.id)
    return course, lesson, task, quiz, final


def pass_quiz(uid, quiz_id):
    """Зачётная сданная попытка сырым объектом: проходить тест по HTTP ради
    одной строки чек-листа — лишний шум."""
    return seed(
        QuizAttempt(
            user_id=uid,
            quiz_id=quiz_id,
            question_order=[],
            finished_at=now_utc(),
            score=1,
            passed=True,
            is_counted=True,
            platform="p1",
        )
    )


def start_attempt(uid, quiz_id):
    """Незавершённая попытка: её finish ещё может изменить зачёт. Зачётной
    не помечена — такой её и заводит пересдача поверх уже сданной."""
    return seed(
        QuizAttempt(
            user_id=uid, quiz_id=quiz_id, question_order=[], is_counted=False, platform="p1"
        )
    )


def complete_everything(uid, lesson, task, quiz, final):
    make_progress(uid, lesson.id)
    make_submission(uid, task.id, status="accepted")
    pass_quiz(uid, quiz.id)
    pass_quiz(uid, final.id)


def issue(admin, certificate_id, registration_number=REGISTRATION_NUMBER):
    """Выдача админом — здесь инструмент, а не предмет проверки: сами
    админские ручки проверяет `tests/test_admin_certificates.py`.

    Без неё половину учительских сцен не разыграть: документ у человека
    появляется только после того, как админ его выписал."""
    resp = admin.post(
        f"/admin/certificates/{certificate_id}/issue",
        json={"registration_number": registration_number},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["certificate"]


def registry_certificate(owner, sms, **kw):
    """Сертификат в реестре вместе с его учителем: проверяющий в это время
    никуда не входил — на то она и публичная проверка."""
    login_named(owner, sms)
    return make_certificate(user_id(owner), make_course().id, **kw)


def certificate_rows():
    with get_engine().begin() as conn:
        return conn.execute(
            text("SELECT id, number, user_id, course_id FROM certificate ORDER BY id")
        ).all()


def snapshot(certificate_id):
    """Снимок, с которого админ будет выписывать бумагу: наружу заявка его
    не отдаёт, а записан он уже в момент нажатия."""
    with get_engine().begin() as conn:
        return conn.execute(
            text(
                "SELECT holder_name, course_title, hours, lang, registration_number,"
                " requested_at IS NOT NULL FROM certificate WHERE id = :i"
            ),
            {"i": certificate_id},
        ).one()


def notification_rows():
    with get_engine().begin() as conn:
        return conn.execute(text("SELECT type, params FROM notification ORDER BY id")).all()


def completed_at(course_id):
    with get_engine().begin() as conn:
        return conn.execute(
            text("SELECT completed_at FROM enrollment WHERE course_id = :c"), {"c": course_id}
        ).scalar()


def hide(table, item_id):
    """Прячет уже заведённый элемент. Так его убирает из программы админ,
    когда элемент кто-то успел пройти и удалить его больше нельзя."""
    with get_engine().begin() as conn:
        conn.execute(
            text(f"UPDATE {table} SET is_hidden = true WHERE id = :i"), {"i": item_id}
        )


def revoke(certificate_id):
    """Отзыв мимо админской ручки: тестам публичной проверки нужен отозванный
    документ, а не разыгранный путь админа."""
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE certificate SET revoked_at = now() WHERE id = :i"),
            {"i": certificate_id},
        )


def codes(body):
    return [condition["code"] for condition in body["conditions"]]


# -- чек-лист ----------------------------------------------------------


def test_completion_public_without_counters(client):
    course, *_ = make_cert_course()

    body = client.get(f"/courses/{course.id}/completion").json()

    assert body == {
        "conditions": [
            {
                "code": "lessons",
                "label": "Пройти все уроки",
                "status": "not_started",
                "done_count": None,
                "total_count": 1,
            },
            {
                "code": "final_quiz",
                "label": "Сдать итоговый тест",
                "status": "not_started",
                "done_count": None,
                "total_count": 1,
                "pass_score": 70,
            },
        ],
        "can_request": False,
        "blocker": None,
        "certificate": None,
    }


def test_completion_404_for_missing_and_invisible_course(client, sms):
    draft, *_ = make_cert_course()
    hidden, *_ = make_cert_course()
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE course SET status = 'draft' WHERE id = :c"), {"c": draft.id})
        conn.execute(
            text("UPDATE course SET status = 'hidden' WHERE id = :c"), {"c": hidden.id}
        )

    login_named(client, sms)
    make_enrollment(user_id(client), draft.id)

    assert client.get("/courses/999999/completion").status_code == 404
    # Доступ выдан, но версии курса для площадки не существует
    assert client.get(f"/courses/{draft.id}/completion").status_code == 404
    assert client.get(f"/courses/{hidden.id}/completion").status_code == 404


def test_completion_without_enrollment_has_no_counters(client, sms):
    login_named(client, sms)
    course, lesson, *_ = make_cert_course()
    # Отметка есть, а доступа нет: счётчики всё равно не показываются
    make_progress(user_id(client), lesson.id)

    body = client.get(f"/courses/{course.id}/completion").json()

    assert [condition["done_count"] for condition in body["conditions"]] == [None, None]
    assert body["can_request"] is False


def test_completion_counts_only_visible_items(client, sms):
    login_named(client, sms)
    uid = user_id(client)
    course, lesson, task, quiz, final = make_cert_course(
        uid, cert_require_tasks=True, cert_require_module_quizzes=True
    )
    module_id = lesson.module_id
    make_lesson(module_id, title="Черновик урока", is_hidden=True)
    make_quiz(module_id, title="Черновик теста", is_hidden=True)
    make_task(module_id, title="Черновик задания", is_hidden=True)
    make_progress(uid, lesson.id)
    pass_quiz(uid, quiz.id)

    body = client.get(f"/courses/{course.id}/completion").json()

    assert codes(body) == ["lessons", "tasks", "module_quizzes", "final_quiz"]
    assert body["conditions"][0] == {
        "code": "lessons",
        "label": "Пройти все уроки",
        "status": "done",
        # Скрытое не считается ни в done, ни в total — как везде в программе,
        # и правило одно на все три вида элемента
        "done_count": 1,
        "total_count": 1,
    }
    assert body["conditions"][1]["status"] == "not_started"
    assert body["conditions"][2] == {
        "code": "module_quizzes",
        "label": "Сдать все тесты модулей",
        "status": "done",
        "done_count": 1,
        "total_count": 1,
    }
    assert body["can_request"] is False
    make_submission(uid, task.id, status="accepted")
    pass_quiz(uid, final.id)

    body = client.get(f"/courses/{course.id}/completion").json()
    assert [condition["status"] for condition in body["conditions"]] == ["done"] * 4
    assert body["can_request"] is True


def test_completion_without_conditions_can_request_at_once(client, sms):
    login_named(client, sms)
    course, *_ = make_cert_course(
        user_id(client), cert_require_lessons=False, cert_require_final_quiz=False
    )

    body = client.get(f"/courses/{course.id}/completion").json()

    assert body == {
        "conditions": [],
        "can_request": True,
        "blocker": None,
        "certificate": None,
    }


def test_completion_blocked_by_unfinished_attempt(client, sms):
    login_named(client, sms)
    uid = user_id(client)
    course, lesson, task, quiz, final = make_cert_course(uid)
    complete_everything(uid, lesson, task, quiz, final)
    start_attempt(uid, quiz.id)

    body = client.get(f"/courses/{course.id}/completion").json()

    assert [condition["status"] for condition in body["conditions"]] == ["done", "done"]
    assert body["can_request"] is False
    assert body["blocker"] == {
        "code": "attempt_in_progress",
        "message": "Завершите начатый тест — он ещё может изменить зачёт",
    }


def test_completion_shows_the_request_and_then_the_document(client, client2, sms):
    """Экран завершения курса показывает одну и ту же строку в двух состояниях:
    сразу после нажатия — заявку без номера, после выдачи — документ.

    Кнопка гаснет уже на заявке: просить дважды нечего, а `requested_at`
    у документа остаётся тем же — это та же строка, а не вторая.
    """
    login_named(client, sms)
    uid = user_id(client)
    course, lesson, task, quiz, final = make_cert_course(uid)
    complete_everything(uid, lesson, task, quiz, final)
    requested = client.post(f"/courses/{course.id}/certificate").json()

    body = client.get(f"/courses/{course.id}/completion").json()
    assert body["certificate"] == requested
    assert body["certificate"]["status"] == "requested"
    assert body["certificate"]["number"] is None
    assert body["can_request"] is False

    login_admin(client2, sms)
    issue(client2, requested["id"])

    body = client.get(f"/courses/{course.id}/completion").json()
    assert body["certificate"]["id"] == requested["id"]
    assert body["certificate"]["status"] == "issued"
    assert NUMBER_RE.match(body["certificate"]["number"]), body["certificate"]["number"]
    assert body["certificate"]["requested_at"] == requested["requested_at"]
    assert body["certificate"]["issued_at"] is not None
    assert body["can_request"] is False


# -- заявка ------------------------------------------------------------


def test_certificate_requires_auth(client):
    course, *_ = make_cert_course()

    assert client.post(f"/courses/{course.id}/certificate").status_code == 401
    assert client.get("/me/certificates").status_code == 401


def test_certificate_404_for_missing_and_invisible_course(client, sms):
    draft, *_ = make_cert_course()
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE course SET status = 'draft' WHERE id = :c"), {"c": draft.id})
    login_named(client, sms)
    make_enrollment(user_id(client), draft.id)

    assert client.post("/courses/999999/certificate").status_code == 404
    assert client.post(f"/courses/{draft.id}/certificate").status_code == 404


def test_certificate_403_without_enrollment(client, sms):
    login_named(client, sms)
    # Курс без единого условия: отказ именно по доступу, а не по чек-листу
    course, *_ = make_cert_course(cert_require_lessons=False, cert_require_final_quiz=False)

    resp = client.post(f"/courses/{course.id}/certificate")

    assert resp.status_code == 403
    assert resp.json()["error"] == {
        "code": "forbidden",
        "message": "Доступ к курсу не открыт",
    }
    assert certificate_rows() == []


def test_certificate_409_when_conditions_not_met(client, sms):
    login_named(client, sms)
    uid = user_id(client)
    course, lesson, _, _, final = make_cert_course(uid)
    pass_quiz(uid, final.id)

    resp = client.post(f"/courses/{course.id}/certificate")

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "conditions_not_met"
    assert error["message"] == "Условия сертификата ещё не выполнены"
    # В details тот же чек-лист, что отдаёт completion: экран показывает, чего нет
    assert error["details"]["conditions"] == [
        {
            "code": "lessons",
            "label": "Пройти все уроки",
            "status": "not_started",
            "done_count": 0,
            "total_count": 1,
        },
        {
            "code": "final_quiz",
            "label": "Сдать итоговый тест",
            "status": "done",
            "done_count": 1,
            "total_count": 1,
            "pass_score": 70,
        },
    ]
    assert certificate_rows() == []
    make_progress(uid, lesson.id)
    assert client.post(f"/courses/{course.id}/certificate").status_code == 200


def test_certificate_409_while_attempt_in_progress(client, sms):
    login_named(client, sms)
    uid = user_id(client)
    course, lesson, task, quiz, final = make_cert_course(uid)
    complete_everything(uid, lesson, task, quiz, final)
    start_attempt(uid, final.id)

    resp = client.post(f"/courses/{course.id}/certificate")

    assert resp.status_code == 409
    assert resp.json()["error"] == {
        "code": "attempt_in_progress",
        "message": "Завершите начатый тест — он ещё может изменить зачёт",
    }
    assert certificate_rows() == []


def test_request_writes_a_snapshot_and_no_document(client, sms):
    """Нажатие заводит заявку, а не документ: номера и даты выдачи у неё нет,
    курс пройденным ещё не считается и колокольчик молчит — всё это
    принадлежит выдаче, а её делает админ.

    Снимок при этом снимается сейчас: админ выпишет бумагу по тому ФИО
    и по тем часам, которые были в момент заявки.
    """
    login_named(
        client, sms, last_name="Смагулова", first_name="Гульмира", middle_name="Токтарбековна"
    )
    uid = user_id(client)
    course, *_ = make_cert_course(
        uid,
        title="Критериальное оценивание в начальной школе",
        hours=72,
        cert_require_lessons=False,
        cert_require_final_quiz=False,
    )

    resp = client.post(f"/courses/{course.id}/certificate")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "id": body["id"],
        "status": "requested",
        "number": None,
        "requested_at": body["requested_at"],
        "issued_at": None,
    }
    assert body["requested_at"] is not None
    assert snapshot(body["id"]) == (
        "Смагулова Гульмира Токтарбековна",
        "Критериальное оценивание в начальной школе",
        72,
        "ru",
        # Номер академии вписывает админ в момент выдачи, и до неё он пуст
        "",
        True,
    )
    # «Курс пройден» и колокольчик придут с выдачей, а не с заявкой
    assert completed_at(course.id) is None
    assert notification_rows() == []


def test_the_request_goes_to_telegram_without_personal_data(client, sms, telegram):
    """Заявку кто-то должен увидеть, поэтому она уходит в тот же чат, что
    заявки на курс и работы на проверку, — с меткой площадки первой строкой.

    ФИО и ИИН в карточку не попадают: чат общий, читают его с телефона,
    а подробности админ открывает по кнопке — за дверью со входом.
    """
    iin = fake_iin()
    login_named(client, sms, last_name="Смагулова", first_name="Гульмира", iin=iin)
    uid = user_id(client)
    course, *_ = make_cert_course(
        uid,
        title="Критериальное оценивание в начальной школе",
        cert_require_lessons=False,
        cert_require_final_quiz=False,
    )

    body = client.post(f"/courses/{course.id}/certificate").json()

    assert len(telegram.sent) == 1
    card = telegram.sent[0]
    assert card.title == "Заявка на сертификат"
    assert card.lines[0].startswith("Площадка: ")
    assert card.lines[1] == "Курс: Критериальное оценивание в начальной школе"
    assert len(card.lines) == 2
    assert card.link_url.endswith(f"/certificates/{body['id']}")
    printed = " ".join([card.title, *card.lines, card.link_text, card.link_url])
    for secret in (iin, "Смагулова", "Гульмира"):
        assert secret not in printed


def test_request_twice_gives_one_request(client, sms, telegram):
    """Экран завершения дёргает ручку при открытии, а человек ещё и жмёт F5:
    повтор обязан вернуть ту же заявку — не отказ, не вторую строку и не второе
    сообщение админам."""
    login_named(client, sms)
    uid = user_id(client)
    course, lesson, task, quiz, final = make_cert_course(uid)
    complete_everything(uid, lesson, task, quiz, final)

    first = client.post(f"/courses/{course.id}/certificate")
    second = client.post(f"/courses/{course.id}/certificate")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(certificate_rows()) == 1
    assert len(telegram.sent) == 1


def test_request_without_an_iin_is_refused(client, sms):
    """Без ИИН академия не внесёт человека в реестр, и упереться в это лучше
    здесь, чем в момент выдачи: там учитель уже всё сдал и ждёт документ.

    Самого номера в отказе нет — ни присланного, ни чужого: ИИН
    персональные данные и в текст ошибки не попадает.
    """
    login_named(client, sms, iin=None)
    uid = user_id(client)
    course, lesson, task, quiz, final = make_cert_course(uid)
    complete_everything(uid, lesson, task, quiz, final)

    resp = client.post(f"/courses/{course.id}/certificate")

    assert resp.status_code == 409
    assert resp.json()["error"] == {
        "code": "iin_required",
        "message": "Заполните ИИН — без него сертификат не выдать",
    }
    assert certificate_rows() == []

    assert client.patch("/me", json={"iin": fake_iin()}).status_code == 200
    assert client.post(f"/courses/{course.id}/certificate").json()["status"] == "requested"


def test_second_insert_is_stopped_by_the_index(client, sms):
    """Единственность заявки держит база, а не проверка в сценарии: сцену
    двойного клика разыгрываем на репозитории, минуя идемпотентность."""
    login_named(client, sms)
    uid = user_id(client)
    course, *_ = make_cert_course(uid, cert_require_lessons=False, cert_require_final_quiz=False)
    client.post(f"/courses/{course.id}/certificate")

    with OrmSession(get_engine()) as db:
        repo = CertificateRepo(db)
        rejected = repo.create(
            user_id=uid,
            course_id=course.id,
            holder_name="Смагулова Гульмира Токтарбековна",
            course_title="Курс",
            hours=72,
            lang="ru",
            platform="p1",
        )
        # Отбитая вставка не роняет транзакцию запроса: SAVEPOINT откатил её одну
        found = repo.active_for(uid, course.id, "p1")
        db.commit()

    assert rejected is None
    assert found is not None
    assert len(certificate_rows()) == 1


def test_a_revoked_request_lets_the_teacher_ask_again(client, client2, sms):
    """Ошибочную заявку снимает только отзыв — и он же возвращает человеку
    возможность попросить снова: одну действующую строку на пару человек+курс
    держит `uq_certificate_active`, и отозванная в него больше не входит."""
    login_named(client, sms)
    uid = user_id(client)
    course, *_ = make_cert_course(uid, cert_require_lessons=False, cert_require_final_quiz=False)
    first = client.post(f"/courses/{course.id}/certificate").json()

    login_admin(client2, sms)
    revoked = client2.post(f"/admin/certificates/{first['id']}/revoke")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["certificate"]["status"] == "revoked"

    second = client.post(f"/courses/{course.id}/certificate")

    assert second.status_code == 200
    assert second.json()["id"] != first["id"]
    assert len(certificate_rows()) == 2
    # Обе строки — заявки, и в кабинете по-прежнему пусто
    assert client.get("/me/certificates").json() == {"items": []}


def test_a_revoked_document_lets_the_teacher_ask_again(client, client2, sms):
    """Отозванный документ из кабинета пропадает, а место в очереди
    освобождает: новую бумагу человек просит заново, и это будет новая
    строка с новым номером."""
    login_named(client, sms)
    uid = user_id(client)
    course, *_ = make_cert_course(uid, cert_require_lessons=False, cert_require_final_quiz=False)
    first = client.post(f"/courses/{course.id}/certificate").json()
    login_admin(client2, sms)
    document = issue(client2, first["id"])
    assert [item["number"] for item in client.get("/me/certificates").json()["items"]] == [
        document["number"]
    ]

    assert client2.post(f"/admin/certificates/{first['id']}/revoke").status_code == 200

    second = client.post(f"/courses/{course.id}/certificate")
    assert second.status_code == 200
    assert second.json()["status"] == "requested"
    assert len(certificate_rows()) == 2
    # Отозванный документ в кабинете не показывается, а новая заявка туда
    # ещё не попала
    assert client.get("/me/certificates").json() == {"items": []}


def test_request_ignores_other_courses(client, sms):
    login_named(client, sms)
    uid = user_id(client)
    mine, *_ = make_cert_course(uid, cert_require_lessons=False, cert_require_final_quiz=False)
    other, *_ = make_cert_course(uid, cert_require_lessons=False, cert_require_final_quiz=False)

    client.post(f"/courses/{mine.id}/certificate")
    client.post(f"/courses/{other.id}/certificate")

    rows = certificate_rows()
    # Уникальность держится парой (человек, курс), а не одной заявкой на человека
    assert len(rows) == 2
    assert [row[3] for row in rows] == [mine.id, other.id]


def test_a_request_locks_the_quiz_start(client, client2, sms):
    """Заявка запирает тест наравне с выданным документом: результат,
    поехавший после неё, попал бы под бумагу, которую админ уже выписывает.

    Текст отказа при этом свой — человек ещё ждёт документ, а не получил его,
    и «сертификат уже выдан» на экране было бы неправдой.
    """
    login_named(client, sms)
    uid = user_id(client)
    course, _, _, quiz, _ = make_cert_course(
        uid, cert_require_lessons=False, cert_require_final_quiz=False
    )
    requested = client.post(f"/courses/{course.id}/certificate").json()

    denied = client.post(f"/quizzes/{quiz.id}/quiz_attempts")
    assert denied.status_code == 409
    assert denied.json()["error"] == {
        "code": "certificate_issued",
        "message": "Заявка на сертификат отправлена — результаты теста изменить нельзя",
    }

    login_admin(client2, sms)
    issue(client2, requested["id"])

    denied = client.post(f"/quizzes/{quiz.id}/quiz_attempts")
    assert denied.status_code == 409
    assert denied.json()["error"]["message"] == (
        "Сертификат уже выдан — результаты теста изменить нельзя"
    )


def test_a_request_has_no_pdf(client, client2, sms):
    """Печатать по заявке нечего: ни номера, ни даты выдачи у неё нет.
    Отказ тот же, что у отозванной, — «не найден»."""
    login_named(client, sms)
    uid = user_id(client)
    course, *_ = make_cert_course(uid, cert_require_lessons=False, cert_require_final_quiz=False)
    requested = client.post(f"/courses/{course.id}/certificate").json()

    resp = client.get(f"/certificates/{requested['id']}/pdf")
    assert resp.status_code == 404
    assert resp.json()["error"] == {"code": "not_found", "message": "Сертификат не найден"}

    login_admin(client2, sms)
    issue(client2, requested["id"])
    # Тот же id после выдачи печатается: дело было в состоянии строки
    assert client.get(f"/certificates/{requested['id']}/pdf").status_code == 200


# -- мои сертификаты ---------------------------------------------------


def test_my_certificates_empty(client, sms):
    login_named(client, sms)

    assert client.get("/me/certificates").json() == {"items": []}


def test_my_certificates_shows_only_issued_documents(client, sms, client2):
    """В кабинете сетка выданных документов: заявка сюда не попадает — её
    человек видит на экране своего курса, — отозванный исчезает, а чужой
    не появляется вовсе."""
    login_named(client, sms)
    uid = user_id(client)
    mine = make_course()
    make_certificate(
        uid, mine.id, number="KZ-2026-XB7K2M", registration_number=REGISTRATION_NUMBER
    )
    make_certificate_request(uid, make_course().id)
    dropped = make_certificate(uid, make_course().id, number="KZ-2026-QP4T9L")
    revoke(dropped.id)
    login_named(client2, sms, phone="+7 (777) 555-44-33")
    make_certificate(user_id(client2), make_course().id, number="KZ-2026-NN3H8C")

    items = client.get("/me/certificates").json()["items"]

    assert len(items) == 1
    assert items[0]["course_id"] == mine.id
    assert items[0]["registration_number"] == REGISTRATION_NUMBER
    # revoked_at в кабинете не печатается: отозванных здесь не бывает
    assert set(items[0]) == {
        "id",
        "number",
        "registration_number",
        "course_id",
        "course_title",
        "holder_name",
        "hours",
        "lang",
        "issued_at",
    }


# -- публичная проверка ------------------------------------------------


def test_verify_finds_by_dirty_number_without_login(client, client2, sms):
    registry_certificate(client2, sms, number="KZ-2026-XB7K2M", hours=72)

    for typed in ("KZ-2026-XB7K2M", "kz2026xb7k2m", " kz 2026 xb7k2m ", "KZ_2026_XB7K2M"):
        body = client.get(f"/verify/{typed}").json()
        assert body["status"] == "valid", typed
        assert body["number"] == "KZ-2026-XB7K2M"


def test_verify_shows_both_numbers_and_never_the_iin(client, client2, sms):
    """Комиссии полезнее номер академии, чем наш, поэтому на странице оба.
    А ИИН на неё не попадает: страницу открывает посторонний человек —
    ему довольно того, что напечатано на бумаге, кроме самого номера."""
    iin = fake_iin()
    login_named(client2, sms, iin=iin)
    make_certificate(
        user_id(client2),
        make_course().id,
        number="KZ-2026-XB7K2M",
        registration_number=REGISTRATION_NUMBER,
        course_title="Критериальное оценивание в начальной школе",
        hours=72,
    )

    resp = client.get("/verify/KZ-2026-XB7K2M")

    body = resp.json()
    # Ни user_id, ни course_id, ни id: страница публичная, и школа с телефоном
    # к подлинности отношения не имеют
    assert set(body) == {
        "status",
        "number",
        "registration_number",
        "holder_name",
        "course_title",
        "hours",
        "issued_at",
        "revoked_at",
    }
    assert body["registration_number"] == REGISTRATION_NUMBER
    assert body["holder_name"] == "Смагулова Гульмира Токтарбековна"
    assert body["course_title"] == "Критериальное оценивание в начальной школе"
    assert body["revoked_at"] is None
    assert iin not in resp.text


def test_verify_reports_revoked(client, client2, sms):
    certificate = registry_certificate(client2, sms, number="KZ-2026-XB7K2M")
    revoke(certificate.id)

    body = client.get("/verify/kz-2026-xb7k2m").json()

    # Запись в реестре есть, документ недействителен — это не 404
    assert body["status"] == "revoked"
    assert body["revoked_at"] is not None


def test_verify_404_for_unknown_number(client, client2, sms):
    registry_certificate(client2, sms, number="KZ-2026-XB7K2M")

    for typed in ("KZ-2026-ZZZZZZ", "не номер", "KZ-2026-XB7K2"):
        resp = client.get(f"/verify/{typed}")
        assert resp.status_code == 404, typed
        assert resp.json()["error"] == {
            "code": "not_found",
            "message": "Сертификат не найден",
        }


def test_verify_does_not_find_a_request(client, client2, sms):
    """Заявка в реестр не попадает: номера у неё нет вовсе, и искать её
    по нему нечем — а форма отказа та же, что у несуществующего номера."""
    login_named(client2, sms)
    make_certificate_request(user_id(client2), make_course().id)

    assert client.get("/verify/KZ-2026-XB7K2M").status_code == 404


def test_verify_rate_limited_per_ip(client, client2, sms, verify_limiter_clock):
    registry_certificate(client2, sms, number="KZ-2026-XB7K2M")

    for _ in range(20):
        assert client.get("/verify/KZ-2026-XB7K2M").status_code == 200

    resp = client.get("/verify/KZ-2026-XB7K2M")
    assert resp.status_code == 429
    error = resp.json()["error"]
    assert error["code"] == "rate_limited"
    assert 0 < error["details"]["retry_after_sec"] <= 60
    # Окно уехало — перебор снова разрешён
    verify_limiter_clock.shift(61)
    assert client.get("/verify/KZ-2026-XB7K2M").status_code == 200


# -- находки адверсариальной проверки ----------------------------------


def test_a_request_is_not_accepted_without_a_name(client, sms):
    """ФИО — снимок на бумаге, и снимается он в момент заявки: поправить его
    постфактум нельзя, документ с пустым именем пришлось бы отзывать."""
    login(client, sms)  # вход по SMS заводит человека без фамилии и имени
    uid = user_id(client)
    course, lesson, task, quiz, final = make_cert_course(uid)
    complete_everything(uid, lesson, task, quiz, final)

    resp = client.post(f"/courses/{course.id}/certificate")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "profile_incomplete"
    assert certificate_rows() == []

    # Тот же вход не заполнил и ИИН — второй замок сразу за первым
    client.patch(
        "/me", json={"last_name": "Нурланова", "first_name": "Айгуль", "iin": fake_iin()}
    )
    assert client.post(f"/courses/{course.id}/certificate").status_code == 200
    assert certificate_rows()[0][2] == uid


def test_enabled_condition_without_items_is_not_done(client, sms):
    """Флаг, выставленный до наполнения курса, не должен раздать сертификаты:
    условие без единого элемента не выполнено, а не выполнено само собой."""
    login_named(client, sms)
    uid = user_id(client)
    course = make_course(cert_require_lessons=True, cert_require_tasks=True)
    make_module(course.id)  # ни уроков, ни заданий
    make_enrollment(uid, course.id)

    body = client.get(f"/courses/{course.id}/completion").json()
    assert [c["status"] for c in body["conditions"]] == ["not_started", "not_started"]
    assert body["can_request"] is False

    resp = client.post(f"/courses/{course.id}/certificate")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conditions_not_met"
    assert certificate_rows() == []


def test_hidden_last_lesson_does_not_collapse_the_condition(client, sms):
    """Урок с чужим прогрессом не удаляют, а скрывают. Если бы скрытие
    схлопывало условие, сертификат выдался бы всем, кто до него не дошёл."""
    login_named(client, sms)
    uid = user_id(client)
    course = make_course(cert_require_lessons=True)
    module = make_module(course.id)
    make_lesson(module.id, order_index=1, is_hidden=True)
    make_enrollment(uid, course.id)

    body = client.get(f"/courses/{course.id}/completion").json()
    assert body["conditions"][0]["total_count"] == 0
    assert body["can_request"] is False


def test_hidden_last_quiz_does_not_collapse_the_condition(client, sms):
    """То же правило у теста: тест с попытками не удаляют, а скрывают. Скрытие
    последнего несданного теста не выполняет условие само собой — и не отбирает
    зачёт у того, кто уже прошёл видимый."""
    login_named(client, sms)
    uid = user_id(client)
    course = make_course(cert_require_module_quizzes=True)
    module = make_module(course.id)
    make_quiz(module.id, title="Скрытый тест", order_index=1, is_hidden=True)
    make_enrollment(uid, course.id)

    body = client.get(f"/courses/{course.id}/completion").json()
    assert body["conditions"][0]["total_count"] == 0
    assert body["conditions"][0]["status"] == "not_started"
    assert body["can_request"] is False
    resp = client.post(f"/courses/{course.id}/certificate")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conditions_not_met"

    # Появился видимый тест и он сдан — условие закрыто, скрытый в счётчики
    # не входит ни числителем, ни знаменателем
    visible = make_quiz(module.id, title="Тест модуля", order_index=2)
    pass_quiz(uid, visible.id)
    body = client.get(f"/courses/{course.id}/completion").json()
    assert body["conditions"][0]["done_count"] == 1
    assert body["conditions"][0]["total_count"] == 1
    assert body["can_request"] is True


def test_hiding_a_passed_quiz_does_not_take_the_pass_away(client, sms):
    """Тест с попытками не удаляют, а скрывают — и тот, кто его уже сдал,
    не должен от этого лишиться сертификата: скрытие снимает требование,
    но не отбирает засчитанное (решение владельца).

    До правки строка схлопывалась в «0 из 0 — не начато», и кнопка заявки
    гасла у человека, прошедшего курс целиком.
    """
    login_named(client, sms)
    uid = user_id(client)
    course = make_course(cert_require_module_quizzes=True)
    module = make_module(course.id)
    quiz = make_quiz(module.id, title="Тест модуля", order_index=1)
    make_enrollment(uid, course.id)
    pass_quiz(uid, quiz.id)

    hide("quiz", quiz.id)

    body = client.get(f"/courses/{course.id}/completion").json()
    assert body["conditions"][0] == {
        "code": "module_quizzes",
        "label": "Сдать все тесты модулей",
        "status": "done",
        # Сданное скрытое остаётся и в числителе, и в знаменателе
        "done_count": 1,
        "total_count": 1,
    }
    assert body["can_request"] is True
    assert client.post(f"/courses/{course.id}/certificate").status_code == 200


def test_hiding_a_passed_lesson_and_task_keeps_them_counted(client, sms):
    """То же правило у урока и у задания: скрыть их админ может ровно потому,
    что кто-то их уже прошёл, — и это тот самый человек."""
    login_named(client, sms)
    uid = user_id(client)
    course = make_course(cert_require_lessons=True, cert_require_tasks=True)
    module = make_module(course.id)
    lesson = make_lesson(module.id, order_index=1)
    task = make_task(module.id, order_index=2)
    make_enrollment(uid, course.id)
    make_progress(uid, lesson.id)
    make_submission(uid, task.id, status="accepted")

    hide("lesson", lesson.id)
    hide("task", task.id)

    body = client.get(f"/courses/{course.id}/completion").json()
    assert codes(body) == ["lessons", "tasks"]
    assert [condition["status"] for condition in body["conditions"]] == ["done", "done"]
    assert [
        (condition["done_count"], condition["total_count"]) for condition in body["conditions"]
    ] == [(1, 1), (1, 1)]
    assert body["can_request"] is True


def test_hidden_unpassed_item_stops_being_a_requirement(client, sms):
    """Обратная сторона того же решения, и это не дефект: скрытый элемент
    перестаёт быть требованием. Из двух заданий одно сдано, второе сняли
    с программы — условие закрыто «1 из 1», и заявку принимают."""
    login_named(client, sms)
    uid = user_id(client)
    course = make_course(cert_require_tasks=True)
    module = make_module(course.id)
    submitted = make_task(module.id, title="Сдано", order_index=1)
    removed = make_task(module.id, title="Снято с программы", order_index=2)
    make_enrollment(uid, course.id)
    make_submission(uid, submitted.id, status="accepted")

    body = client.get(f"/courses/{course.id}/completion").json()
    assert (body["conditions"][0]["done_count"], body["conditions"][0]["total_count"]) == (1, 2)
    assert body["can_request"] is False

    hide("task", removed.id)

    body = client.get(f"/courses/{course.id}/completion").json()
    assert (body["conditions"][0]["done_count"], body["conditions"][0]["total_count"]) == (1, 1)
    assert body["can_request"] is True


def test_checklist_counts_the_hidden_pass_but_progress_does_not(client, sms):
    """Два счётчика одного курса считают разное намеренно. Прогресс — это
    программа, которую человек видит перед собой; чек-лист — требования
    к документу, и уже полученный зачёт из него не пропадает."""
    login_named(client, sms)
    uid = user_id(client)
    course = make_course(cert_require_lessons=True)
    module = make_module(course.id)
    first = make_lesson(module.id, title="Урок 1", order_index=1)
    second = make_lesson(module.id, title="Урок 2", order_index=2)
    make_enrollment(uid, course.id)
    make_progress(uid, first.id)
    make_progress(uid, second.id)

    hide("lesson", second.id)

    access = client.get(f"/courses/{course.id}").json()["access"]
    # В программе остался один урок, и он пройден
    assert (access["done_count"], access["total_count"]) == (1, 1)
    assert access["progress_percent"] == 100

    condition = client.get(f"/courses/{course.id}/completion").json()["conditions"][0]
    # А в чек-листе уроков по-прежнему два: снятый с программы человек прошёл
    assert (condition["done_count"], condition["total_count"]) == (2, 2)
    assert condition["status"] == "done"


def test_unfinished_attempt_of_hidden_quiz_does_not_block_the_request(client, sms):
    """Попытка скрытого теста заявку не задерживает: её finish в чек-листе
    уже ничего не поменяет, а брошенная попытка висит незавершённой вечно —
    человек остался бы без документа навсегда."""
    login_named(client, sms)
    uid = user_id(client)
    course, lesson, task, quiz, final = make_cert_course(uid)
    complete_everything(uid, lesson, task, quiz, final)
    hidden = make_quiz(lesson.module_id, title="Скрытый тест", order_index=5, is_hidden=True)
    start_attempt(uid, hidden.id)

    body = client.get(f"/courses/{course.id}/completion").json()
    assert body["blocker"] is None
    assert body["can_request"] is True
    assert client.post(f"/courses/{course.id}/certificate").status_code == 200


def test_unfinished_attempt_does_not_hide_the_checklist(client, sms):
    """Пока условия не выполнены, человеку надо показать чек-лист, а не
    «завершите начатый тест»: он и так видит, чего не хватает."""
    login_named(client, sms)
    uid = user_id(client)
    course, lesson, task, quiz, final = make_cert_course(uid)
    start_attempt(uid, final.id)

    body = client.get(f"/courses/{course.id}/completion").json()
    assert body["blocker"] is None

    resp = client.post(f"/courses/{course.id}/certificate")
    assert resp.json()["error"]["code"] == "conditions_not_met"

    # А когда чек-лист закрыт — блокер появляется, и он же приходит ошибкой
    complete_everything(uid, lesson, task, quiz, final)
    body = client.get(f"/courses/{course.id}/completion").json()
    assert body["blocker"]["code"] == "attempt_in_progress"
    assert body["can_request"] is False
    resp = client.post(f"/courses/{course.id}/certificate")
    assert resp.json()["error"]["code"] == "attempt_in_progress"


def test_race_leaves_one_request_and_one_message(client, sms, telegram, monkeypatch):
    """Проигравший гонку запрос отдаёт чужую заявку — и не должен сверх неё
    слать второе сообщение админам: в очереди она одна."""
    login_named(client, sms)
    uid = user_id(client)
    course, lesson, task, quiz, final = make_cert_course(uid)
    complete_everything(uid, lesson, task, quiz, final)

    real_create = CertificateRepo.create

    def create_after_a_neighbour(self, **kw):
        # Соседний запрос успел завести заявку и отправить своё сообщение
        CertificateRepo.create = real_create
        real_create(self, **kw)
        self.db.flush()
        return None

    monkeypatch.setattr(CertificateRepo, "create", create_after_a_neighbour)
    resp = client.post(f"/courses/{course.id}/certificate")

    assert resp.status_code == 200
    assert resp.json()["status"] == "requested"
    assert len(certificate_rows()) == 1
    # Сообщение отправил победитель, а он в этой сцене до бота не дошёл
    assert telegram.sent == []
