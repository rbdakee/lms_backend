from tests.conftest import BASE_CATEGORIES, login, login_admin, make_course


def titles(client) -> list[str]:
    return [item["title"] for item in client.get("/admin/categories").json()["items"]]


# -- права ---------------------------------------------------------------


def test_categories_require_admin(client, sms):
    def statuses() -> list[int]:
        return [
            client.get("/admin/categories").status_code,
            client.post("/admin/categories", json={"title": "Новая"}).status_code,
            client.patch("/admin/categories/1", json={"title": "Новая"}).status_code,
            client.delete("/admin/categories/1").status_code,
        ]

    assert statuses() == [401] * 4

    login(client, sms)
    assert statuses() == [403] * 4
    assert client.get("/admin/categories").json()["error"]["code"] == "forbidden"


def test_404_for_unknown_category(client, sms):
    login_admin(client, sms)
    resp = client.patch("/admin/categories/999999", json={"title": "Новая"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert client.delete("/admin/categories/999999").status_code == 404


# -- список ---------------------------------------------------------------


def test_list_counts_courses_with_drafts(client, sms):
    """`courses_count` считает все версии, включая черновики и скрытые:
    по нему решают, можно ли категорию удалять, а курс держит её в любом
    статусе."""
    login_admin(client, sms)
    make_course(category_id=1)
    make_course(category_id=1, status="draft")
    make_course(category_id=1, lang="kz", status="hidden")
    make_course(category_id=2)

    body = client.get("/admin/categories").json()
    assert len(body["items"]) == len(BASE_CATEGORIES)
    # Порядок — по order_index: он же порядок фильтра в каталоге
    assert body["items"][0] == {
        "id": 1,
        "title": "Цифровые навыки",
        "order_index": 1,
        "courses_count": 3,
    }
    assert body["items"][1]["courses_count"] == 1
    assert body["items"][2]["courses_count"] == 0


# -- создание и переименование --------------------------------------------


def test_create_appends_to_the_end(client, sms):
    login_admin(client, sms)

    resp = client.post("/admin/categories", json={"title": "Функциональная грамотность"})
    assert resp.status_code == 200
    assert resp.json() == {
        "id": len(BASE_CATEGORIES) + 1,
        "title": "Функциональная грамотность",
        "order_index": len(BASE_CATEGORIES) + 1,
        # Курсов на новой категории нет по определению
        "courses_count": 0,
    }
    assert titles(client)[-1] == "Функциональная грамотность"


def test_created_category_is_accepted_by_the_course_editor(client, sms):
    """Справочник переехал в таблицу целиком: редактор курса сверяет
    category_id с ней, а не с константой, иначе новая категория была бы
    видна в фильтре и не принималась в курсе."""
    login_admin(client, sms)
    created = client.post("/admin/categories", json={"title": "Функциональная грамотность"})

    resp = client.post(
        "/admin/courses",
        json={
            "title": "Читательская грамотность",
            "lang": "ru",
            "category_id": created.json()["id"],
            "hours": 36,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["category_id"] == created.json()["id"]


def test_create_rejects_a_taken_title(client, sms):
    login_admin(client, sms)

    resp = client.post("/admin/categories", json={"title": "Оценивание"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "category_exists"
    assert resp.json()["error"]["details"] == {"category_id": 3}


def test_blank_title_is_rejected(client, sms):
    login_admin(client, sms)

    for resp in (
        client.post("/admin/categories", json={"title": "   "}),
        client.patch("/admin/categories/1", json={"title": ""}),
    ):
        assert resp.status_code == 422
        assert resp.json()["error"]["details"]["fields"][0]["field"] == "title"


def test_rename_changes_the_dictionary(client, client2, sms):
    login_admin(client, sms)

    resp = client.patch("/admin/categories/1", json={"title": "Цифровые компетенции"})
    assert resp.status_code == 200
    assert resp.json() == {
        "id": 1,
        "title": "Цифровые компетенции",
        "order_index": 1,
        "courses_count": 0,
    }
    # Публичный справочник читает ту же таблицу
    assert client2.get("/dictionaries").json()["categories"][0] == {
        "id": 1,
        "title": "Цифровые компетенции",
    }


def test_rename_to_own_title_is_not_a_conflict(client, sms):
    """Сохранение без правки названия — обычное дело на экране настроек,
    и конфликтом сама с собой категория быть не может."""
    login_admin(client, sms)
    resp = client.patch("/admin/categories/1", json={"title": "Цифровые навыки"})
    assert resp.status_code == 200


def test_rename_to_a_taken_title_409(client, sms):
    login_admin(client, sms)
    resp = client.patch("/admin/categories/1", json={"title": "Оценивание"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "category_exists"
    assert resp.json()["error"]["details"] == {"category_id": 3}


# -- удаление --------------------------------------------------------------


def test_delete_frees_the_dictionary(client, client2, sms):
    login_admin(client, sms)

    assert client.delete("/admin/categories/6").status_code == 204
    assert 6 not in [item["id"] for item in client.get("/admin/categories").json()["items"]]
    assert 6 not in [item["id"] for item in client2.get("/dictionaries").json()["categories"]]


def test_delete_of_a_used_category_409(client, sms):
    """category_id у курса обязателен: осиротевший курс исчез бы из каталога.
    Черновик держит категорию так же, как опубликованный курс."""
    login_admin(client, sms)
    make_course(category_id=4)
    make_course(category_id=4, status="draft")

    resp = client.delete("/admin/categories/4")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "category_in_use"
    assert resp.json()["error"]["details"] == {"courses_count": 2}
    assert "2 курса" in resp.json()["error"]["message"]

    assert 4 in [item["id"] for item in client.get("/admin/categories").json()["items"]]
