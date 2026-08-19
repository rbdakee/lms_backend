from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import (
    AdminQuizIn,
    AdminQuizOut,
    AdminQuizPatchIn,
    AdminQuizQuestionOut,
    QuizQuestionIn,
    QuizQuestionPatchIn,
)
from app.application.quizzes_admin import QuizzesAdminService

router = APIRouter(prefix="/admin")


@router.post("/modules/{module_id}/quizzes")
def create_quiz(
    module_id: int,
    body: AdminQuizIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[QuizzesAdminService, Depends(deps.get_quizzes_admin_service)],
) -> AdminQuizOut:
    # Ответ — редактор теста целиком: фронт сразу уводит на него
    return AdminQuizOut(**svc.create(module_id, body.model_dump()))


@router.get("/quizzes/{quiz_id}")
def admin_quiz(
    quiz_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[QuizzesAdminService, Depends(deps.get_quizzes_admin_service)],
) -> AdminQuizOut:
    # Единственный эндпоинт, где верные ответы уходят наружу: право на них
    # проверяет сервер, а не то, что экран лежит на другом домене
    return AdminQuizOut(**svc.card(quiz_id))


@router.patch("/quizzes/{quiz_id}")
def patch_quiz(
    quiz_id: int,
    body: AdminQuizPatchIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[QuizzesAdminService, Depends(deps.get_quizzes_admin_service)],
) -> AdminQuizOut:
    return AdminQuizOut(**svc.patch(quiz_id, body.model_dump(exclude_unset=True)))


@router.delete("/quizzes/{quiz_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_quiz(
    quiz_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[QuizzesAdminService, Depends(deps.get_quizzes_admin_service)],
) -> None:
    svc.delete(quiz_id)


@router.post("/quizzes/{quiz_id}/questions")
def create_quiz_question(
    quiz_id: int,
    body: QuizQuestionIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[QuizzesAdminService, Depends(deps.get_quizzes_admin_service)],
) -> AdminQuizQuestionOut:
    return AdminQuizQuestionOut(**svc.add_question(quiz_id, body.model_dump()))


# Путь не /admin/questions/{id}: там уже живёт очередь вопросов учителей
# под уроками, и два разных «вопроса» в одном пространстве имён рано или
# поздно сойдутся не тем эндпоинтом.
@router.patch("/quiz_questions/{question_id}")
def patch_quiz_question(
    question_id: int,
    body: QuizQuestionPatchIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[QuizzesAdminService, Depends(deps.get_quizzes_admin_service)],
) -> AdminQuizQuestionOut:
    return AdminQuizQuestionOut(
        **svc.patch_question(question_id, body.model_dump(exclude_unset=True))
    )


@router.delete("/quiz_questions/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_quiz_question(
    question_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[QuizzesAdminService, Depends(deps.get_quizzes_admin_service)],
) -> None:
    svc.delete_question(question_id)
