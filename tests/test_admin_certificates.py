"""Страница «Сертификаты» в админке: очередь заявок, карточка, выдача,
правка и отзыв.

Документ здесь выписывает человек, а не сервер (CERTIFICATES_BRIEF, 3),
и потому главное, что проверяется, — порядок проверок перед записью: отозвать
выданный документ «уже неловко» (backend/CLAUDE.md), и всё, что должно
остановить выдачу, обязано остановить её до появления номера.

Через страницу ходят персональные данные — ФИО и ИИН. Все номера в тестах
вымышленные и приходят из `fake_iin()`: месяц рождения в них «99», такого
ИИН не бывает.
"""

from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.adapters.db.repos import now_utc
from app.domain.certificate import canonical_number
from app.domain.iin import IIN_PLACEHOLDER
from tests.conftest import (
    login,
    login_admin,
    login_named,
    make_certificate,
    make_certificate_request,
    make_course,
    make_enrollment,
    make_user,
    user_id,
)


def teacher(number: int, **kw):
    """Учитель с вымышленным номером телефона: настоящих в тестах не бывает."""
    return make_user(f"+7703000{number:04d}", **kw)


def waiting(course, number: int = 1, **user_kw):
    """Учитель с доступом к курсу и поданной заявкой — сцена перед выдачей."""
    person = teacher(number, **user_kw)
    make_enrollment(person.id, course.id)
    certificate = make_certificate_request(
        person.id, course.id, course_title=course.title, hours=course.hours
    )
    return person, certificate


def card(client, certificate_id: int) -> dict:
    return client.get(f"/admin/certificates/{certificate_id}").json()["certificate"]


def ids(client, query: str = "") -> list[int]:
    return [item["id"] for item in client.get(f"/admin/certificates{query}").json()["items"]]


def fields_of(resp) -> list[dict]:
    """Поля, которые экран подсветит: у отказа формы они лежат в details."""
    return resp.json()["error"]["details"]["fields"]


# -- права ---------------------------------------------------------------


def test_all_five_handles_are_admin_only(client, sms):
    """Права проверяются на сервере, всегда: на странице чужие ФИО и ИИН,
    и одной ненакрытой ручки хватает, чтобы отдать их учителю."""
    course = make_course()
    certificate = make_certificate(teacher(1).id, course.id)

    def statuses() -> list[int]:
        return [
            client.get("/admin/certificates").status_code,
            client.get(f"/admin/certificates/{certificate.id}").status_code,
            client.post(
                f"/admin/certificates/{certificate.id}/issue",
                json={"registration_number": "АК-2026/117"},
            ).status_code,
            client.patch(
                f"/admin/certificates/{certificate.id}", json={"hours": 36}
            ).status_code,
            client.post(f"/admin/certificates/{certificate.id}/revoke").status_code,
        ]

    assert statuses() == [401] * 5

    login(client, sms)
    assert statuses() == [403] * 5
    assert client.get("/admin/certificates").json()["error"]["code"] == "forbidden"


# -- список --------------------------------------------------------------


def test_empty_list_is_not_an_error(client, sms):
    login_admin(client, sms)

    assert client.get("/admin/certificates").json() == {
        "items": [],
        "total": 0,
        "page": 1,
        "per_page": 20,
    }


def test_row_carries_the_snapshot_and_its_teacher(client, sms):
    """Строка списка собрана из снимков документа, а учитель в ней живой:
    ФИО и ИИН админ сверяет с профилем перед выдачей."""
    login_admin(client, sms)
    course = make_course(title="Критериальное оценивание")
    person = teacher(1)
    certificate = make_certificate(
        person.id,
        course.id,
        course_title="Критериальное оценивание",
        hours=36,
        registration_number="АК-2026/117",
    )

    body = client.get("/admin/certificates").json()

    assert body["total"] == 1
    row = body["items"][0]
    assert row.pop("requested_at")
    assert row.pop("issued_at")
    assert row == {
        "id": certificate.id,
        "status": "issued",
        "platform": "p1",
        "number": "KZ-2026-XB7K2M",
        "registration_number": "АК-2026/117",
        "holder_name": "Смагулова Гульмира Токтарбековна",
        "course_id": course.id,
        "course_title": "Критериальное оценивание",
        "hours": 36,
        "lang": "ru",
        "revoked_at": None,
        "teacher": {
            "id": person.id,
            "last_name": "Смагулова",
            "first_name": "Гульмира",
            "middle_name": "Токтарбековна",
            "iin": person.iin,
        },
    }


def test_three_tabs_split_requests_issued_and_revoked(client, sms):
    """Вкладки «Ждут выдачи», «Выданные» и «Отозванные» — это одна и та же
    строка в трёх состояниях, отдельной таблицы заявок нет."""
    login_admin(client, sms)
    course = make_course()
    request = make_certificate_request(teacher(1).id, course.id)
    issued = make_certificate(teacher(2).id, course.id, number="KZ-2026-AAAAAA")
    revoked = make_certificate(
        teacher(3).id, course.id, number="KZ-2026-BBBBBB", revoked_at=now_utc()
    )

    # Свежие сверху — по дате запроса: у заявки даты выдачи нет вовсе
    assert ids(client) == [revoked.id, issued.id, request.id]
    assert ids(client, "?status=requested") == [request.id]
    assert ids(client, "?status=issued") == [issued.id]
    assert ids(client, "?status=revoked") == [revoked.id]
    # Пустой параметр — то же, что без него
    assert ids(client, "?status=") == [revoked.id, issued.id, request.id]

    # Неизвестная вкладка — внятный отказ по-русски, а не 500
    resp = client.get("/admin/certificates?status=draft")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_list_filters_by_platform(client, sms):
    """Админка одна на обе площадки, и документы у них свои
    (PLATFORMS_BRIEF): фильтр обязан их разводить."""
    login_admin(client, sms)
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    person = teacher(1)
    first = make_certificate(person.id, course.id, platform="p1")
    second = make_certificate(
        person.id, course.id, platform="p2", number="KZ-2026-AAAAAA"
    )

    assert ids(client, "?platform=p1") == [first.id]
    assert ids(client, "?platform=p2") == [second.id]
    assert sorted(ids(client)) == sorted([first.id, second.id])
    assert client.get("/admin/certificates?platform=p3").status_code == 422


def test_search_finds_by_name_iin_and_both_numbers(client, sms):
    """Поиск идёт по всему, что видно в строке (CERTIFICATES_BRIEF, 4),
    и ФИО ищется дважды: в живом профиле и в снимке на бумаге — человек мог
    сменить фамилию после выдачи."""
    login_admin(client, sms)
    course = make_course()
    person = teacher(
        1, last_name="Нурланова", first_name="Айгуль", middle_name="Сериковна"
    )
    mine = make_certificate(
        person.id,
        course.id,
        holder_name="Абдиева Айгуль Сериковна",
        number="KZ-2026-AAAAAA",
        registration_number="АК-2026/117",
    )
    make_certificate(
        teacher(2).id,
        course.id,
        number="KZ-2026-BBBBBB",
        registration_number="АК-2026/900",
    )

    def found(query: str) -> list[int]:
        return ids(client, f"?q={query}")

    assert found("Нурлан") == [mine.id]
    assert found("Абдиева") == [mine.id]
    assert found(person.iin) == [mine.id]
    assert found("KZ-2026-AAAAAA") == [mine.id]
    assert found("АК-2026/117") == [mine.id]
    assert found("Ахметов") == []
    # Телефона на этой странице нет, и искать по невидимому столбцу нечем
    assert found(person.phone.replace("+", "")) == []


# -- карточка ------------------------------------------------------------


def test_card_repeats_the_row_and_warns_about_nothing(client, sms):
    """Карточка и строка списка — одной формы: экран перерисовывается одним
    и тем же куском кода. Предупреждение отвечает на ввод админа, а не висит
    у документа постоянной меткой."""
    login_admin(client, sms)
    certificate = make_certificate(teacher(1).id, make_course().id)

    body = client.get(f"/admin/certificates/{certificate.id}").json()

    assert body["warning"] is None
    assert body["certificate"] == client.get("/admin/certificates").json()["items"][0]


def test_unknown_certificate_is_404_in_all_four(client, sms):
    login_admin(client, sms)

    assert client.get("/admin/certificates/999999").status_code == 404
    assert (
        client.post(
            "/admin/certificates/999999/issue", json={"registration_number": "АК-1"}
        ).status_code
        == 404
    )
    assert client.patch("/admin/certificates/999999", json={"hours": 36}).status_code == 404
    assert client.post("/admin/certificates/999999/revoke").status_code == 404
    assert client.get("/admin/certificates/999999").json()["error"]["code"] == "not_found"


# -- выдача --------------------------------------------------------------


def test_issue_writes_the_number_the_date_the_bell_and_the_completion(client, client2, sms):
    """Выдача — одно движение: наш номер, дата, номер академии, колокольчик
    учителю и отметка «курс пройден» у доступа."""
    login_named(client2, sms)
    teacher_id = user_id(client2)
    course = make_course(title="Критериальное оценивание")
    enrollment = make_enrollment(teacher_id, course.id)
    certificate = make_certificate_request(
        teacher_id, course.id, course_title=course.title
    )
    login_admin(client, sms)

    resp = client.post(
        f"/admin/certificates/{certificate.id}/issue",
        json={"registration_number": "  АК-2026/117  "},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["warning"] is None
    row = body["certificate"]
    assert row["status"] == "issued"
    # Номер выписал сервер — в том самом виде, в каком его найдёт публичная
    # проверка: год берётся у даты выдачи, поэтому в тесте он не зашит
    assert canonical_number(row["number"]) == row["number"]
    assert row["issued_at"] is not None
    # Пробелы по краям снимаются: в реестр академии уходит то, что в базе
    assert row["registration_number"] == "АК-2026/117"

    # Учителю — колокольчик со ссылкой на курс, без хранимого текста
    bell = client2.get("/notifications").json()["items"][0]
    assert bell["type"] == "certificate_issued"
    assert bell["params"] == {
        "course_id": course.id,
        "course_title": course.title,
        "certificate_id": certificate.id,
    }
    # Документ появился в кабинете — до выдачи заявки там не было
    assert [item["number"] for item in client2.get("/me/certificates").json()["items"]] == [
        row["number"]
    ]

    # «Курс пройден» и «сертификат выдан» — одно событие: вкладка «Пройденные»
    # на /my держится именно на нём
    with get_engine().connect() as conn:
        completed_at = conn.execute(
            text("SELECT completed_at FROM enrollment WHERE id = :id"),
            {"id": enrollment.id},
        ).scalar_one()
    assert completed_at is not None


def test_issue_without_a_registration_number_is_refused(client, sms):
    """Документ без номера академии — документ, которого нет в её реестре."""
    login_admin(client, sms)
    course = make_course()
    _, certificate = waiting(course)

    # Поля нет вовсе — отбивает схема
    assert (
        client.post(f"/admin/certificates/{certificate.id}/issue", json={}).status_code == 422
    )
    for value in ("", "   "):
        resp = client.post(
            f"/admin/certificates/{certificate.id}/issue",
            json={"registration_number": value},
        )
        assert resp.status_code == 422, value
        assert fields_of(resp) == [
            {"field": "registration_number", "message": "Регистрационный номер обязателен"}
        ]

    # Номер так и не выписан: отказ пришёл до первой записи
    assert card(client, certificate.id)["status"] == "requested"


def test_issue_to_a_teacher_without_an_iin_is_refused(client, sms):
    """Между заявкой и выдачей проходит время, и ИИН спрашивается второй раз:
    без него человека не внести в реестр академии."""
    login_admin(client, sms)
    course = make_course()
    _, certificate = waiting(course, iin=IIN_PLACEHOLDER)

    resp = client.post(
        f"/admin/certificates/{certificate.id}/issue",
        json={"registration_number": "АК-2026/117"},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "iin_required"
    # Ни номера, ни заглушки в тексте отказа: его читает экран и лог фронта
    assert IIN_PLACEHOLDER not in resp.text
    assert card(client, certificate.id)["number"] is None


def test_issuing_an_issued_document_is_a_conflict(client, sms):
    """Второй номер за той же строкой — второй документ по одному курсу."""
    login_admin(client, sms)
    certificate = make_certificate(teacher(1).id, make_course().id)

    resp = client.post(
        f"/admin/certificates/{certificate.id}/issue",
        json={"registration_number": "АК-2026/117"},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "certificate_issued"
    # Номер и номер академии остались прежними
    row = card(client, certificate.id)
    assert row["number"] == "KZ-2026-XB7K2M"
    assert row["registration_number"] == ""


def test_issuing_a_revoked_document_is_a_conflict(client, sms):
    """Отозванная строка остаётся отозванной: вторая бумага по ней была бы
    документом с чужой историей. Нужен новый — учитель просит его заново."""
    login_admin(client, sms)
    certificate = make_certificate(
        teacher(1).id, make_course().id, revoked_at=now_utc()
    )

    resp = client.post(
        f"/admin/certificates/{certificate.id}/issue",
        json={"registration_number": "АК-2026/117"},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"
    assert card(client, certificate.id)["status"] == "revoked"


def test_issue_with_a_closed_access_still_gives_the_document(client, sms):
    """Доступ закрыли после заявки: отмечать «курс пройден» нечего, а бумага
    всё равно выписывается — условия проверены в момент заявки."""
    login_admin(client, sms)
    course = make_course()
    person = teacher(1)
    make_enrollment(person.id, course.id, revoked_at=now_utc())
    certificate = make_certificate_request(person.id, course.id)

    resp = client.post(
        f"/admin/certificates/{certificate.id}/issue",
        json={"registration_number": "АК-2026/117"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["certificate"]["status"] == "issued"
    with get_engine().connect() as conn:
        completed_at = conn.execute(text("SELECT completed_at FROM enrollment")).scalar_one()
    assert completed_at is None


# -- повтор номера академии ----------------------------------------------


def test_a_repeated_registration_number_warns_and_saves_anyway(client, sms):
    """Повтор — предупреждение, а не запрет: правил чужой нумерации мы
    не знаем, а запрет остановил бы админа посреди работы
    (CERTIFICATES_BRIEF, 2)."""
    login_admin(client, sms)
    course = make_course()
    other = make_certificate(
        teacher(1).id,
        course.id,
        number="KZ-2026-AAAAAA",
        registration_number="АК-2026/117",
    )
    _, certificate = waiting(course, 2)

    body = client.post(
        f"/admin/certificates/{certificate.id}/issue",
        json={"registration_number": "АК-2026/117"},
    ).json()

    assert body["warning"] == {
        "code": "registration_number_taken",
        "message": "Такой номер уже есть у сертификата №KZ-2026-AAAAAA",
        "certificate_id": other.id,
        "number": "KZ-2026-AAAAAA",
    }
    # Сохранение при этом произошло: предупреждение уходит вместе с карточкой
    assert body["certificate"]["registration_number"] == "АК-2026/117"
    assert body["certificate"]["status"] == "issued"


def test_the_warning_comes_from_editing_too(client, sms):
    """Тот же номер можно вписать и правкой — предупреждение обязано прийти
    и оттуда, иначе оно зависит от того, каким путём админ шёл."""
    login_admin(client, sms)
    course = make_course()
    other = make_certificate(
        teacher(1).id,
        course.id,
        number="KZ-2026-AAAAAA",
        registration_number="АК-2026/117",
    )
    certificate = make_certificate(teacher(2).id, course.id, number="KZ-2026-BBBBBB")

    body = client.patch(
        f"/admin/certificates/{certificate.id}",
        json={"registration_number": "АК-2026/117"},
    ).json()

    assert body["warning"]["certificate_id"] == other.id
    assert body["certificate"]["registration_number"] == "АК-2026/117"
    # Стёр ошибочный номер — предупреждать снова не о чем
    cleared = client.patch(
        f"/admin/certificates/{certificate.id}", json={"registration_number": ""}
    ).json()
    assert cleared["warning"] is None


def test_an_empty_registration_number_is_not_a_repeat(client, sms):
    """Пустой номер стоит у всех документов до 04.09.2026, и предупреждать
    о нём нечего: иначе первая же правка старого документа кричала бы
    о совпадении со всеми остальными."""
    login_admin(client, sms)
    course = make_course()
    make_certificate(teacher(1).id, course.id, number="KZ-2026-AAAAAA")
    certificate = make_certificate(teacher(2).id, course.id, number="KZ-2026-BBBBBB")

    body = client.patch(
        f"/admin/certificates/{certificate.id}", json={"holder_name": "Абдиева Айгуль"}
    ).json()

    assert body["warning"] is None


# -- правка --------------------------------------------------------------


def test_patch_edits_the_snapshots_and_the_hand_written_number(client, sms):
    """Правятся снимки на бумаге и то, что админ вписал руками: опечатку
    правят в документе, а не в профиле человека (CERTIFICATES_BRIEF, 4)."""
    login_admin(client, sms)
    certificate = make_certificate(teacher(1).id, make_course().id)

    row = client.patch(
        f"/admin/certificates/{certificate.id}",
        json={
            "registration_number": "  АК-2026/117 ",
            "holder_name": "  Нурланова Айгуль Сериковна  ",
            "course_title": "  Критериальное оценивание ",
            "hours": 36,
            "lang": "kz",
            "issued_at": "2026-09-01T06:00:00Z",
        },
    ).json()["certificate"]

    assert row["registration_number"] == "АК-2026/117"
    assert row["holder_name"] == "Нурланова Айгуль Сериковна"
    assert row["course_title"] == "Критериальное оценивание"
    assert row["hours"] == 36
    assert row["lang"] == "kz"
    assert row["issued_at"].startswith("2026-09-01T06:00:00")
    # Наш номер правка не трогает: на нём держится проверка по QR
    assert row["number"] == "KZ-2026-XB7K2M"


def test_patch_refuses_what_would_make_it_another_document(client, sms):
    """`number`, `user_id`, `course_id` и `platform` не правятся: смена
    любого означает другой документ, а не правку этого. Отбивает их схема —
    `extra: forbid`, — то есть до сценария они не доходят вовсе."""
    login_admin(client, sms)
    course = make_course()
    another = make_course(title="Информационная безопасность")
    certificate = make_certificate(teacher(1).id, course.id)
    before = card(client, certificate.id)

    for field, value in (
        ("number", "KZ-2026-ZZZZZZ"),
        ("user_id", teacher(2).id),
        ("course_id", another.id),
        ("platform", "p2"),
    ):
        resp = client.patch(f"/admin/certificates/{certificate.id}", json={field: value})
        assert resp.status_code == 422, field
        assert fields_of(resp)[0]["field"] == field

    assert card(client, certificate.id) == before


def test_patch_cannot_erase_the_date_or_set_it_on_a_request(client, sms):
    """Дату выдачи ставит выдача, а снимает отзыв: стёртая дата отменила бы
    выдачу в обход отзыва, а у заявки её нет вовсе."""
    login_admin(client, sms)
    course = make_course()
    issued = make_certificate(teacher(1).id, course.id)
    _, request = waiting(course, 2)

    resp = client.patch(f"/admin/certificates/{issued.id}", json={"issued_at": None})
    assert resp.status_code == 422
    assert fields_of(resp) == [
        {"field": "issued_at", "message": "Дату выдачи нельзя стереть"}
    ]
    assert card(client, issued.id)["issued_at"] is not None

    resp = client.patch(
        f"/admin/certificates/{request.id}", json={"issued_at": "2026-09-01T06:00:00Z"}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "certificate_not_issued"
    assert card(client, request.id)["status"] == "requested"


def test_patch_refuses_an_empty_name_or_title(client, sms):
    """Снимок без имени или без названия печатать не на чем."""
    login_admin(client, sms)
    certificate = make_certificate(teacher(1).id, make_course().id)

    for field in ("holder_name", "course_title"):
        resp = client.patch(f"/admin/certificates/{certificate.id}", json={field: "   "})
        assert resp.status_code == 422, field
        assert fields_of(resp)[0]["field"] == field


def test_patch_touches_only_what_came(client, sms):
    """Неприсланное поле — «не трогать», а не «стереть»: экран шлёт форму
    целиком не всегда, и половина полей у документа обязательные."""
    login_admin(client, sms)
    certificate = make_certificate(
        teacher(1).id, make_course().id, registration_number="АК-2026/117"
    )
    before = card(client, certificate.id)

    row = client.patch(f"/admin/certificates/{certificate.id}", json={"hours": 36}).json()[
        "certificate"
    ]

    assert row == {**before, "hours": 36}


# -- отзыв ---------------------------------------------------------------


def test_revoke_takes_the_document_out_of_everything_but_the_registry(
    client, client2, sms
):
    """Отозванный уходит во вкладку «Отозванные», пропадает из кабинета
    и не печатается — а на публичной проверке остаётся, но недействительным:
    запись в реестре никуда не девается."""
    login_named(client2, sms)
    teacher_id = user_id(client2)
    certificate = make_certificate(teacher_id, make_course().id)
    login_admin(client, sms)

    body = client.post(f"/admin/certificates/{certificate.id}/revoke").json()

    row = body["certificate"]
    assert row["status"] == "revoked"
    assert row["revoked_at"] is not None
    # Номер и дата выдачи остаются на месте
    assert row["number"] == "KZ-2026-XB7K2M"
    assert row["issued_at"] is not None

    assert ids(client, "?status=revoked") == [certificate.id]
    assert ids(client, "?status=issued") == []
    assert client2.get("/me/certificates").json()["items"] == []
    assert client2.get(f"/certificates/{certificate.id}/pdf").status_code == 404

    verify = client.get("/verify/KZ-2026-XB7K2M").json()
    assert verify["status"] == "revoked"
    assert verify["revoked_at"] is not None


def test_second_revoke_keeps_the_first_time(client, sms):
    """Повторный отзыв ничего не меняет: время остаётся временем первого
    отзыва — так же устроено закрытие доступа к курсу."""
    login_admin(client, sms)
    certificate = make_certificate(teacher(1).id, make_course().id)

    first = client.post(f"/admin/certificates/{certificate.id}/revoke").json()["certificate"]
    second = client.post(f"/admin/certificates/{certificate.id}/revoke").json()["certificate"]

    assert second == first


def test_revoke_works_on_a_request_too(client, sms):
    """Снять ошибочную заявку больше нечем: отозванная строка освобождает
    `uq_certificate_active`, и человек может попросить сертификат снова."""
    login_admin(client, sms)
    course = make_course()
    _, certificate = waiting(course)

    row = client.post(f"/admin/certificates/{certificate.id}/revoke").json()["certificate"]

    assert row["status"] == "revoked"
    # Выдачи не было — номер так и не появился
    assert row["number"] is None
    assert row["issued_at"] is None
    assert ids(client, "?status=requested") == []
