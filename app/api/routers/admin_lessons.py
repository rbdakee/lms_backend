from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import (
    AdminLessonIn,
    AdminLessonOut,
    AdminLessonPatchIn,
    LessonFileIn,
    LessonFileOut,
)
from app.application.lessons_admin import LessonsAdminService

router = APIRouter(prefix="/admin")


@router.post("/modules/{module_id}/lessons")
def create_lesson(
    module_id: int,
    body: AdminLessonIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[LessonsAdminService, Depends(deps.get_lessons_admin_service)],
) -> AdminLessonOut:
    # Ответ — редактор урока целиком: фронт сразу уводит на него
    return AdminLessonOut(**svc.create(module_id, body.model_dump()))


@router.get("/lessons/{lesson_id}")
def admin_lesson(
    lesson_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[LessonsAdminService, Depends(deps.get_lessons_admin_service)],
) -> AdminLessonOut:
    return AdminLessonOut(**svc.card(lesson_id))


@router.patch("/lessons/{lesson_id}")
def patch_lesson(
    lesson_id: int,
    body: AdminLessonPatchIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[LessonsAdminService, Depends(deps.get_lessons_admin_service)],
) -> AdminLessonOut:
    return AdminLessonOut(**svc.patch(lesson_id, body.model_dump(exclude_unset=True)))


@router.delete("/lessons/{lesson_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lesson(
    lesson_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[LessonsAdminService, Depends(deps.get_lessons_admin_service)],
) -> None:
    svc.delete(lesson_id)


@router.post("/lessons/{lesson_id}/files")
def add_lesson_file(
    lesson_id: int,
    body: LessonFileIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[LessonsAdminService, Depends(deps.get_lessons_admin_service)],
) -> LessonFileOut:
    # Файл уже лежит в хранилище: сюда приходит key из ответа POST /files
    return LessonFileOut(**svc.add_file(lesson_id, body.key, body.name))


@router.delete("/lesson_files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lesson_file(
    file_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[LessonsAdminService, Depends(deps.get_lessons_admin_service)],
) -> None:
    svc.delete_file(file_id)
