import json

from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.config import get_settings
from tests.conftest import login, login_admin

PNG = b"\x89PNG\r\n\x1a\n" + b"fake" * 64
# Логотипом кладут и svg — так в контракте. Скрипт внутри тут не выдумка:
# такой файл открывают прямым адресом на домене API.
SVG = (
    b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    b"<script>alert(document.cookie)</script></svg>"
)
BASE = get_settings().public_base_url

# Контакты из примера контракта. Номера вымышленные; whatsapp — номер,
# а не ссылка: ссылку wa.me собирает фронт.
CONTACTS = {
    "name": "Аскарова Бақыт",
    "phone": "+77010000000",
    "whatsapp": "+77010000001",
    "hours": "Будни, 9:00–18:00",
}
EMPTY_CONTACTS = {"name": "", "phone": "", "whatsapp": "", "hours": ""}


def upload(client, name, content=PNG):
    """Картинка кладётся настоящим POST /files: настройки привязываются
    к тому самому ключу, который получил браузер."""
    resp = client.post("/files", files={"file": (name, content, "application/octet-stream")})
    assert resp.status_code == 200, resp.text
    return resp.json()["key"]


def image(client, name):
    return {"key": upload(client, name), "name": name}


def bind_telegram(**value):
    """Привязка кладётся прямо в строку настройки: здесь проверяется экран
    настроек, а не бот, и тащить сюда код с вебхуком незачем — своё у них
    есть в test_telegram. Идентификатор чата вымышленный."""
    stored = {
        "chat_id": "-1001234567890",
        "chat_title": "LMS — заявки",
        "connected_at": "2026-08-06T05:30:00Z",
    }
    stored.update(value)
    with get_engine().begin() as conn:
        conn.execute(
            text("INSERT INTO setting (key, value) VALUES ('telegram', :value)"),
            {"value": json.dumps(stored)},
        )


# -- права ---------------------------------------------------------------


def test_admin_settings_require_admin(client, sms):
    def statuses() -> list[int]:
        return [
            client.get("/admin/settings").status_code,
            client.patch("/admin/settings", json={"platform_name": "LMS"}).status_code,
        ]

    assert statuses() == [401, 401]

    login(client, sms)
    assert statuses() == [403, 403]
    assert client.get("/admin/settings").json()["error"]["code"] == "forbidden"


# -- GET /admin/settings --------------------------------------------------


def test_empty_settings_fill_the_screen(client, sms):
    """Ничего не настраивали: поля приходят пустыми, а не отсутствующими —
    экран рисует их всегда."""
    login_admin(client, sms)

    assert client.get("/admin/settings").json() == {
        "platform_name": "",
        "org_name": "",
        "logo": None,
        "contacts": EMPTY_CONTACTS,
        "certificate_images": {"logo": None, "sign": None, "stamp": None},
        "telegram": {
            "connected": False,
            "chat_title": None,
            "connected_at": None,
            # Флаги включены по умолчанию: бот привязывают затем, чтобы
            # получать заявки
            "notify_leads": True,
            "notify_submissions": True,
        },
    }


def test_filled_settings_come_back_whole(client, sms, storage):
    login_admin(client, sms)

    resp = client.patch(
        "/admin/settings",
        json={
            "platform_name": "LMS",
            "org_name": "Институт повышения квалификации",
            "logo": image(client, "logo.svg"),
            "contacts": CONTACTS,
            "certificate_images": {"logo": image(client, "gerb.png")},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "platform_name": "LMS",
        "org_name": "Институт повышения квалификации",
        "logo": {"url": f"{BASE}/branding/logo", "name": "logo.svg"},
        "contacts": CONTACTS,
        "certificate_images": {
            "logo": {"url": f"{BASE}/branding/cert_logo", "name": "gerb.png"},
            "sign": None,
            "stamp": None,
        },
        "telegram": {
            "connected": False,
            "chat_title": None,
            "connected_at": None,
            "notify_leads": True,
            "notify_submissions": True,
        },
    }
    # PATCH возвращает ровно то же, что следующий GET
    assert client.get("/admin/settings").json() == resp.json()


# -- PATCH /admin/settings ------------------------------------------------


def test_patch_changes_only_what_was_sent(client, sms, storage):
    """Вкладки экрана сохраняются по одной: второй PATCH не должен стирать
    то, что записал первый."""
    login_admin(client, sms)
    client.patch("/admin/settings", json={"platform_name": "LMS", "contacts": CONTACTS})

    body = client.patch("/admin/settings", json={"org_name": "ИПК"}).json()
    assert body["platform_name"] == "LMS"
    assert body["org_name"] == "ИПК"
    assert body["contacts"] == CONTACTS

    # И наоборот: правка контактов не трогает названия
    body = client.patch("/admin/settings", json={"contacts": {"phone": "+77010000001"}}).json()
    assert body["platform_name"] == "LMS"
    assert body["contacts"] == {**CONTACTS, "phone": "+77010000001"}


def test_certificate_images_are_set_and_cleared_one_by_one(client, sms, storage):
    """`certificate_images` с одним ключом гасит или ставит только его:
    «поля нет» и «прислали null» — разные случаи."""
    login_admin(client, sms)
    client.patch(
        "/admin/settings",
        json={
            "certificate_images": {
                "logo": image(client, "gerb.png"),
                "sign": image(client, "podpis.png"),
                "stamp": image(client, "pechat.png"),
            }
        },
    )

    body = client.patch("/admin/settings", json={"certificate_images": {"stamp": None}}).json()
    assert body["certificate_images"]["stamp"] is None
    assert body["certificate_images"]["logo"]["name"] == "gerb.png"
    assert body["certificate_images"]["sign"]["name"] == "podpis.png"

    # Логотип платформы и картинки сертификата — разные слоты
    client.patch("/admin/settings", json={"logo": image(client, "logo.png")})
    body = client.get("/admin/settings").json()
    assert body["logo"]["url"] == f"{BASE}/branding/logo"
    assert body["certificate_images"]["logo"]["url"] == f"{BASE}/branding/cert_logo"


def test_unknown_storage_key_404(client, sms, storage):
    login_admin(client, sms)

    resp = client.patch(
        "/admin/settings",
        json={"logo": {"key": "uploads/2026/08/19/00000000.png", "name": "logo.png"}},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert resp.json()["error"]["message"] == "Загруженный файл не найден — загрузите его заново"
    assert client.get("/admin/settings").json()["logo"] is None


def test_null_logo_clears_the_platform_logo(client, sms, storage):
    """`null` убирает картинку из слота — у логотипа платформы так же, как
    у картинок сертификата."""
    login_admin(client, sms)
    client.patch(
        "/admin/settings", json={"platform_name": "LMS", "logo": image(client, "logo.png")}
    )

    body = client.patch("/admin/settings", json={"logo": None}).json()
    assert body["logo"] is None
    # Убрали картинку — не значит стёрли вкладку
    assert body["platform_name"] == "LMS"
    assert client.get("/settings").json()["logo_url"] is None
    assert client.get("/branding/logo").status_code == 404


def test_not_an_image_is_rejected_with_the_screen_field_name(client, sms, storage):
    """В шаблон сертификата уходит картинка: подсунутый туда docx сломал бы
    генерацию PDF в момент выдачи документа, а не сейчас."""
    login_admin(client, sms)
    docx = {"key": upload(client, "Скан печати.docx", b"PK fake"), "name": "Скан печати.docx"}

    resp = client.patch("/admin/settings", json={"certificate_images": {"stamp": docx}})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"] == [
        {"field": "certificate_images.stamp", "message": "Нужна картинка: PNG или JPEG"}
    ]

    # Логотипу платформы имя поля своё
    resp = client.patch("/admin/settings", json={"logo": docx})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "logo"


def test_a_renamed_file_is_still_not_an_image(client, sms, storage):
    """Картинка проверяется по байтам объекта, а не по присланному имени:
    имя сочиняет клиент, и `договор.docx`, названный `печать.png`, проходил
    бы насквозь — а сломалась бы генерация PDF, потом и молча."""
    login_admin(client, sms)
    docx = upload(client, "Договор.docx", b"PK\x03\x04 fake docx")

    stamp = {"key": docx, "name": "печать.png"}
    resp = client.patch("/admin/settings", json={"certificate_images": {"stamp": stamp}})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"] == [
        {"field": "certificate_images.stamp", "message": "Нужна картинка: PNG или JPEG"}
    ]
    assert client.get("/admin/settings").json()["certificate_images"]["stamp"] is None

    # И обратная сторона: приватный pdf, поставленный логотипом, получил бы
    # публичный адрес раздачи
    pdf = upload(client, "Ведомость.pdf", b"%PDF-1.4\n1 0 obj")
    resp = client.patch("/admin/settings", json={"logo": {"key": pdf, "name": "logo.png"}})
    assert resp.status_code == 422
    assert client.get("/branding/logo").status_code == 404


def test_an_image_is_recognised_by_its_bytes_not_its_name(client, sms, storage):
    """Настоящая картинка проходит под любым именем: проверяем сигнатуру,
    а не расширение. Логотипом кладут и svg — так в контракте."""
    login_admin(client, sms)
    jpeg = upload(client, "gerb.bin", b"\xff\xd8\xff\xe0" + b"jfif" * 32)
    svg = upload(client, "logo.svg", SVG)

    resp = client.patch(
        "/admin/settings",
        json={
            "logo": {"key": svg, "name": "logo.svg"},
            "certificate_images": {"logo": {"key": jpeg, "name": "gerb.bin"}},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["logo"]["name"] == "logo.svg"
    assert resp.json()["certificate_images"]["logo"]["name"] == "gerb.bin"


def test_a_bad_image_takes_the_whole_patch_down_with_it(client, sms, storage):
    """Смешанный запрос сохраняется целиком или не сохраняется вовсе: иначе
    админ увидел бы новое название рядом с ошибкой про картинку и не понял,
    что записалось."""
    login_admin(client, sms)
    client.patch("/admin/settings", json={"platform_name": "LMS"})
    docx = upload(client, "Договор.docx", b"PK\x03\x04 fake docx")

    resp = client.patch(
        "/admin/settings",
        json={
            "org_name": "ИПК",
            "contacts": {"phone": "+77010000000"},
            "certificate_images": {"stamp": {"key": docx, "name": "печать.png"}},
        },
    )
    assert resp.status_code == 422

    body = client.get("/admin/settings").json()
    assert body["org_name"] == ""
    assert body["contacts"] == EMPTY_CONTACTS
    assert body["certificate_images"]["stamp"] is None
    # А то, что записалось до запроса, на месте
    assert body["platform_name"] == "LMS"


def test_patch_never_writes_chat_id(client, sms):
    """Чужой chat_id — это заявки с телефонами учителей, ушедшие незнакомому
    человеку: поля в запросе нет вовсе, и попытку его прислать отбивает
    `extra: forbid`."""
    login_admin(client, sms)

    resp = client.patch("/admin/settings", json={"telegram": {"chat_id": "-1001234567890"}})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "telegram.chat_id"
    assert client.get("/admin/settings").json()["telegram"]["connected"] is False

    # Флаги при этом пишутся, и по одному
    body = client.patch("/admin/settings", json={"telegram": {"notify_leads": False}}).json()
    assert body["telegram"]["notify_leads"] is False
    assert body["telegram"]["notify_submissions"] is True
    assert body["telegram"]["connected"] is False


def test_connected_bot_shows_the_chat_but_not_its_id(client, sms):
    """Экран узнаёт о привязке по признаку и названию чата: chat_id наружу
    не уходит, а connected_at приходит тем же ISO, что и лёг в базу."""
    login_admin(client, sms)
    bind_telegram(notify_leads=False)

    body = client.get("/admin/settings").json()
    assert body["telegram"] == {
        "connected": True,
        "chat_title": "LMS — заявки",
        "connected_at": "2026-08-06T05:30:00Z",
        "notify_leads": False,
        "notify_submissions": True,
    }

    # Флаг правится, привязка при этом на месте
    body = client.patch("/admin/settings", json={"telegram": {"notify_leads": True}}).json()
    assert body["telegram"] == {
        "connected": True,
        "chat_title": "LMS — заявки",
        "connected_at": "2026-08-06T05:30:00Z",
        "notify_leads": True,
        "notify_submissions": True,
    }


# -- GET /settings --------------------------------------------------------


def test_public_settings_have_nothing_extra(client, client2, sms, storage):
    """Публичный ответ сверяется множеством ключей целиком: вход здесь
    не нужен, и лишнее поле утекает наружу вместе с ответом."""
    login_admin(client, sms)
    client.patch(
        "/admin/settings",
        json={
            "platform_name": "LMS",
            "org_name": "Институт повышения квалификации",
            "logo": image(client, "logo.svg"),
            "contacts": CONTACTS,
            "certificate_images": {"stamp": image(client, "pechat.png")},
        },
    )

    resp = client2.get("/settings")
    assert resp.status_code == 200
    assert resp.json() == {
        "platform_name": "LMS",
        "org_name": "Институт повышения квалификации",
        "logo_url": f"{BASE}/branding/logo",
        "contacts": CONTACTS,
    }
    # Ни привязки бота, ни картинок сертификата, ни ключей хранилища
    assert set(resp.json()) == {"platform_name", "org_name", "logo_url", "contacts"}


def test_public_settings_work_before_anything_is_set(client):
    assert client.get("/settings").json() == {
        "platform_name": "",
        "org_name": "",
        "logo_url": None,
        "contacts": EMPTY_CONTACTS,
    }


# -- GET /branding/{slot} -------------------------------------------------


def test_branding_serves_bytes_without_login(client, client2, sms, storage):
    login_admin(client, sms)
    client.patch("/admin/settings", json={"logo": image(client, "logo.png")})

    resp = client2.get("/branding/logo")
    assert resp.status_code == 200
    assert resp.content == PNG
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-length"] == str(len(PNG))
    # Логотип меняют раз в год, а стоит он на каждой странице лендинга
    assert resp.headers["cache-control"] == "public, max-age=86400"
    # Картинка стоит в <img>, а не скачивается
    assert "content-disposition" not in resp.headers


def test_branding_404_for_an_empty_slot_and_for_an_invented_one(client, sms, storage):
    login_admin(client, sms)
    client.patch("/admin/settings", json={"logo": image(client, "logo.png")})

    for path in ("/branding/cert_stamp", "/branding/favicon"):
        resp = client.get(path)
        assert resp.status_code == 404, path
        assert resp.json()["error"]["code"] == "not_found"
        # На 404 кэш не ставим: поставленная позже картинка появится сразу
        assert "cache-control" not in resp.headers


def test_branding_404_when_the_object_is_gone(client, client2, sms, storage):
    """Ключ в настройках есть, объекта в хранилище уже нет: для открывшего
    адрес это то же самое, что не поставленная картинка."""
    login_admin(client, sms)
    logo = image(client, "logo.png")
    client.patch("/admin/settings", json={"logo": logo})
    storage.objects.pop(logo["key"])

    resp = client2.get("/branding/logo")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    # В настройках картинка при этом остаётся: пропал объект, а не настройка
    assert client.get("/admin/settings").json()["logo"]["name"] == "logo.png"


def test_branding_does_not_let_an_svg_run_on_the_api_domain(client, client2, sms, storage):
    """Внутри svg бывает <script>, а кука сессии стоит на родительском домене
    и покрывает оба фронта: открытый прямым адресом логотип выполнялся бы
    на источнике, делящем куку с админкой. Сам svg при этом остаётся
    разрешённым — в <img> заголовки картинке не мешают."""
    login_admin(client, sms)
    client.patch("/admin/settings", json={"logo": {"key": upload(client, "logo.svg", SVG),
                                                  "name": "logo.svg"}})

    resp = client2.get("/branding/logo")
    assert resp.status_code == 200
    assert resp.content == SVG
    assert resp.headers["content-type"] == "image/svg+xml"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'"
    )


# -- сессия 8: слоты сертификата принимают только png и jpeg ---------------

GIF = b"GIF89a" + b"fake" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"fake" * 64
JPEG = b"\xff\xd8\xff" + b"fake" * 64


def test_certificate_slots_take_only_png_and_jpeg(client, sms, storage):
    """Шаблон сертификата рисует fpdf2, а он берёт только растр. Пропущенный
    сюда svg не вставится, и админ узнает об этом с уже выданной бумаги,
    где на месте печати пусто, — поэтому отказ приходит при загрузке."""
    login_admin(client, sms)

    for field in ("logo", "sign", "stamp"):
        for name, content in (("pechat.svg", SVG), ("pechat.gif", GIF), ("pechat.webp", WEBP)):
            resp = client.patch(
                "/admin/settings",
                json={"certificate_images": {field: {"key": upload(client, name, content),
                                                     "name": name}}},
            )
            assert resp.status_code == 422, (field, name, resp.text)
            fields = resp.json()["error"]["details"]["fields"]
            # Имя поля в ошибке — как на экране
            assert fields[0]["field"] == f"certificate_images.{field}"
            assert fields[0]["message"] == "Нужна картинка: PNG или JPEG"

    # Ни один отказ ничего не записал: слоты остались пустыми
    body = client.get("/admin/settings").json()
    assert body["certificate_images"] == {"logo": None, "sign": None, "stamp": None}

    # А png и jpeg проходят
    for field, content, name in (
        ("logo", PNG, "gerb.png"),
        ("sign", JPEG, "podpis.jpg"),
        ("stamp", PNG, "pechat.png"),
    ):
        resp = client.patch(
            "/admin/settings",
            json={"certificate_images": {field: {"key": upload(client, name, content),
                                                 "name": name}}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["certificate_images"][field]["name"] == name


def test_platform_logo_still_takes_svg(client, sms, storage):
    """Логотипу платформы svg нужен: он стоит на лендинге и растянут
    по-разному в шапке и в подвале. Сузили только слоты сертификата."""
    login_admin(client, sms)
    resp = client.patch(
        "/admin/settings",
        json={"logo": {"key": upload(client, "logo.svg", SVG), "name": "logo.svg"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["logo"]["name"] == "logo.svg"


def test_the_logo_error_names_what_the_slot_takes(client, sms, storage):
    """Текст отказа у слотов разный: обещать логотипу «PNG или JPEG» —
    значит врать про слот, который берёт и svg."""
    login_admin(client, sms)
    resp = client.patch(
        "/admin/settings",
        json={"logo": {"key": upload(client, "dogovor.docx", b"PK\x03\x04not a picture"),
                       "name": "dogovor.docx"}},
    )
    assert resp.status_code == 422, resp.text
    message = resp.json()["error"]["details"]["fields"][0]["message"]
    assert "SVG" in message and message != "Нужна картинка: PNG или JPEG"
