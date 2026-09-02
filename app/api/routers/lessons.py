from typing import Annotated

from fastapi import APIRouter, Depends

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import LessonCompleteOut, LessonOut, PlaybackOut
from app.application.lessons import LessonsService

router = APIRouter(prefix="/lessons")


@router.get("/{lesson_id}")
def lesson(
    lesson_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[LessonsService, Depends(deps.get_lessons_service)],
) -> LessonOut:
    return LessonOut(**svc.lesson_page(user, lesson_id, platform))


@router.post("/{lesson_id}/complete")
def complete_lesson(
    lesson_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[LessonsService, Depends(deps.get_lessons_service)],
) -> LessonCompleteOut:
    # Тело пустое: что именно отмечено, сказано в пути. Ответ — блок прогресса,
    # чтобы экран не ходил за ним вторым запросом
    return LessonCompleteOut(**svc.complete(user, lesson_id, platform))


@router.get("/{lesson_id}/playback")
def lesson_playback(
    lesson_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[LessonsService, Depends(deps.get_lessons_service)],
) -> PlaybackOut:
    # Плеер ходит сюда, а не за video_url: смена провайдера видео на свой
    # хостинг его не коснётся
    return PlaybackOut(**svc.playback(user, lesson_id, platform))
