from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.adapters.db.models import User
from app.api import deps
from app.api.pagination import PageParams, page_out
from app.api.schemas import (
    AdminQuestionsPageOut,
    QuestionsPageOut,
    ThreadMessageIn,
    ThreadQuestionOut,
)
from app.application.questions import QuestionsService

# Вопросы висят на уроке, а сводная очередь — экран админки.
router = APIRouter(prefix="/lessons")
admin_router = APIRouter(prefix="/admin")


@router.get("/{lesson_id}/questions")
def lesson_questions(
    lesson_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    params: Annotated[PageParams, Depends()],
    svc: Annotated[QuestionsService, Depends(deps.get_questions_service)],
) -> QuestionsPageOut:
    data = svc.lesson_questions(user, lesson_id, params.offset, params.per_page)
    return QuestionsPageOut(**page_out(data["items"], data["total"], params))


@router.post("/{lesson_id}/questions", status_code=status.HTTP_201_CREATED)
def add_question(
    lesson_id: int,
    body: ThreadMessageIn,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[QuestionsService, Depends(deps.get_questions_service)],
) -> ThreadQuestionOut:
    # Один эндпоинт на вопрос и на ответ — различает их parent_id
    return ThreadQuestionOut(**svc.add_message(user, lesson_id, body.text, body.parent_id))


@admin_router.get("/questions")
def admin_questions(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    params: Annotated[PageParams, Depends()],
    svc: Annotated[QuestionsService, Depends(deps.get_questions_service)],
    # false — только без ответа: экран открывается именно так
    answered: Annotated[bool | None, Query()] = None,
    course_id: Annotated[int | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> AdminQuestionsPageOut:
    data = svc.admin_list(
        answered=answered,
        course_id=course_id,
        q=q,
        offset=params.offset,
        limit=params.per_page,
    )
    return AdminQuestionsPageOut(**page_out(data["items"], data["total"], params))
