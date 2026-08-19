from sqlalchemy import text

from app.adapters.db.base import get_engine
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
STUB = {"title": "Опишите свой опыт", "time_required_min": 40}
# Ключ шаблона из примера контракта: имя наружу берётся из него, отдельной
# колонки под имя у задания нет.
TEMPLATE_KEY = "uploads/2026/08/19/shablon-deskriptorov.docx"
DOCX = b"PK fake docx" * 2048


def make_editable_task():
    """Задание из примера контракта вместе с курсом и модулем: их названия —
    хлебные крошки шапки редактора, и в ответе они целиком."""
    course = make_course(title="Критериальное оценивание в начальной школе", category_id=3)
    module = make_module(course.id, title="Модуль 1. Зачем менять оценивание")
    task = make_task(
        module.id,
        title="Составьте дескрипторы к своему уроку",
        statement={"html": "<p>Возьмите ближайший урок и составьте…</p>"},
        submit_format="both",
        allowed_ext=["pdf", "doc", "docx"],
        max_size_mb=20,
        time_required_min=40,
    )
    return course, module, task


def upload(client, name, size_bytes):
    """Файл кладётся настоящим POST /files: шаблон привязывается к тому самому
    ключу, который получил браузер, и подменять его нечем."""
    resp = client.post("/files", files={"file": (name, b"x" * size_bytes, "text/plain")})
    assert resp.status_code == 200, resp.text
    return resp.json()["key"]


def rows(table: str, where: str) -> int:
    """Заглянуть в базу там, где ответа уже нет: после удаления."""
    with get_engine().begin() as conn:
        return conn.scalar(text(f"SELECT count(*) FROM {table} WHERE {where}"))


def stored_html(task_id: int) -> str:
    """Разметка как она легла в базу: чистит её сервер, и проверять чистку
    по одному лишь ответу — значит поверить ответу на слово."""
    with get_engine().begin() as conn:
        return conn.scalar(
            text(f"SELECT statement ->> 'html' FROM task WHERE id = {task_id}")
        )


def updated_at(client, course_id: int) -> str:
    return client.get(f"/admin/courses/{course_id}").json()["updated_at"]


# -- права -------------------------------------------------------------


def test_401_without_login(client):
    _, module, task = make_editable_task()
    assert client.post(f"/admin/modules/{module.id}/tasks", json=STUB).status_code == 401
    assert client.get(f"/admin/tasks/{task.id}").status_code == 401
    assert client.patch(f"/admin/tasks/{task.id}", json={"title": "З"}).status_code == 401
    assert client.delete(f"/admin/tasks/{task.id}").status_code == 401


def test_403_for_teacher(client, sms):
    course, module, task = make_editable_task()
    login(client, sms)
    # Доступ к курсу правки не открывает: редактор — не экран учителя
    make_enrollment(user_id(client), course.id)

    resp = client.get(f"/admin/tasks/{task.id}")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert client.post(f"/admin/modules/{module.id}/tasks", json=STUB).status_code == 403
    assert client.patch(f"/admin/tasks/{task.id}", json={"title": "З"}).status_code == 403
    assert client.delete(f"/admin/tasks/{task.id}").status_code == 403


def test_404_for_missing_task_and_module(client, sms):
    login_admin(client, sms)
    assert client.post("/admin/modules/999999/tasks", json=STUB).status_code == 404
    resp = client.get("/admin/tasks/999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert client.patch("/admin/tasks/999999", json={"title": "З"}).status_code == 404
    assert client.delete("/admin/tasks/999999").status_code == 404


# -- заготовка ---------------------------------------------------------


def test_stub_answers_with_the_editor(client, sms, storage):
    course, module, _ = make_editable_task()
    login_admin(client, sms)

    resp = client.post(f"/admin/modules/{module.id}/tasks", json=STUB)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "id": body["id"],
        "module_id": module.id,
        "course": {
            "id": course.id,
            "lang": "ru",
            "title": "Критериальное оценивание в начальной школе",
        },
        "module": {"id": module.id, "title": "Модуль 1. Зачем менять оценивание"},
        "title": "Опишите свой опыт",
        # Условие пустое: колонка обязательная, а текст появится в редакторе
        "statement": {"html": ""},
        "template_file": None,
        "submit_format": "both",
        "allowed_ext": [],
        "max_size_mb": 20,
        "time_required_min": 40,
        # Заготовка заводится скрытой: задание без условия учителю
        # показывать нечем
        "is_hidden": True,
        "is_ready": False,
        "has_data": False,
    }


def test_stub_goes_last_in_the_module(client, sms):
    """Порядок в модуле общий на все три вида: свой максимум у каждой таблицы
    поставил бы новое задание в середину дерева."""
    course, module, task = make_editable_task()
    login_admin(client, sms)

    created = client.post(f"/admin/modules/{module.id}/tasks", json=STUB).json()
    program = client.get(f"/admin/courses/{course.id}").json()["program"]
    assert [item["id"] for item in program[0]["items"]] == [task.id, created["id"]]


def test_stub_validates_fields(client, sms):
    _, module, _ = make_editable_task()
    login_admin(client, sms)

    def field_of(payload):
        resp = client.post(f"/admin/modules/{module.id}/tasks", json=payload)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        return resp.json()["error"]["details"]["fields"][0]["field"]

    assert field_of({**STUB, "title": "   "}) == "title"
    assert field_of({**STUB, "time_required_min": 601}) == "time_required_min"
    assert field_of({**STUB, "time_required_min": -1}) == "time_required_min"


# -- редактор задания --------------------------------------------------


def test_editor_shows_the_task_whole(client, client2, sms, storage):
    course, module, task = make_editable_task()
    storage.objects[TEMPLATE_KEY] = DOCX
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    make_submission(teacher, task.id)

    login_admin(client2, sms)
    client2.patch(
        f"/admin/tasks/{task.id}",
        json={"template_file": {"key": TEMPLATE_KEY, "name": "Шаблон дескрипторов.docx"}},
    )

    body = client2.get(f"/admin/tasks/{task.id}").json()
    assert body == {
        "id": task.id,
        "module_id": module.id,
        "course": {
            "id": course.id,
            "lang": "ru",
            "title": "Критериальное оценивание в начальной школе",
        },
        "module": {"id": module.id, "title": "Модуль 1. Зачем менять оценивание"},
        "title": "Составьте дескрипторы к своему уроку",
        "statement": {"html": "<p>Возьмите ближайший урок и составьте…</p>"},
        # Так же, как учителю: имя, размер и тип, без ключа хранилища
        "template_file": {
            "name": "Шаблон дескрипторов.docx",
            "size_bytes": len(DOCX),
            "mime": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        },
        "submit_format": "both",
        "allowed_ext": ["pdf", "doc", "docx"],
        "max_size_mb": 20,
        "time_required_min": 40,
        "is_hidden": False,
        "is_ready": True,
        "has_data": True,
    }


def test_editor_shows_hidden_task_of_a_draft_course(client, sms, storage):
    """Админ видит то, чего нет на площадке: фильтр видимости каталога
    к редактору не применяется вовсе."""
    course = make_course(status="draft")
    task = make_task(make_module(course.id).id, is_hidden=True)

    login_admin(client, sms)
    assert client.get(f"/admin/tasks/{task.id}").json()["is_hidden"] is True
    # То же задание учителю не существует: оно скрыто, а курс — черновик
    assert client.get(f"/tasks/{task.id}").status_code == 404


# -- правка ------------------------------------------------------------


def test_patch_returns_the_saved_object(client, sms, storage):
    _, _, task = make_editable_task()
    login_admin(client, sms)

    body = client.patch(
        f"/admin/tasks/{task.id}",
        json={"title": "  Составьте дескрипторы  ", "submit_format": "file",
              "max_size_mb": 5, "time_required_min": 30, "is_hidden": True},
    ).json()
    assert body["title"] == "Составьте дескрипторы"
    assert body["submit_format"] == "file"
    assert body["max_size_mb"] == 5
    assert body["time_required_min"] == 30
    assert body["is_hidden"] is True
    assert client.patch(f"/admin/tasks/{task.id}", json={"title": "   "}).status_code == 422


def test_patch_cleans_the_markup(client, sms, storage):
    """Условие чистит сервер, как и текст урока: что вернулось из PATCH,
    то и лежит в базе — иначе автор не увидит, что его разметку почистили."""
    _, _, task = make_editable_task()
    login_admin(client, sms)

    dirty = (
        '<h2 style="color:red">Дескриптор</h2><script>alert(1)</script>'
        '<p><span class="x">Наблюдаемое <b>действие</b></span>'
        '<a href="javascript:alert(1)">ссылка</a></p>'
    )
    body = client.patch(f"/admin/tasks/{task.id}", json={"statement": {"html": dirty}}).json()
    html = body["statement"]["html"]
    assert "<script>" not in html and "javascript:" not in html
    assert "style=" not in html and "<span" not in html
    assert "<h2>Дескриптор</h2>" in html
    assert "<b>действие</b>" in html
    assert stored_html(task.id) == html
    assert body["is_ready"] is True


def test_empty_statement_is_a_draft_not_an_error(client, sms, storage):
    """Пустое условие — законное состояние: ровно такой заводится заготовка,
    и дерево программы показывает её как «черновик»."""
    _, _, task = make_editable_task()
    login_admin(client, sms)

    body = client.patch(f"/admin/tasks/{task.id}", json={"statement": {"html": "<p><br></p>"}})
    assert body.status_code == 200
    assert body.json()["is_ready"] is False


def test_allowed_ext_is_stored_the_way_submitting_compares_it(client, sms, storage):
    """Расширения без точки и в нижнем регистре — иначе `.PDF` из формы
    не совпадёт ни с чем, а в тексте отказа человек прочитает «можно: .PDF»."""
    _, _, task = make_editable_task()
    login_admin(client, sms)

    body = client.patch(
        f"/admin/tasks/{task.id}", json={"allowed_ext": [".PDF", "Docx", "pdf", "  "]}
    ).json()
    assert body["allowed_ext"] == ["pdf", "docx"]
    # Пустой список — ограничения нет, и это значение, а не «не прислано»
    assert client.patch(f"/admin/tasks/{task.id}",
                        json={"allowed_ext": []}).json()["allowed_ext"] == []


def test_max_size_cannot_promise_more_than_upload_accepts(client, sms, storage):
    _, _, task = make_editable_task()
    login_admin(client, sms)

    resp = client.patch(f"/admin/tasks/{task.id}", json={"max_size_mb": 50})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"] == [
        {"field": "max_size_mb", "message": "Больше 20 МБ сервер не примет"}
    ]
    assert client.patch(f"/admin/tasks/{task.id}",
                        json={"max_size_mb": 0}).status_code == 422
    # Отбитый PATCH не меняет ничего
    assert client.get(f"/admin/tasks/{task.id}").json()["max_size_mb"] == 20


def test_template_file_is_attached_replaced_and_removed(client, sms, storage):
    _, _, task = make_editable_task()
    login_admin(client, sms)
    key = upload(client, "Шаблон.docx", 25600)

    body = client.patch(
        f"/admin/tasks/{task.id}", json={"template_file": {"key": key, "name": "Шаблон.docx"}}
    ).json()
    # Размер сервер взял из хранилища, тип — из имени объекта
    assert body["template_file"]["size_bytes"] == 25600
    assert body["template_file"]["mime"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

    # null убирает шаблон, а байты в приватном хранилище остаются:
    # удалять порт storage не умеет
    assert client.patch(f"/admin/tasks/{task.id}",
                        json={"template_file": None}).json()["template_file"] is None
    assert key in storage.objects


def test_template_file_with_unknown_key_is_404(client, sms, storage):
    _, _, task = make_editable_task()
    login_admin(client, sms)

    resp = client.patch(
        f"/admin/tasks/{task.id}",
        json={"template_file": {"key": "uploads/2026/08/19/нет-такого.docx",
                                "name": "Шаблон.docx"}},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == (
        "Загруженный файл не найден — загрузите его заново"
    )
    assert client.get(f"/admin/tasks/{task.id}").json()["template_file"] is None


def test_hiding_never_bounces(client, client2, sms, storage):
    """PATCH, в котором нет ничего, кроме is_hidden, не отбивается никогда —
    иначе выполнить совет из текста 409 было бы нечем."""
    course, module, task = make_editable_task()
    stub = make_task(module.id, title="Заготовка", statement={"html": ""})
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    make_submission(teacher, task.id)

    login_admin(client2, sms)
    # Задание с чужими сдачами
    assert client2.patch(f"/admin/tasks/{task.id}",
                         json={"is_hidden": True}).json()["is_hidden"] is True
    # И пустая заготовка: условия у неё нет, а спрятать её нужно
    assert client2.patch(f"/admin/tasks/{stub.id}",
                         json={"is_hidden": True}).json()["is_hidden"] is True


# -- удаление ----------------------------------------------------------


def test_delete_removes_the_task(client, sms, storage):
    _, module, task = make_editable_task()
    survivor = make_task(module.id, title="Соседнее задание")
    login_admin(client, sms)

    assert client.delete(f"/admin/tasks/{task.id}").status_code == 204
    assert rows("task", f"id = {task.id}") == 0
    assert rows("task", f"id = {survivor.id}") == 1


def test_delete_of_a_task_with_submissions_is_409_and_changes_nothing(
    client, client2, sms, storage
):
    course, _, task = make_editable_task()
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    make_submission(teacher, task.id)

    login_admin(client2, sms)
    resp = client2.delete(f"/admin/tasks/{task.id}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "has_submissions"
    assert resp.json()["error"]["details"] == {"submissions_count": 1}
    assert resp.json()["error"]["message"] == (
        "На задание сдали 1 работу — удалить его нельзя"
    )
    assert rows("task", f"id = {task.id}") == 1
    assert rows("submission", f"task_id = {task.id}") == 1

    # Дальше его прячут — это и предлагает текст ошибки
    assert client2.patch(f"/admin/tasks/{task.id}",
                         json={"is_hidden": True}).json()["is_hidden"] is True


# -- «Изменён» у курса -------------------------------------------------


def test_every_edit_moves_the_course_updated_at(client, sms, storage):
    """Правка задания — это правка курса: столбец «Изменён» в списке курсов
    без этого врал бы."""
    course, module, task = make_editable_task()
    login_admin(client, sms)

    was = updated_at(client, course.id)
    created = client.post(f"/admin/modules/{module.id}/tasks", json=STUB).json()
    after_create = updated_at(client, course.id)
    assert after_create > was

    client.patch(f"/admin/tasks/{task.id}", json={"time_required_min": 30})
    after_patch = updated_at(client, course.id)
    assert after_patch > after_create

    client.delete(f"/admin/tasks/{created['id']}")
    assert updated_at(client, course.id) > after_patch
