"""Схемы ассистента — отдельным файлом, а не в `schemas.py`.

Причина простая и механическая: `schemas.py` — файл на две с лишним тысячи
строк, и в работе над ассистентом в него пишут сразу несколько человек.
Двое в одном файле дают потерянные куски, которых не видит ни один тест,
поэтому весь ассистент складывает свои схемы сюда.

Формы ответов зафиксированы примерами в `CONTRACT.md`: фронт следующей
сессии работает от них и не гадает.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.application.assistant_chat import MESSAGE_MAX, TITLE_MAX


class AssistantThreadOut(BaseModel):
    id: int
    # Пока ассистент не написал свой — начало первого сообщения
    title: str
    course_id: int | None
    archived: bool
    created_at: datetime
    updated_at: datetime


class AssistantThreadsOut(BaseModel):
    items: list[AssistantThreadOut]


class AssistantThreadCreateIn(BaseModel):
    model_config = {"extra": "forbid"}

    # Курс можно закрепить сразу: так открывается чат с карточки курса
    course_id: int | None = None


class AssistantThreadPatchIn(BaseModel):
    model_config = {"extra": "forbid"}

    title: str | None = Field(default=None, max_length=TITLE_MAX)
    course_id: int | None = None
    archived: bool | None = None


class AssistantProposalOut(BaseModel):
    """Предложение опасного действия: то, что админ читает перед кнопкой.

    Аргументы наружу не уходят вовсе — их место в строке базы. Кнопка
    посылает только `id`, иначе подтверждение подтверждало бы не то,
    что было показано (ASSISTANT_BRIEF, раздел 6).
    """

    id: int
    tool_name: str
    what_happens: str
    status: Literal["waiting", "executed", "expired", "cancelled"]
    expires_at: datetime
    executed_at: datetime | None
    result: dict[str, Any] | None


class AssistantMessageOut(BaseModel):
    id: int
    role: Literal["user", "assistant", "tool"]
    content: str
    # Заполнены только у role=tool: экран рисует свёрнутую карточку вызова
    tool_name: str | None
    tool_args: dict[str, Any] | None
    # У инструмента с персданными здесь пометка, а не карточка (раздел 5)
    tool_result: dict[str, Any] | None
    proposal: AssistantProposalOut | None
    tokens_in: int
    tokens_out: int
    created_at: datetime


class AssistantMessagesOut(BaseModel):
    items: list[AssistantMessageOut]


class AssistantSayIn(BaseModel):
    model_config = {"extra": "forbid"}

    text: str = Field(max_length=MESSAGE_MAX)


class AssistantTurnOut(BaseModel):
    """Событие `done` стрима: ход закончен, вот лента целиком.

    Лента целиком, а не приращением: за один ход в неё добавляется от одной
    до одиннадцати строк, и склеивать их на фронте дороже, чем перерисовать.
    Форма пережила включение стрима — поменялся способ доставки, а не ответ.

    Расход отдельными полями, хотя он же лежит в последнем сообщении ленты:
    экран показывает цену хода, не пересобирая ленту (ASSISTANT_BRIEF, раздел 9).
    """

    thread: AssistantThreadOut
    items: list[AssistantMessageOut]
    stopped_at_cap: bool
    tokens_in: int = 0
    tokens_out: int = 0


class AssistantGrantIn(BaseModel):
    model_config = {"extra": "forbid"}

    user_id: int
    level: Literal["read", "write"]


class AssistantGrantOut(BaseModel):
    """Допуск. Имени человека здесь нет и не будет: чип в шапке чата
    подписывает браузер обычной ручкой `/admin/teachers`."""

    subject_user_id: int
    level: Literal["read", "write"]
    granted_at: datetime
    expires_at: datetime


class AssistantGrantsOut(BaseModel):
    items: list[AssistantGrantOut]


class AssistantExecuteOut(BaseModel):
    proposal: AssistantProposalOut
    result: dict[str, Any]


class AssistantMemoryItemOut(BaseModel):
    id: int
    name: str
    description: str
    scope: Literal["global", "course"]
    course_id: int | None
    author_id: int
    created_at: datetime
    updated_at: datetime


class AssistantMemoryOut(BaseModel):
    """Экран памяти: индекс целиком. Тела записей здесь нет — их читает
    сам ассистент по одной, и на экране они открываются отдельно."""

    items: list[AssistantMemoryItemOut]


class AssistantDraftReplyOut(BaseModel):
    """Черновик ответа учителю (ASSISTANT_BRIEF, раздел 11).

    `built_from` — строка «из чего собрано»: чего в курсе не было, видно
    админу. Это не вопрос ему и не требование что-то заполнить.
    """

    text: str
    built_from: str
