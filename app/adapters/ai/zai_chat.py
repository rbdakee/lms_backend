"""Адаптер порта `AiPort` поверх GLM (Z.ai).

API у Z.ai OpenAI-совместимый, поэтому клиент тот же пакет `openai` с другим
адресом, и новой зависимости нет. Отличий от OpenAI три, и каждое сверено
с документацией docs.z.ai 25.09.2026:

1. **Ответ по схеме.** `response_format` принимает только `text` и
   `json_object` — строгого режима по JSON-схеме нет. Схема уходит текстом
   в системный промпт, а форму ответа, как и раньше, проверяет вызывающий:
   кривой ответ у него — повторяемая ошибка, а не падение.
2. **Расход в стриме** приходит последним куском сам; `stream_options`
   в API Z.ai не описан, и посылать его незачем.
3. **Кэш промпта** у Z.ai свой и без параметра: `prompt_cache_retention`
   — параметр OpenAI, и сюда он не идёт.

Плюс размышление модели (`thinking`) — см. `ZAI_THINKING` в настройках.
"""

import json

from openai import OpenAI

from app.adapters.ai.openai_chat import MediaUnsupportedError, OpenAiChat
from app.application.ports import AiMedia, AiReply
from app.config import Settings


class ZaiChat(OpenAiChat):
    label = "Z.ai"

    def __init__(self, cfg: Settings, *, model: str = "", timeout_sec: int = 0):
        self.model = model or cfg.zai_model
        self._key = cfg.zai_api_key
        self.client = OpenAI(
            api_key=cfg.zai_api_key,
            base_url=cfg.zai_base_url,
            timeout=timeout_sec or cfg.openai_timeout_sec,
            # Повтор один — по той же причине, что у OpenAI
            max_retries=1,
        )
        # `thinking` не из API OpenAI, и SDK пропускает его только в `extra_body`
        self._extra = (
            {"extra_body": {"thinking": {"type": cfg.zai_thinking}}} if cfg.zai_thinking else {}
        )
        self._stream_extra = {}

    def _json_request(self, system: str, name: str, schema: dict) -> tuple[str, dict]:
        # Схема дописывается в конец: начало системного промпта остаётся общим
        # с остальными запросами, и кэш провайдера его узнаёт
        system += (
            f"\n\nОтветь одним JSON-объектом «{name}» строго по этой JSON-схеме,"
            f" без текста вокруг:\n{json.dumps(schema, ensure_ascii=False)}"
        )
        return system, {"type": "json_object"}

    def describe_media(
        self, *, system: str, media: AiMedia, instruction: str
    ) -> AiReply:
        """GLM в OpenAI-совместимом API Z.ai content part'ы `image_url`/`file`
        не описывает (docs.z.ai, сверено 25.09.2026) — разбор вложений
        на нём не работает. Отказ понятным кодом: когда провайдер «Жоспара»
        сменится, вложения заработают без правки сценария (учителя сейчас
        на OpenAI)."""
        raise MediaUnsupportedError(f"{self.label} не читает файлы")
