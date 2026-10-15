"""«Жоспар» — помощник учителя. Слой тонкий: разбор запроса, вызов сценария,
сериализация — логика в `app/application/lesson_kits.py`.

Кука — учительская, площадки: продукт один на обе, и учитель в нём один
и тот же человек (TEACHER_AI_BRIEF, раздел 8).

Каждая ручка продукта — за действующим доступом (`lesson_ai_access`, L7):
зависимость стоит на роутере, и ручка, которую забыли закрыть, закрыта сама.
Вне его — одна `GET /lesson_ai/access`: по ней экран узнаёт, пускать ли
учителя внутрь, и отказом она отвечать не может.
"""

import json
from collections.abc import Iterator
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session as DbSession

from app.adapters import lesson_files, lesson_preview
from app.adapters.db.base import get_engine
from app.adapters.db.lesson_ai_repos import (
    LessonKitRepo,
    LessonMaterialRepo,
    record_objectives_event,
)
from app.adapters.db.models import User
from app.adapters.docx.common import DocHeader
from app.api import deps
from app.api.pagination import PageParams, page_out
from app.api.schemas_lesson_ai import (
    LessonAccessStatusOut,
    LessonBlockEditIn,
    LessonBlockOut,
    LessonBlockRegenerateIn,
    LessonBlocksOut,
    LessonBlockVersionsOut,
    LessonChatConfirmIn,
    LessonChatConfirmOut,
    LessonChatFeedOut,
    LessonChatOrderPatchIn,
    LessonChatOut,
    LessonChatSayIn,
    LessonChatsPageOut,
    LessonChatTurnOut,
    LessonKitIn,
    LessonKitObjectivesIn,
    LessonKitOut,
    LessonKitRegenerateIn,
    LessonKitsPageOut,
    LessonObjectivesOut,
    LessonSubjectsOut,
)
from app.application.lesson_ai_access import LessonAccessService
from app.application.lesson_ai_chat import LessonChatService
from app.application.lesson_kits import LessonKitsService
from app.application.lesson_materials import LessonMaterialsService, MaterialFile
from app.application.ports import AiPort
from app.application.ratelimit import SlidingWindowLimiter
from app.config import get_settings

router = APIRouter(
    prefix="/lesson_ai",
    dependencies=[Depends(deps.require_lesson_ai), Depends(deps.require_lesson_ai_access)],
)
access_router = APIRouter(prefix="/lesson_ai", dependencies=[Depends(deps.require_lesson_ai)])


@access_router.get("/access")
def access(
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[LessonAccessService, Depends(deps.get_lesson_access_service)],
) -> LessonAccessStatusOut:
    return LessonAccessStatusOut(**svc.status(user))


@router.post("/kits", status_code=201)
def create_kit(
    body: LessonKitIn,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[LessonKitsService, Depends(deps.get_lesson_kits_service)],
) -> LessonKitOut:
    return LessonKitOut(
        **svc.create(
            user,
            objective_ids=body.objective_ids,
            topic=body.topic,
            kinds=list(body.kinds),
            class_size=body.class_size,
            notes=body.notes,
            engagement_level=body.engagement_level,
            difficulty_level=body.difficulty_level,
        )
    )


@router.get("/kits")
def kits(
    user: Annotated[User, Depends(deps.get_current_user)],
    params: Annotated[PageParams, Depends()],
    svc: Annotated[LessonKitsService, Depends(deps.get_lesson_kits_service)],
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> LessonKitsPageOut:
    data = svc.my_kits(user, q=q, offset=params.offset, limit=params.per_page)
    return LessonKitsPageOut(**page_out(data["items"], data["total"], params))


@router.get("/kits/{kit_id}")
def kit(
    kit_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[LessonKitsService, Depends(deps.get_lesson_kits_service)],
) -> LessonKitOut:
    return LessonKitOut(**svc.kit(user, kit_id))


@router.put("/kits/{kit_id}/objectives")
def set_kit_objectives(
    kit_id: int,
    body: LessonKitObjectivesIn,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[LessonKitsService, Depends(deps.get_lesson_kits_service)],
) -> LessonKitOut:
    """Кнопка «Изменить цели» на экране; тот же сценарий, что у инструмента чата."""
    out = svc.set_objectives(user, kit_id, body.objective_ids)
    # Событие в ленту чата комплекта — как у правки из чата: плашка и обновление экрана
    record_objectives_event(svc.kits.db, kit_id, out["objectives"])
    return LessonKitOut(**out)


@router.post("/kits/{kit_id}/regenerate")
def regenerate_kit(
    kit_id: int,
    body: LessonKitRegenerateIn,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[LessonKitsService, Depends(deps.get_lesson_kits_service)],
) -> LessonKitOut:
    """Кнопка «Пересобрать урок»; тот же сценарий, что у инструмента чата.
    Комплект уже собирается — 409, суточный лимит исчерпан — 429."""
    return LessonKitOut(**svc.regenerate(user, kit_id, body.kinds))


# -- Материал: файл, блоки, история (сессии L4 и L5) ----------------------

Materials = Annotated[LessonMaterialsService, Depends(deps.get_lesson_materials_service)]
CurrentUser = Annotated[User, Depends(deps.get_current_user)]


@router.get(
    "/materials/{material_id}/file",
    response_class=Response,
    responses={
        200: {
            "content": {media: {} for media in lesson_files.MEDIA_TYPES.values()},
            "description": "Документ Word, у презентации — PowerPoint",
        }
    },
)
def material_file(material_id: int, user: CurrentUser, svc: Materials) -> Response:
    # Сборка документа из данных сценария — сериализация ответа, как PDF
    # сертификата: свести содержимое в байты роутеру можно
    file = svc.file(user, material_id)
    header = DocHeader(
        lang=file.lang, teacher_name=file.teacher_name, grade=file.grade, topic=file.topic
    )
    return Response(
        lesson_files.render(file.kind, file.content, header),
        media_type=lesson_files.MEDIA_TYPES[file.format],
        headers={"content-disposition": _disposition(file)},
    )


@router.get(
    "/materials/{material_id}/preview",
    response_class=HTMLResponse,
    responses={200: {"content": {"text/html": {}}, "description": "Страница просмотра"}},
)
def material_preview(material_id: int, user: CurrentUser, svc: Materials) -> HTMLResponse:
    """Тот же файл, что отдаёт `/file`, развёрнутый в HTML: учитель смотрит
    материал на сайте, не открывая Word. Собирается на каждый запрос, как
    и файл, — правка блока видна сразу, хранить нечего. Отказы те же."""
    file = svc.file(user, material_id)
    header = DocHeader(
        lang=file.lang, teacher_name=file.teacher_name, grade=file.grade, topic=file.topic
    )
    data = lesson_files.render(file.kind, file.content, header)
    return HTMLResponse(
        lesson_preview.render(file.format, data, file.lang),
        headers={
            # Скриптов в странице нет и не будет; открытая напрямую, а не во
            # фрейме экрана, она всё равно не выполнит ничего, кроме стилей.
            # Картинки презентации — data:, внешних адресов нет
            "content-security-policy": (
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
            ),
            "cache-control": "no-store",
        },
    )


def _disposition(file: MaterialFile) -> str:
    """Имя файла — «Вид — тема урока.docx» на языке обучения: учитель
    складывает материалы в одну папку, и по номеру их не различить. Имя
    в UTF-8 уходит в `filename*`, а в `filename` — запасное из ASCII для
    старых клиентов."""
    title = lesson_files.TITLES[file.lang][file.kind]
    topic = "".join(ch for ch in file.topic if ch not in '\\/:*?"<>|\r\n\t')[:80].strip()
    name = f"{title} — {topic}.{file.format}" if topic else f"{title}.{file.format}"
    fallback = f"{file.kind}_{file.kit_id}.{file.format}"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


@router.get("/materials/{material_id}/blocks")
def material_blocks(material_id: int, user: CurrentUser, svc: Materials) -> LessonBlocksOut:
    return LessonBlocksOut(**svc.blocks(user, material_id))


@router.post("/materials/{material_id}/blocks/{block}/regenerate")
def regenerate_block(
    material_id: int, block: str, body: LessonBlockRegenerateIn, user: CurrentUser, svc: Materials
) -> LessonBlockOut:
    return LessonBlockOut(**svc.regenerate(user, material_id, block, body.instruction))


@router.put("/materials/{material_id}/blocks/{block}")
def edit_block(
    material_id: int, block: str, body: LessonBlockEditIn, user: CurrentUser, svc: Materials
) -> LessonBlockOut:
    return LessonBlockOut(**svc.edit(user, material_id, block, body.content))


@router.get("/materials/{material_id}/blocks/{block}/versions")
def block_versions(
    material_id: int, block: str, user: CurrentUser, svc: Materials
) -> LessonBlockVersionsOut:
    return LessonBlockVersionsOut(**svc.versions(user, material_id, block))


@router.post("/materials/{material_id}/blocks/{block}/versions/{version_id}/restore")
def restore_block(
    material_id: int, block: str, version_id: int, user: CurrentUser, svc: Materials
) -> LessonBlockOut:
    return LessonBlockOut(**svc.restore(user, material_id, block, version_id))


# -- Чат сбора заказа (сессия L6) ---------------------------------------------

Chats = Annotated[LessonChatService, Depends(deps.get_lesson_chat_service)]


@router.post("/chats", status_code=201)
def create_chat(user: CurrentUser, svc: Chats) -> LessonChatOut:
    return LessonChatOut(**svc.create(user))


@router.get("/chats")
def chats(
    user: CurrentUser, params: Annotated[PageParams, Depends()], svc: Chats
) -> LessonChatsPageOut:
    data = svc.chats(user, offset=params.offset, limit=params.per_page)
    return LessonChatsPageOut(**page_out(data["items"], data["total"], params))


@router.get("/chats/{chat_id}")
def chat(chat_id: int, user: CurrentUser, svc: Chats) -> LessonChatOut:
    return LessonChatOut(**svc.chat(user, chat_id))


@router.get("/chats/{chat_id}/messages")
def chat_feed(
    chat_id: int,
    user: CurrentUser,
    svc: Chats,
    after_id: Annotated[int, Query(ge=0)] = 0,
) -> LessonChatFeedOut:
    return LessonChatFeedOut(**svc.feed(user, chat_id, after_id=after_id))


@router.post(
    "/chats/{chat_id}/messages",
    response_class=StreamingResponse,
    responses={
        # Модель названа, чтобы форма события `done` осталась в схеме OpenAPI:
        # из неё фронт генерирует типы
        200: {
            "model": LessonChatTurnOut,
            "description": (
                "Ход событиями `text/event-stream`: `delta`, `tool`, `material_changed`,"
                " `done`, `error`."
                " Событие `done` несёт эту форму — чат целиком."
            ),
            "content": {
                "text/event-stream": {"schema": {"$ref": "#/components/schemas/LessonChatTurnOut"}}
            },
        }
    },
)
def say(
    chat_id: int,
    body: LessonChatSayIn,
    user: CurrentUser,
    ai: Annotated[AiPort, Depends(deps.get_lesson_block_ai)],
    limiter: Annotated[SlidingWindowLimiter, Depends(deps.get_lesson_chat_limiter)],
    svc: Chats,
) -> StreamingResponse:
    """Реплика учителя и ответ помощника — стримом. Проверки входа идут
    **до** стрима и остаются обычными отказами: `404`, `409`, `422`, `429`."""
    text = svc.check_say(user, chat_id, body.text)
    return StreamingResponse(
        _turn(user.id, chat_id, text, ai, limiter),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Nginx иначе копит ответ в буфере и отдаёт его целиком
            "X-Accel-Buffering": "no",
        },
    )


def _turn(
    user_id: int, chat_id: int, text: str, ai: AiPort, limiter: SlidingWindowLimiter
) -> Iterator[str]:
    """Тело стрима — со **своей** сессией базы: зависимости с `yield`
    FastAPI закрывает до того, как отдаёт тело ответа (как у ассистента)."""
    db = DbSession(get_engine())
    try:
        cfg = get_settings()
        svc = LessonChatService(
            db,
            ai,
            kits=LessonKitsService(LessonKitRepo(db), limiter=deps.get_lesson_kits_limiter()),
            limiter=limiter,
            materials=LessonMaterialsService(
                LessonMaterialRepo(db),
                deps.lesson_block_rewriter(ai),
                db=db,
                commit=db.commit,
                model=cfg.lesson_model,
            ),
            max_tool_calls=cfg.lesson_ai_chat_max_tool_calls,
            max_edit_tool_calls=cfg.lesson_ai_edit_max_tool_calls,
            history_messages=cfg.lesson_ai_chat_history_messages,
            model=cfg.lesson_model,
        )
        user = db.get(User, user_id)
        if user is None:
            return
        for event in svc.say_stream(user, chat_id, text):
            yield _sse(event["event"], event["data"])
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _sse(event: str, data: dict) -> str:
    body = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {body}\n\n"


@router.patch("/chats/{chat_id}/order")
def patch_order(
    chat_id: int, body: LessonChatOrderPatchIn, user: CurrentUser, svc: Chats
) -> LessonChatOut:
    # exclude_unset: «поля нет» и «прислали null» — разные случаи
    return LessonChatOut(**svc.patch_order(user, chat_id, body.model_dump(exclude_unset=True)))


@router.post("/chats/{chat_id}/confirm", status_code=201)
def confirm(
    chat_id: int, body: LessonChatConfirmIn, user: CurrentUser, svc: Chats
) -> LessonChatConfirmOut:
    result = svc.confirm(
        user,
        chat_id,
        kinds=list(body.kinds),
        engagement_level=body.engagement_level,
        difficulty_level=body.difficulty_level,
        class_size=body.class_size,
        notes=body.notes,
        fields_set=frozenset(body.model_fields_set),
    )
    return LessonChatConfirmOut(**result)


@router.get("/objectives")
def objectives(
    user: CurrentUser,
    svc: Chats,
    q: Annotated[str, Query(max_length=200)] = "",
    grade: Annotated[int | None, Query(ge=1, le=12)] = None,
    lang: Annotated[Literal["ru", "kz"] | None, Query()] = None,
    subject_code: Annotated[str | None, Query(max_length=64)] = None,
) -> LessonObjectivesOut:
    # Учитель вошёл — этого достаточно: справочник государственный, а видно
    # в нём только опубликованное
    return LessonObjectivesOut(
        **svc.objectives(q=q, grade=grade, lang=lang, subject_code=subject_code)
    )


@router.get("/subjects")
def subjects(user: CurrentUser, svc: Chats) -> LessonSubjectsOut:
    return LessonSubjectsOut(**svc.subjects())
