"""Цикл вызовов ассистента — общий `ToolLoop` с реестром админских действий.

Сам цикл живёт в `app/application/ai_loop.py`: на нём же работает «Жоспар»,
а реестр в учительский продукт не импортируется (TEACHER_AI_BRIEF, раздел 6).
Здесь только привязка: какие инструменты видит модель ассистента и что она
читает, упёршись в потолок. Прежние имена реэкспортированы — `assistant_chat`
и тесты берут их отсюда, как брали.
"""

from app.application.ai_loop import AiStreamBrokenError, LoopEvent, LoopResult, Step, ToolLoop
from app.application.ports import AiPort
from app.assistant import registry
from app.assistant.registry import ToolCtx, ToolError

__all__ = [
    "CAP_MESSAGE",
    "AiStreamBrokenError",
    "AssistantLoop",
    "LoopEvent",
    "LoopResult",
    "Step",
]

CAP_MESSAGE = (
    "Потолок вызовов инструментов за один ход исчерпан. Остановись: скажи"
    " администратору, что успел выяснить и на чём именно встал, и предложи"
    " продолжить следующим сообщением. Не делай вид, что закончил."
)


class _Registry:
    """Реестр в том виде, в каком его ждёт цикл.

    Отказ инструмента (`ToolError`) — обычный результат, который читает
    модель: сам запрос чата удался, и правило «ошибку показывает как есть»
    держится на этом.
    """

    def schemas(self) -> list[dict]:
        return registry.openai_tools()

    def call(self, name: str, args: dict, ctx: ToolCtx) -> dict:
        try:
            return registry.call(name, args, ctx)
        except ToolError as exc:
            return exc.as_result()


class AssistantLoop(ToolLoop):
    def __init__(self, ai: AiPort, max_tool_calls: int):
        super().__init__(ai, max_tool_calls, tools=_Registry(), cap_message=CAP_MESSAGE)
