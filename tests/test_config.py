"""Проверка провайдеров при старте.

Опечатка в имени провайдера раньше доживала до первого запроса: сервис
поднимался, отвечал на health и падал `ValueError` на каждой заявке
учителя. Хуже того, `SMS_PROVIDER` не читался вообще — настройка обещала
отправку, а код входа продолжал уходить в лог заглушкой.
"""

import logging

import pytest

from app.config import (
    PROVIDERS,
    TELEGRAM_UPDATES_MODES,
    Settings,
    check_platform_origins,
    check_providers,
    get_settings,
)
from app.domain.platform import DEFAULT_PLATFORM


def test_a_typo_in_a_provider_stops_the_service_at_start(monkeypatch):
    """Неизвестное значение роняет сервис здесь, а не в середине сценария.
    Сообщение называет переменную и то, что в неё можно положить, — иначе
    разбираться с ним придётся по трассировке."""
    for field, allowed in PROVIDERS.items():
        monkeypatch.setattr(get_settings(), field, "smsc")
        with pytest.raises(RuntimeError) as failed:
            check_providers(get_settings())
        message = str(failed.value)
        assert field.upper() in message
        for value in allowed:
            assert value in message
        monkeypatch.undo()


def test_the_shipped_defaults_pass_the_check():
    """Значения по умолчанию — рабочая конфигурация разработки: проверка
    не должна мешать `docker compose up`."""
    check_providers(get_settings())


def test_every_provider_setting_is_checked():
    """Настройка провайдера, забытая в этом списке, снова начнёт значить
    ничего — как это было с `sms_provider`."""
    settings_fields = {name for name in type(get_settings()).model_fields if "provider" in name}
    assert settings_fields == set(PROVIDERS)


def test_bot_without_a_token_stops_the_service_at_start(monkeypatch):
    """Имени провайдера мало. `bot` с пустым токеном проверку проходил,
    а дальше каждое уведомление админу уходило в 4xx — и терялось молча,
    потому что осмысленный отказ Telegram не повторяют."""
    monkeypatch.setattr(get_settings(), "telegram_provider", "bot")
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "")
    with pytest.raises(RuntimeError) as failed:
        check_providers(get_settings())
    assert "TELEGRAM_BOT_TOKEN" in str(failed.value)

    monkeypatch.setattr(get_settings(), "telegram_bot_token", "123456:AA-fake-token")
    check_providers(get_settings())


def test_a_typo_in_telegram_updates_stops_the_service_at_start(monkeypatch):
    """`telegram_updates` не заканчивается на `_provider` и потому не в
    `PROVIDERS` (см. `test_every_provider_setting_is_checked`) — но опечатка
    в нём так же не должна дожить до первого апдейта от Telegram."""
    monkeypatch.setattr(get_settings(), "telegram_updates", "webhok")
    with pytest.raises(RuntimeError) as failed:
        check_providers(get_settings())
    message = str(failed.value)
    assert "TELEGRAM_UPDATES" in message
    for value in TELEGRAM_UPDATES_MODES:
        assert value in message


def test_s3_without_an_address_or_keys_stops_the_service_at_start(monkeypatch):
    """То же и у бакета: `s3` с пустым адресом проверку проходил бы, а падало
    бы это на первой загрузке материала — у методиста, а не у нас."""
    monkeypatch.setattr(get_settings(), "storage_provider", "s3")
    # По той же причине, что у whatsapp: тест не должен зависеть от .env
    for name in ("s3_endpoint_url", "s3_bucket", "s3_access_key", "s3_secret_key"):
        monkeypatch.setattr(get_settings(), name, "")
    with pytest.raises(RuntimeError) as failed:
        check_providers(get_settings())
    message = str(failed.value)
    for name in ("S3_ENDPOINT_URL", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY"):
        assert name in message

    for name, value in (
        ("s3_endpoint_url", "https://object.pscloud.io"),
        ("s3_bucket", "lms-main"),
        ("s3_access_key", "key"),
        ("s3_secret_key", "secret"),
    ):
        monkeypatch.setattr(get_settings(), name, value)
    check_providers(get_settings())


def test_whatsapp_without_a_sender_or_token_stops_the_service_at_start(monkeypatch):
    """Имя шаблона у провайдера своё, а без номера отправителя и токена
    не уйдёт ни одно сообщение — и узнали бы мы об этом на первом входе
    учителя, а не при старте."""
    monkeypatch.setattr(get_settings(), "sms_provider", "whatsapp")
    # Поля чистим явно: у разработчика в .env может стоять рабочая связка,
    # и тогда проверка «пустое роняет сервис» проверяла бы не то
    monkeypatch.setattr(get_settings(), "whatsapp_phone_number_id", "")
    monkeypatch.setattr(get_settings(), "whatsapp_access_token", "")
    with pytest.raises(RuntimeError) as failed:
        check_providers(get_settings())
    message = str(failed.value)
    assert "WHATSAPP_PHONE_NUMBER_ID" in message
    assert "WHATSAPP_ACCESS_TOKEN" in message

    monkeypatch.setattr(get_settings(), "whatsapp_phone_number_id", "1035760399630606")
    monkeypatch.setattr(get_settings(), "whatsapp_access_token", "fake-token")
    check_providers(get_settings())


# -- Первый админ и вход без провайдера ---------------------------------


def test_a_bootstrap_code_without_a_phone_stops_the_service_at_start(monkeypatch):
    """Код без номера не пускает никого и значит только опечатку. Проверить
    это на живом сервере нечем: отказа не будет — просто не пустит."""
    monkeypatch.setattr(get_settings(), "auth_bootstrap_code", "0909")
    with pytest.raises(RuntimeError) as failed:
        check_providers(get_settings())
    assert "AUTH_BOOTSTRAP_PHONE" in str(failed.value)


def test_a_bootstrap_phone_without_a_code_is_the_normal_production_setting(monkeypatch):
    """А номер без кода — обычная боевая настройка: админ заведён, входит
    он кодом от провайдера. Фиксированный код нужен только там, где кода
    взять неоткуда."""
    monkeypatch.setattr(get_settings(), "auth_bootstrap_phone", "+77000000077")
    check_providers(get_settings())


def test_a_bootstrap_phone_that_is_not_a_phone_stops_the_service_at_start(monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_bootstrap_phone", "admin")
    monkeypatch.setattr(get_settings(), "auth_bootstrap_code", "0909")
    with pytest.raises(RuntimeError) as failed:
        check_providers(get_settings())
    assert "AUTH_BOOTSTRAP_PHONE" in str(failed.value)


def test_a_bootstrap_code_the_login_screen_cannot_take_stops_the_service(monkeypatch):
    """Экран входа принимает ровно `code_length` цифр: код длиннее в него
    не влезет, короче — не отправится, и вход просто не состоится."""
    monkeypatch.setattr(get_settings(), "auth_bootstrap_phone", "+77000000077")
    for wrong in ("09", "090909", "09o9"):
        monkeypatch.setattr(get_settings(), "auth_bootstrap_code", wrong)
        with pytest.raises(RuntimeError) as failed:
            check_providers(get_settings())
        assert "AUTH_BOOTSTRAP_CODE" in str(failed.value)

    monkeypatch.setattr(get_settings(), "auth_bootstrap_code", "0909")
    check_providers(get_settings())


# -- Источники площадок -------------------------------------------------


def test_missing_platform_origins_warns_and_leaves_one_platform(caplog):
    """Настройки нет — всё уходит первой площадке, то есть работает ровно
    как до разделения. Отказывать в обслуживании за это дороже."""
    cfg = Settings(cors_origins=["https://lms.kz"], platform_origins={})
    with caplog.at_level(logging.WARNING):
        check_platform_origins(cfg)
    assert "PLATFORM_ORIGINS" in caplog.text
    assert DEFAULT_PLATFORM in caplog.text


def test_a_platform_origin_outside_cors_warns_but_lets_the_service_start(caplog):
    """Источник без CORS до API не доходит вовсе: браузер с него не пустят,
    и площадкой у него останется первая."""
    cfg = Settings(
        cors_origins=["https://lms.kz"],
        platform_origins={"https://lms.kz": "p1", "https://second.kz": "p2"},
    )
    with caplog.at_level(logging.WARNING):
        check_platform_origins(cfg)
    assert "PLATFORM_ORIGINS" in caplog.text
    assert "https://second.kz" in caplog.text


def test_an_unknown_platform_code_stops_the_service_at_start():
    """В отличие от забытой настройки, опечатка в коде площадки роняет сервис:
    такую строку отвергнет `CHECK` в базе, и падать это начнёт на каждой
    записи учителя — на доступе, прогрессе, попытке теста."""
    cfg = Settings(
        cors_origins=["https://lms.kz"],
        platform_origins={"https://lms.kz": "p3"},
    )
    with pytest.raises(RuntimeError) as failed:
        check_platform_origins(cfg)
    message = str(failed.value)
    assert "PLATFORM_ORIGINS" in message
    assert "p3" in message
    assert DEFAULT_PLATFORM in message


def test_the_shipped_platform_defaults_do_not_raise():
    """Голые умолчания — рабочая конфигурация: проверка не мешает старту."""
    check_platform_origins(Settings())
    check_platform_origins(get_settings())
