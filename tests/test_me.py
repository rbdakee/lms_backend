import pytest

from app.domain.iin import IIN_PLACEHOLDER
from tests.conftest import NORM, fake_iin, login, login_admin, login_named, user_id

# Второй учитель этого файла. Номер вымышленный, как и всё в тестовых данных.
OTHER_PHONE = "+7 (707) 765-43-21"


def test_me_requires_auth(client):
    resp = client.get("/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_patch_profile(client, sms):
    login(client, sms)
    iin = fake_iin()
    resp = client.patch(
        "/me",
        json={
            "last_name": "Нурланова",
            "first_name": "Айгуль",
            "middle_name": "Сериковна",
            # ИИН спрашивается в онбординге вместе с ФИО: без него
            # сертификат не выдать (CERTIFICATES_BRIEF, 1)
            "iin": iin,
            "school": "КГУ «Средняя школа №27»",
            "position": "Учитель математики",
            "region": "Алматы",
            "city": "Алматы",
            "subject": "Математика",
            "experience": 12,
            "lang": "kz",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["onboarding_done"] is True
    assert body["iin"] == iin
    assert body["experience"] == 12
    assert body["lang"] == "kz"

    me = client.get("/me").json()
    assert me["last_name"] == "Нурланова"


def test_patch_partial_and_clear(client, sms):
    login(client, sms)
    client.patch("/me", json={"first_name": "Айгуль", "last_name": "Нурланова"})
    resp = client.patch("/me", json={"experience": None, "first_name": "  Дана  "})
    body = resp.json()
    assert body["experience"] is None
    assert body["first_name"] == "Дана"
    assert body["last_name"] == "Нурланова"


def test_experience_out_of_range_is_reported_in_russian(client, sms):
    """Стаж учитель вводит руками, и ошибка под полем обязана быть на языке
    остальных ошибок формы."""
    login(client, sms)

    resp = client.patch("/me", json={"experience": 100})

    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0] == {
        "field": "experience",
        "message": "Стаж — от 0 до 70 лет",
    }


def test_patch_rejects_unknown_field(client, sms):
    login(client, sms)
    resp = client.patch("/me", json={"is_admin": True})
    assert resp.status_code == 422


def test_patch_rejects_bad_lang(client, sms):
    login(client, sms)
    resp = client.patch("/me", json={"lang": "en"})
    assert resp.status_code == 422


# -- ИИН ------------------------------------------------------------------


def test_iin_starts_as_the_placeholder_and_comes_back_saved(client, sms):
    """Свой номер человек видит: он его и вводил. До заполнения на его месте
    стоит заглушка, а не пустая строка, — это метка «не заполнен», и по ней
    экран узнаёт, что человека надо вернуть в онбординг."""
    login(client, sms)
    assert client.get("/me").json()["iin"] == IIN_PLACEHOLDER

    iin = fake_iin()
    assert client.patch("/me", json={"iin": iin}).json()["iin"] == iin
    assert client.get("/me").json()["iin"] == iin


def test_onboarding_is_not_done_until_the_iin_is_filled(client, sms):
    """Вошёл учитель с нулевым ИИН — экран онбординга его не выпускает:
    спрашивать номер в момент выдачи сертификата поздно, админ упрётся
    в пустое поле тогда, когда учитель уже всё сдал и ждёт документ
    (CERTIFICATES_BRIEF, 1)."""
    login(client, sms)

    body = client.patch("/me", json={"last_name": "Нурланова", "first_name": "Айгуль"})
    assert body.json()["onboarding_done"] is False

    body = client.patch("/me", json={"iin": fake_iin()})
    assert body.json()["onboarding_done"] is True


def test_admin_passes_onboarding_without_an_iin(client, sms):
    """Админу ИИН не нужен: он не учится и сертификатов не получает,
    а требовать с него номер — держать чужие персональные данные без причины.
    Заслон онбординга на нём стоять не должен: админку он им и откроет."""
    login_admin(client, sms)

    body = client.patch(
        "/me", json={"last_name": "Нурланова", "first_name": "Айгуль"}
    ).json()

    assert body["iin"] == IIN_PLACEHOLDER
    assert body["onboarding_done"] is True


@pytest.mark.parametrize(
    "value",
    [
        "90991234567",  # одиннадцать цифр
        "9099123456789",  # тринадцать
        "9099 1234 5678",  # пробелы внутри
        "9099X2345678",  # буква
        "٩٠٩٩١٢٣٤٥٦٧٨",  # арабские цифры: str.isdigit() их пропускает
        IIN_PLACEHOLDER,  # заглушку себе не вписать: такого ИИН не бывает
    ],
)
def test_iin_that_is_not_twelve_digits_is_refused(client, sms, value):
    """ИИН уходит в реестр академии, и туда ложится ровно то, что лежит
    у нас: 12 цифр, ничего больше. Отказ идёт полем, чтобы экран подсветил
    именно его, а присланного номера в ответе нет — ИИН персональные данные
    и в текст ошибки не попадает.
    """
    login(client, sms)

    resp = client.patch("/me", json={"iin": value})

    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["fields"][0]["field"] == "iin"
    assert value not in resp.text
    # Заглушка на месте: отбитый номер её не сдвинул
    assert client.get("/me").json()["iin"] == IIN_PLACEHOLDER


def test_the_check_digit_is_not_verified(client, sms):
    """Контрольную цифру не считаем (решение владельца 04.09.2026): формула
    поймала бы опечатку, но ошибка в нашей реализации заперла бы живого
    человека снаружи собственного аккаунта, и цена ошибки несимметрична.
    Значит принимаются любые 12 цифр."""
    login(client, sms)

    resp = client.patch("/me", json={"iin": "999999999999"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["iin"] == "999999999999"


def test_iin_is_saved_without_the_spaces_around_it(client, sms):
    """Номер вставляют в форму копированием, и пробел по краю приезжает
    вместе с ним: отбивать из-за него человека незачем, а вот в базу он
    попасть не должен — иначе номер перестанет совпадать сам с собой."""
    login(client, sms)
    iin = fake_iin()

    assert client.patch("/me", json={"iin": f"  {iin}  "}).json()["iin"] == iin


def test_iin_cannot_be_erased(client, sms):
    """Снять ИИН нельзя ни null, ни пустой строкой: без него админ упрётся
    в пустое поле в момент выдачи. null здесь значит «не трогать» — так же
    ведёт себя язык интерфейса, — а пустая строка это те же «не 12 цифр».
    """
    login(client, sms)
    iin = fake_iin()
    client.patch("/me", json={"iin": iin})

    # null не стирает и не мешает соседним полям сохраниться
    body = client.patch("/me", json={"iin": None, "city": "Астана"}).json()
    assert body["iin"] == iin
    assert body["city"] == "Астана"

    assert client.patch("/me", json={"iin": ""}).status_code == 422
    assert client.get("/me").json()["iin"] == iin


def test_someone_elses_iin_does_not_name_the_account_that_holds_it(client, client2, sms):
    """Один человек — один аккаунт, и второй номер телефона второго аккаунта
    не даёт (CERTIFICATES_BRIEF, 1).

    Отказ не называет, в чьём именно аккаунте номер стоит (решение владельца
    04.09.2026), и этим он отличается от phone_taken, который user_id отдаёт:
    там номер меняет админ и чинит доступ конкретному человеку, а сюда
    упирается кто угодно — назвать аккаунт значит отдать его тому, кто
    перебирает чужие ИИН.
    """
    iin = fake_iin()
    login_named(client, sms, iin=iin, last_name="Абдрахманова", first_name="Сауле")
    owner = user_id(client)

    login(client2, sms, phone=OTHER_PHONE)
    resp = client2.patch("/me", json={"iin": iin})

    assert resp.status_code == 409
    assert resp.json() == {
        "error": {"code": "iin_taken", "message": "Этот ИИН уже указан в другом аккаунте"}
    }
    # Ни поля с владельцем, ни его телефона, ФИО или самого номера —
    # ни в message, ни в details, которых здесь нет вовсе
    for trace in ("user_id", str(owner), NORM, "Абдрахманова", "Сауле", iin):
        assert trace not in resp.text
    # И чужой номер к себе не приехал
    assert client2.get("/me").json()["iin"] == IIN_PLACEHOLDER


def test_my_own_iin_is_not_taken(client, sms):
    """Профиль сохраняют целиком и не по одному разу: свой же номер,
    приехавший обратно нетронутым, отказом быть не должен."""
    login(client, sms)
    profile = {"last_name": "Нурланова", "first_name": "Айгуль", "iin": fake_iin()}
    assert client.patch("/me", json=profile).status_code == 200

    resp = client.patch("/me", json=profile)

    assert resp.status_code == 200, resp.text
    assert resp.json()["iin"] == profile["iin"]


def test_the_placeholder_is_shared_by_everyone_who_has_not_filled_it(client, client2, sms):
    """Уникальность на заглушку не распространяется: она стоит у всех, кто
    зарегистрировался до 04.09.2026, и второй такой человек обязан спокойно
    входить и сохранять профиль."""
    login(client, sms)
    login(client2, sms, phone=OTHER_PHONE)

    assert client.get("/me").json()["iin"] == IIN_PLACEHOLDER
    assert client2.get("/me").json()["iin"] == IIN_PLACEHOLDER
    assert client2.patch("/me", json={"first_name": "Дана"}).status_code == 200


def test_sessions_list_marks_current(client, client2, sms):
    login(client, sms)
    login(client2, sms)

    items = client.get("/me/sessions").json()["items"]
    assert len(items) == 2
    assert sum(1 for s in items if s["is_current"]) == 1
    for s in items:
        assert {"id", "created_at", "last_seen_at", "user_agent", "is_current"} <= set(s)


def test_revoke_other_session(client, client2, sms):
    login(client, sms)
    login(client2, sms)

    other = next(s for s in client.get("/me/sessions").json()["items"] if not s["is_current"])
    resp = client.delete(f"/me/sessions/{other['id']}")
    assert resp.status_code == 204

    assert client2.get("/me").status_code == 401
    resp = client.delete(f"/me/sessions/{other['id']}")
    assert resp.status_code == 404


def test_logout_others(client, client2, sms):
    login(client, sms)
    login(client2, sms)

    resp = client.post("/auth/logout_others")
    assert resp.status_code == 200
    assert resp.json() == {"revoked_count": 1}
    assert client2.get("/me").status_code == 401
    assert client.get("/me").status_code == 200


def test_logout(client, sms):
    login(client, sms)
    resp = client.post("/auth/logout")
    assert resp.status_code == 204
    assert client.get("/me").status_code == 401


def test_logout_without_session_is_ok(client):
    assert client.post("/auth/logout").status_code == 204
