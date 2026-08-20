"""Проверка провайдеров при старте.

Опечатка в имени провайдера раньше доживала до первого запроса: сервис
поднимался, отвечал на health и падал `ValueError` на каждой заявке
учителя. Хуже того, `SMS_PROVIDER` не читался вообще — настройка обещала
отправку, а код входа продолжал уходить в лог заглушкой.
"""

import pytest

from app.config import PROVIDERS, TELEGRAM_UPDATES_MODES, check_providers, get_settings


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
