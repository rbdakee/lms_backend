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
    storage_provider: str = "local"

    # Приватное хранилище файлов. Локально — каталог внутри backend, он же
    # версия для разработки: публичного бакета нет нигде (BACKEND_NOTES, 9).
    storage_dir: str = "var/storage"
    # Секрет подписи ссылок. В бою тот же самый лежит в secure_link_md5 nginx.
    storage_secret: str = "dev-secret"
    # Откуда фронт скачивает файл: в бою адрес API, локально сам бэкенд
    public_base_url: str = "http://localhost:8000"
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
