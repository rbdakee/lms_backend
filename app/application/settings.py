"""Настройки площадки: четыре вкладки экрана одним ответом и публичный кусок
для лендинга.

Лежат в таблице `setting` ключ-значение четырьмя строками — `platform`,
`contacts`, `branding`, `telegram`. Значения, которых не ставили, приходят
пустыми, а не отсутствующими: экран рисует поля всегда, и «ещё не настраивали»
не должно отличаться от «настроили пустым» (CONTRACT, GET /admin/settings).

Картинки лежат в том же приватном хранилище, что и материалы уроков: наружу
уходит адрес публичной раздачи `GET /branding/{slot}`, а не ключ объекта.
Отдельным путём, а не подписанной ссылкой, потому что логотип стоит
на лендинге, который открывают без входа, и ссылка со сроком жизни там
протухала бы на каждом заходе.
"""

from collections.abc import Iterator

from app.adapters.db.repos import SettingRepo
from app.application.files import safe_name
from app.application.ports import StoragePort
from app.config import Settings
from app.domain.errors import FieldError, NotFoundError
from app.domain.image import HEAD_SIZE, image_mime
from app.domain.submission import mime_of

PLATFORM_KEY = "platform"
CONTACTS_KEY = "contacts"
BRANDING_KEY = "branding"
TELEGRAM_KEY = "telegram"

PLATFORM_FIELDS = ("platform_name", "org_name")
# Оба номера — именно номера, а не ссылки: ссылку wa.me собирает фронт.
# Telegram-контакта учителю нет (решение владельца 20.08.2026); ключ telegram
# в старых строках настройки просто перестаёт читаться — миграция не нужна
CONTACT_FIELDS = ("name", "phone", "whatsapp", "hours")

# Логотип платформы — свой слот хранилища; три картинки сертификата экран
# показывает вложенным объектом, и имя поля там короче имени слота.
LOGO_SLOT = "logo"
CERT_SLOTS = {"logo": "cert_logo", "sign": "cert_sign", "stamp": "cert_stamp"}
SLOTS = (LOGO_SLOT, *CERT_SLOTS.values())

NO_IMAGE = "Картинка не найдена"

# Что принимает слот. Логотип платформы стоит на лендинге, и svg ему нужен;
# три картинки сертификата уходят в fpdf2, а он берёт только растр — svg
# в документ не вставится, и админ узнает об этом с уже выданной бумаги,
# где печати просто нет.
CERT_MIME = ("image/png", "image/jpeg")
NOT_A_CERT_IMAGE = "Нужна картинка: PNG или JPEG"
NOT_AN_IMAGE = "Нужна картинка: PNG, JPEG, GIF, WEBP или SVG"


class SettingsService:
    def __init__(self, settings: SettingRepo, storage: StoragePort, cfg: Settings):
        self.settings = settings
        self.storage = storage
        self.cfg = cfg

    # -- чтение настроек ----------------------------------------------------

    def platform(self) -> dict:
        stored = self.settings.get(PLATFORM_KEY)
        return {field: stored.get(field, "") for field in PLATFORM_FIELDS}

    def contacts(self) -> dict:
        stored = self.settings.get(CONTACTS_KEY)
        return {field: stored.get(field, "") for field in CONTACT_FIELDS}

    def telegram(self) -> dict:
        """Привязка бота как она лежит в базе, вместе с `chat_id`: его читают
        отправка уведомлений и ручки привязки. Наружу `chat_id` не уходит
        никогда — в ответе экрана от него остаётся признак `connected`."""
        stored = self.settings.get(TELEGRAM_KEY)
        return {
            "chat_id": stored.get("chat_id"),
            "chat_title": stored.get("chat_title"),
            "connected_at": stored.get("connected_at"),
            # По умолчанию включены: бот привязывают затем, чтобы получать
            # заявки и работы на проверку
            "notify_leads": stored.get("notify_leads", True),
            "notify_submissions": stored.get("notify_submissions", True),
        }

    def image(self, slot: str) -> dict | None:
        """Картинка слота — ключ хранилища и имя файла, — или None, если её
        не ставили. Ключ нужен генерации сертификата, наружу он не уходит."""
        if slot not in SLOTS:
            return None
        return self.settings.get(BRANDING_KEY).get(slot)

    # -- GET /admin/settings ------------------------------------------------

    def admin_get(self) -> dict:
        telegram = self.telegram()
        return {
            **self.platform(),
            "logo": self._image_out(LOGO_SLOT),
            "contacts": self.contacts(),
            "certificate_images": {
                field: self._image_out(slot) for field, slot in CERT_SLOTS.items()
            },
            "telegram": {
                # Привязан — значит известен чат, куда слать: сам chat_id
                # экрану не нужен и наружу не уходит
                "connected": telegram["chat_id"] is not None,
                "chat_title": telegram["chat_title"],
                "connected_at": telegram["connected_at"],
                "notify_leads": telegram["notify_leads"],
                "notify_submissions": telegram["notify_submissions"],
            },
        }

    # -- PATCH /admin/settings ----------------------------------------------

    def patch(self, data: dict) -> dict:
        """Меняет только присланное: вкладку, которой в запросе нет, не трогаем
        вовсе — экран сохраняет их по одной.

        Каждая строка настроек пишется слиянием, а не целиком: соседние ключи
        в ней меняют другие ручки того же экрана, и «прочитать, изменить,
        записать» отменяло бы их правку. Дороже всего это в `telegram`: рядом
        с флагами лежит `chat_id`, и отменённая отвязка отправляет заявки
        с телефонами учителей в чат, который админ уже считает отключённым.
        """
        platform = _sent(data, PLATFORM_FIELDS)
        if platform:
            self.settings.merge(PLATFORM_KEY, platform)

        contacts = _sent(data.get("contacts") or {}, CONTACT_FIELDS)
        if contacts:
            self.settings.merge(CONTACTS_KEY, contacts)

        branding = self._patched_branding(data)
        if branding is not None:
            self.settings.merge(BRANDING_KEY, branding)

        telegram = _sent(data.get("telegram") or {}, ("notify_leads", "notify_submissions"))
        if telegram:
            # Уходят ровно флаги: `chat_id` этим PATCH не пишется никогда.
            # Чужой чат — это заявки с телефонами учителей, ушедшие
            # незнакомому человеку, и отозвать их уже нельзя
            self.settings.merge(TELEGRAM_KEY, telegram)

        return self.admin_get()

    # -- GET /settings ------------------------------------------------------

    def public(self) -> dict:
        """Лендинг и страница курса: название, логотип и контакты админа.

        Сверх этого сюда не попадает ничего — ни привязка бота, ни картинки
        сертификата, ни ключи хранилища: входа здесь нет, и лишнее поле
        утекает наружу вместе с ответом.
        """
        logo = self._image_out(LOGO_SLOT)
        return {
            **self.platform(),
            "logo_url": logo["url"] if logo else None,
            "contacts": self.contacts(),
        }

    # -- GET /branding/{slot} -----------------------------------------------

    def content(self, slot: str) -> tuple[str, int, Iterator[bytes]]:
        """Байты картинки: тип, размер и поток. Выдуманный слот и не
        поставленная картинка дают один и тот же 404 — для открывшего адрес
        это одно и то же, а перебирать имена слотов незачем."""
        image = self.image(slot)
        if image is None:
            raise NotFoundError(NO_IMAGE)
        size = self.storage.size(image["key"])
        if size is None:
            # Ключ в настройках есть, объекта нет: для открывшего тот же 404
            raise NotFoundError(NO_IMAGE)
        return mime_of(image["name"]), size, self.storage.read(image["key"])

    # -- сборка ответа ------------------------------------------------------

    def _image_out(self, slot: str) -> dict | None:
        image = self.image(slot)
        if image is None:
            return None
        # Наружу — адрес публичной раздачи; ключ объекта остаётся внутри,
        # как и у материалов урока
        return {
            "url": f"{self.cfg.public_base_url}/branding/{slot}",
            "name": image["name"],
        }

    def _patched_branding(self, data: dict) -> dict | None:
        """Присланные слоты строки `branding` — или None, если картинок
        в запросе не было.

        Возвращаются только тронутые слоты: остальные в строке остаются
        как были, их доливает слияние на стороне базы.
        """
        changed: dict = {}
        if "logo" in data:
            changed[LOGO_SLOT] = self._checked_image("logo", data["logo"])
        certificate_images = data.get("certificate_images") or {}
        for field, slot in CERT_SLOTS.items():
            if field in certificate_images:
                # Имя поля в ошибке — как на экране: certificate_images.stamp
                changed[slot] = self._checked_image(
                    f"certificate_images.{field}",
                    certificate_images[field],
                    for_certificate=True,
                )
        return changed or None

    def _checked_image(
        self, field: str, value: dict | None, *, for_certificate: bool = False
    ) -> dict | None:
        """Ключ объекта и имя файла; null убирает картинку из слота.

        Проверок две и порядок у них важен: сперва объект в хранилище есть
        (иначе админ увидит пустой блок вместо только что загруженного файла),
        и только потом — формат. Формат у слотов разный: логотипу платформы
        годится любая картинка, включая svg, а слоты сертификата принимают
        только png и jpeg — остальное fpdf2 либо не возьмёт, либо не покажет,
        и обнаружится это на выданном документе, а не здесь.

        Картинка опознаётся по байтам объекта, а не по присланному имени: имя
        сочиняет клиент, и `договор.docx`, названный `печать.png`, проходил бы
        насквозь. Заодно это закрывает и обратное — чужой приватный pdf,
        поставленный логотипом, получил бы публичный адрес раздачи.

        Байты при снятии картинки остаются: порт storage умеет писать, читать
        и мерить, но не удалять.
        """
        if value is None:
            return None
        if self.storage.size(value["key"]) is None:
            raise NotFoundError("Загруженный файл не найден — загрузите его заново")
        mime = image_mime(self._head(value["key"])) or ""
        fits = mime in CERT_MIME if for_certificate else mime.startswith("image/")
        if not fits:
            raise FieldError(field, NOT_A_CERT_IMAGE if for_certificate else NOT_AN_IMAGE)
        return {"key": value["key"], "name": safe_name(value["name"])}

    def _head(self, key: str) -> bytes:
        """Начало объекта — по нему и опознаётся формат. Читаем первый кусок
        потока и на этом останавливаемся: дальше сигнатуры смотреть нечего."""
        return next(iter(self.storage.read(key)), b"")[:HEAD_SIZE]


def _sent(data: dict, fields: tuple[str, ...]) -> dict:
    """Присланные поля из перечисленных. null здесь — то же самое, что поле
    не прислали: строку настройки он не стирает, для этого есть пустая
    строка, — так же ведёт себя PATCH курса."""
    return {field: data[field] for field in fields if data.get(field) is not None}
