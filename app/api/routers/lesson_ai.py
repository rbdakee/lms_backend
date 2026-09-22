"""«Жоспар» — помощник учителя. Слой тонкий: разбор запроса, вызов сценария,
сериализация — логика в `app/application/lesson_kits.py`.

Кука — учительская, площадки: продукт один на обе, и учитель в нём один
и тот же человек (TEACHER_AI_BRIEF, раздел 8). Проверки доступа к продукту
здесь пока нет — она приходит с `lesson_ai_access` в сессии L7, а до тех пор
продукт закрыт выключателем `LESSON_AI_ENABLED`.
"""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Response

from app.adapters import lesson_files
from app.adapters.db.models import User
from app.adapters.docx.common import DocHeader
from app.api import deps
from app.api.schemas_lesson_ai import (
    LessonBlockEditIn,
    LessonBlockOut,
    LessonBlockRegenerateIn,
    LessonBlocksOut,
    LessonBlockVersionsOut,
    LessonKitIn,
    LessonKitOut,
)
from app.application.lesson_kits import LessonKitsService
from app.application.lesson_materials import LessonMaterialsService, MaterialFile

router = APIRouter(prefix="/lesson_ai", dependencies=[Depends(deps.require_lesson_ai)])


@router.post("/kits", status_code=201)
def create_kit(
    body: LessonKitIn,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[LessonKitsService, Depends(deps.get_lesson_kits_service)],
) -> LessonKitOut:
    return LessonKitOut(
        **svc.create(
            user,
            objective_id=body.objective_id,
            topic=body.topic,
            kinds=list(body.kinds),
            class_size=body.class_size,
            notes=body.notes,
            engagement_level=body.engagement_level,
            difficulty_level=body.difficulty_level,
        )
    )


@router.get("/kits/{kit_id}")
def kit(
    kit_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[LessonKitsService, Depends(deps.get_lesson_kits_service)],
) -> LessonKitOut:
    return LessonKitOut(**svc.kit(user, kit_id))


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
