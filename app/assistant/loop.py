"""Цикл вызовов: спросили модель — она попросила инструмент — вызвали —
спросили снова, пока не ответит текстом.

Потолок вызовов за ход — не украшение (`ASSISTANT_BRIEF`, раздел 9): цикл
без потолка на длинной задаче стоит реальных денег. Упёрлись — останавливаемся
**честно**: модель получает отказ «потолок исчерпан» и последним запросом,
уже без инструментов, говорит админу, где встала.

Клиент провайдера сюда не попадает вовсе — только порт `AiPort`.

Ход отдаётся **событиями** (`run_stream`), а не готовым результатом: карточка
вызова обязана появиться в момент вызова, иначе на длинной задаче экран молчит
полминуты (`ASSISTANT_BRIEF`, раздел 13, фаза B3).
"""

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field

from app.application.ports import AiMessage, AiPort, AiToolCall
from app.assistant import registry
from app.assistant.registry import ToolCtx, ToolError
from app.domain.errors import AppError

log = logging.getLogger(__name__)


class AiStreamBrokenError(Exception):
    """Стрим оборвался, не дойдя до ответа целиком. Ход при этом не состоялся:
    наверх это уходит тем же `ai_failed`, что и молчание провайдера."""

CAP_MESSAGE = (
    "Потолок вызовов инструментов за один ход исчерпан. Остановись: скажи"
    " администратору, что успел выяснить и на чём именно встал, и предложи"
    " продолжить следующим сообщением. Не делай вид, что закончил."
)


@dataclass
class Step:
    """Один вызов — то, что ляжет в ленту карточкой."""

    call_id: str
    name: str
    args: dict
    result: dict
    proposal_id: int | None = None


@dataclass
class LoopResult:
    text: str
    steps: list[Step] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    # Ход оборвался о потолок: это видно и админу, и в QA
    stopped_at_cap: bool = False


@dataclass
class LoopEvent:
    """Что видно снаружи по ходу дела.

    Ровно одно из трёх заполнено: `delta` — кусок текста по мере генерации,
    `step` — только что отработавший вызов, `result` — ход целиком, последним
    событием.
    """

    delta: str = ""
    step: Step | None = None
    result: LoopResult | None = None


class AssistantLoop:
    def __init__(self, ai: AiPort, max_tool_calls: int):
        self.ai = ai
        self.max_tool_calls = max_tool_calls

    def run_stream(
        self, *, system: str, history: list[AiMessage], ctx: ToolCtx
    ) -> Iterator[LoopEvent]:
        """Ход событиями: куски текста, отработавшие вызовы и результат.

        Текст, сказанный перед вызовом инструмента, в ленту не ложится —
        в ней остаётся только последний ответ. Поэтому событие `step` для
        экрана значит ещё и «то, что я успел показать, было присказкой»:
        собранный кусками черновик там сбрасывается.
        """
        tools = registry.openai_tools()
        messages = list(history)
        out = LoopResult(text="")
        used = 0
        while True:
            reply = yield from self._ask(system=system, messages=messages, tools=tools)
            out.tokens_in += reply.tokens_in
            out.tokens_out += reply.tokens_out
            if not reply.tool_calls:
                out.text = reply.text
                yield LoopEvent(result=out)
                return
            messages.append(
                AiMessage(
                    role="assistant", content=reply.text, tool_calls=tuple(reply.tool_calls)
                )
            )
            for call in reply.tool_calls:
                if used >= self.max_tool_calls:
                    out.stopped_at_cap = True
                    messages.append(_tool_message(call, {"error": {"code": "tool_call_cap"}}))
                    continue
                used += 1
                step = self._one(call, ctx)
                out.steps.append(step)
                # Карточка уходит на экран сразу, а не после всего ответа
                yield LoopEvent(step=step)
                messages.append(_tool_message(call, step.result))
            if out.stopped_at_cap:
                # Последний запрос без инструментов: модель обязана
                # закончить словами, а не новой попыткой вызвать
                final = yield from self._ask(
                    system=system,
                    messages=[*messages, AiMessage(role="user", content=CAP_MESSAGE)],
                    tools=None,
                )
                out.tokens_in += final.tokens_in
                out.tokens_out += final.tokens_out
                out.text = final.text
                yield LoopEvent(result=out)
                return

    def _ask(self, *, system: str, messages: list[AiMessage], tools: list[dict] | None):
        """Один запрос к модели: куски наружу, готовый ответ — вызывающему.

        Провайдер без стрима (подставной в тестах) — обычный `complete`:
        цикл от этого не меняется, просто кусков не будет ни одного.
        """
        stream = getattr(self.ai, "stream", None)
        if stream is None:
            return self.ai.complete(system=system, messages=messages, tools=tools)
        reply = None
        for chunk in stream(system=system, messages=messages, tools=tools):
            if chunk.reply is not None:
                reply = chunk.reply
            elif chunk.text:
                yield LoopEvent(delta=chunk.text)
        if reply is None:
            # Стрим кончился, а ответа целиком не пришло: считать это удачным
            # ходом нельзя — ход не был
            raise AiStreamBrokenError("Модель не ответила")
        return reply

    def _one(self, call: AiToolCall, ctx: ToolCtx) -> Step:
        """Один вызов вместе со всеми его бедами.

        Отказ инструмента — обычный результат, который читает модель:
        сам запрос чата удался, и правило «ошибку показывает как есть»
        держится на этом. Ошибка сценария (`AppError`) — тоже: «курс
        не найден» не повод ронять весь ход. Всё остальное — наша ошибка,
        и наружу от неё уходит общий текст: в исключении бывают данные
        запроса.
        """
        try:
            result = registry.call(call.name, call.arguments, ctx)
        except ToolError as exc:
            return Step(call.id, call.name, call.arguments, exc.as_result())
        except AppError as exc:
            return Step(
                call.id,
                call.name,
                call.arguments,
                {"error": {"code": exc.code, "message": exc.message}},
            )
        except Exception:
            log.exception("Инструмент %s упал", call.name)
            return Step(
                call.id,
                call.name,
                call.arguments,
                {"error": {"code": "tool_failed", "message": "Инструмент не отработал"}},
            )
        return Step(
            call.id,
            call.name,
            call.arguments,
            result,
            proposal_id=result.get("proposal_id") if isinstance(result, dict) else None,
        )


def _tool_message(call: AiToolCall, result: dict) -> AiMessage:
    return AiMessage(
        role="tool",
        tool_call_id=call.id,
        # ensure_ascii=False — кириллица в ответе инструмента иначе раздувает
        # счёт токенов вчетверо на пустом месте
        content=json.dumps(result, ensure_ascii=False, default=str),
    )
