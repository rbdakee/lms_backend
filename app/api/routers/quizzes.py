from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import AnswerIn, QuizAttemptOut, QuizOut, QuizResultOut, QuizReviewOut
from app.application.quizzes import QuizzesService

router = APIRouter(prefix="/quizzes")
# Попытка живёт своей жизнью: её id приходит с фронта без теста в пути
attempts_router = APIRouter(prefix="/quiz_attempts")


@router.get("/{quiz_id}")
def quiz(
    quiz_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[QuizzesService, Depends(deps.get_quizzes_service)],
) -> QuizOut:
    return QuizOut(**svc.quiz_page(user, quiz_id))


@router.post("/{quiz_id}/quiz_attempts")
def start_attempt(
    quiz_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[QuizzesService, Depends(deps.get_quizzes_service)],
) -> QuizAttemptOut:
    # Тело пустое: время пошло на сервере с этого запроса
    return QuizAttemptOut(**svc.start(user, quiz_id))


@attempts_router.post("/{attempt_id}/answers", status_code=status.HTTP_204_NO_CONTENT)
def save_answer(
    attempt_id: int,
    body: AnswerIn,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[QuizzesService, Depends(deps.get_quizzes_service)],
) -> None:
    # По одному вопросу и сразу: при единственной попытке потеря ответов недопустима
    svc.answer(user, attempt_id, body.question_id, body.option_ids)


@attempts_router.post("/{attempt_id}/finish")
def finish_attempt(
    attempt_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[QuizzesService, Depends(deps.get_quizzes_service)],
) -> QuizResultOut:
    return QuizResultOut(**svc.finish(user, attempt_id))


@attempts_router.get("/{attempt_id}/review")
def attempt_review(
    attempt_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[QuizzesService, Depends(deps.get_quizzes_service)],
) -> QuizReviewOut:
    return QuizReviewOut(**svc.review(user, attempt_id))
