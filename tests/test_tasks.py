from urllib.parse import quote

from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.adapters.db.repos import now_utc
from app.config import get_settings
from app.domain.signed_link import sign
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

PDF = b"%PDF-1.4 fake content"
TEMPLATE_KEY = "uploads/2026/08/18/9f3c1a7e4b2d8c05.pdf"
OTHER_PHONE = "+7 (701) 555-33-22"


def make_learning_task(uid, **kw):
    """Задание, доступ к курсу у учителя уже есть. Возвращает курс и задание."""
    course = make_course(title="Критериальное оценивание")
    kw.setdefault("title", "Составьте дескрипторы")
    task = make_task(make_module(course.id).id, **kw)
    make_enrollment(uid, course.id)
    return course, task


def upload(client, name, content=PDF):
    """Настоящая загрузка через POST /files: ключ берётся только оттуда."""
    resp = client.post("/files", files={"file": (name, content)})
    assert resp.status_code == 200, resp.text
    return resp.json()["key"]


def submit(client, task_id, **body):
    return client.post(f"/tasks/{task_id}/submissions", json=body)


def signed_template_path(task_id, name, *, ttl_sec=900, secret=None):
    """Ссылка на шаблон, собранная тестом теми же правилами, что и сервером."""
    path = f"/files/task/{task_id}/{name}"
    expires = int(now_utc().timestamp()) + ttl_sec
    signature = sign(path, expires, secret or get_settings().storage_secret)
    return f"{quote(path)}?e={expires}&s={signature}"


def submission_rows(task_id):
    with get_engine().begin() as conn:
        return conn.execute(
            text(
                "SELECT id, status, comment, text FROM submission"
                " WHERE task_id = :t ORDER BY id"
            ),
            {"t": task_id},
        ).all()


# -- рамка доступа -----------------------------------------------------


def test_task_requires_auth(client):
    task = make_task(make_module(make_course().id).id)
    assert client.get(f"/tasks/{task.id}").status_code == 401
    assert client.get(f"/tasks/{task.id}/template_file").status_code == 401
    assert client.post(f"/tasks/{task.id}/submissions", json={"text": "x"}).status_code == 401


def test_task_404_for_missing_and_invisible_course(client, sms):
    draft = make_course(status="draft")
    hidden_task = make_task(make_module(draft.id).id)

    login(client, sms)
    make_enrollment(user_id(client), draft.id)

    assert client.get("/tasks/999999").status_code == 404
    resp = client.get(f"/tasks/{hidden_task.id}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert client.get(f"/tasks/{hidden_task.id}/template_file").status_code == 404
    assert submit(client, hidden_task.id, text="x").status_code == 404


def test_hidden_task_is_404(client, sms):
    """Скрытое задание исчезает у учителя целиком: не открывается и по прямой
    ссылке, даже когда доступ к курсу выдан (CONTRACT, сессия 7а)."""
    course = make_course()
    task = make_task(make_module(course.id).id, is_hidden=True)

    login(client, sms)
    make_enrollment(user_id(client), course.id)

    assert client.get(f"/tasks/{task.id}").status_code == 404
    assert client.get(f"/tasks/{task.id}/template_file").status_code == 404
    assert submit(client, task.id, text="x").status_code == 404


def test_task_403_without_enrollment_and_after_revoke(client, sms):
    stranger = make_task(make_module(make_course().id).id)
    revoked_course = make_course()
    revoked = make_task(make_module(revoked_course.id).id)

    login(client, sms)
    make_enrollment(user_id(client), revoked_course.id, revoked_at=now_utc())

    resp = client.get(f"/tasks/{stranger.id}")
    assert resp.status_code == 403
    assert resp.json()["error"] == {
        "code": "forbidden",
        "message": "Задание доступно после выдачи доступа к курсу",
    }
    assert client.get(f"/tasks/{revoked.id}").status_code == 403
    assert client.get(f"/tasks/{revoked.id}/template_file").status_code == 403
    assert submit(client, revoked.id, text="x").status_code == 403


# -- GET /tasks/{id} ---------------------------------------------------


def test_task_page_without_submissions(client, sms, storage):
    login(client, sms)
    _, task = make_learning_task(
        user_id(client), submit_format="text", time_required_min=30
    )

    body = client.get(f"/tasks/{task.id}").json()

    assert body == {
        "id": task.id,
        "module_id": task.module_id,
        "title": "Составьте дескрипторы",
        "statement": {"text": "Опишите свой урок"},
        "template_file": None,
        "submit_format": "text",
        "allowed_ext": [],
        "max_size_mb": 20,
        "time_required_min": 30,
        "status": "none",
        "can_submit": True,
        "submissions": [],
    }


def test_task_page_status_follows_last_submission(client, sms, storage):
    login(client, sms)
    uid = user_id(client)
    _, task = make_learning_task(uid)

    make_submission(uid, task.id, status="rework", comment="Дескрипторов нет")
    assert client.get(f"/tasks/{task.id}").json()["status"] == "rework"
    assert client.get(f"/tasks/{task.id}").json()["can_submit"] is True

    make_submission(uid, task.id, status="pending")
    body = client.get(f"/tasks/{task.id}").json()
    assert body["status"] == "pending"
    # Работа у админа — сдавать нечего
    assert body["can_submit"] is False

    make_submission(uid, task.id, status="accepted")
    body = client.get(f"/tasks/{task.id}").json()
    assert body["status"] == "accepted"
    assert body["can_submit"] is False


def test_task_page_history_is_newest_first_and_hides_reviewer(client, client2, sms, storage):
    login(client, sms)
    uid = user_id(client)
    _, task = make_learning_task(uid)
    login_admin(client2, sms)
    admin_id = user_id(client2)

    old = make_submission(
        uid,
        task.id,
        text="Первый вариант",
        status="rework",
        comment="Критерии есть, дескрипторов нет",
        reviewed_by=admin_id,
        reviewed_at=now_utc(),
    )
    fresh = make_submission(uid, task.id, text="Второй вариант")

    history = client.get(f"/tasks/{task.id}").json()["submissions"]

    assert [item["id"] for item in history] == [fresh.id, old.id]
    assert history[1]["comment"] == "Критерии есть, дескрипторов нет"
    assert history[1]["reviewed_at"] is not None
    # Имя проверяющего учителю не отдаётся — поля нет вовсе
    assert "reviewed_by" not in history[0]
    assert "reviewed_by" not in history[1]
    assert set(history[0]) == {
        "id", "status", "created_at", "text", "files", "comment", "reviewed_at",
    }


# -- GET /tasks/{id}/template_file -------------------------------------


def test_template_file_null_and_missing_object(client, sms, storage):
    login(client, sms)
    uid = user_id(client)
    _, no_template = make_learning_task(uid)
    course = make_course()
    lost = make_task(make_module(course.id).id, template_file="uploads/потерян.pdf")
    make_enrollment(uid, course.id)

    assert client.get(f"/tasks/{no_template.id}").json()["template_file"] is None
    assert client.get(f"/tasks/{no_template.id}/template_file").status_code == 404
    # Ключ есть, объекта в хранилище нет: наполнение курса недоделано, не 500
    assert client.get(f"/tasks/{lost.id}").json()["template_file"] is None
    assert client.get(f"/tasks/{lost.id}/template_file").status_code == 404


def test_template_file_link_downloads_bytes(client, sms, storage):
    login(client, sms)
    _, task = make_learning_task(user_id(client), template_file=TEMPLATE_KEY)
    storage.objects[TEMPLATE_KEY] = PDF

    assert client.get(f"/tasks/{task.id}").json()["template_file"] == {
        "name": "9f3c1a7e4b2d8c05.pdf",
        "size_bytes": len(PDF),
        "mime": "application/pdf",
    }

    body = client.get(f"/tasks/{task.id}/template_file").json()
    assert body["url"].startswith(
        f"{get_settings().public_base_url}/files/task/{task.id}/9f3c1a7e4b2d8c05.pdf?e="
    )
    resp = client.get(body["url"].removeprefix(get_settings().public_base_url))
    assert resp.status_code == 200
    assert resp.content == PDF
    assert resp.headers["content-type"] == "application/pdf"


def test_template_file_403_on_forged_and_expired_link(client, storage):
    course = make_course()
    task = make_task(make_module(course.id).id, template_file=TEMPLATE_KEY)
    storage.objects[TEMPLATE_KEY] = PDF
    name = "9f3c1a7e4b2d8c05.pdf"

    forged = signed_template_path(task.id, name, secret="not-the-secret")
    assert client.get(forged).status_code == 403
    expired = client.get(signed_template_path(task.id, name, ttl_sec=-60))
    assert expired.status_code == 403
    assert expired.json()["error"]["code"] == "forbidden"
    # Подпись без сессии действует — право доказывает она, как у материалов
    assert client.get(signed_template_path(task.id, name)).status_code == 200
    assert client.get(signed_template_path(999999, name)).status_code == 404


# -- POST /tasks/{id}/submissions --------------------------------------


def test_submit_text(client, sms, storage, telegram):
    login(client, sms)
    _, task = make_learning_task(user_id(client), submit_format="text")

    resp = submit(client, task.id, text="  Дескрипторы к уроку чтения  ")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["text"] == "Дескрипторы к уроку чтения"
    assert body["files"] == []
    assert body["comment"] is None
    assert body["reviewed_at"] is None
    # Без ФИО и телефона: подробности админ откроет в карточке
    assert telegram.sent == [
        f"Работа №{body['id']} на проверку:"
        " задание „Составьте дескрипторы“, курс „Критериальное оценивание“"
    ]


def test_submit_file_hides_storage_key(client, sms, storage, telegram):
    login(client, sms)
    _, task = make_learning_task(user_id(client), allowed_ext=["pdf", "doc", "docx"])
    key = upload(client, "Дескрипторы.pdf")

    resp = submit(
        client,
        task.id,
        text="Дескрипторы в приложенном файле.",
        files=[{"key": key, "name": "Дескрипторы.pdf"}],
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["files"] == [
        {
            "name": "Дескрипторы.pdf",
            "size_bytes": len(PDF),
            "mime": "application/pdf",
            "url": f"{get_settings().public_base_url}"
            f"/files/submission/{body['id']}/0/deskriptory.pdf",
        }
    ]
    # Ключ хранилища наружу не уходит ни в ответе, ни на экране задания
    assert "uploads/" not in resp.text
    assert "uploads/" not in client.get(f"/tasks/{task.id}").text


def test_submit_422_for_empty_and_wrong_composition(client, sms, storage):
    login(client, sms)
    uid = user_id(client)
    _, both = make_learning_task(uid, submit_format="both")
    course = make_course()
    only_file = make_task(make_module(course.id).id, submit_format="file")
    only_text = make_task(make_module(course.id).id, submit_format="text")
    make_enrollment(uid, course.id)
    key = upload(client, "работа.pdf")

    empty = submit(client, both.id)
    assert empty.status_code == 422
    error = empty.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["fields"] == [
        {"field": "text", "message": "Напишите ответ или приложите файл"}
    ]

    # Задание сдаётся файлом — текст лишний, и наоборот
    extra_text = submit(client, only_file.id, text="ответ", files=[{"key": key, "name": "r.pdf"}])
    assert extra_text.status_code == 422
    assert extra_text.json()["error"]["details"]["fields"][0]["field"] == "text"
    extra_file = submit(client, only_text.id, text="ответ", files=[{"key": key, "name": "r.pdf"}])
    assert extra_file.status_code == 422
    assert extra_file.json()["error"]["details"]["fields"][0]["field"] == "files"


def test_submit_422_for_bad_files(client, sms, storage):
    login(client, sms)
    uid = user_id(client)
    _, task = make_learning_task(uid, allowed_ext=["pdf", "doc", "docx"])
    small = make_course()
    limited = make_task(make_module(small.id).id, max_size_mb=1)
    make_enrollment(uid, small.id)

    wrong_ext = submit(
        client, task.id, files=[{"key": upload(client, "вирус.exe"), "name": "вирус.exe"}]
    )
    assert wrong_ext.status_code == 422
    assert wrong_ext.json()["error"]["details"]["fields"] == [
        {"field": "files", "message": "Формат .exe не принимается — можно: pdf, doc, docx"}
    ]

    unknown_key = submit(
        client, task.id, files=[{"key": "uploads/2026/08/18/нет.pdf", "name": "нет.pdf"}]
    )
    assert unknown_key.status_code == 422
    assert unknown_key.json()["error"]["details"]["fields"] == [
        {"field": "files", "message": "Файл не найден — загрузите заново"}
    ]

    one_file = {"key": upload(client, "работа.pdf"), "name": "работа.pdf"}
    too_many = submit(client, task.id, files=[one_file] * 11)
    assert too_many.status_code == 422
    assert too_many.json()["error"]["details"]["fields"] == [
        {"field": "files", "message": "Можно приложить не больше 10 файлов"}
    ]

    big_key = upload(client, "big.pdf", b"x" * (1024 * 1024 + 1))
    too_big = submit(client, limited.id, files=[{"key": big_key, "name": "big.pdf"}])
    assert too_big.status_code == 422
    assert too_big.json()["error"]["details"]["fields"] == [
        {"field": "files", "message": "Файл больше 1 МБ"}
    ]
    # Ни одна отклонённая работа в базу не попала
    assert submission_rows(task.id) == []


def test_submit_409_while_pending_and_after_accepted(client, sms, storage, telegram):
    login(client, sms)
    uid = user_id(client)
    _, task = make_learning_task(uid)

    assert submit(client, task.id, text="Первый вариант").status_code == 200
    pending = submit(client, task.id, text="Ещё раз")
    assert pending.status_code == 409
    assert pending.json()["error"] == {
        "code": "submission_pending",
        "message": "Работа уже на проверке — дождитесь ответа",
    }

    with get_engine().begin() as conn:
        conn.execute(text("UPDATE submission SET status = 'accepted'"))
    accepted = submit(client, task.id, text="Ещё раз")
    assert accepted.status_code == 409
    assert accepted.json()["error"] == {
        "code": "task_accepted",
        "message": "Задание уже зачтено",
    }
    assert len(submission_rows(task.id)) == 1


def test_resubmit_after_rework_adds_row(client, sms, storage, telegram):
    login(client, sms)
    uid = user_id(client)
    _, task = make_learning_task(uid)
    old = make_submission(
        uid, task.id, status="rework", comment="Добавьте дескрипторы", text="Первый вариант"
    )

    resp = submit(client, task.id, text="Второй вариант")

    assert resp.status_code == 200
    rows = submission_rows(task.id)
    assert len(rows) == 2
    # Прежняя работа не переписывается: вердикт остаётся при ней
    assert rows[0] == (old.id, "rework", "Добавьте дескрипторы", "Первый вариант")
    assert rows[1][1:] == ("pending", None, "Второй вариант")


# -- GET /files/submission/{id}/{index}/{name} -------------------------


def test_submission_file_bytes_for_author_and_admin(client, client2, sms, storage):
    login(client, sms)
    _, task = make_learning_task(user_id(client))
    key = upload(client, "Дескрипторы.pdf")
    body = submit(client, task.id, files=[{"key": key, "name": "Дескрипторы.pdf"}]).json()
    url = body["files"][0]["url"].removeprefix(get_settings().public_base_url)

    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.content == PDF
    assert resp.headers["content-type"] == "application/pdf"
    # Имя для сохранения — из снимка метаданных, а не из адреса
    assert quote("Дескрипторы.pdf") in resp.headers["content-disposition"]

    # Чужому учителю работа не показывается, даже по прямой ссылке
    login(client2, sms, OTHER_PHONE)
    stranger = client2.get(url)
    assert stranger.status_code == 403
    assert stranger.json()["error"]["code"] == "forbidden"

    # Админ проверяет работы — значит и файлы открывает
    login_admin(client2, sms)
    assert client2.get(url).content == PDF


def test_submission_file_401_and_404(client, sms, storage):
    course = make_course()
    task = make_task(make_module(course.id).id)
    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    storage.objects["uploads/2026/08/18/rabota.pdf"] = PDF
    submission = make_submission(
        uid,
        task.id,
        files=[
            {
                "name": "Работа.pdf",
                "url": "uploads/2026/08/18/rabota.pdf",
                "size_bytes": len(PDF),
                "mime": "application/pdf",
            }
        ],
    )
    url = f"/files/submission/{submission.id}/0/rabota.pdf"

    client.cookies.clear()
    assert client.get(url).status_code == 401

    login(client, sms)
    assert client.get(url).status_code == 200
    # Файла с таким номером в работе нет; работы с таким id тоже
    assert client.get(f"/files/submission/{submission.id}/7/rabota.pdf").status_code == 404
    assert client.get("/files/submission/999999/0/rabota.pdf").status_code == 404


def test_second_pending_submission_blocked_by_index(client, sms):
    """Одну работу «на проверке» держит база, а не проверка в коде: гонка
    двух одновременных отправок не создаёт вторую pending-строку."""
    from sqlalchemy.orm import Session as OrmSession

    from app.adapters.db.repos import SubmissionRepo

    login(client, sms)
    uid = user_id(client)
    course = make_course()
    task = make_task(make_module(course.id).id)

    with OrmSession(get_engine()) as db:
        repo = SubmissionRepo(db)
        first = repo.create(uid, task.id, text="Первый", files=[])
        second = repo.create(uid, task.id, text="Второй", files=[])
        db.commit()

    assert first is not None
    assert second is None
    with get_engine().begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM submission WHERE task_id = :t"), {"t": task.id}
        ).scalar()
    assert count == 1


def test_template_with_a_russian_name_still_downloads(client, client2, sms, storage):
    """Имя шаблона задаёт админ, и с сессии 7а оно обычно русское, с пробелами.
    Оно попадает в подписанную ссылку — значит подпись считается по одному
    и тому же виду пути и на выдаче, и на раздаче, иначе учитель получит 403
    на файл, который ему только что приложили."""
    login(client, sms)
    uid = user_id(client)
    course, task = make_learning_task(uid)

    login_admin(client2, sms)
    key = upload(client2, "Шаблон дескрипторов.docx")
    attached = client2.patch(
        f"/admin/tasks/{task.id}",
        json={"template_file": {"key": key, "name": "Шаблон дескрипторов.docx"}},
    )
    assert attached.status_code == 200
    assert attached.json()["template_file"]["name"] == "Шаблон дескрипторов.docx"

    assert client.get(f"/tasks/{task.id}").json()["template_file"]["name"] == (
        "Шаблон дескрипторов.docx"
    )
    body = client.get(f"/tasks/{task.id}/template_file").json()
    resp = client.get(body["url"].removeprefix(get_settings().public_base_url))
    assert resp.status_code == 200
    assert resp.content == PDF
