import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.phone import normalize_phone
from app.domain.platform import DEFAULT_PLATFORM, PLATFORMS

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"  # dev | prod
    database_url: str = "postgresql+psycopg://lms:lms@localhost:5445/lms"

    # Два фронта — два источника (BACKEND_NOTES, раздел 1).
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:3001"]

    # Какие из них — админка. Имя куки сессии зависит от приложения, а само
    # приложение узнаётся по `Origin` запроса: только он приходит от браузера
    # и совпадает с тем, что уже разрешено в CORS. `ADMIN_BASE_URL` для этого
    # не годится — локально он нарочно `127.0.0.1` (Bot API отвергает кнопки
    # на localhost), а браузер ходит на localhost, и разделение молча
    # не срабатывало бы.
    #
    # По умолчанию пусто, и это не забывчивость: значение годится либо
    # для разработки, либо для боя, и «удобное» localhost-умолчание
    # 21.08.2026 уронило прод — оно не совпало с боевым CORS_ORIGINS.
    # Пустое значение значит «кука общая», как было до разделения.
    admin_origins: list[str] = []

    # Какая из площадок спрашивает: «источник → код платформы». Способ тот же,
    # что у `admin_origins`, и по той же причине — `Origin` единственное, что
    # приходит от браузера на каждый запрос и уже проверено CORS.
    #
    # По умолчанию пусто по той же причине, что и у `admin_origins`: значение
    # годится либо для разработки, либо для боя, и «удобное» localhost-умолчание
    # 21.08.2026 уронило прод. Пустое значит «всё — первой площадке», как было
    # до разделения.
    platform_origins: dict[str, str] = {}

    cookie_secure: bool = False
    session_ttl_days: int = 180

    # Адрес клиента для лимитов. X-Forwarded-For не читается вовсе: канонический
    # прокси свой адрес в него дописывает, а клиентское значение оставляет
    # первым — то есть ведро лимита выбирал бы себе сам клиент. Читается
    # X-Real-IP, который наш прокси переписывает целиком, и тогда число прокси
    # перед сервисом перестаёт иметь значение. Включать только там, где до
    # сервиса нельзя достучаться мимо прокси (DEPLOY.md, «Адрес клиента»).
    trust_real_ip: bool = False

    sms_provider: str = "log"
    # WhatsApp Cloud API (SMS_PROVIDER=whatsapp). Код входа уходит
    # согласованным шаблоном категории Authentication: свободное сообщение
    # WhatsApp примет только внутри 24-часового окна, а на входе его нет
    # никогда (`app/adapters/sms/whatsapp.py`).
    whatsapp_phone_number_id: str = ""
    # Постоянный токен системного пользователя. Место ему рядом с паролем
    # базы: вписанный в настройки площадки, он утекает вместе с ней.
    whatsapp_access_token: str = ""
    whatsapp_api_version: str = "v21.0"
    # Имя и язык шаблона — как они заведены в Meta Business Manager.
    whatsapp_template_name: str = "otp_ru"
    whatsapp_template_language: str = "ru"
    # Шаблон Authentication по умолчанию идёт с кнопкой «Скопировать код»,
    # и тогда код передаётся дважды — в теле и в кнопке. Шаблон без кнопки
    # существует, поэтому переключатель, а не константа.
    whatsapp_template_has_button: bool = True
    telegram_provider: str = "log"
    # Токен бота — ключ доступа, и его место рядом с паролем базы, а не
    # в настройках платформы: вписанный в админку, он утекает вместе с ней.
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    # Секрет вебхука: Telegram присылает его заголовком, и это единственное,
    # что отличает настоящий запрос бота от чужого.
    telegram_webhook_secret: str = ""
    # Как получать апдейты. `webhook` — прод: api.domain.kz и так публичный,
    # Telegram стучится сам. `poll` — только локально, без туннеля наружу:
    # сервис сам вытягивает апдейты через getUpdates фоновой задачей
    # (`app/adapters/telegram/poller.py`), пока `TELEGRAM_PROVIDER=bot`.
    telegram_updates: str = "webhook"
    # Код привязки живёт 10 минут: за это время админ успевает дойти
    # до телефона, а подобрать шесть знаков за столько — нет.
    telegram_bind_code_min: int = 10
    storage_provider: str = "local"

    # Приватное хранилище файлов. Локально — каталог внутри backend, он же
    # версия для разработки: публичного бакета нет нигде (BACKEND_NOTES, 9).
    storage_dir: str = "var/storage"

    # Объектное хранилище (STORAGE_PROVIDER=s3). Адрес площадки, а не AWS:
    # у PS Cloud это https://object.pscloud.io, бакет приватный. Регион
    # подписи S3-совместимые площадки не проверяют, но botocore его требует.
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    # Секрет подписи ссылок на файлы. Проверяет подпись сам сервис
    # (`app/application/files.py`), отдельного раздатчика нет.
    storage_secret: str = "dev-secret"
    # Откуда фронт скачивает файл: в бою адрес API, локально сам бэкенд
    public_base_url: str = "http://localhost:8000"
    # Куда ведёт QR с бумажного сертификата. Это НЕ public_base_url: страница
    # проверки живёт в клиентском приложении (domain.kz), а не в API
    # (api.domain.kz). Адрес меняется при смене домена, а не при правке
    # настроек админом, поэтому он здесь, а не в настройках площадки.
    # Пусто — QR не печатается: код, ведущий в никуда, с бумаги не исправить.
    verify_base_url: str = "http://localhost:3000"
    # Куда ведёт кнопка «Открыть в админке» в уведомлениях бота. Свой адрес,
    # а не public_base_url: админка — третий, отдельный деплой, не API.
    # `127.0.0.1`, а не `localhost` — Bot API отказывает кнопке (и всему
    # сообщению) на хосте `localhost` целиком, а тот же адрес по IP принимает.
    admin_base_url: str = "http://127.0.0.1:3001"
    # Ссылка живёт 15 минут и перевыдаётся молча — длина TTL не болезненна
    file_url_ttl_min: int = 15
    upload_max_mb: int = 20
    # Без лимита playback превращается в удобную качалку курса
    playback_per_min: int = 10

    # Правила входа. Числа сняты с экрана /login и раздела 8 BACKEND_NOTES:
    # код 4 цифры живёт 5 минут, 3 попытки ввода, блок на 10 минут,
    # повторная отправка через 60 секунд.
    code_length: int = 4
    code_ttl_min: int = 5
    code_max_attempts: int = 3
    code_block_min: int = 10
    code_resend_sec: int = 60
    # SMS стоит денег: потолки в сутки на номер и на IP.
    phone_codes_per_day: int = 10
    ip_codes_per_day: int = 30
    # Проверка сертификата публична: без потолка на адрес номера перебираются
    # скриптом, и реестр становится общим достоянием.
    verify_per_min: int = 20
    # Форма вопроса под уроком без капчи — потолок на пользователя.
    thread_messages_per_min: int = 3

    # Первый админ на чистой базе: номер заводится админом при старте
    # (`app/application/bootstrap.py`). Прод поднимается пустым, и без этого
    # некому выдать первый доступ и завести курс.
    auth_bootstrap_phone: str = ""
    # Необязательная вторая половина: с ней тот же номер входит фиксированным
    # кодом, минуя провайдера. Это бэкдор, и нужен он ровно там, где кода
    # взять неоткуда, — при SMS_PROVIDER=log. С работающим WhatsApp
    # оставлять его незачем: пустое значение выключает.
    auth_bootstrap_code: str = ""


# Значения провайдеров, под которые в коде есть адаптер. Опечатка в них
# не должна доживать до первого запроса: сервис с неизвестным
# TELEGRAM_PROVIDER поднимается здоровым и падает на каждой заявке учителя,
# а неизвестный SMS_PROVIDER без этой проверки не значит вообще ничего —
# код входа продолжает уходить в лог заглушкой.
PROVIDERS = {
    "sms_provider": ("log", "whatsapp"),
    "telegram_provider": ("log", "bot"),
    "storage_provider": ("local", "s3"),
}

# `telegram_updates` не провайдер — не выбирает адаптер, а выбирает, кто
# первым заговорит с Telegram (сервис или Telegram с сервисом). Проверяется
# рядом, а не в PROVIDERS: тот список сверяется с полями `*_provider`
# по имени (`test_every_provider_setting_is_checked`), и это поле в него
# не попадает нарочно.
TELEGRAM_UPDATES_MODES = ("webhook", "poll")


def check_providers(cfg: Settings) -> None:
    """Проверка провайдеров при старте: неизвестное значение роняет сервис
    здесь, а не в середине сценария. Зовётся из `create_app`."""
    for field, allowed in PROVIDERS.items():
        value = getattr(cfg, field)
        if value not in allowed:
            raise RuntimeError(
                f"{field.upper()}={value!r} — такого провайдера нет."
                f" Допустимые значения: {', '.join(allowed)}"
            )
    # Имени провайдера мало: `bot` без токена проверку проходил, а дальше
    # каждое уведомление админу уходило в 4xx — и терялось молча, потому
    # что осмысленный отказ Telegram не повторяют. Это ровно тот случай,
    # ради которого проверка и заведена: сервис выглядит здоровым,
    # а ломается на заявке учителя.
    # Имя шаблона у провайдера своё, а вот без номера отправителя и токена
    # не уйдёт ни одно сообщение — и узнали бы мы об этом на первом входе
    # учителя, а не при старте.
    if cfg.sms_provider == "whatsapp":
        empty = [
            name.upper()
            for name in ("whatsapp_phone_number_id", "whatsapp_access_token")
            if not getattr(cfg, name)
        ]
        if empty:
            raise RuntimeError(
                f"SMS_PROVIDER=whatsapp, но пусты: {', '.join(empty)} —"
                " код входа уходил бы в никуда"
            )
    if cfg.telegram_provider == "bot" and not cfg.telegram_bot_token:
        raise RuntimeError(
            "TELEGRAM_PROVIDER=bot, но TELEGRAM_BOT_TOKEN пуст —"
            " уведомления админу уходили бы в никуда"
        )
    if cfg.telegram_updates not in TELEGRAM_UPDATES_MODES:
        raise RuntimeError(
            f"TELEGRAM_UPDATES={cfg.telegram_updates!r} — такого режима нет."
            f" Допустимые значения: {', '.join(TELEGRAM_UPDATES_MODES)}"
        )
    # То же и у бакета: `s3` без адреса и ключей проверку проходил бы, а падало
    # бы это на первой загрузке материала — то есть у методиста, а не у нас.
    if cfg.storage_provider == "s3":
        empty = [
            name.upper()
            for name in ("s3_endpoint_url", "s3_bucket", "s3_access_key", "s3_secret_key")
            if not getattr(cfg, name)
        ]
        if empty:
            raise RuntimeError(
                f"STORAGE_PROVIDER=s3, но пусты: {', '.join(empty)} —"
                " материалы уроков и сданные работы уходили бы в никуда"
            )
    check_bootstrap_login(cfg)
    check_admin_origins(cfg)
    check_platform_origins(cfg)


def check_admin_origins(cfg: Settings) -> None:
    """Источники админки — предупреждение, а не отказ.

    Цена ошибки здесь — вернувшаяся общая кука: вход в админку снова закрывал
    бы кабинет учителя. Это неприятно, но именно так площадка и работала
    до 21.08.2026, а гасить из-за этого живой API дороже. В тот же день
    забытая переменная прод и уронила — сервис уходил в цикл перезапуска,
    пока настройку не дописали.

    Поэтому проверка говорит громко и в лог, а сервис поднимается.
    """
    if not cfg.admin_origins:
        log.warning(
            "ADMIN_ORIGINS не задан: админка получит ту же куку, что и кабинет"
            " учителя, и вход в одну сторону будет закрывать сессию в другой"
        )
        return
    unknown = [o for o in cfg.admin_origins if o not in cfg.cors_origins]
    if unknown:
        log.warning(
            "ADMIN_ORIGINS вне CORS_ORIGINS: %s — браузер до API с такого источника"
            " не дойдёт вовсе, и кука у админки останется общей",
            ", ".join(unknown),
        )


def check_platform_origins(cfg: Settings) -> None:
    """Источники площадок: забытая настройка — предупреждение, опечатка
    в коде площадки — отказ.

    Цена ошибки разная. Настройки нет — всё уходит первой площадке, то есть
    ровно то, как площадка работала до разделения; гасить из-за этого живой
    API дороже (та же логика, что у `check_admin_origins`).

    А код вне `PLATFORMS` пишется в базу и там его отвергает `CHECK`: сервис
    поднимается здоровым и падает на каждой записи учителя — на доступе,
    прогрессе, попытке. Это тот же случай, ради которого `check_providers`
    роняет сервис на неизвестном провайдере.
    """
    unknown = sorted({code for code in cfg.platform_origins.values() if code not in PLATFORMS})
    if unknown:
        raise RuntimeError(
            f"PLATFORM_ORIGINS: неизвестные коды площадок — {', '.join(unknown)}."
            f" Допустимые значения: {', '.join(PLATFORMS)}"
        )
    if not cfg.platform_origins:
        log.warning(
            "PLATFORM_ORIGINS не задан: все запросы считаются площадкой %s —"
            " вторая получит каталог, доступы и сертификаты первой",
            DEFAULT_PLATFORM,
        )
        return
    outside = [origin for origin in cfg.platform_origins if origin not in cfg.cors_origins]
    if outside:
        log.warning(
            "PLATFORM_ORIGINS вне CORS_ORIGINS: %s — браузер до API с такого источника"
            " не дойдёт вовсе, и площадкой у него останется %s",
            ", ".join(outside),
            DEFAULT_PLATFORM,
        )


def check_bootstrap_login(cfg: Settings) -> None:
    """Первый админ и вход без провайдера проверяются при старте целиком:
    неверный номер или код не дают ни отказа, ни ошибки — они просто
    не пускают, и разбираться с этим пришлось бы на живом сервере.

    Номер без кода — обычная боевая настройка: админ заведён, а входит он
    кодом от провайдера. А вот код без номера не пускает никого и значит
    только опечатку.
    """
    if cfg.auth_bootstrap_code and not cfg.auth_bootstrap_phone:
        raise RuntimeError(
            "AUTH_BOOTSTRAP_CODE задан без AUTH_BOOTSTRAP_PHONE —"
            " фиксированный код без номера не пускает никого"
        )
    if not cfg.auth_bootstrap_phone:
        return
    if normalize_phone(cfg.auth_bootstrap_phone) is None:
        raise RuntimeError(
            f"AUTH_BOOTSTRAP_PHONE={cfg.auth_bootstrap_phone!r} — не казахстанский номер:"
            " нужен +7 и 10 цифр"
        )
    if not cfg.auth_bootstrap_code:
        return
    if not cfg.auth_bootstrap_code.isdigit() or len(cfg.auth_bootstrap_code) != cfg.code_length:
        raise RuntimeError(
            f"AUTH_BOOTSTRAP_CODE — ровно {cfg.code_length} цифр:"
            " экран входа других не примет, а лишние знаки в него не влезут"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
