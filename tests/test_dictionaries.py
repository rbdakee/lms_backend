from tests.conftest import login_admin


def test_dictionaries_are_public(client):
    resp = client.get("/dictionaries")
    assert resp.status_code == 200
    body = resp.json()

    # 17 областей и 3 города республиканского значения
    assert len(body["regions"]) == 20
    assert "Астана" in body["regions"]

    assert len(body["subjects"]) > 0
    assert all(isinstance(s, str) for s in body["subjects"])

    assert len(body["categories"]) == 6
    assert body["categories"][0] == {"id": 1, "title": "Цифровые навыки"}


def test_categories_come_from_the_table(client, client2, sms):
    """Форма ответа с переездом категорий в таблицу не изменилась —
    изменился источник: заведённая в настройках категория приходит сюда
    сразу, а удалённая исчезает."""
    login_admin(client2, sms)
    created = client2.post(
        "/admin/categories", json={"title": "Функциональная грамотность"}
    ).json()

    categories = client.get("/dictionaries").json()["categories"]
    assert categories[-1] == {"id": created["id"], "title": "Функциональная грамотность"}
    assert all(set(item) == {"id", "title"} for item in categories)

    client2.delete(f"/admin/categories/{created['id']}")
    assert len(client.get("/dictionaries").json()["categories"]) == 6
