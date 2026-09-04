"""Настройки: справочник площадок, привязка бота и публичный бренд.

Бренд перестал быть настройкой: название, организация, контакты и картинки
лежат константами в `app/domain/brands.py` и файлами в
`app/assets/brands/<площадка>/` (PLATFORMS_BRIEF, решение 4). Поэтому здесь
проверяется не «сохранилось ли», а «своё ли»: разные площадки должны получать
разный бренд, а PATCH — отказывать полю бренда.

Самих файлов картинок в репозитории нет — их кладёт владелец. Тест подставляет
свою директорию фикстурой `brand_file`: имя файла она берёт из тех же констант,
что и сервер, поэтому переименование картинки бренда тест не ломает.
"""

import json

import pytest
from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.application import settings as settings_module
from app.config import get_settings
from app.domain import brands
from tests.conftest import login, login_admin

BASE = get_settings().public_base_url

PNG = b"\x89PNG\r\n\x1a\n" + b"first" * 64
# Байты второй площадки заметно другие: тест сравнивает не размер, а содержимое
PNG2 = b"\x89PNG\r\n\x1a\n" + b"second" * 64
# Логотипом может лежать и svg, а внутри svg бывает <script>: такой файл
# открывают прямым адресом на домене API.
SVG = (
    b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    b"<script>alert(document.cookie)</script></svg>"
)

# Свои источники, а не из `.env`: у разработчика там localhost с портами,
# и тест не должен зависеть от того, дописал ли он себе вторую площадку.
P1_ORIGIN = "https://first.example.kz"
P2_ORIGIN = "https://second.example.kz"
P1 = {"Origin": P1_ORIGIN}
P2 = {"Origin": P2_ORIGIN}


@pytest.fixture(autouse=True)
def platform_origins(monkeypatch):
    monkeypatch.setattr(get_settings(), "platform_origins", {P1_ORIGIN: "p1", P2_ORIGIN: "p2"})


@pytest.fixture
def brand_file(tmp_path, monkeypatch):
    """Кладёт картинку площадке. Директория подменяется на временную: класть
    файлы в `app/assets/brands/` значило бы оставлять их в репозитории."""
    monkeypatch.setattr(settings_module, "BRANDS_DIR", tmp_path)

    def put(platform: str, slot: str, content: bytes = PNG) -> None:
        directory = tmp_path / platform
        directory.mkdir(exist_ok=True)
        (directory / brands.BRANDS[platform].images[slot]).write_bytes(content)

    return put


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
            client.patch(
                "/admin/settings", json={"telegram": {"notify_leads": False}}
            ).status_code,
        ]

    assert statuses() == [401, 401]

    login(client, sms)
    assert statuses() == [403, 403]
    assert client.get("/admin/settings").json()["error"]["code"] == "forbidden"


# -- GET /admin/settings --------------------------------------------------


def test_admin_settings_are_the_platform_directory_and_the_bot(client, sms):
    """Ничего не настраивали: справочник площадок на месте — он из кода, —
    а привязки бота нет."""
    login_admin(client, sms)

    assert client.get("/admin/settings").json() == {
        "platforms": [
            {
                "platform": "p1",
                "platform_name": brands.BRANDS["p1"].platform_name,
                "org_name": brands.BRANDS["p1"].org_name,
            },
            {
                "platform": "p2",
                "platform_name": brands.BRANDS["p2"].platform_name,
                "org_name": brands.BRANDS["p2"].org_name,
            },
        ],
        "telegram": {
            "connected": False,
            "chat_title": None,
            "connected_at": None,
            # Флаги включены по умолчанию: бот привязывают затем, чтобы
            # получать заявки, работы на проверку и просьбы о сертификате
            "notify_leads": True,
            "notify_submissions": True,
            "notify_certificates": True,
        },
    }


def test_admin_settings_have_no_brand_left(client, sms):
    """Бренд ушёл в код целиком: ни названия, ни контактов, ни картинок
    в ответе больше нет — иначе админка рисовала бы вкладку, которой нет."""
    login_admin(client, sms)

    body = client.get("/admin/settings").json()
    assert set(body) == {"platforms", "telegram"}
    assert set(body["platforms"][0]) == {"platform", "platform_name", "org_name"}


# -- PATCH /admin/settings ------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("platform_name", "Чужое название"),
        ("org_name", "Чужая организация"),
        ("contacts", {"phone": "+70000000009"}),
        ("logo", {"key": "uploads/2026/09/03/9f3c1a7e.png", "name": "logo.png"}),
        ("certificate_images", {"stamp": None}),
    ],
)
def test_patch_refuses_brand_fields(client, sms, field, value):
    """Бренд правится выкаткой, а не экраном: старая форма запроса должна
    получить внятный отказ, а не тихое «сохранено» без последствий."""
    login_admin(client, sms)

    resp = client.patch("/admin/settings", json={field: value})

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_error"
    assert resp.json()["error"]["details"]["fields"][0]["field"] == field


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
    assert body["telegram"]["notify_certificates"] is True
    assert body["telegram"]["connected"] is False


def test_notify_certificates_is_saved_on_its_own(client, sms):
    """Заявки на сертификат — третий тип сообщений в чат (CERTIFICATES_BRIEF, 3),
    и переключатель у него свой: выключенный не должен утащить за собой два
    соседних."""
    login_admin(client, sms)

    body = client.patch(
        "/admin/settings", json={"telegram": {"notify_certificates": False}}
    ).json()

    assert body["telegram"]["notify_certificates"] is False
    assert body["telegram"]["notify_leads"] is True
    assert body["telegram"]["notify_submissions"] is True


def test_patch_returns_the_whole_get(client, sms):
    """Ответ PATCH — это ответ GET целиком, вместе со справочником площадок:
    экран после сохранения перерисовывается им же."""
    login_admin(client, sms)

    resp = client.patch("/admin/settings", json={"telegram": {"notify_submissions": False}})

    assert resp.status_code == 200, resp.text
    assert resp.json() == client.get("/admin/settings").json()
    assert [row["platform"] for row in resp.json()["platforms"]] == ["p1", "p2"]


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
        "notify_certificates": True,
    }

    # Флаг правится, привязка при этом на месте
    body = client.patch("/admin/settings", json={"telegram": {"notify_leads": True}}).json()
    assert body["telegram"] == {
        "connected": True,
        "chat_title": "LMS — заявки",
        "connected_at": "2026-08-06T05:30:00Z",
        "notify_leads": True,
        "notify_submissions": True,
        "notify_certificates": True,
    }


# -- GET /settings --------------------------------------------------------


def test_public_settings_come_from_the_platform_of_the_request(client):
    """Два домена — два бренда: подвал и кнопка «Связаться с администратором»
    у площадок свои, и телефон соседней там появиться не должен."""
    first = client.get("/settings", headers=P1).json()
    second = client.get("/settings", headers=P2).json()

    assert first["platform_name"] == brands.BRANDS["p1"].platform_name
    assert first["org_name"] == brands.BRANDS["p1"].org_name
    assert first["contacts"]["phone"] == brands.BRANDS["p1"].contacts.phone
    assert second["platform_name"] == brands.BRANDS["p2"].platform_name
    assert second["org_name"] == brands.BRANDS["p2"].org_name
    assert second["contacts"]["phone"] == brands.BRANDS["p2"].contacts.phone
    assert first != second


def test_public_settings_have_nothing_extra(client):
    """Публичный ответ сверяется множеством ключей целиком: вход здесь
    не нужен, и лишнее поле утекает наружу вместе с ответом."""
    resp = client.get("/settings", headers=P1)

    assert resp.status_code == 200
    assert set(resp.json()) == {"platform_name", "org_name", "logo_url", "contacts"}
    assert set(resp.json()["contacts"]) == {"name", "phone", "whatsapp", "hours"}


def test_public_settings_without_an_origin_are_the_first_platform(client):
    """Без `Origin` — первая площадка: так ходят curl и предпросмотр ссылки,
    и отказывать им дороже, чем ответить как до разделения."""
    assert client.get("/settings").json() == client.get("/settings", headers=P1).json()


def test_logo_url_shows_up_only_for_the_platform_that_has_the_file(client, brand_file):
    """Файла нет — `logo_url` пустой: экран рисует название текстом, а не
    битую картинку. Положили первой — вторая по-прежнему без логотипа."""
    assert client.get("/settings", headers=P1).json()["logo_url"] is None

    brand_file("p1", "logo")

    first = f"{BASE}/branding/logo?platform=p1"
    assert client.get("/settings", headers=P1).json()["logo_url"] == first
    assert client.get("/settings", headers=P2).json()["logo_url"] is None


def test_logo_url_carries_the_code_of_its_own_platform(client, brand_file):
    """В адресе логотипа стоит код площадки: картинку тянет `<img>`, а в такой
    запрос браузер `Origin` не кладёт вовсе — без параметра вторая площадка
    получала бы логотип первой. Адрес собирает сервер, фронт его не сочиняет.
    """
    brand_file("p1", "logo", PNG)
    brand_file("p2", "logo", PNG2)

    first = client.get("/settings", headers=P1).json()["logo_url"]
    second = client.get("/settings", headers=P2).json()["logo_url"]

    assert first == f"{BASE}/branding/logo?platform=p1"
    assert second == f"{BASE}/branding/logo?platform=p2"
    # Адрес рабочий: по нему приходит картинка своей площадки — и без Origin
    assert client.get(second.removeprefix(BASE)).content == PNG2


# -- GET /branding/{slot} -------------------------------------------------


def test_branding_serves_the_file_of_the_requesting_platform(client, brand_file):
    """Адрес картинки один на обе площадки, а байты разные: их разводит
    площадка запроса."""
    brand_file("p1", "logo", PNG)
    brand_file("p2", "logo", PNG2)

    first = client.get("/branding/logo", headers=P1)
    second = client.get("/branding/logo", headers=P2)

    assert first.status_code == 200
    assert first.content == PNG
    assert second.status_code == 200
    assert second.content == PNG2


def test_branding_serves_certificate_slots_too(client, brand_file):
    """Три картинки сертификата раздаются тем же адресом: их показывает
    предпросмотр сертификата в админке. Параметр площадки работает и здесь —
    правило у раздачи одно на все четыре слота."""
    for slot in ("cert_logo", "cert_sign", "cert_stamp"):
        brand_file("p1", slot, PNG)
        brand_file("p2", slot, PNG2)
        assert client.get(f"/branding/{slot}", headers=P1).content == PNG
        assert client.get(f"/branding/{slot}?platform=p2").content == PNG2


def test_branding_takes_the_platform_from_the_query_before_the_origin(client, brand_file):
    """Тот самый дефект: `<img>` ходит без `Origin`, и вторая площадка
    получала бы логотип первой. Параметр адреса это и чинит — он главнее
    `Origin`, а без него поведение прежнее."""
    brand_file("p1", "logo", PNG)
    brand_file("p2", "logo", PNG2)

    # Запрос без Origin — так его и шлёт браузер за картинкой
    assert client.get("/branding/logo?platform=p2").content == PNG2
    assert client.get("/branding/logo").content == PNG
    # Параметр главнее источника
    assert client.get("/branding/logo?platform=p2", headers=P1).content == PNG2
    assert client.get("/branding/logo", headers=P2).content == PNG2


def test_branding_404_for_an_invented_platform(client, brand_file):
    """Выдуманный код площадки отвечает тем же 404, что и выдуманный слот:
    адрес публичный, его открывают из `<img>`, и перебирать по ответам коды
    площадок незачем."""
    brand_file("p1", "logo")

    resp = client.get("/branding/logo?platform=px")

    assert resp.status_code == 404
    assert resp.json() == client.get("/branding/favicon", headers=P1).json()


def test_branding_headers_let_the_picture_be_cached_but_not_run(client, brand_file):
    brand_file("p1", "logo")

    resp = client.get("/branding/logo", headers=P1)

    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-length"] == str(len(PNG))
    # Логотип меняют выкаткой, а стоит он на каждой странице лендинга
    assert resp.headers["cache-control"] == "public, max-age=86400"
    # Адрес один, байты разные: без vary кэш отдал бы второй площадке первую
    assert resp.headers["vary"].lower() == "origin"
    assert resp.headers["x-content-type-options"] == "nosniff"
    # Картинка стоит в <img>, а не скачивается
    assert "content-disposition" not in resp.headers


def test_branding_404_for_an_empty_slot_and_for_an_invented_one(client, brand_file):
    """Слот без файла и выдуманное имя слота отвечают одинаково: для
    открывшего адрес это одно и то же, а перебирать имена слотов незачем."""
    brand_file("p1", "logo")

    paths = ("/branding/cert_stamp", "/branding/favicon")
    answers = [client.get(path, headers=P1) for path in paths]

    for resp in answers:
        assert resp.status_code == 404, resp.request.url
        assert resp.json()["error"]["code"] == "not_found"
        # На 404 кэш не ставим: положенная позже картинка появится сразу
        assert "cache-control" not in resp.headers
    assert answers[0].json() == answers[1].json()


def test_branding_404_when_the_platform_has_no_file_but_its_neighbour_does(client, brand_file):
    """Картинка первой площадки не подменяет отсутствующую у второй: пустой
    слот остаётся пустым."""
    brand_file("p1", "logo")

    assert client.get("/branding/logo", headers=P2).status_code == 404


def test_branding_does_not_let_an_svg_run_on_the_api_domain(client, brand_file, monkeypatch):
    """Внутри svg бывает <script>, а домен API общий для обеих площадок
    и админки: открытый прямым адресом логотип выполнялся бы на нём. Сам svg
    при этом остаётся разрешённым — в <img> заголовки картинке не мешают."""
    # Логотипом владелец может положить и svg — расширение стоит в константах
    monkeypatch.setitem(brands.BRANDS["p1"].images, "logo", "logo.svg")
    brand_file("p1", "logo", SVG)

    resp = client.get("/branding/logo", headers=P1)

    assert resp.status_code == 200
    assert resp.content == SVG
    assert resp.headers["content-type"] == "image/svg+xml"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'"
    )
