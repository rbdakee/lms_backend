from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"  # dev | prod
    database_url: str = "postgresql+psycopg://lms:lms@localhost:5445/lms"

    # Два фронта — два источника (BACKEND_NOTES, раздел 1).
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:3001"]

    # В бою кука ставится на родительский домен и покрывает web и admin.
    cookie_domain: str | None = None
    cookie_secure: bool = False
    session_ttl_days: int = 180

    # X-Forwarded-For подделывается кем угодно, а на нём держится лимит
    # публичной проверки сертификата. Верим заголовку только там, где перед
    # сервисом стоит наш прокси и он этот заголовок переписывает.
    trust_forwarded_for: bool = False

    sms_provider: str = "log"
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
    # Секрет подписи ссылок. В бою тот же самый лежит в secure_link_md5 nginx.
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


# Значения провайдеров, под которые в коде есть адаптер. Опечатка в них
# не должна доживать до первого запроса: сервис с неизвестным
# TELEGRAM_PROVIDER поднимается здоровым и падает на каждой заявке учителя,
# а неизвестный SMS_PROVIDER без этой проверки не значит вообще ничего —
# код входа продолжает уходить в лог заглушкой.
PROVIDERS = {
    "sms_provider": ("log",),
    "telegram_provider": ("log", "bot"),
    "storage_provider": ("local",),
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
