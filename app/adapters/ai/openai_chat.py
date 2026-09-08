"""Адаптер порта `AiPort` поверх OpenAI.

Единственная новая зависимость этой работы (`ASSISTANT_BRIEF`, решение 9).
Всё, что знает про формат запросов провайдера, живёт здесь: `loop.py` видит
только дата-классы порта.

Заглушки у этого порта нет, в отличие от SMS и Telegram, и это осознанно:
без провайдера ассистента нет вовсе — страница не появляется, кнопки
черновика нет. Заглушка SMS нужна, потому что без неё нельзя войти;
заглушка ИИ была бы кодом, который в бою не работает никогда.
"""

import json
import logging

from openai import OpenAI

from app.application.ports import AiMessage, AiReply, AiToolCall
from app.config import Settings

log = logging.getLogger(__name__)


class AiUnavailableError(Exception):
    """Провайдер не ответил. Наружу уходит общим текстом: в исключении
    провайдера бывает и тело запроса, а в нём — переписка админа."""


class OpenAiChat:
    def __init__(self, cfg: Settings):
        self.model = cfg.openai_model
        self._key = cfg.openai_api_key
        self.client = OpenAI(
            api_key=cfg.openai_api_key,
            # Пустой адрес значит «к провайдеру напрямую»: у SDK своё
            # умолчание, и подставлять сюда пустую строку нельзя
            base_url=cfg.openai_base_url or None,
            timeout=cfg.openai_timeout_sec,
            # Повтор один: ход ассистента и так может стоить десяти вызовов,
            # и трижды повторённый каждый — это тройной счёт за месяц
            max_retries=1,
        )

    def complete(
        self,
        *,
        system: str,
        messages: list[AiMessage],
        tools: list[dict] | None = None,
    ) -> AiReply:
        payload = [{"role": "system", "content": system}]
        payload += [_wire(message) for message in messages]
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=payload,
                **({"tools": tools, "tool_choice": "auto"} if tools else {}),
            )
        except Exception as exc:
            # Ключ не попадает ни в лог, ни в текст ошибки: репозитории
            # публичные, а лог читают не только мы
            log.error("OpenAI не ответил: %s: %s", type(exc).__name__, self._safe(str(exc)))
            raise AiUnavailableError("Модель не ответила") from None
        return _reply(resp)

    def _safe(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text


def _wire(message: AiMessage) -> dict:
    """Сообщение порта в формате провайдера."""
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }
    if message.tool_calls:
        return {
            "role": "assistant",
            # content обязателен даже пустым: модель в этом ходе только
            # просила вызвать инструменты, текста у неё нет
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ],
        }
    return {"role": message.role, "content": message.content}


def _reply(resp) -> AiReply:
    choice = resp.choices[0].message
    calls = []
    for call in choice.tool_calls or []:
        calls.append(
            AiToolCall(
                id=call.id,
                name=call.function.name,
                arguments=_args(call.function.arguments),
            )
        )
    usage = resp.usage
    return AiReply(
        text=choice.content or "",
        tool_calls=tuple(calls),
        tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
        tokens_out=getattr(usage, "completion_tokens", 0) or 0,
    )


def _args(raw: str) -> dict:
    """Аргументы приходят строкой JSON, и модель изредка присылает мусор.
    Разбор здесь, а не в цикле: пустой словарь дальше отобьёт проверка
    аргументов, и модель прочитает внятный отказ вместо падения хода."""
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
