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

    sms_provider: str = "log"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
