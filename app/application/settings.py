"""Настройки: справочник площадок и привязка Telegram-бота, плюс публичный
бренд для лендинга.

Бренд — название, организация, контакты и картинки — настройкой быть перестал:
он свой у каждой площадки и лежит константами в `app/domain/brands.py`
(PLATFORMS_BRIEF, решение 4). Из таблицы `setting` здесь читается и пишется
только строка `telegram`.

Картинки бренда лежат файлами в `app/assets/brands/<площадка>/` — рядом
со шрифтом сертификата и по той же причине: это часть выкатываемого кода,
а не то, что кто-то загружает через браузер. Порт хранилища им поэтому
не нужен. Наружу они уходят публичной раздачей `GET /branding/{slot}`,
а не подписанной ссылкой, потому что логотип стоит на лендинге, который
открывают без входа, и ссылка со сроком жизни там протухала бы на каждом
заходе.
"""

from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from app.adapters.db.repos import SettingRepo
from app.config import Settings
from app.domain.brands import BRANDS, LOGO_SLOT, brand
from app.domain.errors import NotFoundError
from app.domain.platform import PLATFORMS
from app.domain.submission import mime_of

TELEGRAM_KEY = "telegram"

NO_IMAGE = "Картинка не найдена"

# Путь считается от пакета, а не от рабочей директории процесса — так же,
# как у шрифта сертификата (`adapters/pdf/certificate.py`)
BRANDS_DIR = Path(__file__).resolve().parents[1] / "assets" / "brands"

# Кусок чтения тот же, что у локального хранилища: картинка бренда — файл
# на диске, и держать её в памяти целиком незачем
CHUNK_SIZE = 1024 * 1024


def brand_image(platform: str, slot: str) -> Path | None:
    """Путь к картинке слота у площадки — или None, если площадки такой нет,
    слота такого нет или файл в репозиторий не положили.

    Все три случая отвечают одинаково не по лени: раздача публична, её
    открывают из `<img>`, и перебирать по ответам имена слотов и коды
    площадок незачем. Пустой слот при этом не ошибка и на сертификате:
    место на бумаге просто остаётся пустым.

    Код площадки сюда приходит и параметром адреса, а не только из `Origin`,
    поэтому он проверяется по `BRANDS`, а не считается известным. Имя файла
    берётся из констант по слоту — именем из адреса до чужого файла
    не добраться.
    """
    site = BRANDS.get(platform)
    if site is None:
        return None
    name = site.images.get(slot)
    if name is None:
        return None
    path = BRANDS_DIR / platform / name
    return path if path.is_file() else None


class SettingsService:
    def __init__(self, settings: SettingRepo, cfg: Settings):
        self.settings = settings
        self.cfg = cfg

    # -- чтение настроек ----------------------------------------------------

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

    # -- GET /admin/settings ------------------------------------------------

    def admin_get(self) -> dict:
        """Справочник площадок и привязка бота.

        Бренд правке не подлежит, но имена площадок админке нужны: ими
        подписаны галочки публикации в редакторе курса и чипы фильтра
        площадки в списках заявок, отзывов, вопросов и работ. Взять их
        больше неоткуда — из базы бренд ушёл.
        """
        telegram = self.telegram()
        return {
            # Порядок — как в PLATFORMS, всегда: экран не должен зависеть
            # от того, в каком порядке сложился ответ
            "platforms": [
                {
                    "platform": code,
                    "platform_name": brand(code).platform_name,
                    "org_name": brand(code).org_name,
                }
                for code in PLATFORMS
            ],
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
        """Меняет только присланное. Осталась одна вкладка — Telegram: бренд
        и контакты правятся выкаткой, а не экраном.

        Строка настроек пишется слиянием, а не целиком: рядом с флагами лежит
        `chat_id`, и «прочитать, изменить, записать» отменяло бы отвязку —
        а отменённая отвязка отправляет заявки с телефонами учителей в чат,
        который админ уже считает отключённым.
        """
        telegram = _sent(data.get("telegram") or {}, ("notify_leads", "notify_submissions"))
        if telegram:
            # Уходят ровно флаги: `chat_id` этим PATCH не пишется никогда.
            # Чужой чат — это заявки с телефонами учителей, ушедшие
            # незнакомому человеку, и отозвать их уже нельзя
            self.settings.merge(TELEGRAM_KEY, telegram)

        return self.admin_get()

    # -- GET /settings ------------------------------------------------------

    def public(self, platform: str) -> dict:
        """Лендинг и страница курса: название, логотип и контакты площадки,
        с которой пришёл запрос.

        Сверх этого сюда не попадает ничего — ни привязка бота, ни картинки
        сертификата: входа здесь нет, и лишнее поле утекает наружу вместе
        с ответом.
        """
        site = brand(platform)
        return {
            "platform_name": site.platform_name,
            "org_name": site.org_name,
            "logo_url": self._logo_url(platform),
            "contacts": asdict(site.contacts),
        }

    # -- GET /branding/{slot} -----------------------------------------------

    def content(self, platform: str, slot: str) -> tuple[str, int, Iterator[bytes]]:
        """Байты картинки площадки: тип, размер и поток. Выдуманный слот
        и не положенный файл дают один и тот же 404 — для открывшего адрес
        это одно и то же, а перебирать имена слотов незачем."""
        path = brand_image(platform, slot)
        if path is None:
            raise NotFoundError(NO_IMAGE)
        return mime_of(path.name), path.stat().st_size, _read(path)

    # -- сборка ответа ------------------------------------------------------

    def _logo_url(self, platform: str) -> str | None:
        """Адрес публичной раздачи логотипа — или None, если файла у площадки
        нет: экран рисует название текстом, а не пустую картинку.

        Код площадки стоит в адресе, и это не украшение: логотип тянет `<img>`,
        а в такой запрос браузер `Origin` не кладёт вовсе — без параметра
        вторая площадка получала бы логотип первой. Адрес собирает сервер,
        фронт его не сочиняет.
        """
        if brand_image(platform, LOGO_SLOT) is None:
            return None
        return f"{self.cfg.public_base_url}/branding/{LOGO_SLOT}?platform={platform}"


def _read(path: Path) -> Iterator[bytes]:
    with path.open("rb") as file:
        while chunk := file.read(CHUNK_SIZE):
            yield chunk


def _sent(data: dict, fields: tuple[str, ...]) -> dict:
    """Присланные поля из перечисленных. null здесь — то же самое, что поле
    не прислали: строку настройки он не стирает, для этого есть пустая
    строка, — так же ведёт себя PATCH курса."""
    return {field: data[field] for field in fields if data.get(field) is not None}
