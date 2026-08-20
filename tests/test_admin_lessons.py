from sqlalchemy import text

from app.adapters.db.base import get_engine
from tests.conftest import (
    login,
    login_admin,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_progress,
    make_quiz,
    make_thread_message,
    user_id,
)

TEACHER_PHONE = "+7 (707) 123-45-67"
STUB = {"title": "Обратная связь без оценки", "kind": "video", "time_required_min": 15}


def make_editable_course():
    """Урок из примера контракта вместе с курсом и модулем: их названия —
    хлебные крошки шапки редактора, и в ответе они целиком."""
    course = make_course(title="Критериальное оценивание в начальной школе", category_id=3)
    module = make_module(course.id, title="Модуль 1. Зачем менять оценивание")
    lesson = make_lesson(
        module.id,
        title="Что не так с пятибалльной шкалой",
        body={"html": "<p>Короткая заметка к видео.</p>"},
        video_url="https://youtu.be/dQw4w9WgXcQ",
        duration_label="14:20",
        time_required_min=20,
    )
    return course, module, lesson


def upload(client, name, size_bytes):
    """Файл кладётся настоящим POST /files: привязка материала работает
    с тем самым ключом, который получил браузер, и подменять его нечем."""
    resp = client.post("/files", files={"file": (name, b"x" * size_bytes, "text/plain")})
    assert resp.status_code == 200, resp.text
    return resp.json()["key"]


def rows(table: str, where: str) -> int:
    """Заглянуть в базу там, где ответа уже нет: после удаления."""
    with get_engine().begin() as conn:
        return conn.scalar(text(f"SELECT count(*) FROM {table} WHERE {where}"))


def stored_html(lesson_id: int) -> str:
    """Разметка как она легла в базу: чистит её сервер, и проверять чистку
    по одному лишь ответу — значит поверить ответу на слово."""
    with get_engine().begin() as conn:
        return conn.scalar(text(f"SELECT body ->> 'html' FROM lesson WHERE id = {lesson_id}"))


def updated_at(client, course_id: int) -> str:
    return client.get(f"/admin/courses/{course_id}").json()["updated_at"]


# -- права -------------------------------------------------------------


def test_401_without_login(client):
    _, module, lesson = make_editable_course()
    assert client.post(f"/admin/modules/{module.id}/lessons", json=STUB).status_code == 401
    assert client.get(f"/admin/lessons/{lesson.id}").status_code == 401
    assert client.patch(f"/admin/lessons/{lesson.id}", json={"title": "У"}).status_code == 401
    assert client.delete(f"/admin/lessons/{lesson.id}").status_code == 401
    assert client.post(f"/admin/lessons/{lesson.id}/files",
                       json={"key": "k", "name": "n.pdf"}).status_code == 401
    assert client.delete("/admin/lesson_files/1").status_code == 401


def test_403_for_teacher(client, sms):
    _, module, lesson = make_editable_course()
    login(client, sms)
    resp = client.get(f"/admin/lessons/{lesson.id}")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert client.post(f"/admin/modules/{module.id}/lessons", json=STUB).status_code == 403
    assert client.patch(f"/admin/lessons/{lesson.id}", json={"title": "У"}).status_code == 403
    assert client.delete(f"/admin/lessons/{lesson.id}").status_code == 403
    assert client.post(f"/admin/lessons/{lesson.id}/files",
                       json={"key": "k", "name": "n.pdf"}).status_code == 403
    assert client.delete("/admin/lesson_files/1").status_code == 403


def test_404_for_missing_lesson_module_and_file(client, sms):
    login_admin(client, sms)
    assert client.post("/admin/modules/999999/lessons", json=STUB).status_code == 404
    resp = client.get("/admin/lessons/999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert client.patch("/admin/lessons/999999", json={"title": "У"}).status_code == 404
    assert client.delete("/admin/lessons/999999").status_code == 404
    assert client.post("/admin/lessons/999999/files",
                       json={"key": "k", "name": "n.pdf"}).status_code == 404
    assert client.delete("/admin/lesson_files/999999").status_code == 404


# -- заготовка ---------------------------------------------------------


def test_video_stub_answers_with_the_editor(client, sms):
    course, module, _ = make_editable_course()
    login_admin(client, sms)

    resp = client.post(f"/admin/modules/{module.id}/lessons", json=STUB)
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
        "title": "Обратная связь без оценки",
        "kind": "video",
        # Единственное место, где урок сохраняется пустым: ссылки в момент
        # создания ещё нет, она появится в редакторе
        "body": None,
        "video_url": None,
        "duration_label": None,
        "time_required_min": 15,
        # Заготовка заводится скрытой: в открытом курсе пустой урок иначе
        # появился бы у учителя сразу и не проиграл бы ничего
        "is_hidden": True,
        "is_ready": False,
        "has_data": False,
        "files": [],
    }


def test_text_stub_is_saved_empty_too(client, sms):
    """Заготовка текстового урока тоже пустая: проверка в базе довольна
    и без body — иначе окно «Добавить в программу» не завело бы строку."""
    _, module, _ = make_editable_course()
    login_admin(client, sms)

    body = client.post(
        f"/admin/modules/{module.id}/lessons",
        json={"title": "Критерии и дескрипторы", "kind": "text", "time_required_min": 25},
    ).json()
    assert body["kind"] == "text"
    assert body["body"] is None
    assert body["is_ready"] is False


def test_stub_goes_last_in_the_module(client, sms):
    """Порядок в модуле общий на все три вида: свой максимум у каждой
    таблицы поставил бы новый урок в середину дерева."""
    course, module, lesson = make_editable_course()
    make_quiz(module.id, title="Тест модуля 1", order_index=lesson.order_index + 1)
    login_admin(client, sms)

    created = client.post(f"/admin/modules/{module.id}/lessons", json=STUB).json()
    program = client.get(f"/admin/courses/{course.id}").json()["program"]
    assert [item["id"] for item in program[0]["items"]][-1] == created["id"]


def test_stub_survives_the_copy_of_the_program(client, sms):
    """Заготовка копируется вместе с программой: пустой урок — законная
    строка, и языковая версия курса, где он есть, заводиться не перестаёт."""
    course, module, _ = make_editable_course()
    login_admin(client, sms)
    client.post(f"/admin/modules/{module.id}/lessons", json=STUB)

    resp = client.post(f"/admin/courses/{course.id}/versions",
                       json={"lang": "kz", "copy_program": True})
    assert resp.status_code == 200
    copied = resp.json()["program"][0]["items"][-1]
    assert copied["title"] == "Обратная связь без оценки"
    assert copied["is_ready"] is False


def test_stub_does_not_close_the_publish_button(client, sms):
    """Чек-лист публикации считает по видимым элементам, а заготовка скрыта:
    в `empty_lessons` она не попадёт и «Открыть набор» не погасит. Потеряться
    ей при этом негде — в дереве редактора она есть и помечена сразу
    и «черновиком», и «скрыт»."""
    course = make_course(cover="https://cdn.example.kz/covers/assessment_ru.jpg")
    module = make_module(course.id, title="Модуль 1")
    make_lesson(module.id, title="Что не так с пятибалльной шкалой", order_index=1)
    login_admin(client, sms)
    assert client.get(f"/admin/courses/{course.id}").json()["readiness"]["can_open"] is True

    created = client.post(f"/admin/modules/{module.id}/lessons", json=STUB).json()

    body = client.get(f"/admin/courses/{course.id}").json()
    checks = {item["code"]: item for item in body["readiness"]["items"]}
    assert checks["empty_lessons"] == {
        "code": "empty_lessons",
        "ok": True,
        "blocking": True,
        "text": "Уроков без содержимого нет",
        "items": [],
    }
    assert body["readiness"]["can_open"] is True
    # И «всего по программе» не выросло: скрытое не занимает времени ни у кого
    assert body["program_minutes"] == 15
    stub = next(item for item in body["program"][0]["items"] if item["id"] == created["id"])
    assert (stub["is_hidden"], stub["is_ready"]) == (True, False)


def test_stub_validates_fields(client, sms):
    _, module, _ = make_editable_course()
    login_admin(client, sms)

    def field_of(payload):
        resp = client.post(f"/admin/modules/{module.id}/lessons", json=payload)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        return resp.json()["error"]["details"]["fields"][0]["field"]

    assert field_of({**STUB, "title": "   "}) == "title"
    assert field_of({**STUB, "time_required_min": 601}) == "time_required_min"
    assert field_of({**STUB, "time_required_min": -1}) == "time_required_min"
    assert field_of({**STUB, "kind": "quiz"}) == "kind"


def test_number_out_of_range_is_reported_in_russian(client, sms):
    """Требуемое время звучит одинаково у урока, теста и задания: ограничение
    у них одно и то же."""
    _, module, _ = make_editable_course()
    login_admin(client, sms)

    resp = client.post(
        f"/admin/modules/{module.id}/lessons", json={**STUB, "time_required_min": 601}
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0] == {
        "field": "time_required_min",
        "message": "Требуемое время — от 0 до 600 минут",
    }


# -- редактор урока ----------------------------------------------------


def test_editor_shows_the_lesson_whole(client, client2, sms, storage):
    course, module, lesson = make_editable_course()
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    make_progress(teacher, lesson.id)

    login_admin(client2, sms)
    key = upload(client2, "Памятка по критериям.pdf", 184320)
    file = client2.post(
        f"/admin/lessons/{lesson.id}/files",
        json={"key": key, "name": "Памятка по критериям.pdf"},
    ).json()

    body = client2.get(f"/admin/lessons/{lesson.id}").json()
    assert body == {
        "id": lesson.id,
        "module_id": module.id,
        "course": {
            "id": course.id,
            "lang": "ru",
            "title": "Критериальное оценивание в начальной школе",
        },
        "module": {"id": module.id, "title": "Модуль 1. Зачем менять оценивание"},
        "title": "Что не так с пятибалльной шкалой",
        "kind": "video",
        "body": {"html": "<p>Короткая заметка к видео.</p>"},
        # Учителю ссылку не отдают никогда — только подписанный playback
        "video_url": "https://youtu.be/dQw4w9WgXcQ",
        "duration_label": "14:20",
        "time_required_min": 20,
        "is_hidden": False,
        "is_ready": True,
        "has_data": True,
        "files": [
            {
                "id": file["id"],
                "name": "Памятка по критериям.pdf",
                "size_bytes": 184320,
                "mime": "application/pdf",
            }
        ],
    }


def test_editor_shows_hidden_lesson_of_a_draft_course(client, sms):
    """Админ видит то, чего нет на площадке: фильтр видимости каталога
    к редактору не применяется вовсе."""
    course = make_course(status="draft")
    module = make_module(course.id)
    lesson = make_lesson(module.id, is_hidden=True)

    login_admin(client, sms)
    body = client.get(f"/admin/lessons/{lesson.id}").json()
    assert body["is_hidden"] is True
    # Тот же урок учителю не существует: у него он скрыт, а курс — черновик
    assert client.get(f"/lessons/{lesson.id}").status_code == 404


# -- правка ------------------------------------------------------------


def test_patch_returns_the_saved_object(client, sms):
    _, _, lesson = make_editable_course()
    login_admin(client, sms)

    body = client.patch(
        f"/admin/lessons/{lesson.id}",
        json={"title": "  Что не так с оценкой  ", "duration_label": "12:04",
              "time_required_min": 25, "is_hidden": True},
    ).json()
    assert body["title"] == "Что не так с оценкой"
    assert body["duration_label"] == "12:04"
    assert body["time_required_min"] == 25
    assert body["is_hidden"] is True
    assert client.patch(f"/admin/lessons/{lesson.id}",
                        json={"title": "   "}).status_code == 422


def test_patch_cleans_the_markup(client, sms):
    """Разметку чистит сервер: что вернулось из PATCH, то и лежит в базе —
    иначе автор не увидит, что его разметку почистили."""
    _, module, _ = make_editable_course()
    lesson = make_lesson(module.id, kind="text", video_url=None, body={"html": "<p>Текст</p>"})
    login_admin(client, sms)

    dirty = (
        '<h2 style="color:red">Дескриптор</h2><script>alert(1)</script>'
        '<p><span class="x">Наблюдаемое <b>действие</b></span>'
        '<a href="javascript:alert(1)">ссылка</a></p>'
    )
    body = client.patch(f"/admin/lessons/{lesson.id}", json={"body": {"html": dirty}}).json()
    html = body["body"]["html"]
    assert "<script>" not in html and "javascript:" not in html
    assert "style=" not in html and "<span" not in html
    assert "<h2>Дескриптор</h2>" in html
    assert "<b>действие</b>" in html
    assert stored_html(lesson.id) == html

    # У ссылки остаётся title, а rel сервер проставляет сам: чужая вкладка
    # не должна получить доступ к нашей (CONTRACT, «Содержимое»)
    link = '<p><a href="https://example.kz" title="Подсказка">ссылка</a></p>'
    body = client.patch(f"/admin/lessons/{lesson.id}", json={"body": {"html": link}}).json()
    assert body["body"]["html"] == (
        '<p><a href="https://example.kz" title="Подсказка"'
        ' rel="noopener noreferrer">ссылка</a></p>'
    )


def test_patch_normalizes_every_youtube_spelling(client, sms):
    """Одно и то же видео не должно лежать в базе четырьмя строками:
    найти его правкой тогда нельзя."""
    _, _, lesson = make_editable_course()
    login_admin(client, sms)

    for spelling in (
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
    ):
        body = client.patch(f"/admin/lessons/{lesson.id}", json={"video_url": spelling}).json()
        assert body["video_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_kind_switch_alone_is_422_but_with_content_it_passes(client, sms):
    """Проверяется то, что получится, а не то, что было: вид урока
    и содержимое складываются, и уже эта пара проверяется."""
    _, module, noted = make_editable_course()
    video = make_lesson(module.id, title="Видеоурок без заметки")
    text = make_lesson(module.id, title="Критерии и дескрипторы", kind="text",
                       video_url=None, body={"html": "<p>Критерии</p>"})
    login_admin(client, sms)

    resp = client.patch(f"/admin/lessons/{video.id}", json={"kind": "text"})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"] == [
        {"field": "body", "message": "Без текста текстовый урок не сохранится"}
    ]
    # Отбитый PATCH не меняет ничего
    assert client.get(f"/admin/lessons/{video.id}").json()["kind"] == "video"

    body = client.patch(
        f"/admin/lessons/{video.id}",
        json={"kind": "text", "body": {"html": "<p>Критерии и дескрипторы</p>"}},
    ).json()
    assert body["kind"] == "text"
    assert body["is_ready"] is True

    # В обратную сторону так же: без ссылки текстовый урок видеоуроком не станет
    resp = client.patch(f"/admin/lessons/{text.id}", json={"kind": "video"})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"] == [
        {"field": "video_url", "message": "Без ссылки видеоурок не сохранится"}
    ]
    body = client.patch(
        f"/admin/lessons/{text.id}",
        json={"kind": "video", "video_url": "https://youtu.be/dQw4w9WgXcQ"},
    ).json()
    assert body["kind"] == "video"
    assert body["video_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    # А видеоурок с заметкой станет текстовым и одним kind: заметка под видео
    # и есть тот текст, которого требует новый вид
    assert client.patch(f"/admin/lessons/{noted.id}",
                        json={"kind": "text"}).status_code == 200


def test_empty_video_link_and_bad_link_are_different_errors(client, sms):
    _, _, lesson = make_editable_course()
    login_admin(client, sms)

    # Очищенное поле формы — это «ссылки нет», а не «ссылка неверная»
    resp = client.patch(f"/admin/lessons/{lesson.id}", json={"video_url": ""})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["message"] == (
        "Без ссылки видеоурок не сохранится"
    )

    resp = client.patch(f"/admin/lessons/{lesson.id}", json={"video_url": "https://vimeo.com/1"})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"] == [
        {"field": "video_url", "message": "Не похоже на ссылку YouTube — проверьте адрес"}
    ]
    assert client.get(f"/admin/lessons/{lesson.id}").json()["video_url"] == (
        "https://youtu.be/dQw4w9WgXcQ"
    )


def test_blank_markup_is_empty_content(client, sms):
    """Редактор на пустом поле присылает не пустую строку, а `<p><br></p>`:
    сохранять такой урок заполненным — значит соврать в чек-листе курса."""
    _, module, _ = make_editable_course()
    lesson = make_lesson(module.id, kind="text", video_url=None, body={"html": "<p>Текст</p>"})
    login_admin(client, sms)

    resp = client.patch(f"/admin/lessons/{lesson.id}", json={"body": {"html": "<p><br></p>"}})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "body"
    assert stored_html(lesson.id) == "<p>Текст</p>"


def test_nbsp_is_blank_content_too(client, sms):
    """`&nbsp;` из редактора — такая же пустота, как `<p><br></p>`: сохранив
    её заполненной, чек-лист напишет «Уроков без содержимого нет», а курс
    откроется с пустым уроком."""
    _, module, _ = make_editable_course()
    lesson = make_lesson(module.id, kind="text", video_url=None, body={"html": "<p>Текст</p>"})
    login_admin(client, sms)

    blank = ("<p></p>", "<p><br></p>", "<p>&nbsp;</p>", "<p>&#160;&#8203;</p>", "<p> </p>")
    for html in blank:
        resp = client.patch(f"/admin/lessons/{lesson.id}", json={"body": {"html": html}})
        assert resp.status_code == 422, html
        assert resp.json()["error"]["details"]["fields"][0]["field"] == "body"
    assert stored_html(lesson.id) == "<p>Текст</p>"

    # Буква — уже содержимое, и урок становится готовым
    resp = client.patch(f"/admin/lessons/{lesson.id}", json={"body": {"html": "<p>а</p>"}})
    assert resp.status_code == 200
    assert resp.json()["is_ready"] is True


def test_body_null_erases_the_note_under_the_video(client, sms):
    _, _, lesson = make_editable_course()
    login_admin(client, sms)

    body = client.patch(f"/admin/lessons/{lesson.id}", json={"body": None}).json()
    assert body["body"] is None
    # Видеоурок от этого готовым быть не перестал: заметка была по желанию
    assert body["is_ready"] is True


def test_hiding_never_bounces(client, client2, sms):
    """PATCH, в котором нет ничего, кроме is_hidden, не отбивается никогда —
    иначе выполнить совет из текста 409 было бы нечем."""
    course, module, lesson = make_editable_course()
    stub = make_lesson(module.id, kind="video", video_url=None, body=None, title="Заготовка")
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    make_progress(teacher, lesson.id)

    login_admin(client2, sms)
    # Урок с чужим прогрессом
    assert client2.patch(f"/admin/lessons/{lesson.id}",
                         json={"is_hidden": True}).json()["is_hidden"] is True
    # И незаполненная заготовка: содержимого у неё нет, а спрятать её нужно
    assert client2.patch(f"/admin/lessons/{stub.id}",
                         json={"is_hidden": True}).json()["is_hidden"] is True


# -- удаление ----------------------------------------------------------


def test_delete_takes_files_and_questions_with_it(client, client2, sms, storage):
    """Материалы уходят вместе с уроком; вопросы под уроком — тоже: ссылаться
    им после удаления некуда, а очередь админа джойнит урок."""
    course, module, lesson = make_editable_course()
    survivor = make_lesson(module.id, title="Соседний урок")
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    make_thread_message(lesson.id, course.id, teacher)

    login_admin(client2, sms)
    key = upload(client2, "Памятка.pdf", 512)
    client2.post(f"/admin/lessons/{lesson.id}/files", json={"key": key, "name": "Памятка.pdf"})

    assert client2.delete(f"/admin/lessons/{lesson.id}").status_code == 204
    assert rows("lesson", f"id = {lesson.id}") == 0
    assert rows("lesson_file", f"lesson_id = {lesson.id}") == 0
    assert rows("thread_message", f"lesson_id = {lesson.id}") == 0
    # Соседний урок не задет
    assert rows("lesson", f"id = {survivor.id}") == 1


def test_delete_of_a_passed_lesson_is_409_and_changes_nothing(client, client2, sms):
    course, _, lesson = make_editable_course()
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    make_progress(teacher, lesson.id)

    login_admin(client2, sms)
    resp = client2.delete(f"/admin/lessons/{lesson.id}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "has_progress"
    assert resp.json()["error"]["details"] == {"progress_count": 1}
    assert resp.json()["error"]["message"] == (
        "Урок прошёл 1 человек — его можно скрыть, но не удалить"
    )
    assert rows("lesson", f"id = {lesson.id}") == 1
    assert rows("lesson_progress", f"lesson_id = {lesson.id}") == 1

    # Дальше его прячут — это и предлагает текст ошибки
    assert client2.patch(f"/admin/lessons/{lesson.id}",
                         json={"is_hidden": True}).status_code == 200


# -- материалы ---------------------------------------------------------


def test_file_is_attached_and_detached(client, sms, storage):
    _, _, lesson = make_editable_course()
    login_admin(client, sms)
    key = upload(client, "Памятка по критериям.pdf", 184320)

    resp = client.post(
        f"/admin/lessons/{lesson.id}/files",
        json={"key": key, "name": "Памятка по критериям.pdf"},
    )
    assert resp.status_code == 200
    file = resp.json()
    assert file == {
        "id": file["id"],
        "name": "Памятка по критериям.pdf",
        # Размер сервер взял из хранилища, тип — из имени
        "size_bytes": 184320,
        "mime": "application/pdf",
    }
    second = client.post(
        f"/admin/lessons/{lesson.id}/files", json={"key": key, "name": "Второй.pdf"}
    ).json()
    # Файл встаёт последним
    assert [row["id"] for row in client.get(f"/admin/lessons/{lesson.id}").json()["files"]] == [
        file["id"],
        second["id"],
    ]

    assert client.delete(f"/admin/lesson_files/{file['id']}").status_code == 204
    assert client.get(f"/admin/lessons/{lesson.id}").json()["files"] == [second]
    # Байты в приватном хранилище остаются: удалять порт storage не умеет
    assert key in storage.objects


def test_file_with_unknown_key_is_404(client, sms, storage):
    _, _, lesson = make_editable_course()
    login_admin(client, sms)

    resp = client.post(
        f"/admin/lessons/{lesson.id}/files",
        json={"key": "uploads/2026/08/18/нет-такого.pdf", "name": "Памятка.pdf"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == (
        "Загруженный файл не найден — загрузите его заново"
    )
    assert client.get(f"/admin/lessons/{lesson.id}").json()["files"] == []


# -- «Изменён» у курса -------------------------------------------------


def test_every_edit_moves_the_course_updated_at(client, sms, storage):
    """Правка урока — это правка курса: столбец «Изменён» в списке курсов
    без этого врал бы."""
    course, module, lesson = make_editable_course()
    login_admin(client, sms)
    key = upload(client, "Памятка.pdf", 512)

    was = updated_at(client, course.id)
    created = client.post(f"/admin/modules/{module.id}/lessons", json=STUB).json()
    after_create = updated_at(client, course.id)
    assert after_create > was

    client.patch(f"/admin/lessons/{lesson.id}", json={"time_required_min": 30})
    after_patch = updated_at(client, course.id)
    assert after_patch > after_create

    file = client.post(
        f"/admin/lessons/{lesson.id}/files", json={"key": key, "name": "Памятка.pdf"}
    ).json()
    after_file = updated_at(client, course.id)
    assert after_file > after_patch

    client.delete(f"/admin/lesson_files/{file['id']}")
    after_unlink = updated_at(client, course.id)
    assert after_unlink > after_file

    client.delete(f"/admin/lessons/{created['id']}")
    assert updated_at(client, course.id) > after_unlink
