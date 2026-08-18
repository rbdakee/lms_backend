from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.adapters.db.models import LessonFile
from app.adapters.db.repos import now_utc
from tests.conftest import (
    login,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    make_quiz,
    seed,
    user_id,
)


def completed_at(user_id_: int, lesson_id: int):
    with get_engine().begin() as conn:
        return conn.execute(
            text(
                "SELECT completed_at FROM lesson_progress"
                " WHERE user_id = :u AND lesson_id = :l"
            ),
            {"u": user_id_, "l": lesson_id},
        ).scalar_one()


def test_lesson_requires_auth(client):
    course = make_course()
    lesson = make_lesson(make_module(course.id).id)
    assert client.get(f"/lessons/{lesson.id}").status_code == 401
    assert client.post(f"/lessons/{lesson.id}/complete").status_code == 401


def test_lesson_404_for_hidden_lesson_and_invisible_course(client, sms):
    course = make_course()
    hidden = make_lesson(make_module(course.id).id, is_hidden=True)
    draft = make_course(status="draft")
    draft_lesson = make_lesson(make_module(draft.id).id)

    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_enrollment(uid, draft.id)

    assert client.get("/lessons/999999").status_code == 404
    assert client.get(f"/lessons/{hidden.id}").status_code == 404
    assert client.post(f"/lessons/{hidden.id}/complete").status_code == 404
    # Курс-черновик прячет и свои уроки, даже когда доступ выдан
    assert client.get(f"/lessons/{draft_lesson.id}").status_code == 404


def test_lesson_403_without_enrollment_and_after_revoke(client, sms):
    stranger_course = make_course()
    stranger_lesson = make_lesson(make_module(stranger_course.id).id)
    revoked_course = make_course()
    revoked_lesson = make_lesson(make_module(revoked_course.id).id)

    login(client, sms)
    make_enrollment(user_id(client), revoked_course.id, revoked_at=now_utc())

    resp = client.get(f"/lessons/{stranger_lesson.id}")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert client.get(f"/lessons/{revoked_lesson.id}").status_code == 403
    assert client.post(f"/lessons/{revoked_lesson.id}/complete").status_code == 403


def test_lesson_body_and_files(client, sms):
    course = make_course()
    module = make_module(course.id)
    lesson = make_lesson(
        module.id,
        title="Что не так с пятибалльной шкалой",
        body={"html": "<p>Заметка к видео.</p>"},
        duration_label="14:20",
        time_required_min=20,
    )
    second = seed(
        LessonFile(
            lesson_id=lesson.id,
            name="Шаблон.docx",
            url="uploads/2026/08/template.docx",
            size_bytes=25600,
            mime="application/msword",
            order_index=2,
        )
    )
    first = seed(
        LessonFile(
            lesson_id=lesson.id,
            name="Памятка.pdf",
            url="uploads/2026/08/memo.pdf",
            size_bytes=184320,
            mime="application/pdf",
            order_index=1,
        )
    )

    login(client, sms)
    make_enrollment(user_id(client), course.id)
    body = client.get(f"/lessons/{lesson.id}").json()

    assert body["id"] == lesson.id
    assert body["module_id"] == module.id
    assert body["kind"] == "video"
    assert body["title"] == "Что не так с пятибалльной шкалой"
    assert body["body"] == {"html": "<p>Заметка к видео.</p>"}
    assert body["duration_label"] == "14:20"
    assert body["time_required_min"] == 20
    assert body["is_completed"] is False
    # Ссылка на видео и на файлы наружу не уходит — за ними отдельные эндпоинты
    assert "video_url" not in body
    assert [f["id"] for f in body["files"]] == [first.id, second.id]
    assert body["files"][0] == {
        "id": first.id,
        "name": "Памятка.pdf",
        "size_bytes": 184320,
        "mime": "application/pdf",
    }


def test_lesson_without_files(client, sms):
    course = make_course()
    lesson = make_lesson(make_module(course.id).id, kind="text", body={"html": "<p>Текст</p>"})
    login(client, sms)
    make_enrollment(user_id(client), course.id)

    body = client.get(f"/lessons/{lesson.id}").json()
    assert body["files"] == []
    assert body["duration_label"] is None


def test_strict_order_does_not_close_lesson_content(client, sms):
    course = make_course(strict_order=True)
    module = make_module(course.id)
    make_lesson(module.id, title="Первый", order_index=1)
    locked = make_lesson(module.id, title="Второй", order_index=2)

    login(client, sms)
    make_enrollment(user_id(client), course.id)
    program = client.get(f"/courses/{course.id}/program").json()["program"]
    assert program[0]["items"][1]["status"] == "locked"

    # Строгий порядок — правило показа: содержимое отдаётся при доступе
    assert client.get(f"/lessons/{locked.id}").status_code == 200
    assert client.post(f"/lessons/{locked.id}/complete").status_code == 200


def test_complete_is_idempotent(client, sms):
    course = make_course()
    module = make_module(course.id)
    lesson = make_lesson(module.id, title="Первый", order_index=1)
    make_lesson(module.id, title="Второй", order_index=2)

    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)

    first = client.post(f"/lessons/{lesson.id}/complete").json()
    marked_at = completed_at(uid, lesson.id)
    second = client.post(f"/lessons/{lesson.id}/complete").json()

    assert first == second
    # Повторная отметка не сдвигает время первой
    assert completed_at(uid, lesson.id) == marked_at
    assert client.get(f"/lessons/{lesson.id}").json()["is_completed"] is True


def test_complete_returns_progress_block(client, sms):
    course = make_course()
    module = make_module(course.id)
    lesson = make_lesson(module.id, title="Первый", order_index=1)
    quiz = make_quiz(module.id, title="Тест модуля", order_index=2)

    login(client, sms)
    make_enrollment(user_id(client), course.id)

    body = client.post(f"/lessons/{lesson.id}/complete").json()
    assert body == {
        "is_completed": True,
        "done_count": 1,
        "total_count": 2,
        "progress_percent": 50,
        "next_lesson": {"id": quiz.id, "title": "Тест модуля", "kind": "quiz"},
    }


def test_complete_last_item_finishes_course(client, sms):
    course = make_course()
    lesson = make_lesson(make_module(course.id).id)
    login(client, sms)
    make_enrollment(user_id(client), course.id)

    body = client.post(f"/lessons/{lesson.id}/complete").json()
    assert body["done_count"] == body["total_count"] == 1
    assert body["progress_percent"] == 100
    assert body["next_lesson"] is None


# -- GET /lessons/{id}/playback ----------------------------------------


def test_playback_returns_youtube_link(client, sms):
    course = make_course()
    lesson = make_lesson(
        make_module(course.id).id, video_url="https://www.youtube.com/watch?v=kZ3vX1a9Qd0"
    )
    assert client.get(f"/lessons/{lesson.id}/playback").status_code == 401

    login(client, sms)
    make_enrollment(user_id(client), course.id)
    assert client.get(f"/lessons/{lesson.id}/playback").json() == {
        "provider": "youtube",
        "url": "https://www.youtube.com/watch?v=kZ3vX1a9Qd0",
        # Ссылка на YouTube не протухает
        "expires_at": None,
    }


def test_playback_403_after_revoke(client, sms):
    course = make_course()
    lesson = make_lesson(make_module(course.id).id)
    login(client, sms)
    make_enrollment(user_id(client), course.id, revoked_at=now_utc())

    resp = client.get(f"/lessons/{lesson.id}/playback")
    assert resp.status_code == 403
    # Текст свой: экран плеера показывает «не удалось продолжить», а не замок
    assert resp.json()["error"] == {"code": "forbidden", "message": "Доступ к курсу закрыт"}


def test_playback_404_for_text_lesson(client, sms):
    course = make_course()
    text_lesson = make_lesson(
        make_module(course.id).id, kind="text", body={"html": "<p>Текст</p>"}, video_url=None
    )
    login(client, sms)
    make_enrollment(user_id(client), course.id)

    # Урок открывается, а видео у него нет — плееру отдавать нечего
    assert client.get(f"/lessons/{text_lesson.id}").status_code == 200
    assert client.get(f"/lessons/{text_lesson.id}/playback").status_code == 404


def test_playback_open_for_locked_lesson(client, sms):
    course = make_course(strict_order=True)
    module = make_module(course.id)
    make_lesson(module.id, title="Первый", order_index=1)
    locked = make_lesson(module.id, title="Второй", order_index=2)

    login(client, sms)
    make_enrollment(user_id(client), course.id)

    # Строгий порядок — правило показа: видео закрытого урока отдаётся
    assert client.get(f"/lessons/{locked.id}/playback").status_code == 200


def test_playback_rate_limited(client, sms, limiter_clock):
    course = make_course()
    lesson = make_lesson(make_module(course.id).id)
    login(client, sms)
    make_enrollment(user_id(client), course.id)

    for _ in range(10):
        assert client.get(f"/lessons/{lesson.id}/playback").status_code == 200

    resp = client.get(f"/lessons/{lesson.id}/playback")
    assert resp.status_code == 429
    body = resp.json()["error"]
    assert body["code"] == "rate_limited"
    assert body["details"]["retry_after_sec"] > 0

    # Окно скользящее: через минуту ссылку снова выдают
    limiter_clock.shift(61)
    assert client.get(f"/lessons/{lesson.id}/playback").status_code == 200
