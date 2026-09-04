"""Сквозной путь: админ собрал курс — учитель прошёл его — админ выдал документ.

Поштучные проверки эндпоинтов живут в своих файлах; здесь проверяется то,
чего не видно поштучно, — стыки между сессиями. Id, который вернул редактор
админа, должен приниматься экраном учителя; поле, записанное на входе, должно
приходить под тем же именем на выходе; состояние после шага обязано быть тем,
которого ждёт следующий. Поэтому содержимое курса создаётся не seed-хелперами,
а настоящими админскими ручками: seed кладёт строку в базу мимо всего, что
стоит на этом стыке, и разрыв на нём остался бы незамеченным.

Тест один и длинный сознательно. Разбить его на несколько функций нельзя без
потерь: `_clean_db` чистит базу перед каждой, и предыдущие шаги пришлось бы
прогонять заново общим хелпером — то есть выполнять один и тот же путь по
многу раз, теряя ровно то, ради чего он написан: непрерывность состояния.
"""

from fastapi.testclient import TestClient

from app.config import get_settings
from app.domain.platform import DEFAULT_PLATFORM
from app.main import app
from tests.conftest import ADMIN_PHONE, PHONE, fake_iin, login, login_admin, user_id

COURSE_TITLE = "Критериальное оценивание в начальной школе"

# Обложка кладётся настоящей загрузкой: у поля `cover` формы входа и выхода
# разные — объект внутрь, адрес раздачи наружу, — и стык между ними виден
# только так.
COVER_PNG = b"\x89PNG\r\n\x1a\n" + b"cover" * 32

# Ссылка в одном из обычных написаний: редактор обязан привести её к
# каноническому виду, и плеер учителя ждёт уже приведённую.
VIDEO_URL = "https://youtu.be/QmDzKcMSbG4"
VIDEO_CANONICAL = "https://www.youtube.com/watch?v=QmDzKcMSbG4"

LESSON_BODY = "<p>Дескриптор описывает, что именно ученик умеет.</p>"
TASK_STATEMENT = "<p>Возьмите ближайший урок и составьте к нему дескрипторы.</p>"

# Профиль учителя: ФИО печатается на сертификате снимком, школа и регион —
# то, по чему админ узнаёт человека в очереди заявок. ИИН нужен академии,
# чтобы внести человека в реестр, и без него заявку не примут. Данные
# вымышленные, номер — тоже.
TEACHER_PROFILE = {
    "last_name": "Смагулова",
    "first_name": "Гульмира",
    "middle_name": "Токтарбековна",
    "iin": fake_iin(),
    "school": "КГУ «Средняя школа №27»",
    "region": "Алматы",
    "city": "Алматы",
    "subject": "Начальные классы",
}
HOLDER_NAME = "Смагулова Гульмира Токтарбековна"

PRICE = 45000
HOURS = 72
# Номер академии: мы его не знаем и не вычисляем — админ вводит его руками
# в момент выдачи.
REGISTRATION_NUMBER = "АКД-2026/117"


def ok(resp):
    """Ответ с телом; текст в сообщении, иначе по коду не понять, что упало."""
    assert resp.status_code == 200, resp.text
    return resp.json()


def build_course(admin) -> dict:
    """Шаг 1: админ собирает курс редакторами сессии 7а и публикует его.

    Возвращает id всего, что понадобится учителю, и правильные варианты
    ответов — те самые, которые редактор только что записал: на сверке этих
    id с теми, что придёт в попытке, и держится проверка стыка.
    """
    course = ok(
        admin.post(
            "/admin/courses",
            json={"title": COURSE_TITLE, "lang": "ru", "category_id": 3, "hours": HOURS},
        )
    )
    # Только что созданный курс — черновик с пустой программой: каталог его
    # ещё не видит, и учителю на этом шаге показывать нечего
    assert course["status"] == "draft"
    assert course["program"] == []
    course_id = course["id"]

    cover = ok(
        admin.post(
            "/files", files={"file": ("assessment.png", COVER_PNG, "application/octet-stream")}
        )
    )
    course = ok(
        admin.patch(
            f"/admin/courses/{course_id}",
            json={
                "short": "Как перейти на критерии, не сломав журнал",
                "full": "Курс для учителей начальной школы.",
                "cover": {"key": cover["key"], "name": cover["name"]},
                "platforms": [{"platform": DEFAULT_PLATFORM, "price": PRICE}],
                "duration_text": "6 недель",
                "strict_order": True,
                "cert_require_lessons": True,
                "cert_require_tasks": True,
                "cert_require_module_quizzes": True,
            },
        )
    )
    assert course["strict_order"] is True
    # Галочка публикации и цена — одна строка `course_platform`: без неё
    # собранного курса не будет ни в одном каталоге
    assert course["platforms"] == [{"platform": DEFAULT_PLATFORM, "price": PRICE}]
    # Наружу обложка уходит адресом публичной раздачи, а не ключом хранилища
    assert course["cover"] == f"{get_settings().public_base_url}/courses/{course_id}/cover"

    module_one = ok(
        admin.post(
            f"/admin/courses/{course_id}/modules",
            json={"title": "Модуль 1. Зачем менять оценивание"},
        )
    )
    module_two = ok(
        admin.post(
            f"/admin/courses/{course_id}/modules",
            json={"title": "Модуль 2. Итоговая работа"},
        )
    )
    assert module_one["items"] == []

    # -- уроки --------------------------------------------------------------

    video = ok(
        admin.post(
            f"/admin/modules/{module_one['id']}/lessons",
            json={
                "title": "Что не так с пятибалльной шкалой",
                "kind": "video",
                "time_required_min": 20,
            },
        )
    )
    # Заготовка заводится скрытой и неготовой: показать её учителю нечем
    assert video["is_hidden"] is True
    assert video["is_ready"] is False
    assert video["course"]["id"] == course_id
    assert video["module"]["id"] == module_one["id"]

    video = ok(
        admin.patch(
            f"/admin/lessons/{video['id']}",
            json={"video_url": VIDEO_URL, "duration_label": "14:20", "is_hidden": False},
        )
    )
    assert video["is_ready"] is True
    assert video["is_hidden"] is False
    assert video["video_url"] == VIDEO_CANONICAL

    text = ok(
        admin.post(
            f"/admin/modules/{module_one['id']}/lessons",
            json={"title": "Критерии и дескрипторы", "kind": "text", "time_required_min": 25},
        )
    )
    text = ok(
        admin.patch(
            f"/admin/lessons/{text['id']}",
            json={"body": {"html": LESSON_BODY}, "is_hidden": False},
        )
    )
    # Разметку чистит сервер, и в базе лежит то, что он вернул: экран урока
    # получит ровно это
    assert text["body"] == {"html": LESSON_BODY}
    assert text["is_ready"] is True

    # -- тест ---------------------------------------------------------------

    quiz = ok(
        admin.post(
            f"/admin/modules/{module_one['id']}/quizzes",
            json={"title": "Тест модуля 1", "time_required_min": 15, "pass_score": 70},
        )
    )
    assert quiz["is_hidden"] is True
    assert quiz["questions"] == []
    assert quiz["max_score"] == 0
    # Единственная попытка — умолчание теста, а не настройка сцены
    assert quiz["retakable"] is False
    quiz_id = quiz["id"]

    single = ok(
        admin.post(
            f"/admin/quizzes/{quiz_id}/questions",
            json={
                "type": "single",
                "text": "Что описывает дескриптор?",
                "explanation": "Дескриптор — про наблюдаемое действие ученика.",
                "points": 2,
                "options": [
                    {"text": "Отношение учителя к работе", "is_correct": False},
                    {"text": "Наблюдаемое действие ученика", "is_correct": True},
                    {"text": "Средний балл за четверть", "is_correct": False},
                ],
            },
        )
    )
    boolean = ok(
        admin.post(
            f"/admin/quizzes/{quiz_id}/questions",
            json={
                "type": "bool",
                "text": "Критерии сообщают ученику до работы?",
                "points": 1,
                "options": [
                    {"text": "Да", "is_correct": True},
                    {"text": "Нет", "is_correct": False},
                ],
            },
        )
    )
    # Верные варианты запоминаем прямо из ответа редактора: их id должны
    # прийти учителю в попытке и приняться сохранением ответа
    correct = {
        question["id"]: next(o["id"] for o in question["options"] if o["is_correct"])
        for question in (single, boolean)
    }

    quiz = ok(
        admin.patch(f"/admin/quizzes/{quiz_id}", json={"time_limit_min": 15, "is_hidden": False})
    )
    assert quiz["time_limit_min"] == 15
    # Максимум собран из баллов вопросов, а не из числа вопросов
    assert quiz["max_score"] == 3

    # -- задание ------------------------------------------------------------

    task = ok(
        admin.post(
            f"/admin/modules/{module_two['id']}/tasks",
            json={"title": "Составьте дескрипторы к своему уроку", "time_required_min": 40},
        )
    )
    assert task["statement"] == {"html": ""}
    assert task["is_ready"] is False
    task = ok(
        admin.patch(
            f"/admin/tasks/{task['id']}",
            json={
                "statement": {"html": TASK_STATEMENT},
                "submit_format": "text",
                "is_hidden": False,
            },
        )
    )
    assert task["is_ready"] is True

    # -- публикация ---------------------------------------------------------

    card = ok(admin.get(f"/admin/courses/{course_id}"))
    # Порядок элементов задан порядком создания: перетаскивания не было,
    # и именно в этом порядке курс будет проходить учитель
    assert [
        (item["kind"], item["id"]) for module in card["program"] for item in module["items"]
    ] == [
        ("video", video["id"]),
        ("text", text["id"]),
        ("quiz", quiz_id),
        ("task", task["id"]),
    ]
    assert card["program_minutes"] == 20 + 25 + 15 + 40
    # Чек-лист «Публикация» и сама публикация должны сходиться: экран говорит
    # «можно», а PATCH обязан принять
    assert card["readiness"]["can_open"] is True, card["readiness"]["items"]

    card = ok(admin.patch(f"/admin/courses/{course_id}", json={"status": "open"}))
    assert card["status"] == "open"

    return {
        "course_id": course_id,
        "video_id": video["id"],
        "text_id": text["id"],
        "quiz_id": quiz_id,
        "task_id": task["id"],
        "correct": correct,
    }


def test_full_path_from_editor_to_certificate(client, client2, sms, telegram, storage):
    teacher, admin = client, client2

    # -- 1. Админ собирает и публикует курс ---------------------------------

    login_admin(admin, sms, phone=ADMIN_PHONE)
    built = build_course(admin)
    course_id = built["course_id"]

    # -- 2. Учитель входит, заполняет профиль и находит курс в каталоге -----

    login(teacher, sms, phone=PHONE)
    me = ok(teacher.patch("/me", json=TEACHER_PROFILE))
    assert me["onboarding_done"] is True
    teacher_id = me["id"]
    assert teacher_id != user_id(admin)

    catalog = ok(teacher.get("/courses"))
    version = next(
        version
        for group in catalog["items"]
        for version in group["versions"]
        if version["id"] == course_id
    )
    # Каталог считает уроки, а не элементы программы: тест и задание сюда
    # не попадают
    assert version["lessons_count"] == 2
    assert version["price"] == PRICE
    assert version["title"] == COURSE_TITLE

    page = ok(teacher.get(f"/courses/{course_id}"))
    assert page["access"] == {"state": "none"}
    assert page["strict_order"] is True
    assert [
        (item["kind"], item["id"]) for module in page["program"] for item in module["items"]
    ] == [
        ("video", built["video_id"]),
        ("text", built["text_id"]),
        ("quiz", built["quiz_id"]),
        ("task", built["task_id"]),
    ]

    # -- 3. Заявка на курс --------------------------------------------------

    lead = ok(teacher.post(f"/courses/{course_id}/lead"))
    assert lead["status"] == "new"
    assert lead["waiting_days"] == 0
    # Заявка обязана быть в базе до того, как уйдёт в бот
    assert len(telegram.sent) == 1

    page = ok(teacher.get(f"/courses/{course_id}"))
    assert page["access"] == {"state": "requested", "waiting_days": 0}
    mine = ok(teacher.get("/me/courses"))
    assert mine["items"] == []
    assert [row["id"] for row in mine["leads"]] == [lead["id"]]
    assert mine["leads"][0]["course"]["id"] == course_id

    # -- 4. Админ видит заявку и выдаёт доступ ------------------------------

    queue = ok(admin.get("/admin/leads", params={"status": "open"}))
    assert queue["total"] == 1
    row = queue["items"][0]
    assert row["id"] == lead["id"]
    assert row["teacher"]["id"] == teacher_id
    assert row["teacher"]["school"] == TEACHER_PROFILE["school"]
    assert row["course"]["id"] == course_id
    # Цена снята в момент подачи: курс подорожает — заявка помнит прежнюю
    assert row["price_snapshot"] == PRICE

    enrollment = ok(
        admin.post(
            "/admin/enrollments",
            json={
                "user_id": teacher_id,
                "course_id": course_id,
                "platform": "p1",
                "paid": True,
                "note": "Оплата 45 000 ₸, Kaspi, 20.08",
            },
        )
    )
    assert enrollment["user_id"] == teacher_id
    assert enrollment["course_id"] == course_id
    assert enrollment["paid"] is True
    assert enrollment["note"] == "Оплата 45 000 ₸, Kaspi, 20.08"

    # Выдача закрывает заявку — иначе она вечно висела бы в очереди
    closed = ok(admin.get("/admin/leads", params={"course_id": course_id}))["items"][0]
    assert closed["id"] == lead["id"]
    assert closed["status"] == "granted"
    assert closed["waiting_days"] == 0

    page = ok(teacher.get(f"/courses/{course_id}"))
    access = page["access"]
    assert access["state"] == "granted"
    assert (access["done_count"], access["total_count"]) == (0, 4)
    assert access["progress_percent"] == 0
    # Кнопка «Продолжить» ведёт на первый элемент программы
    assert access["next_lesson"] == {
        "id": built["video_id"],
        "title": "Что не так с пятибалльной шкалой",
        "kind": "video",
    }

    # -- 5. Уроки по порядку ------------------------------------------------

    def statuses():
        program = ok(teacher.get(f"/courses/{course_id}/program"))["program"]
        return {
            (item["kind"], item["id"]): item["status"]
            for module in program
            for item in module["items"]
        }

    now = statuses()
    assert now[("video", built["video_id"])] == "available"
    # Строгий порядок закрывает всё после первого непройденного
    assert now[("text", built["text_id"])] == "locked"
    assert now[("quiz", built["quiz_id"])] == "locked"
    assert now[("task", built["task_id"])] == "locked"

    # Урок закрыт только на вид: содержимое сервер отдаёт и заглянувшему
    # вперёд. Так решено владельцем в сессии 8 — уроку и заданию забег
    # вперёд ничего не стоит. У теста иначе: попытка единственная и
    # не возвращается, поэтому её старт закрыт настоящим отказом
    # (app/application/quizzes.py, `_check_unlocked`), и проверено это
    # в tests/test_quizzes.py.
    assert teacher.get(f"/lessons/{built['text_id']}").status_code == 200

    lesson = ok(teacher.get(f"/lessons/{built['video_id']}"))
    assert lesson["is_completed"] is False
    assert lesson["duration_label"] == "14:20"
    # Ссылку на видео учителю не отдают никогда — только подписанный playback
    assert "video_url" not in lesson
    playback = ok(teacher.get(f"/lessons/{built['video_id']}/playback"))
    assert playback["url"] == VIDEO_CANONICAL
    assert playback["provider"] == "youtube"

    done = ok(teacher.post(f"/lessons/{built['video_id']}/complete"))
    assert done["is_completed"] is True
    assert (done["done_count"], done["total_count"]) == (1, 4)
    assert done["next_lesson"]["id"] == built["text_id"]

    now = statuses()
    assert now[("video", built["video_id"])] == "done"
    assert now[("text", built["text_id"])] == "available"
    # Замок сдвинулся на один элемент, а не открылся весь курс
    assert now[("quiz", built["quiz_id"])] == "locked"

    lesson = ok(teacher.get(f"/lessons/{built['text_id']}"))
    assert lesson["body"] == {"html": LESSON_BODY}
    done = ok(teacher.post(f"/lessons/{built['text_id']}/complete"))
    assert (done["done_count"], done["total_count"]) == (2, 4)
    assert done["next_lesson"] == {
        "id": built["quiz_id"],
        "title": "Тест модуля 1",
        "kind": "quiz",
    }
    assert statuses()[("quiz", built["quiz_id"])] == "available"

    # -- 6. Тест: серверный таймер, единственная попытка, разбор ------------

    quiz = ok(teacher.get(f"/quizzes/{built['quiz_id']}"))
    assert quiz["questions_count"] == 2
    assert quiz["max_score"] == 3
    assert quiz["state"] == {"status": "not_started", "can_start": True}
    assert quiz["attempts"] == []

    attempt = ok(teacher.post(f"/quizzes/{built['quiz_id']}/quiz_attempts"))
    # Таймер считает сервер: часы клиента к делу не относятся
    assert 0 < attempt["remaining_sec"] <= 15 * 60
    assert attempt["answers"] == []
    assert [question["id"] for question in attempt["questions"]] == list(built["correct"])
    # Верные ответы до разбора наружу не уходят
    assert all(
        set(option) == {"id", "text"}
        for question in attempt["questions"]
        for option in question["options"]
    )

    for question in attempt["questions"]:
        # Вариант выбирается по id, который пришёл в попытке; редактор
        # админа записал верным именно его
        chosen = built["correct"][question["id"]]
        assert chosen in [option["id"] for option in question["options"]]
        resp = teacher.post(
            f"/quiz_attempts/{attempt['id']}/answers",
            json={"question_id": question["id"], "option_ids": [chosen]},
        )
        assert resp.status_code == 204, resp.text

    result = ok(teacher.post(f"/quiz_attempts/{attempt['id']}/finish"))
    assert result["id"] == attempt["id"]
    assert (result["score"], result["max_score"]) == (3, 3)
    assert result["score_percent"] == 100
    assert result["passed"] is True
    assert result["is_counted"] is True
    assert result["timed_out"] is False

    # Попытка одна: второй старт по тому же тесту уже не пускают
    resp = teacher.post(f"/quizzes/{built['quiz_id']}/quiz_attempts")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "attempt_used"

    review = ok(teacher.get(f"/quiz_attempts/{attempt['id']}/review"))
    assert review["result"]["id"] == attempt["id"]
    assert [question["id"] for question in review["questions"]] == list(built["correct"])
    # В разборе впервые видны верные ответы — и это те же id, что записал админ
    assert {
        question["id"]: next(
            option["id"] for option in question["options"] if option["is_correct"]
        )
        for question in review["questions"]
    } == built["correct"]
    assert all(
        option["is_chosen"] == option["is_correct"]
        for question in review["questions"]
        for option in question["options"]
    )
    assert statuses()[("quiz", built["quiz_id"])] == "done"
    assert statuses()[("task", built["task_id"])] == "available"

    # -- 7. Задание: сдача и проверка ---------------------------------------

    task = ok(teacher.get(f"/tasks/{built['task_id']}"))
    assert task["statement"] == {"html": TASK_STATEMENT}
    assert task["submit_format"] == "text"
    assert task["status"] == "none"
    assert task["can_submit"] is True
    assert task["submissions"] == []

    submission = ok(
        teacher.post(
            f"/tasks/{built['task_id']}/submissions",
            json={"text": "Дескрипторы к уроку про доли: находит долю, читает запись."},
        )
    )
    assert submission["status"] == "pending"
    assert len(telegram.sent) == 2

    # Сертификат до зачёта работы не просят — условие «Сдать все задания»
    # ещё не закрыто
    completion = ok(teacher.get(f"/courses/{course_id}/completion"))
    assert completion["can_request"] is False
    assert completion["certificate"] is None
    tasks_row = next(row for row in completion["conditions"] if row["code"] == "tasks")
    assert (tasks_row["done_count"], tasks_row["total_count"]) == (0, 1)
    resp = teacher.post(f"/courses/{course_id}/certificate")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conditions_not_met"

    pending = ok(admin.get("/admin/submissions"))
    assert pending["total"] == 1
    card = pending["items"][0]
    assert card["id"] == submission["id"]
    assert card["attempt_number"] == 1
    assert card["teacher"]["id"] == teacher_id
    assert card["task"]["id"] == built["task_id"]
    assert card["course"]["id"] == course_id

    reviewed = ok(
        admin.post(
            f"/admin/submissions/{submission['id']}/review",
            json={"verdict": "accepted", "comment": "Дескрипторы наблюдаемые, зачтено."},
        )
    )
    assert reviewed["id"] == submission["id"]
    assert reviewed["status"] == "accepted"
    assert reviewed["reviewed_at"] is not None

    task = ok(teacher.get(f"/tasks/{built['task_id']}"))
    assert task["status"] == "accepted"
    # Зачтённое задание пересдавать нечем
    assert task["can_submit"] is False
    assert task["submissions"][0]["comment"] == "Дескрипторы наблюдаемые, зачтено."

    # -- 8. Заявка на сертификат --------------------------------------------

    completion = ok(teacher.get(f"/courses/{course_id}/completion"))
    assert [row["code"] for row in completion["conditions"]] == [
        "lessons",
        "tasks",
        "module_quizzes",
    ]
    assert all(row["status"] == "done" for row in completion["conditions"])
    assert completion["can_request"] is True
    assert completion["blocker"] is None
    assert completion["certificate"] is None

    requested = ok(teacher.post(f"/courses/{course_id}/certificate"))
    assert requested["status"] == "requested"
    # Ни номера, ни даты выдачи: их поставит админ, когда выпишет документ
    assert requested["number"] is None
    assert requested["issued_at"] is None
    # Третье сообщение в бот за весь путь: заявка на курс, работа на проверку
    # и теперь заявка на сертификат
    assert len(telegram.sent) == 3
    assert telegram.sent[-1].title == "Заявка на сертификат"

    # Кнопка гаснет уже на заявке, а в кабинете по-прежнему пусто: там сетка
    # выданных документов, и печатать по заявке нечего
    completion = ok(teacher.get(f"/courses/{course_id}/completion"))
    assert completion["can_request"] is False
    assert completion["certificate"] == requested
    assert ok(teacher.get("/me/certificates"))["items"] == []
    assert teacher.get(f"/certificates/{requested['id']}/pdf").status_code == 404

    # -- 9. Админ выписывает документ ---------------------------------------

    queue = ok(admin.get("/admin/certificates", params={"status": "requested"}))
    assert queue["total"] == 1
    row = queue["items"][0]
    assert row["id"] == requested["id"]
    assert row["platform"] == DEFAULT_PLATFORM
    assert row["course_id"] == course_id
    # На бумаге — снимок: ФИО из профиля, название и часы курса
    assert row["holder_name"] == HOLDER_NAME
    assert row["course_title"] == COURSE_TITLE
    assert row["hours"] == HOURS
    assert row["teacher"]["id"] == teacher_id
    # ИИН админ сверяет с профилем перед выдачей — за этим он здесь и есть
    assert row["teacher"]["iin"] == TEACHER_PROFILE["iin"]

    card = ok(
        admin.post(
            f"/admin/certificates/{requested['id']}/issue",
            json={"registration_number": REGISTRATION_NUMBER},
        )
    )
    # Второго документа с таким номером академии нет — предупреждать не о чем
    assert card["warning"] is None
    certificate = card["certificate"]
    assert certificate["status"] == "issued"
    assert certificate["registration_number"] == REGISTRATION_NUMBER
    assert certificate["lang"] == "ru"
    assert certificate["revoked_at"] is None
    # Строка та же, что была заявкой: дата запроса не поехала
    assert certificate["requested_at"] == requested["requested_at"]
    number = certificate["number"]

    # Учитель видит документ там же, где оставлял заявку
    assert ok(teacher.get(f"/courses/{course_id}/completion"))["certificate"] == {
        "id": requested["id"],
        "status": "issued",
        "number": number,
        "requested_at": requested["requested_at"],
        "issued_at": certificate["issued_at"],
    }
    my_certificates = ok(teacher.get("/me/certificates"))["items"]
    assert [row["id"] for row in my_certificates] == [requested["id"]]
    assert my_certificates[0]["registration_number"] == REGISTRATION_NUMBER
    assert my_certificates[0]["holder_name"] == HOLDER_NAME
    assert my_certificates[0]["course_title"] == COURSE_TITLE
    assert my_certificates[0]["hours"] == HOURS

    mine = ok(teacher.get("/me/courses"))
    assert mine["leads"] == []
    row = next(row for row in mine["items"] if row["id"] == course_id)
    assert row["progress_percent"] == 100
    assert row["next_lesson"] is None
    # «Курс пройден» и «сертификат выдан» — одно событие, и ставит его теперь
    # выдача: до неё вкладка «Пройденные» этот курс не показывала
    assert row["completed_at"] is not None

    # Колокольчик собрал весь путь; params несут те же id, по которым фронт
    # строит адрес перехода
    bell = ok(teacher.get("/notifications"))
    assert [item["type"] for item in bell["items"]] == [
        "certificate_issued",
        "submission_reviewed",
        "access_granted",
    ]
    assert bell["unread_count"] == 3
    assert bell["items"][0]["params"]["certificate_id"] == certificate["id"]
    assert bell["items"][0]["params"]["course_id"] == course_id
    assert bell["items"][1]["params"]["task_id"] == built["task_id"]

    # -- 10. Публичная проверка по номеру -----------------------------------

    with TestClient(app) as anonymous:
        # Номер перебивают с бумаги: регистр и дефисы не в счёт
        resp = anonymous.get(f"/verify/{number.replace('-', '').lower()}")
    verified = ok(resp)
    assert verified == {
        "status": "valid",
        "number": number,
        # Комиссии полезнее номер академии, чем наш, — он тут и есть
        "registration_number": REGISTRATION_NUMBER,
        "holder_name": HOLDER_NAME,
        "course_title": COURSE_TITLE,
        "hours": HOURS,
        "issued_at": certificate["issued_at"],
        "revoked_at": None,
    }
    # А ИИН на публичную страницу не выходит: её открывает посторонний
    assert TEACHER_PROFILE["iin"] not in resp.text

    # -- 11. PDF ------------------------------------------------------------

    resp = teacher.get(f"/certificates/{certificate['id']}/pdf")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"] == f'attachment; filename="{number}.pdf"'
    assert resp.content.startswith(b"%PDF")
    assert resp.content.rstrip().endswith(b"%%EOF")
