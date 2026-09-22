"""«Жоспар» — помощник учителя. Слой тонкий: разбор запроса, вызов сценария,
сериализация — логика в `app/application/lesson_kits.py`.

Кука — учительская, площадки: продукт один на обе, и учитель в нём один
и тот же человек (TEACHER_AI_BRIEF, раздел 8). Проверки доступа к продукту
здесь пока нет — она приходит с `lesson_ai_access` в сессии L7, а до тех пор
продукт закрыт выключателем `LESSON_AI_ENABLED`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas_lesson_ai import LessonKitIn, LessonKitOut
from app.application.lesson_kits import LessonKitsService

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
        )
    )


@router.get("/kits/{kit_id}")
def kit(
    kit_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[LessonKitsService, Depends(deps.get_lesson_kits_service)],
) -> LessonKitOut:
    return LessonKitOut(**svc.kit(user, kit_id))
