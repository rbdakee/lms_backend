from datetime import timedelta

from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.adapters.db.repos import now_utc
from tests.conftest import (
    login,
    login_admin,
    make_course,
    make_enrollment,
    make_module,
    make_submission,
    make_task,
    user_id,
)

TEACHER_PHONE = "+7 (707) 123-45-67"
TEMPLATE_KEY = "uploads/2026/08/18/shablon.pdf"


def teacher_with_task(client, sms, **task_kw):
    """Учитель с заполненным профилем и доступом к курсу с заданием."""
    login(client, sms, TEACHER_PHONE)
    client.patch("/me", json={"last_name": "Нурланова", "first_name": "Айгуль",
                              "middle_name": "Сериковна"})
    uid = user_id(client)
    course = make_course(title="Критериальное оценивание")
    task_kw.setdefault("title", "Составьте дескрипторы")
    task = make_task(make_module(course.id).id, **task_kw)
    make_enrollment(uid, course.id)
    return uid, course, task


def submission_row(submission_id):
    with get_engine().connect() as conn:
        return conn.execute(
            text(
                "SELECT status, comment, reviewed_by, reviewed_at FROM submission"
                " WHERE id = :id"
            ),
            {"id": submission_id},
        ).one()


def review(client, submission_id, **body):
    return client.post(f"/admin/submissions/{submission_id}/review", json=body)


# -- права --------------------------------------------------------------


def test_admin_submissions_forbidden_for_teacher(client, sms):
    login(client, sms)
    resp = client.get("/admin/submissions")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert client.get("/admin/submissions/1").status_code == 403
    assert review(client, 1, verdict="accepted").status_code == 403


# -- GET /admin/submissions --------------------------------------------


def test_queue_defaults_to_pending_and_filters(client, client2, sms, storage):
    uid, course, task = teacher_with_task(client, sms)
    other_course = make_course(title="Курс Б")
    other_task = make_task(make_module(other_course.id).id)
    make_enrollment(uid, other_course.id)

    waiting = make_submission(uid, task.id, created_at=now_utc() - timedelta(days=4))
    fresh = make_submission(uid, other_task.id)
    make_submission(
        uid, other_task.id, status="accepted", reviewed_at=now_utc(),
        created_at=now_utc() - timedelta(days=2),
    )

    login_admin(client2, sms)
    body = client2.get("/admin/submissions").json()

    # По умолчанию это очередь: только pending, и старые сверху
    assert body["total"] == 2
    assert body["page"] == 1 and body["per_page"] == 20
    assert [item["id"] for item in body["items"]] == [waiting.id, fresh.id]
    item = body["items"][0]
    assert item["status"] == "pending"
    assert item["attempt_number"] == 1
    assert item["waiting_days"] == 4
    assert item["teacher"] == {
        "id": uid, "last_name": "Нурланова", "first_name": "Айгуль",
        "middle_name": "Сериковна", "photo_url": None,
    }
    assert item["task"] == {"id": task.id, "title": "Составьте дескрипторы"}
    assert item["course"] == {
        "id": course.id, "lang": "ru", "title": "Критериальное оценивание",
    }
    # Текста и файлов в списке нет — за ними идут в карточку
    assert "text" not in item and "files" not in item

    # Проверенная работа не ждёт
    accepted = client2.get("/admin/submissions?status=accepted").json()
    assert accepted["total"] == 1
    assert accepted["items"][0]["waiting_days"] == 0

    assert client2.get("/admin/submissions?status=all").json()["total"] == 3
    assert client2.get("/admin/submissions?status=rework").json()["total"] == 0
    assert client2.get(f"/admin/submissions?course_id={course.id}").json()["total"] == 1
    assert (
        client2.get(f"/admin/submissions?status=all&course_id={other_course.id}").json()["total"]
        == 2
    )


def test_attempt_number_counts_rework(client, client2, sms, storage):
    uid, _, task = teacher_with_task(client, sms)
    make_submission(
        uid, task.id, status="rework", comment="Дескрипторов нет",
        created_at=now_utc() - timedelta(days=3),
    )
    make_submission(uid, task.id, created_at=now_utc() - timedelta(days=1))

    login_admin(client2, sms)
    items = client2.get("/admin/submissions?status=all").json()["items"]

    # Первая работа и доработка после неё — по одному запросу на всю страницу
    assert [(item["attempt_number"], item["status"]) for item in items] == [
        (1, "rework"),
        (2, "pending"),
    ]


# -- GET /admin/submissions/{id} ---------------------------------------


def test_submission_card_has_statement_and_history(client, client2, sms, storage):
    uid, course, task = teacher_with_task(
        client, sms, template_file=TEMPLATE_KEY, allowed_ext=["pdf"], max_size_mb=10
    )
    storage.objects[TEMPLATE_KEY] = b"%PDF-1.4"
    old = make_submission(
        uid, task.id, text="Первый вариант", status="rework",
        comment="Дескрипторов нет", reviewed_at=now_utc(),
        created_at=now_utc() - timedelta(days=3),
    )
    fresh = make_submission(uid, task.id, text="Второй вариант")

    login_admin(client2, sms)
    body = client2.get(f"/admin/submissions/{fresh.id}").json()

    assert body["id"] == fresh.id
    assert body["attempt_number"] == 2
    assert body["status"] == "pending"
    assert body["text"] == "Второй вариант"
    assert body["files"] == []
    assert body["comment"] is None
    assert body["reviewed_at"] is None
    assert body["task"] == {
        "id": task.id,
        "title": "Составьте дескрипторы",
        "statement": {"text": "Опишите свой урок"},
        "template_file": {
            "name": "shablon.pdf", "size_bytes": 8, "mime": "application/pdf",
        },
        "submit_format": "both",
        "allowed_ext": ["pdf"],
        "max_size_mb": 10,
    }
    # История — прежние сдачи, самой работы в ней нет
    assert [item["id"] for item in body["history"]] == [old.id]
    assert body["history"][0]["comment"] == "Дескрипторов нет"
    assert "reviewed_by" not in body
    assert "reviewed_by" not in body["history"][0]

    assert client2.get("/admin/submissions/999999").status_code == 404


def test_submission_card_visible_for_hidden_course(client, client2, sms, storage):
    login(client, sms)
    uid = user_id(client)
    hidden = make_course(status="hidden")
    task = make_task(make_module(hidden.id).id)
    submission = make_submission(uid, task.id)

    login_admin(client2, sms)
    # Учителю курс уже не показывается, админ работу по нему всё равно видит
    assert client.get(f"/tasks/{task.id}").status_code == 404
    assert client2.get(f"/admin/submissions/{submission.id}").status_code == 200


# -- POST /admin/submissions/{id}/review -------------------------------


def test_review_rework_requires_comment(client, client2, sms, storage):
    uid, _, task = teacher_with_task(client, sms)
    submission = make_submission(uid, task.id)

    login_admin(client2, sms)
    resp = review(client2, submission.id, verdict="rework")

    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"] == [
        {"field": "comment", "message": "При отправке на доработку комментарий обязателен"}
    ]
    # Работа осталась в очереди
    assert submission_row(submission.id) == ("pending", None, None, None)


def test_review_rework_writes_verdict_and_notification(client, client2, sms, storage):
    uid, course, task = teacher_with_task(client, sms)
    submission = make_submission(uid, task.id)

    login_admin(client2, sms)
    admin_id = user_id(client2)
    resp = review(
        client2, submission.id, verdict="rework", comment="Добавьте по одному дескриптору"
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "rework"
    assert body["comment"] == "Добавьте по одному дескриптору"
    assert body["reviewed_at"] is not None
    # Проверенная работа не ждёт
    assert body["waiting_days"] == 0
    # Кто проверил — в базе, наружу не уходит
    assert "reviewed_by" not in body
    status, comment, reviewed_by, reviewed_at = submission_row(submission.id)
    assert (status, comment) == ("rework", "Добавьте по одному дескриптору")
    assert reviewed_by == admin_id
    assert reviewed_at is not None

    with get_engine().connect() as conn:
        n_type, params, n_user = conn.execute(
            text("SELECT type, params, user_id FROM notification")
        ).one()
    assert n_type == "submission_reviewed"
    assert params == {"task_id": task.id, "course_id": course.id, "verdict": "rework"}
    assert n_user == uid

    # Учитель видит вердикт и может прислать доработку
    page = client.get(f"/tasks/{task.id}").json()
    assert page["status"] == "rework"
    assert page["can_submit"] is True
    assert page["submissions"][0]["comment"] == "Добавьте по одному дескриптору"


def test_review_accepted_closes_task(client, client2, sms, storage):
    uid, course, task = teacher_with_task(client, sms)
    submission = make_submission(uid, task.id)

    login_admin(client2, sms)
    # Комментарий при зачёте не обязателен
    body = review(client2, submission.id, verdict="accepted").json()
    assert body["status"] == "accepted"
    assert body["comment"] is None

    # Повторный вердикт не принимается: передумал — будет доработка со своим
    again = review(client2, submission.id, verdict="rework", comment="Нет, доработайте")
    assert again.status_code == 409
    assert again.json()["error"] == {
        "code": "already_reviewed",
        "message": "Работа уже проверена",
    }
    assert submission_row(submission.id)[0] == "accepted"

    page = client.get(f"/tasks/{task.id}").json()
    assert page["status"] == "accepted"
    assert page["can_submit"] is False
    # Зачтённое задание становится пройденным элементом программы
    program = client.get(f"/courses/{course.id}/program").json()["program"]
    item = program[0]["items"][0]
    assert (item["kind"], item["id"], item["status"]) == ("task", task.id, "done")


def test_review_conditional_update_keeps_first_verdict(client, client2, sms, storage):
    """Гонка двух админов: вердикт пишется условным UPDATE из pending, и второе
    решение не затирает первое — mark_reviewed отвечает отказом."""
    from sqlalchemy.orm import Session as OrmSession

    from app.adapters.db.repos import SubmissionRepo

    uid, _, task = teacher_with_task(client, sms)
    submission = make_submission(uid, task.id)

    login_admin(client2, sms)
    admin_id = user_id(client2)
    assert review(client2, submission.id, verdict="accepted").status_code == 200

    with OrmSession(get_engine()) as db:
        repo = SubmissionRepo(db)
        row = repo.by_id(submission.id)
        marked = repo.mark_reviewed(
            row, status="rework", comment="Опоздал", reviewed_by=999, reviewed_at=now_utc()
        )
        db.commit()

    assert marked is False
    status, comment, reviewed_by, _ = submission_row(submission.id)
    assert (status, comment, reviewed_by) == ("accepted", None, admin_id)
