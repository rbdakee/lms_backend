"""Ручки ассистента. Слой тонкий: разбор запроса, вызов сценария,
сериализация — логика в `app/application/assistant_chat.py`.

Все ручки за `get_current_admin`, там же, где обычные админские: отдельной
авторизации у ассистента нет и своей копии проверок тоже (`ASSISTANT_BRIEF`,
раздел 1). Плюс `require_assistant`: без ключа провайдера страницы нет,
и ручки отвечают `assistant_off`, а не молчат — из схемы они не исчезают
никогда, потому что из неё фронт генерирует типы.
"""

import json
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.base import get_engine
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
from app.application.ports import AiPort
from app.application.ratelimit import SlidingWindowLimiter
from app.config import get_settings

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


@router.post(
    "/threads/{thread_id}/messages",
    response_class=StreamingResponse,
    responses={
        # Модель названа, чтобы форма события `done` осталась в схеме OpenAPI:
        # из неё фронт генерирует типы, и стрим не должен их обнулить
        200: {
            "model": AssistantTurnOut,
            "description": (
                "Ход событиями `text/event-stream`: `delta`, `tool`, `done`, `error`."
                " Событие `done` несёт эту форму — ленту целиком и расход хода."
            ),
            "content": {
                "text/event-stream": {
                    "schema": {"$ref": "#/components/schemas/AssistantTurnOut"}
                }
            },
        }
    },
)
def say(
    thread_id: int,
    body: AssistantSayIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    ai: Annotated[AiPort, Depends(deps.get_ai)],
    limiter: Annotated[SlidingWindowLimiter, Depends(deps.get_assistant_limiter)],
    svc: Annotated[AssistantChatService, Depends(deps.get_assistant_chat_service)],
) -> StreamingResponse:
    """Сообщение админа и ответ ассистента — стримом.

    Проверки входа идут **до** стрима и остаются обычными отказами: как только
    тело потекло, статус уже отдан, и `429` с числом секунд сообщить нечем.
    """
    text = svc.check_say(admin, thread_id, body.text)
    return StreamingResponse(
        _turn(admin.id, thread_id, text, ai, limiter),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Nginx иначе копит ответ в буфере и отдаёт его целиком —
            # то есть съедает весь смысл стрима
            "X-Accel-Buffering": "no",
        },
    )


def _turn(
    admin_id: int,
    thread_id: int,
    text: str,
    ai: AiPort,
    limiter: SlidingWindowLimiter,
) -> Iterator[str]:
    """Тело стрима — со **своей** сессией базы, и это не перестраховка.

    Зависимости с `yield` (в том числе `get_db`) FastAPI закрывает **до** того,
    как отдаёт тело ответа. Сессия запроса к моменту первого события уже
    закоммичена и закрыта, и ход, написанный в неё, не сохранился бы вовсе.

    Провайдер и счётчик частоты приходят снаружи готовыми: оба — синглтоны
    приложения, и подменённые в тестах приезжают сюда теми же.
    """
    db = DbSession(get_engine())
    try:
        cfg = get_settings()
        svc = AssistantChatService(
            db,
            ai,
            memory=MemoryService(db),
            limiter=limiter,
            max_tool_calls=cfg.assistant_max_tool_calls,
            history_messages=cfg.assistant_history_messages,
        )
        admin = db.get(User, admin_id)
        if admin is None:
            return
        for event in svc.say_stream(admin, thread_id, text):
            yield _sse(event["event"], event["data"])
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _sse(event: str, data: dict) -> str:
    """Одно событие в формате `text/event-stream`.

    `ensure_ascii=False` — кириллица иначе раздувает тело вчетверо;
    переводов строки внутри `data` быть не может, их выел бы сам формат.
    """
    body = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {body}\n\n"


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
