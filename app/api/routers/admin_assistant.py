"""Ручки ассистента. Слой тонкий: разбор запроса, вызов сценария,
сериализация — логика в `app/application/assistant_chat.py`.

Все ручки за `get_current_admin`, там же, где обычные админские: отдельной
авторизации у ассистента нет и своей копии проверок тоже (`ASSISTANT_BRIEF`,
раздел 1). Плюс `require_assistant`: без ключа провайдера страницы нет,
и ручки отвечают `assistant_off`, а не молчат — из схемы они не исчезают
никогда, потому что из неё фронт генерирует типы.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas_assistant import (
    AssistantExecuteOut,
    AssistantGrantIn,
    AssistantGrantOut,
    AssistantGrantsOut,
    AssistantMemoryOut,
    AssistantMessagesOut,
    AssistantSayIn,
    AssistantThreadCreateIn,
    AssistantThreadOut,
    AssistantThreadPatchIn,
    AssistantThreadsOut,
    AssistantTurnOut,
)
from app.application.assistant_chat import AssistantChatService
from app.application.assistant_memory import MemoryService

# Порядок проверок важен: сначала «кто спрашивает», потом «есть ли ассистент».
# Иначе посторонний, ткнувшийся в адрес, узнаёт про настройки этой установки
# раньше, чем получит отказ по входу.
router = APIRouter(
    prefix="/admin/assistant",
    dependencies=[Depends(deps.get_current_admin), Depends(deps.require_assistant)],
)


@router.get("/threads")
def threads(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[AssistantChatService, Depends(deps.get_assistant_chat_service)],
    archived: Annotated[bool, Query()] = False,
) -> AssistantThreadsOut:
    # Свои и только свои: чужие чаты не существуют, а не «скрыты»
    return AssistantThreadsOut(items=svc.threads(admin, archived=archived))


@router.post("/threads", status_code=status.HTTP_201_CREATED)
def create_thread(
    body: AssistantThreadCreateIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[AssistantChatService, Depends(deps.get_assistant_chat_service)],
) -> AssistantThreadOut:
    return AssistantThreadOut(**svc.create(admin, course_id=body.course_id))


@router.patch("/threads/{thread_id}")
def patch_thread(
    thread_id: int,
    body: AssistantThreadPatchIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[AssistantChatService, Depends(deps.get_assistant_chat_service)],
) -> AssistantThreadOut:
    # exclude_unset: «поля нет» и «прислали null» — разные случаи, снятие
    # закреплённого курса это как раз null
    return AssistantThreadOut(**svc.patch(admin, thread_id, body.model_dump(exclude_unset=True)))


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_thread(
    thread_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[AssistantChatService, Depends(deps.get_assistant_chat_service)],
) -> None:
    svc.delete(admin, thread_id)


@router.get("/threads/{thread_id}/messages")
def messages(
    thread_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[AssistantChatService, Depends(deps.get_assistant_chat_service)],
) -> AssistantMessagesOut:
    return AssistantMessagesOut(items=svc.messages(admin, thread_id))


@router.post("/threads/{thread_id}/messages")
def say(
    thread_id: int,
    body: AssistantSayIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[AssistantChatService, Depends(deps.get_assistant_chat_service)],
) -> AssistantTurnOut:
    # Ответ целиком: стрим включает сессия B, форма ответа при этом остаётся
    turn = svc.say(admin, thread_id, body.text)
    return AssistantTurnOut(
        thread=turn["thread"], items=turn["messages"], stopped_at_cap=turn["stopped_at_cap"]
    )


@router.get("/threads/{thread_id}/grants")
def grants(
    thread_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[AssistantChatService, Depends(deps.get_assistant_chat_service)],
) -> AssistantGrantsOut:
    # Чипы в шапке чата после перезагрузки страницы: имена к ним подставляет
    # браузер обычной ручкой /admin/teachers, а не эта
    return AssistantGrantsOut(items=svc.thread_grants(admin, thread_id))


@router.post("/threads/{thread_id}/grants", status_code=status.HTTP_201_CREATED)
def grant(
    thread_id: int,
    body: AssistantGrantIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[AssistantChatService, Depends(deps.get_assistant_chat_service)],
) -> AssistantGrantOut:
    # Клик админа по строке и есть выдача допуска: подтверждения у неё нет,
    # потому что её делает человек, а не модель
    return AssistantGrantOut(**svc.grant(admin, thread_id, body.user_id, body.level))


@router.delete(
    "/threads/{thread_id}/grants/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
def revoke_grant(
    thread_id: int,
    user_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[AssistantChatService, Depends(deps.get_assistant_chat_service)],
) -> None:
    svc.revoke(admin, thread_id, user_id)


@router.post("/proposals/{proposal_id}/execute")
def execute(
    proposal_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[AssistantChatService, Depends(deps.get_assistant_chat_service)],
) -> AssistantExecuteOut:
    # Тела у запроса нет вовсе, и это главное: аргументы берутся из строки
    # предложения, иначе подтверждение подтверждает не то, что было показано
    result = svc.execute(admin, proposal_id)
    return AssistantExecuteOut(proposal=result["proposal"], result=result["result"])


@router.post("/proposals/{proposal_id}/cancel")
def cancel(
    proposal_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[AssistantChatService, Depends(deps.get_assistant_chat_service)],
) -> AssistantExecuteOut:
    # Кнопка «Отмена» у карточки: без неё предложение доживает до конца часа
    # и остаётся нажимаемым после перезагрузки страницы
    result = svc.cancel(admin, proposal_id)
    return AssistantExecuteOut(proposal=result["proposal"], result={})


@router.get("/memory")
def memory(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[MemoryService, Depends(deps.get_memory_service)],
) -> AssistantMemoryOut:
    # Индекс целиком, вместе с записями курсов: экран памяти в админке
    # показывает всё, а в промпт уходит только общее плюс закреплённый курс
    return AssistantMemoryOut(items=svc.screen())
