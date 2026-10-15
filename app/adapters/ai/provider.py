"""Выбор адаптера ИИ — конфигурацией, а не `if` в месте вызова.

Провайдер «Жоспара» выбирает `LESSON_AI_PROVIDER`. Клиентов у «Жоспара» два —
запросы у экрана и воркер генерации, — и оба берут адаптер отсюда с одним
провайдером: выбор не может разъехаться между ними. Что значения известные, проверено при старте
(`check_providers`).
"""

from app.adapters.ai.openai_chat import OpenAiChat
from app.adapters.ai.zai_chat import ZaiChat
from app.application.ports import AiPort
from app.config import Settings

ADAPTERS = {"openai": OpenAiChat, "zai": ZaiChat}


def ai_chat(cfg: Settings, provider: str, *, model: str = "", timeout_sec: int = 0) -> AiPort:
    return ADAPTERS[provider](cfg, model=model, timeout_sec=timeout_sec)
