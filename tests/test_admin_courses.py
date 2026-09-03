import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.repos import CourseAdminRepo
from app.config import get_settings
from tests.conftest import (
    in_parallel,
    login,
    login_admin,
    make_certificate,
    make_course,
    make_enrollment,
    make_lead,
    make_lesson,
    make_module,
    make_option,
    make_progress,
    make_question,
    make_quiz,
    make_submission,
    make_task,
    make_thread_message,
    make_user,
    meet_at,
    user_id,
)

TEACHER_PHONE = "+7 (707) 123-45-67"

BASE = get_settings().public_base_url

# Сигнатура настоящая: обложка опознаётся по байтам объекта, а не по имени,
# которое сочиняет клиент.
COVER_PNG = b"\x89PNG\r\n\x1a\n" + b"cover" * 32
DOCX = b"PK\x03\x04" + b"docx" * 32

# Адрес-ссылка из прежних времён: колонка `cover` осталась на чтение, и у
# курсов, заведённых до загрузки файлом, обложка обязана продолжать работать.
OLD_LINK = "https://cdn.example.kz/covers/assessment_ru.jpg"

# Свои источники, а не из `.env`: у разработчика там localhost с портами.
# Галочку проверяют по каталогу площадки, а он узнаёт её по Origin.
P1_ORIGIN = "https://first.example.kz"
P2_ORIGIN = "https://second.example.kz"
P1 = {"Origin": P1_ORIGIN}
P2 = {"Origin": P2_ORIGIN}


@pytest.fixture
def platform_origins(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "platform_origins", {P1_ORIGIN: "p1", P2_ORIGIN: "p2"}
    )


def catalog_ids(client, headers):
    """Что видно в каталоге площадки — по её же адресу, без входа."""
    resp = client.get("/courses", headers=headers)
    assert resp.status_code == 200, resp.text
    return [version["id"] for group in resp.json()["items"] for version in group["versions"]]


def upload_cover(client, name="Обложка курса.png", content=COVER_PNG):
    """Картинка кладётся настоящим POST /files: курс привязывается к тому
    самому ключу, который получил браузер."""
    resp = client.post("/files", files={"file": (name, content, "application/octet-stream")})
    assert resp.status_code == 200, resp.text
    return {"key": resp.json()["key"], "name": resp.json()["name"]}


def make_rich_course(**kw):
    """Курс из примера контракта: один модуль, в нём по одному элементу
    каждого вида. Текстовый урок пустой — на нём проверяется чек-лист."""
    course = make_course(
        title="Критериальное оценивание в начальной школе",
        cover=OLD_LINK,
        category_id=3,
        **kw,
    )
    module = make_module(course.id, title="Модуль 1. Зачем менять оценивание", order_index=1)
    make_lesson(
        module.id,
        title="Что не так с пятибалльной шкалой",
        duration_label="14:20",
        time_required_min=20,
        order_index=1,
    )
    make_lesson(
        module.id,
        title="Критерии и дескрипторы",
        kind="text",
        video_url=None,
        body={"html": ""},
        time_required_min=25,
        order_index=2,
    )
    make_task(
        module.id,
        title="Составьте дескрипторы",
        statement={"html": "<p>Возьмите ближайший урок…</p>"},
        time_required_min=40,
        order_index=3,
    )
    quiz = make_quiz(
        module.id, title="Тест модуля 1", time_limit_min=15, time_required_min=15, order_index=4
    )
    make_question(quiz.id)
    return course, module


def rows(table: str, where: str) -> int:
    """Заглянуть в базу там, где ответа уже нет: после удаления."""
    with get_engine().begin() as conn:
        return conn.scalar(text(f"SELECT count(*) FROM {table} WHERE {where}"))


# -- права -------------------------------------------------------------


def test_401_without_login(client):
    course = make_course()
    assert client.get("/admin/courses").status_code == 401
    assert client.get(f"/admin/courses/{course.id}").status_code == 401
    assert client.post("/admin/courses", json={"title": "К", "lang": "ru",
                                               "category_id": 1, "hours": 36}).status_code == 401
    assert client.patch(f"/admin/courses/{course.id}", json={"title": "К"}).status_code == 401
    assert client.delete(f"/admin/courses/{course.id}").status_code == 401
    assert client.post(f"/admin/courses/{course.id}/versions",
                       json={"lang": "kz"}).status_code == 401
    assert client.post(f"/admin/courses/{course.id}/duplicate").status_code == 401


def test_403_for_teacher(client, sms):
    course = make_course()
    login(client, sms)
    resp = client.get("/admin/courses")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert client.get(f"/admin/courses/{course.id}").status_code == 403
    assert client.delete(f"/admin/courses/{course.id}").status_code == 403
    assert client.post(f"/admin/courses/{course.id}/duplicate").status_code == 403


def test_404_for_missing_course(client, sms):
    login_admin(client, sms)
    assert client.get("/admin/courses/999999").status_code == 404
    assert client.patch("/admin/courses/999999", json={"title": "К"}).status_code == 404
    assert client.delete("/admin/courses/999999").status_code == 404
    resp = client.post("/admin/courses/999999/duplicate")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# -- список курсов -----------------------------------------------------


def test_empty_list_is_a_page_not_an_error(client, sms):
    login_admin(client, sms)
    body = client.get("/admin/courses").json()
    assert body == {"items": [], "total": 0, "page": 1, "per_page": 20}


def test_list_shows_drafts_counters_and_versions(client, client2, sms):
    course = make_course(title="Критериальное оценивание", status="open", price=45000)
    kz = make_course(group_id=course.group_id, lang="kz", title="Бағалау", status="draft")
    module = make_module(course.id)
    make_lesson(module.id)
    make_lesson(module.id, is_hidden=True)

    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id, completed_at=None)
    make_lead(teacher, course.id, status="new")
    make_lead(make_user("+77010000001").id, course.id, status="declined")
    make_enrollment(make_user("+77010000002").id, course.id)

    login_admin(client2, sms)
    body = client2.get("/admin/courses").json()
    assert body["total"] == 2
    item = next(row for row in body["items"] if row["id"] == course.id)
    assert item["modules_count"] == 1
    # Скрытый урок админ считает: он в курсе есть, просто не показан людям
    assert item["lessons_count"] == 2
    # В работе одна заявка из двух: declined закрыта
    assert item["open_leads_count"] == 1
    assert item["students_count"] == 2
    assert item["completed_count"] == 0
    # Черновик соседней версии — обычная строка списка и чип переключателя
    assert item["versions"] == [{"id": kz.id, "lang": "kz", "status": "draft"}]
    assert next(row for row in body["items"] if row["id"] == kz.id)["versions"] == [
        {"id": course.id, "lang": "ru", "status": "open"}
    ]


def test_list_filters_and_order(client, sms):
    old = make_course(title="Оценивание", status="open")
    draft = make_course(title="Функциональная грамотность", status="draft", lang="kz")
    login_admin(client, sms)

    # Свежеизменённые сверху: правка поднимает курс в списке
    assert client.patch(f"/admin/courses/{old.id}", json={"short": "Кратко"}).status_code == 200
    assert [item["id"] for item in client.get("/admin/courses").json()["items"]] == [
        old.id,
        draft.id,
    ]

    assert client.get("/admin/courses?status=draft").json()["total"] == 1
    assert client.get("/admin/courses?status=hidden").json()["total"] == 0
    assert client.get("/admin/courses?lang=kz").json()["items"][0]["id"] == draft.id
    assert client.get("/admin/courses?q=грамот").json()["items"][0]["id"] == draft.id
    assert client.get("/admin/courses?q=нет такого").json()["total"] == 0


# -- создание черновика ------------------------------------------------


def test_create_draft_answers_with_the_editor(client, sms):
    login_admin(client, sms)
    resp = client.post(
        "/admin/courses",
        json={"title": "Функциональная грамотность", "lang": "ru",
              "category_id": 2, "hours": 36},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.pop("created_at")
    assert body.pop("updated_at")
    assert body == {
        "id": body["id"],
        "group_id": body["group_id"],
        "lang": "ru",
        "title": "Функциональная грамотность",
        "short": "",
        "full": "",
        "cover": None,
        "category_id": 2,
        "hours": 36,
        "duration_text": None,
        # Только что созданный курс — черновик без единой площадки: в каталоге
        # ему не место
        "platforms": [],
        "status": "draft",
        "starts_at": None,
        "strict_order": False,
        "cert_require_lessons": False,
        "cert_require_tasks": False,
        "cert_require_module_quizzes": False,
        "cert_require_final_quiz": False,
        "has_students": False,
        "program_minutes": 0,
        "versions": [],
        "program": [],
        "readiness": {
            "can_open": False,
            "can_plan": False,
            "items": [
                {"code": "cover", "ok": False, "blocking": False,
                 "text": "Обложки нет — в каталоге курс будет без картинки", "items": []},
                {"code": "hours", "ok": True, "blocking": True,
                 "text": "Объём курса указан", "items": []},
                {"code": "starts_at", "ok": True, "blocking": True,
                 "text": "Дата старта не нужна — курс не запланирован", "items": []},
                {"code": "empty_lessons", "ok": True, "blocking": True,
                 "text": "Уроки не проверялись — в программе нет видимых элементов",
                 "items": []},
                {"code": "empty_quizzes", "ok": True, "blocking": True,
                 "text": "Тесты не проверялись — в программе нет видимых элементов",
                 "items": []},
                {"code": "program", "ok": False, "blocking": True,
                 "text": "В программе нет ни одного элемента", "items": []},
            ],
        },
    }


def test_create_gives_the_course_its_own_group(client, sms):
    existing = make_course()
    login_admin(client, sms)
    body = client.post(
        "/admin/courses",
        json={"title": "Новый", "lang": "ru", "category_id": 1, "hours": 12},
    ).json()
    # Вторая языковая версия заводится отдельно, а не при создании
    assert body["group_id"] != existing.group_id


def test_create_validates_fields(client, sms):
    login_admin(client, sms)

    def field_of(payload):
        resp = client.post("/admin/courses", json=payload)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
        return resp.json()["error"]["details"]["fields"][0]["field"]

    base = {"title": "Курс", "lang": "ru", "category_id": 1, "hours": 36}
    assert field_of({**base, "category_id": 99}) == "category_id"
    assert field_of({**base, "title": "   "}) == "title"
    assert field_of({**base, "hours": 0}) == "hours"
    assert field_of({**base, "hours": 1000}) == "hours"
    assert field_of({**base, "lang": "en"}) == "lang"
    # Статус в запросе не принимается: созданный курс — всегда черновик
    assert client.post("/admin/courses", json={**base, "status": "open"}).status_code == 422


# -- редактор курса ----------------------------------------------------


def test_card_shows_hidden_program_and_checklist(client, client2, sms):
    course, module = make_rich_course(status="draft")
    hidden = make_lesson(module.id, title="Черновик урока", order_index=5, is_hidden=True,
                         time_required_min=100)
    make_quiz(module.id, title="Черновик теста", order_index=6, is_hidden=True,
              time_required_min=100)
    make_task(module.id, title="Черновик задания", order_index=7, is_hidden=True,
              time_required_min=100)
    login(client, sms, TEACHER_PHONE)
    make_enrollment(user_id(client), course.id)

    login_admin(client2, sms)
    body = client2.get(f"/admin/courses/{course.id}").json()

    assert body["has_students"] is True
    # Скрытое в дереве есть — админ должен видеть, что он спрятал, — но
    # во «всего по программе» и в чек-лист не идёт
    by_title = {item["title"]: item for item in body["program"][0]["items"]}
    assert by_title["Черновик урока"]["id"] == hidden.id
    assert [by_title[title]["is_hidden"] for title in
            ("Черновик урока", "Черновик теста", "Черновик задания")] == [True] * 3
    assert body["program_minutes"] == 20 + 25 + 40 + 15
    checks = {item["code"]: item for item in body["readiness"]["items"]}
    assert checks["empty_lessons"] == {
        "code": "empty_lessons",
        "ok": False,
        "blocking": True,
        "text": "У 1 урока нет содержимого",
        "items": ["Критерии и дескрипторы"],
    }
    assert checks["program"]["text"] == "В программе 4 элемента"
    assert checks["empty_quizzes"]["ok"] is True
    assert body["readiness"]["can_open"] is False


def test_card_of_draft_is_not_404(client, sms):
    course = make_course(status="draft")
    login_admin(client, sms)
    assert client.get(f"/admin/courses/{course.id}").json()["status"] == "draft"


# -- правка и публикация -----------------------------------------------


def test_patch_changes_fields_and_moves_updated_at(client, sms):
    course = make_course(cover="https://cdn.example.kz/c.jpg")
    login_admin(client, sms)
    before = client.get(f"/admin/courses/{course.id}").json()

    body = client.patch(
        f"/admin/courses/{course.id}",
        json={"short": "Как оценивать без пятибалльной шкалы",
              "platforms": [{"platform": "p1", "price": None}],
              "duration_text": "6 недель", "strict_order": True,
              "cert_require_lessons": True},
    ).json()
    # null у цены — это «Цена по запросу», а не «поле не прислали»
    assert body["platforms"] == [{"platform": "p1", "price": None}]
    assert body["short"] == "Как оценивать без пятибалльной шкалы"
    assert body["duration_text"] == "6 недель"
    assert body["strict_order"] is True
    assert body["cert_require_lessons"] is True
    assert body["cert_require_tasks"] is False
    assert body["updated_at"] > before["updated_at"]


def test_publishing_checks_the_minimum(client, sms):
    course = make_course(status="draft", starts_at=None)
    login_admin(client, sms)

    resp = client.patch(f"/admin/courses/{course.id}", json={"status": "planned"})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"] == [
        {"field": "starts_at", "message": "У запланированного курса дата старта обязательна"}
    ]
    # Отбитый PATCH не меняет ничего
    assert client.get(f"/admin/courses/{course.id}").json()["status"] == "draft"

    # Дата и статус одним запросом — проверяется то, что получится
    body = client.patch(
        f"/admin/courses/{course.id}",
        json={"status": "planned", "starts_at": "2026-09-01"},
    ).json()
    assert body["status"] == "planned"
    assert body["starts_at"] == "2026-09-01"
    starts_at = next(
        item for item in body["readiness"]["items"] if item["code"] == "starts_at"
    )
    assert starts_at["ok"] is True


def test_can_plan_does_not_wait_for_publication(client, sms):
    """Кнопку «Опубликовать как запланированный» открывает дата старта, а не
    нынешний статус курса: иначе черновик с пустыми уроками не опубликовать
    никогда, а ради этого запланированный курс и придуман."""
    course = make_course(status="draft")
    login_admin(client, sms)

    body = client.get(f"/admin/courses/{course.id}").json()
    assert body["readiness"]["can_plan"] is False

    body = client.patch(f"/admin/courses/{course.id}", json={"starts_at": "2026-09-01"}).json()
    assert body["status"] == "draft"
    assert body["readiness"]["can_plan"] is True


def test_planned_course_publishes_with_empty_program(client, sms):
    """Запланированный курс для того и публикуют пустым — собирать заявки."""
    course = make_course(status="draft", cover="https://cdn.example.kz/c.jpg")
    login_admin(client, sms)
    body = client.patch(
        f"/admin/courses/{course.id}",
        json={"status": "planned", "starts_at": "2026-09-01"},
    ).json()
    # Кнопка «Опубликовать как запланированный» открыта, «Открыть набор» — нет:
    # чек-лист при этом честно показывает, что программы ещё нет.
    assert body["readiness"]["can_plan"] is True
    assert body["readiness"]["can_open"] is False
    assert [item["ok"] for item in body["readiness"]["items"] if item["code"] == "program"] == [
        False
    ]


NOT_CHECKED_LESSONS = "Уроки не проверялись — в программе нет видимых элементов"
NOT_CHECKED_QUIZZES = "Тесты не проверялись — в программе нет видимых элементов"


def checklist(client, course_id) -> dict:
    """Чек-лист публикации по `code` — пункты в нём проверяются поимённо."""
    body = client.get(f"/admin/courses/{course_id}").json()
    return {item["code"]: item for item in body["readiness"]["items"]}


def test_checklist_does_not_pretend_it_checked_an_empty_program(client, sms):
    """Пустая программа — это не «проверено и хорошо»: проверять было нечего,
    и зелёная галочка «уроков без содержимого нет» тут врала бы."""
    course = make_course(status="draft")
    login_admin(client, sms)

    checks = checklist(client, course.id)
    assert checks["program"]["text"] == "В программе нет ни одного элемента"
    assert checks["empty_lessons"] == {
        "code": "empty_lessons", "ok": True, "blocking": True,
        "text": NOT_CHECKED_LESSONS, "items": [],
    }
    assert checks["empty_quizzes"] == {
        "code": "empty_quizzes", "ok": True, "blocking": True,
        "text": NOT_CHECKED_QUIZZES, "items": [],
    }


def test_checklist_counts_the_hidden_when_nothing_is_visible(client, sms):
    """Курс собран, но ни один элемент ещё не показан: заготовка заводится
    скрытой. «В программе нет ни одного элемента» при шести элементах
    на соседней вкладке читается как потеря программы."""
    course = make_course(status="draft")
    first = make_module(course.id, title="Модуль 1", order_index=1)
    second = make_module(course.id, title="Модуль 2", order_index=2)
    for module in (first, second):
        make_lesson(module.id, is_hidden=True, order_index=1)
        make_quiz(module.id, is_hidden=True, order_index=2)
        make_task(module.id, is_hidden=True, order_index=3)
    login_admin(client, sms)

    checks = checklist(client, course.id)
    assert checks["program"] == {
        "code": "program",
        "ok": False,
        "blocking": True,
        "text": "В программе нет ни одного видимого элемента: скрыто 6",
        "items": [],
    }
    # У обоих тестов ноль вопросов, но они скрыты: считать по ним нечего,
    # и пункт про вопросы честно говорит, что не проверялся
    assert checks["empty_quizzes"]["text"] == NOT_CHECKED_QUIZZES
    assert checks["empty_quizzes"]["ok"] is True
    assert checks["empty_lessons"]["text"] == NOT_CHECKED_LESSONS
    assert client.get(f"/admin/courses/{course.id}").json()["readiness"]["can_open"] is False


def test_missing_cover_warns_but_does_not_close_the_enrollment(client, sms):
    """Обложка — единственный неблокирующий пункт: без картинки курс в каталоге
    выглядит бедно, но пускать в него людей это не мешает."""
    course = make_course(status="draft", cover=None)
    module = make_module(course.id, title="Модуль 1")
    make_lesson(module.id, title="Что не так с пятибалльной шкалой", order_index=1)
    login_admin(client, sms)

    checks = checklist(client, course.id)
    assert checks["cover"] == {
        "code": "cover",
        "ok": False,
        "blocking": False,
        "text": "Обложки нет — в каталоге курс будет без картинки",
        "items": [],
    }
    # Пункт не выполнен, а кнопка открыта — предупреждение, а не требование
    assert client.get(f"/admin/courses/{course.id}").json()["readiness"]["can_open"] is True


def test_blocking_check_closes_the_enrollment_even_with_a_cover(client, sms):
    """Остальные пункты кнопку держат: невыполненный blocking гасит «Открыть
    набор» и при загруженной обложке."""
    course = make_course(status="draft", cover="https://cdn.example.kz/c.jpg")
    module = make_module(course.id, title="Модуль 1")
    make_lesson(module.id, title="Критерии и дескрипторы", kind="text", video_url=None,
                body={"html": ""}, order_index=1)
    login_admin(client, sms)

    body = client.get(f"/admin/courses/{course.id}").json()
    assert body["readiness"]["can_open"] is False
    assert [item["code"] for item in body["readiness"]["items"] if not item["ok"]] == [
        "empty_lessons"
    ]


def test_number_out_of_range_is_reported_in_russian(client, sms):
    """Границы числовых полей отвечают тем же видом ошибки, что и самописные
    проверки: английский текст pydantic попадал прямо в подпись под полем."""
    course = make_course()
    login_admin(client, sms)

    resp = client.patch(f"/admin/courses/{course.id}", json={"hours": 0})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0] == {
        "field": "hours",
        "message": "Объём курса — от 1 до 999 часов",
    }

    # Тот же текст на верхней границе и при создании курса: ограничение одно
    resp = client.post(
        "/admin/courses",
        json={"title": "Новый", "lang": "ru", "category_id": 1, "hours": 1000},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["message"] == (
        "Объём курса — от 1 до 999 часов"
    )

    resp = client.patch(
        f"/admin/courses/{course.id}",
        json={"platforms": [{"platform": "p1", "price": -1}]},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0] == {
        "field": "price",
        "message": "Цена не может быть отрицательной",
    }


def test_patch_validates_category_and_empty_title(client, sms):
    course = make_course()
    login_admin(client, sms)
    resp = client.patch(f"/admin/courses/{course.id}", json={"category_id": 99})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0] == {
        "field": "category_id",
        "message": "Такой категории нет",
    }
    nameless = make_course(title="", status="draft")
    resp = client.patch(f"/admin/courses/{nameless.id}", json={"status": "open"})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "title"


def test_patch_refuses_an_empty_title(client, sms):
    """Пустое название отбивается так же, как у модуля, урока и теста: иначе
    в списке курсов остаётся строка без имени, и открыть её нечем."""
    course = make_course(title="Критериальное оценивание")
    login_admin(client, sms)

    resp = client.patch(f"/admin/courses/{course.id}", json={"title": "   "})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0] == {
        "field": "title",
        "message": "Без названия курс не сохранить",
    }
    assert client.get(f"/admin/courses/{course.id}").json()["title"] == "Критериальное оценивание"

    # Пробелы по краям обрезаются, как и при создании
    body = client.patch(f"/admin/courses/{course.id}", json={"title": "  Новое  "}).json()
    assert body["title"] == "Новое"


# -- галочки публикации и цены -----------------------------------------


def test_patch_sets_and_clears_the_publication_checkboxes(
    client, client2, sms, platform_origins
):
    """Галочка площадки — это строка `course_platform`, и ставит её тот же
    PATCH. Курс появляется в каталоге ровно той площадки, которую отметили,
    и уходит из каталога той, с которой галочку сняли."""
    course = make_course(title="Критериальное оценивание", platforms={})
    login_admin(client, sms)

    body = client.patch(
        f"/admin/courses/{course.id}",
        json={"platforms": [{"platform": "p1", "price": 45000},
                            {"platform": "p2", "price": 60000}]},
    ).json()
    assert body["platforms"] == [{"platform": "p1", "price": 45000},
                                 {"platform": "p2", "price": 60000}]
    assert catalog_ids(client2, P1) == [course.id]
    assert catalog_ids(client2, P2) == [course.id]

    # Галочку сняли со второй площадки: там курса нет, на первой он остался
    body = client.patch(
        f"/admin/courses/{course.id}",
        json={"platforms": [{"platform": "p1", "price": 45000}]},
    ).json()
    assert body["platforms"] == [{"platform": "p1", "price": 45000}]
    assert catalog_ids(client2, P1) == [course.id]
    assert catalog_ids(client2, P2) == []


def test_each_platform_keeps_its_own_price(client, sms):
    """Цена — свойство пары «курс и площадка»: у одного курса их две,
    и обе приходят обратно и в карточку редактора, и в строку списка."""
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    login_admin(client, sms)

    both = [{"platform": "p1", "price": 45000}, {"platform": "p2", "price": 60000}]
    assert client.get(f"/admin/courses/{course.id}").json()["platforms"] == both
    assert client.get("/admin/courses").json()["items"][0]["platforms"] == both

    body = client.patch(
        f"/admin/courses/{course.id}",
        json={"platforms": [{"platform": "p1", "price": 50000},
                            {"platform": "p2", "price": None}]},
    ).json()
    # Пустая цена — «Цена по запросу», а не снятая галочка: строка на месте
    assert body["platforms"] == [{"platform": "p1", "price": 50000},
                                 {"platform": "p2", "price": None}]


def test_a_patch_without_platforms_leaves_the_set_alone(client, sms):
    """Поля нет в запросе — набор не трогаем: вкладка «Основное» сохраняется
    отдельно от галочек, и правка названия не имеет права снять публикацию."""
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    login_admin(client, sms)

    body = client.patch(
        f"/admin/courses/{course.id}", json={"title": "Новое название"}
    ).json()
    assert body["platforms"] == [{"platform": "p1", "price": 45000},
                                 {"platform": "p2", "price": 60000}]


def test_an_empty_list_takes_the_course_out_of_every_catalog(
    client, client2, sms, platform_origins
):
    """Пустой список — все галочки сняты: курс не показывают нигде."""
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    login_admin(client, sms)

    body = client.patch(f"/admin/courses/{course.id}", json={"platforms": []}).json()
    assert body["platforms"] == []
    assert catalog_ids(client2, P1) == []
    assert catalog_ids(client2, P2) == []


def test_the_same_platform_twice_is_refused(client, sms):
    """Две цены на одну галочку — противоречие экрана, и выбирать за него
    нельзя. Текст под полем русский: это проверка сценария, а не разбор
    запроса."""
    course = make_course(platforms={"p1": 45000})
    login_admin(client, sms)

    resp = client.patch(
        f"/admin/courses/{course.id}",
        json={"platforms": [{"platform": "p1", "price": 45000},
                            {"platform": "p1", "price": 60000}]},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"] == [
        {"field": "platforms", "message": "Площадка в списке дважды: p1"}
    ]
    # Отбитый запрос не записал ничего: набор остался прежним
    assert client.get(f"/admin/courses/{course.id}").json()["platforms"] == [
        {"platform": "p1", "price": 45000}
    ]

    # Код вне списка площадок отбивается там же, где остальные значения формы
    assert client.patch(
        f"/admin/courses/{course.id}",
        json={"platforms": [{"platform": "px", "price": 45000}]},
    ).status_code == 422


def test_clearing_a_checkbox_never_touches_somebody_who_studies(
    client, client2, sms, platform_origins
):
    """Решение 12: снятая галочка убирает курс из каталога, но не отбирает
    доступ. Никакого 409 здесь нет — иначе снятие стало бы необратимым
    действием над чужой учёбой, и вернуть его было бы нечем."""
    course = make_course(title="Критериальное оценивание")
    lesson = make_lesson(make_module(course.id).id)
    login(client2, sms, TEACHER_PHONE)
    make_enrollment(user_id(client2), course.id)

    login_admin(client, sms)
    resp = client.patch(f"/admin/courses/{course.id}", json={"platforms": []})
    assert resp.status_code == 200, resp.text
    assert resp.json()["platforms"] == []
    # Курс, по которому учатся, снимается так же, как любой другой
    assert resp.json()["has_students"] is True

    assert catalog_ids(client2, P1) == []
    assert client2.get(f"/lessons/{lesson.id}", headers=P1).status_code == 200
    page = client2.get(f"/courses/{course.id}", headers=P1)
    assert page.status_code == 200, page.text
    assert page.json()["access"]["state"] == "granted"


# -- обложка курса -----------------------------------------------------


def test_cover_is_uploaded_and_served_without_login(client, client2, sms, storage):
    """Обложка загружается файлом, а наружу уходит адресом: его рисует
    и каталог, и превью в редакторе, и открывается он без входа."""
    course = make_course(status="open")
    login_admin(client, sms)

    body = client.patch(
        f"/admin/courses/{course.id}", json={"cover": upload_cover(client)}
    ).json()
    assert body["cover"] == f"{BASE}/courses/{course.id}/cover"
    assert checklist(client, course.id)["cover"]["text"] == "Обложка загружена"

    # Каталог показывает тот же адрес — форма поля от загрузки не изменилась
    catalog = client2.get("/courses").json()
    assert catalog["items"][0]["versions"][0]["cover"] == f"{BASE}/courses/{course.id}/cover"

    resp = client2.get(f"/courses/{course.id}/cover")
    assert resp.status_code == 200
    assert resp.content == COVER_PNG
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-length"] == str(len(COVER_PNG))
    assert resp.headers["cache-control"] == "public, max-age=300"
    # Внутри svg бывает <script>, а домен API делит куку сессии с обоими фронтами
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'"
    )
    # Картинка стоит в <img>, а не скачивается
    assert "content-disposition" not in resp.headers


def test_cover_keeps_the_name_of_the_uploaded_file(client, sms, storage):
    """Имя лежит рядом с ключом: ключи загрузки случайные, и тип картинки
    в раздаче считается по имени, а не по ключу."""
    course = make_course()
    login_admin(client, sms)

    client.patch(
        f"/admin/courses/{course.id}",
        json={"cover": upload_cover(client, name="Обложка курса.jpg", content=COVER_PNG)},
    )

    assert client.get(f"/courses/{course.id}/cover").headers["content-type"] == "image/jpeg"


def test_null_removes_the_cover(client, client2, sms, storage):
    course = make_course()
    login_admin(client, sms)
    client.patch(f"/admin/courses/{course.id}", json={"cover": upload_cover(client)})

    body = client.patch(f"/admin/courses/{course.id}", json={"cover": None}).json()

    assert body["cover"] is None
    assert checklist(client, course.id)["cover"]["ok"] is False
    assert client2.get(f"/courses/{course.id}/cover").status_code == 404


def test_null_removes_an_old_link_cover_too(client, sms):
    """Снятие обложки одинаково работает и у загруженной картинки, и у адреса,
    вписанного руками до загрузки файлом."""
    course = make_course(cover=OLD_LINK)
    login_admin(client, sms)

    body = client.patch(f"/admin/courses/{course.id}", json={"cover": None}).json()

    assert body["cover"] is None


def test_a_new_cover_replaces_an_old_link(client, sms, storage):
    """Загруженная картинка гасит прежний адрес-ссылку: иначе она ждала бы
    своей очереди за ним, а на экране ничего бы не изменилось."""
    course = make_course(cover=OLD_LINK)
    login_admin(client, sms)

    body = client.patch(
        f"/admin/courses/{course.id}", json={"cover": upload_cover(client)}
    ).json()

    assert body["cover"] == f"{BASE}/courses/{course.id}/cover"
    assert client.get(f"/courses/{course.id}/cover").status_code == 200


def test_an_old_link_cover_is_still_read(client, client2, sms):
    """Переходное поведение: загруженной картинки нет — отдаём то, что лежало
    в `cover` раньше, как есть. Задать этот адрес через API уже нельзя."""
    course = make_course(status="open", cover=OLD_LINK)
    login_admin(client, sms)

    assert client.get(f"/admin/courses/{course.id}").json()["cover"] == OLD_LINK
    assert client2.get("/courses").json()["items"][0]["versions"][0]["cover"] == OLD_LINK
    # Раздавать нечего: байтов у ссылки на чужой сайт нет
    assert client2.get(f"/courses/{course.id}/cover").status_code == 404


def test_a_link_is_no_longer_accepted_as_a_cover(client, sms):
    course = make_course()
    login_admin(client, sms)

    resp = client.patch(f"/admin/courses/{course.id}", json={"cover": OLD_LINK})

    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "cover"


def test_cover_refuses_a_file_that_is_not_an_image(client, sms, storage):
    """Формат опознаётся по байтам объекта: имя сочиняет клиент, и договор,
    названный «обложка.jpg», проходил бы насквозь."""
    course = make_course()
    login_admin(client, sms)

    resp = client.patch(
        f"/admin/courses/{course.id}",
        json={"cover": upload_cover(client, name="обложка.jpg", content=DOCX)},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0] == {
        "field": "cover",
        "message": "Нужна картинка: PNG, JPEG, GIF, WEBP или SVG",
    }
    assert client.get(f"/admin/courses/{course.id}").json()["cover"] is None


def test_cover_refuses_a_key_without_an_object(client, sms, storage):
    """Иначе админ увидит пустой блок вместо только что загруженного файла."""
    course = make_course()
    login_admin(client, sms)

    resp = client.patch(
        f"/admin/courses/{course.id}",
        json={"cover": {"key": "uploads/2026/08/20/00000000.png", "name": "cover.png"}},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "Загруженный файл не найден — загрузите его заново"


def test_cover_404_without_a_picture_and_without_a_course(client, sms, storage):
    """Курса нет, обложки нет, объект пропал — для открывшего адрес это один
    и тот же 404, а не 422 и не перебор номеров курсов."""
    course = make_course()
    login_admin(client, sms)
    cover = upload_cover(client)
    client.patch(f"/admin/courses/{course.id}", json={"cover": cover})

    assert client.get("/courses/999999/cover").status_code == 404
    assert client.get(f"/courses/{make_course().id}/cover").status_code == 404

    storage.objects.pop(cover["key"])
    resp = client.get(f"/courses/{course.id}/cover")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    # У курса обложка при этом остаётся: пропал объект, а не привязка
    assert client.get(f"/admin/courses/{course.id}").json()["cover"] is not None


def test_cover_of_a_draft_opens_without_login(client, client2, sms, storage):
    """Статус курса раздачу не запирает: ту же картинку показывает превью
    в редакторе, а редактируют как раз черновик."""
    course = make_course(status="draft")
    login_admin(client, sms)
    client.patch(f"/admin/courses/{course.id}", json={"cover": upload_cover(client)})

    assert client2.get(f"/courses/{course.id}/cover").status_code == 200


# -- удаление курса ----------------------------------------------------


def test_delete_removes_the_whole_program(client, sms):
    course, module = make_rich_course(status="draft")
    quiz = make_quiz(module.id, title="Второй тест")
    question = make_question(quiz.id)
    make_option(question.id)
    survivor = make_course()
    survivor_module = make_module(survivor.id)
    make_lesson(survivor_module.id)

    login_admin(client, sms)
    assert client.delete(f"/admin/courses/{course.id}").status_code == 204

    assert rows("course", f"id = {course.id}") == 0
    assert rows("module", f"course_id = {course.id}") == 0
    assert rows("lesson", f"module_id = {module.id}") == 0
    assert rows("task", f"module_id = {module.id}") == 0
    assert rows("quiz", f"module_id = {module.id}") == 0
    assert rows("question", f"quiz_id = {quiz.id}") == 0
    assert rows("option", f"question_id = {question.id}") == 0
    # Соседний курс не задет: удаление обрубается по этому курсу
    assert rows("lesson", f"module_id = {survivor_module.id}") == 1


def test_delete_takes_questions_under_lessons_with_it(client, sms):
    """Вопросы учителей под уроками курса уходят вместе с ним: каскада
    у thread_message нет, и без явного удаления внешний ключ отбивает
    удаление курса целиком."""
    course, module = make_rich_course(status="draft")
    lesson = make_lesson(module.id, title="Ещё урок", order_index=9)
    teacher = make_user("+77010000011")
    root = make_thread_message(lesson.id, course.id, teacher.id)
    make_thread_message(lesson.id, course.id, teacher.id, parent_id=root.id, text="Ответ")

    login_admin(client, sms)
    assert client.delete(f"/admin/courses/{course.id}").status_code == 204
    assert rows("course", f"id = {course.id}") == 0
    assert rows("thread_message", f"course_id = {course.id}") == 0


def test_delete_stops_at_anything_anybody_touched(client, client2, sms):
    course = make_course()
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id, revoked_at="2026-08-01T00:00:00Z")
    make_lead(teacher, course.id, status="declined")
    make_certificate(teacher, course.id)

    login_admin(client2, sms)
    resp = client2.delete(f"/admin/courses/{course.id}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "course_in_use"
    # Считаются и отозванный доступ, и закрытая заявка
    assert resp.json()["error"]["details"] == {
        "enrollments": 1,
        "leads": 1,
        "certificates": 1,
    }
    assert rows("course", f"id = {course.id}") == 1


# -- вторая языковая версия --------------------------------------------


def test_version_copies_the_program_into_a_new_draft(client, sms):
    course, module = make_rich_course(status="open", price=45000, starts_at="2026-09-01")
    quiz = make_quiz(module.id, title="Итоговый", is_final=True, order_index=5)
    question = make_question(quiz.id, text="Что описывает дескриптор?")
    make_option(question.id, text="Наблюдаемое действие", is_correct=True)

    login_admin(client, sms)
    resp = client.post(f"/admin/courses/{course.id}/versions",
                       json={"lang": "kz", "copy_program": True})
    assert resp.status_code == 200
    body = resp.json()
    # Та же группа, свой язык; цена и дата старта — свои и пустые
    assert body["group_id"] == course.group_id
    assert body["lang"] == "kz"
    assert body["status"] == "draft"
    # Ни одной галочки публикации: цену и площадку казахской версии ставят
    # отдельно, молча продублировать их нельзя
    assert body["platforms"] == []
    assert body["starts_at"] is None
    assert body["versions"] == [{"id": course.id, "lang": "ru", "status": "open"}]

    titles = [item["title"] for item in body["program"][0]["items"]]
    assert titles == ["Что не так с пятибалльной шкалой", "Критерии и дескрипторы",
                      "Составьте дескрипторы", "Тест модуля 1", "Итоговый"]
    copied_quiz = body["program"][0]["items"][-1]
    assert copied_quiz["is_final"] is True
    assert copied_quiz["questions_count"] == 1
    # Копия своя: варианты уехали вместе с вопросом, но строки новые
    assert rows("option", f"question_id = {question.id}") == 1
    assert rows("option", f"question_id != {question.id}") == 1
    # Чужого не копируется ничего — программа копии пустая по данным
    assert all(item["has_data"] is False for item in body["program"][0]["items"])


def test_version_without_copy_starts_empty(client, sms):
    course, _ = make_rich_course()
    login_admin(client, sms)
    body = client.post(f"/admin/courses/{course.id}/versions", json={"lang": "kz"}).json()
    assert body["program"] == []
    assert body["program_minutes"] == 0


def test_version_conflicts(client, sms):
    course = make_course(lang="ru")
    kz = make_course(group_id=course.group_id, lang="kz", title="Бағалау")
    login_admin(client, sms)

    resp = client.post(f"/admin/courses/{course.id}/versions", json={"lang": "kz"})
    assert resp.status_code == 409
    assert resp.json()["error"] == {
        "code": "version_exists",
        "message": "Қазақша нұсқа у этого курса уже есть",
        "details": {"course_id": kz.id},
    }
    # Язык самого курса — не вторая версия, а та же самая
    resp = client.post(f"/admin/courses/{course.id}/versions", json={"lang": "ru"})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "lang"


def test_two_versions_at_once_leave_one_and_a_409(client, client2, sms, monkeypatch):
    """Проверка «версия на этом языке уже есть» и вставка не атомарны:
    данные спасает уникальный индекс (group_id, lang), а наружу обязано уйти
    то же 409, что и у проверки, — с id версии победителя."""
    course = make_course(lang="ru")
    login_admin(client, sms)
    login_admin(client2, sms, phone="+7 (700) 000-00-98")
    meet_at(monkeypatch, CourseAdminRepo, "by_id")

    body = {"lang": "kz", "copy_program": False}
    first, second = in_parallel(
        lambda: client.post(f"/admin/courses/{course.id}/versions", json=body),
        lambda: client2.post(f"/admin/courses/{course.id}/versions", json=body),
    )
    winner, loser = sorted([first, second], key=lambda resp: resp.status_code)
    assert winner.status_code == 200
    assert loser.status_code == 409
    assert loser.json()["error"] == {
        "code": "version_exists",
        "message": "Қазақша нұсқа у этого курса уже есть",
        "details": {"course_id": winner.json()["id"]},
    }
    assert rows("course", f"group_id = {course.group_id} AND lang = 'kz'") == 1


# -- дубликат ----------------------------------------------------------


def test_duplicate_makes_a_new_group(client, client2, sms):
    course, module = make_rich_course(status="open", price=45000, starts_at="2026-09-01")
    make_lesson(module.id, title="Скрытый", order_index=6, is_hidden=True)
    login(client, sms, TEACHER_PHONE)
    make_enrollment(user_id(client), course.id)

    login_admin(client2, sms)
    body = client2.post(f"/admin/courses/{course.id}/duplicate").json()

    assert body["group_id"] != course.group_id
    assert body["lang"] == course.lang
    assert body["title"] == "Критериальное оценивание в начальной школе (копия)"
    # Обложка переезжает в копию — и загруженная, и доставшаяся ссылкой
    assert body["cover"] == OLD_LINK
    assert body["status"] == "draft"
    assert body["platforms"] == []
    assert body["starts_at"] is None
    assert body["versions"] == []
    # Доступы не копируются: учатся на исходном курсе
    assert body["has_students"] is False
    assert len(body["program"][0]["items"]) == 5
    assert body["program"][0]["items"][-1]["is_hidden"] is True


# -- общие куски для соседних редакторов -------------------------------


def test_course_of_item_finds_the_course_of_lesson_quiz_and_task():
    """Метод общий: редакторы урока, теста и задания находят по элементу курс,
    чтобы двинуть его updated_at."""
    course, module = make_rich_course()
    lesson = make_lesson(module.id, title="Ещё урок")
    quiz = make_quiz(module.id, title="Ещё тест")
    task = make_task(module.id, title="Ещё задание")

    with OrmSession(get_engine()) as db:
        repo = CourseAdminRepo(db)
        assert repo.course_of_item("lesson", lesson.id).id == course.id
        assert repo.course_of_item("quiz", quiz.id).id == course.id
        assert repo.course_of_item("task", task.id).id == course.id
        assert repo.course_of_item("lesson", 999999) is None


def test_submission_and_attempt_make_has_data(client, client2, sms):
    """Чужие данные закрывают удаление: экран показывает «Скрыть» по has_data,
    не дожидаясь 409."""
    course, module = make_rich_course()
    lesson = make_lesson(module.id, title="Пройденный", order_index=6)
    task = make_task(module.id, title="Сданное", order_index=7,
                     statement={"html": "<p>Условие</p>"})
    login(client, sms, TEACHER_PHONE)
    teacher = user_id(client)
    make_enrollment(teacher, course.id)
    make_progress(teacher, lesson.id)
    make_submission(teacher, task.id)

    login_admin(client2, sms)
    items = {
        (item["kind"], item["id"]): item
        for item in client2.get(f"/admin/courses/{course.id}").json()["program"][0]["items"]
    }
    assert items[("video", lesson.id)]["has_data"] is True
    assert items[("task", task.id)]["has_data"] is True
    assert items[("task", task.id)]["is_ready"] is True


def test_delete_course_that_somebody_previews(client, sms):
    """Режим предпросмотра держит курс ссылкой из строки сессии: не сняв её,
    удаление упёрлось бы во внешний ключ."""
    course = make_course(status="draft")
    login_admin(client, sms)
    assert client.post("/admin/preview/enter", json={"course_id": course.id}).status_code == 204

    assert client.delete(f"/admin/courses/{course.id}").status_code == 204
    assert rows("course", f"id = {course.id}") == 0
    # Режим погас вместе с курсом
    assert client.get("/me").json()["preview"] is None


def test_course_list_does_not_scale_with_courses(client, sms):
    """Пять счётчиков на строку — ровно то место, где список курсов
    превращается в запрос на курс. В шестой сессии на этом уже поймали
    отчёт: 616 запросов на страницу. Число запросов обязано зависеть
    от страницы, а не от того, сколько курсов на ней уместилось."""
    from sqlalchemy import event

    from app.adapters.db.base import get_engine

    def queries_for(courses: int, first_number: int) -> int:
        for number in range(first_number, first_number + courses):
            course = make_course(title=f"Курс {number}")
            module = make_module(course.id)
            make_lesson(module.id)
            make_lead(make_user(f"+7700000{number:04d}").id, course.id)
            make_enrollment(make_user(f"+7701000{number:04d}").id, course.id)

        counted = []
        engine = get_engine()

        def count(*args):
            counted.append(1)

        event.listen(engine, "before_cursor_execute", count)
        try:
            resp = client.get("/admin/courses?per_page=100")
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert resp.status_code == 200
        return len(counted)

    login_admin(client, sms)
    few = queries_for(2, first_number=0)
    many = queries_for(10, first_number=100)
    assert many == few, f"{few} запросов на 2 курса, {many} на 10"
