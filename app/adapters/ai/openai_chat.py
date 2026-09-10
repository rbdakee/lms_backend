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
from collections.abc import Iterator

from openai import OpenAI

from app.application.ports import AiDelta, AiMessage, AiReply, AiToolCall
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
        # Срок жизни кэша промпта. Системный промпт с реестром — это почти
        # весь вход запроса, и он один и тот же во всех чатах: чем дольше
        # живёт кэш, тем реже за него платят полную цену. Пустая настройка
        # значит «не посылать параметр»: с Zero Data Retention он отобьётся.
        self._cache = (
            {"prompt_cache_retention": cfg.openai_cache_retention}
            if cfg.openai_cache_retention
            else {}
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
                **self._cache,
                **({"tools": tools, "tool_choice": "auto"} if tools else {}),
            )
        except Exception as exc:
            # Ключ не попадает ни в лог, ни в текст ошибки: репозитории
            # публичные, а лог читают не только мы
            log.error("OpenAI не ответил: %s: %s", type(exc).__name__, self._safe(str(exc)))
            raise AiUnavailableError("Модель не ответила") from None
        return _reply(resp)

    def stream(
        self,
        *,
        system: str,
        messages: list[AiMessage],
        tools: list[dict] | None = None,
    ) -> Iterator[AiDelta]:
        """Тот же запрос, но ответ приходит кусками.

        Два места, где стрим не похож на обычный вызов, и оба стоили бы дорого,
        если их пропустить:

        1. **Расход надо просить.** Без `stream_options.include_usage`
           провайдер в стриме не присылает расход вовсе — токены молча стали бы
           нулями, и счёт за месяц объяснить было бы нечем (раздел 9).
        2. **Аргументы вызова приходят по буквам.** У куска есть `index`, и
           куски одного вызова надо склеивать по нему, а не по порядку прихода.
        """
        payload = [{"role": "system", "content": system}]
        payload += [_wire(message) for message in messages]
        try:
            chunks = self.client.chat.completions.create(
                model=self.model,
                messages=payload,
                stream=True,
                stream_options={"include_usage": True},
                **self._cache,
                **({"tools": tools, "tool_choice": "auto"} if tools else {}),
            )
        except Exception as exc:
            log.error("OpenAI не ответил: %s: %s", type(exc).__name__, self._safe(str(exc)))
            raise AiUnavailableError("Модель не ответила") from None

        parts: list[str] = []
        calls: dict[int, dict] = {}
        tokens_in = tokens_out = 0
        # `next` вручную, а не `for`: обычный цикл пришлось бы завернуть
        # в `except Exception`, и тогда исключение, брошенное потребителем
        # в точку `yield`, тоже превратилось бы в «модель не ответила»
        source = iter(chunks)
        while True:
            try:
                chunk = next(source)
            except StopIteration:
                break
            except Exception as exc:
                log.error(
                    "OpenAI оборвал ответ: %s: %s", type(exc).__name__, self._safe(str(exc))
                )
                raise AiUnavailableError("Модель не ответила") from None

            usage = getattr(chunk, "usage", None)
            if usage is not None:
                tokens_in, tokens_out = _usage(usage)
            if not chunk.choices:
                # Служебное событие с расходом приходит без вариантов ответа
                continue
            delta = chunk.choices[0].delta
            for call in delta.tool_calls or []:
                slot = calls.setdefault(call.index, {"id": "", "name": "", "args": ""})
                if call.id:
                    slot["id"] = call.id
                if call.function is not None:
                    if call.function.name:
                        slot["name"] = call.function.name
                    if call.function.arguments:
                        slot["args"] += call.function.arguments
            if delta.content:
                parts.append(delta.content)
                yield AiDelta(text=delta.content)

        yield AiDelta(
            reply=AiReply(
                text="".join(parts),
                tool_calls=tuple(
                    AiToolCall(id=slot["id"], name=slot["name"], arguments=_args(slot["args"]))
                    for _, slot in sorted(calls.items())
                    if slot["name"]
                ),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        )

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
    tokens_in, tokens_out = _usage(resp.usage)
    return AiReply(
        text=choice.content or "",
        tool_calls=tuple(calls),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def _usage(usage) -> tuple[int, int]:
    """Расход запроса, попутно в лог — сколько входа взято из кэша промпта.

    В базу `cached_tokens` не пишется: в ленте чата ему делать нечего,
    а нужен он ровно затем, чтобы видеть, работает ли кэш, и объяснять
    счёт за месяц. Поле необязательное — за шлюзом его может не быть вовсе,
    и тогда ноль значит «не сказали», а не «кэш не сработал».
    """
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    log.info(
        "OpenAI: вход %d, из кэша %d, выход %d",
        tokens_in,
        getattr(details, "cached_tokens", 0) or 0,
        tokens_out,
    )
    return tokens_in, tokens_out


def _args(raw: str) -> dict:
    """Аргументы приходят строкой JSON, и модель изредка присылает мусор.
    Разбор здесь, а не в цикле: пустой словарь дальше отобьёт проверка
    аргументов, и модель прочитает внятный отказ вместо падения хода."""
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
