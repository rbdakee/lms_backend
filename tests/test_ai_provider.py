"""Провайдер ИИ: OpenAI по умолчанию, GLM (Z.ai) — только явной строкой.

Сеть не нужна: вызов провайдера подменяется на записывающий, и проверяется
ровно то, что ушло бы в запрос. Ключи в тестах вымышленные.
"""

import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.ai.openai_chat import AiUnavailableError, OpenAiChat
from app.adapters.ai.provider import ai_chat
from app.adapters.ai.zai_chat import ZaiChat
from app.application.ports import AiMessage
from app.config import Settings, check_providers, check_zai

OPENAI_KEY = "sk-test-openai-not-real"
ZAI_KEY = "zai-test-not-real"
SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}
HELLO = [AiMessage(role="user", content="Привет")]


# Настройки ИИ из окружения машины, где идут тесты. Там бывает настоящий ключ,
# и попади он в настройки — упавший тест напечатал бы его в выводе
AI_ENV = (
    "LESSON_AI_PROVIDER",
    "ZAI_API_KEY",
    "ZAI_BASE_URL",
    "ZAI_MODEL",
    "LESSON_AI_ZAI_MODEL",
    "ZAI_THINKING",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_CACHE_RETENTION",
    "LESSON_AI_MODEL",
)


@pytest.fixture(autouse=True)
def _no_ai_env(monkeypatch):
    for name in AI_ENV:
        monkeypatch.delenv(name, raising=False)


def _cfg(**kw) -> Settings:
    # Без .env разработчика: в нём бывает свой провайдер и настоящий ключ
    return Settings(_env_file=None, openai_api_key=OPENAI_KEY, **kw)


def _zai(**kw) -> Settings:
    base = {"lesson_ai_provider": "zai", "zai_api_key": ZAI_KEY, "zai_model": "glm-4.7"}
    return _cfg(**{**base, **kw})


def _usage(tokens_in=7, tokens_out=3):
    return SimpleNamespace(
        prompt_tokens=tokens_in,
        completion_tokens=tokens_out,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )


def _reply(text: str):
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=_usage())


def _record(chat, result) -> list[dict]:
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        return result

    chat.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return calls


def test_openai_is_the_default_and_nothing_changes_for_it():
    """Без настроек провайдера всё как до подготовки GLM: тот же адрес, тот же ключ,
    строгая схема, кэш промпта и расход в стриме."""
    cfg = _cfg()
    assert cfg.lesson_ai_provider == "openai"
    chat = ai_chat(cfg, cfg.lesson_ai_provider)
    assert type(chat) is OpenAiChat
    assert str(chat.client.base_url).startswith("https://api.openai.com/")
    assert chat.model == "gpt-4.1"
    assert cfg.lesson_model == cfg.lesson_ai_model

    calls = _record(chat, _reply('{"title": "x"}'))
    chat.complete_json(system="S", messages=HELLO, name="plan", schema=SCHEMA)
    sent = calls[0]
    assert sent["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "plan", "schema": SCHEMA, "strict": True},
    }
    assert sent["prompt_cache_retention"] == "24h"
    assert sent["messages"][0] == {"role": "system", "content": "S"}
    assert "extra_body" not in sent

    calls = _record(chat, iter([]))
    list(chat.stream(system="S", messages=HELLO))
    assert calls[0]["stream_options"] == {"include_usage": True}


def test_zai_is_picked_by_config_and_goes_to_the_general_api():
    cfg = _zai()
    chat = ai_chat(cfg, cfg.lesson_ai_provider)
    assert type(chat) is ZaiChat
    assert str(chat.client.base_url) == "https://api.z.ai/api/paas/v4/"
    assert chat.client.api_key == ZAI_KEY
    assert chat.model == "glm-4.7"


def test_zai_json_goes_as_json_object_with_the_schema_in_the_prompt():
    """Строгой схемы у Z.ai нет: схема едет текстом в конце системного
    промпта, а OpenAI-параметров кэша в запросе нет вовсе."""
    chat = ai_chat(_zai(), "zai")
    calls = _record(chat, _reply('{"title": "x"}'))
    reply = chat.complete_json(system="S", messages=HELLO, name="plan", schema=SCHEMA)
    sent = calls[0]
    assert sent["response_format"] == {"type": "json_object"}
    system = sent["messages"][0]["content"]
    assert system.startswith("S\n\n")
    assert '"additionalProperties": false' in system
    assert "prompt_cache_retention" not in sent
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}}
    assert reply.text == '{"title": "x"}'
    assert (reply.tokens_in, reply.tokens_out) == (7, 3)


def test_zai_thinking_can_be_left_to_the_model():
    chat = ai_chat(_zai(zai_thinking=""), "zai")
    calls = _record(chat, _reply("ok"))
    chat.complete(system="S", messages=HELLO)
    assert "extra_body" not in calls[0]


def test_zai_stream_takes_usage_from_the_last_chunk_without_stream_options():
    """У Z.ai расход приходит последним куском сам, и в нём же есть `choices`
    с пустой дельтой — расход при этом терять нельзя."""
    delta = SimpleNamespace(content="Привет", tool_calls=None)
    last = SimpleNamespace(content="", tool_calls=None)
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None),
        SimpleNamespace(choices=[SimpleNamespace(delta=last)], usage=_usage(11, 5)),
    ]
    chat = ai_chat(_zai(), "zai")
    calls = _record(chat, iter(chunks))
    events = list(chat.stream(system="S", messages=HELLO))
    assert "stream_options" not in calls[0]
    final = events[-1].reply
    assert final.text == "Привет"
    assert (final.tokens_in, final.tokens_out) == (11, 5)


def test_the_openai_key_does_not_switch_glm_on():
    """При `zai` ключ OpenAI не считается: иначе «Жоспар» «был бы включён»,
    а ходил бы к Z.ai с чужим ключом."""
    assert _cfg(zai_model="glm-4.7").ai_api_key("zai") == ""
    assert not _cfg(lesson_ai_provider="zai", lesson_ai_enabled=True).lesson_ai_on
    assert _zai(lesson_ai_enabled=True).lesson_ai_on


def test_one_product_on_zai_does_not_need_the_others_model():
    """«Жоспару» на GLM хватает своей модели: общий ZAI_MODEL от него не требуется."""
    check_zai(_cfg(lesson_ai_provider="zai", zai_api_key=ZAI_KEY, lesson_ai_zai_model="glm-4.7"))


def test_lesson_model_follows_the_provider():
    assert _zai().lesson_model == "glm-4.7"
    assert _zai(lesson_ai_zai_model="glm-5").lesson_model == "glm-5"
    assert _cfg(lesson_ai_zai_model="glm-5").lesson_model == "gpt-4.1"
    assert _zai(lesson_ai_provider="openai").lesson_model == "gpt-4.1"


def test_a_typo_in_ai_provider_stops_the_service():
    with pytest.raises(RuntimeError, match="LESSON_AI_PROVIDER"):
        check_providers(_cfg(lesson_ai_provider="glm"))


def test_zai_with_a_key_but_no_model_stops_the_service():
    """Модели по умолчанию нет — её выбирает владелец. Текст отказа ключа
    не содержит."""
    with pytest.raises(RuntimeError, match="ZAI_MODEL") as failed:
        check_zai(_zai(zai_model=""))
    assert ZAI_KEY not in str(failed.value)


def test_zai_without_a_key_is_a_warning_not_a_failure(caplog):
    with caplog.at_level(logging.WARNING):
        check_zai(_cfg(lesson_ai_provider="zai"))
    assert "ZAI_API_KEY" in caplog.text


def test_an_unknown_thinking_mode_stops_the_service():
    with pytest.raises(RuntimeError, match="ZAI_THINKING"):
        check_zai(_cfg(zai_thinking="on"))


def test_the_shipped_defaults_pass_the_ai_checks():
    check_zai(Settings(_env_file=None))


def test_the_zai_key_never_reaches_the_log(caplog):
    """Провайдер кладёт в текст ошибки что угодно, вплоть до ключа."""
    chat = ai_chat(_zai(), "zai")

    def create(**kwargs):
        raise RuntimeError(f"401 invalid key {ZAI_KEY}")

    chat.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with caplog.at_level(logging.ERROR), pytest.raises(AiUnavailableError) as failed:
        chat.complete(system="S", messages=HELLO)
    assert ZAI_KEY not in caplog.text
    assert ZAI_KEY not in str(failed.value)
    assert "Z.ai не ответил" in caplog.text


def test_every_ai_client_is_built_through_the_provider_switch():
    """Ассистент, запросы «Жоспара» у экрана и воркер генерации — все через
    `ai_chat`. Клиент, собранный в обход, остался бы на OpenAI после
    переключения, и счёт пришёл бы от провайдера, которого уже не ждут."""
    root = Path(__file__).resolve().parents[1] / "app"
    allowed = {"adapters/ai/openai_chat.py", "adapters/ai/zai_chat.py", "adapters/ai/provider.py"}
    bypass = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if str(path.relative_to(root)) not in allowed
        and re.search(r"\b(OpenAiChat|ZaiChat|OpenAI)\(", path.read_text(encoding="utf-8"))
    ]
    assert bypass == []
