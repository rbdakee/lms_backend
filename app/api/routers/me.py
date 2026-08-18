import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.adapters.db.models import Session, User
from app.api import deps
from app.api.schemas import MyCoursesOut, SessionListOut, UserOut, UserPatch
from app.application.courses import CoursesService
from app.application.users import UsersService

router = APIRouter(prefix="/me")


@router.get("")
def get_me(user: Annotated[User, Depends(deps.get_current_user)]) -> UserOut:
    return UserOut.from_user(user)


@router.patch("")
def patch_me(
    body: UserPatch,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[UsersService, Depends(deps.get_users_service)],
) -> UserOut:
    updated = svc.update_profile(user, body.model_dump(exclude_unset=True))
    return UserOut.from_user(updated)


@router.get("/courses")
def my_courses(
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[CoursesService, Depends(deps.get_courses_service)],
) -> MyCoursesOut:
    # Курсы с прогрессом и открытые заявки — один ответ на весь экран /my
    return MyCoursesOut(**svc.my_courses(user))


@router.get("/sessions")
def my_sessions(
    user: Annotated[User, Depends(deps.get_current_user)],
    session: Annotated[Session, Depends(deps.get_current_session)],
    svc: Annotated[UsersService, Depends(deps.get_users_service)],
) -> SessionListOut:
    return SessionListOut(items=svc.list_sessions(user, session))


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session(
    session_id: uuid.UUID,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[UsersService, Depends(deps.get_users_service)],
) -> None:
    svc.revoke_session(user, session_id)
