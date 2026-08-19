from app.domain.errors import CourseInUseError, HasProgressError, HasSubmissionsError


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_openapi_opens(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert "/auth/request_code" in resp.json()["paths"]


def test_unknown_path_error_format(client):
    resp = client.get("/no_such_path")
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"]


def test_validation_error_format(client):
    resp = client.post("/auth/request_code", json={})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    fields = {f["field"] for f in error["details"]["fields"]}
    assert {"phone", "consent"} <= fields


# -- склонение в текстах ошибок ----------------------------------------
# Эти строки админ читает целиком: на фронте они не собираются, и «1 доступов»
# он видит ровно так, как их собрал сервер.


def test_error_texts_decline_with_the_number():
    assert [HasProgressError(n).message for n in (1, 2, 5, 21)] == [
        "Урок прошёл 1 человек — его можно скрыть, но не удалить",
        "Урок прошли 2 человека — его можно скрыть, но не удалить",
        "Урок прошли 5 человек — его можно скрыть, но не удалить",
        "Урок прошёл 21 человек — его можно скрыть, но не удалить",
    ]
    assert [HasSubmissionsError(n).message for n in (1, 2, 5, 21)] == [
        "На задание сдали 1 работу — удалить его нельзя",
        "На задание сдали 2 работы — удалить его нельзя",
        "На задание сдали 5 работ — удалить его нельзя",
        "На задание сдали 21 работу — удалить его нельзя",
    ]
    assert [
        CourseInUseError(enrollments=n, leads=n, certificates=n).message
        for n in (0, 1, 2, 5, 21)
    ] == [
        "Курс нельзя удалить: 0 доступов, 0 заявок, 0 сертификатов",
        "Курс нельзя удалить: 1 доступ, 1 заявка, 1 сертификат",
        "Курс нельзя удалить: 2 доступа, 2 заявки, 2 сертификата",
        "Курс нельзя удалить: 5 доступов, 5 заявок, 5 сертификатов",
        "Курс нельзя удалить: 21 доступ, 21 заявка, 21 сертификат",
    ]
    # 11–14 склоняются не по последней цифре: «11 доступов», а не «11 доступ»
    assert CourseInUseError(enrollments=11, leads=12, certificates=14).message == (
        "Курс нельзя удалить: 11 доступов, 12 заявок, 14 сертификатов"
    )
