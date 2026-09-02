from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.adapters.db.models import User
from app.api import deps
from app.api.pagination import PageParams, page_out
from app.api.schemas import (
    AdminTeacherCardOut,
    AdminTeacherPatchIn,
    AdminTeachersPageOut,
    TeacherRetakeIn,
)
from app.application.teachers_admin import TeachersAdminService

router = APIRouter(prefix="/admin")


@router.get("/teachers")
def admin_teachers(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    params: Annotated[PageParams, Depends()],
    svc: Annotated[TeachersAdminService, Depends(deps.get_teachers_admin_service)],
    region: Annotated[str | None, Query(max_length=100)] = None,
    school: Annotated[str | None, Query(max_length=200)] = None,
    # Учителя с действующим доступом к этой версии курса
    course_id: Annotated[int | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> AdminTeachersPageOut:
    data = svc.admin_list(
        region=region,
        school=school,
        course_id=course_id,
        q=q,
        offset=params.offset,
        limit=params.per_page,
    )
    return AdminTeachersPageOut(**page_out(data["items"], data["total"], params))


@router.get("/teachers/{user_id}")
def admin_teacher(
    user_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[TeachersAdminService, Depends(deps.get_teachers_admin_service)],
) -> AdminTeacherCardOut:
    return AdminTeacherCardOut(**svc.card(user_id))


@router.patch("/teachers/{user_id}")
def patch_teacher(
    user_id: int,
    body: AdminTeacherPatchIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[TeachersAdminService, Depends(deps.get_teachers_admin_service)],
) -> AdminTeacherCardOut:
    return AdminTeacherCardOut(
        **svc.patch(admin, user_id, body.model_dump(exclude_unset=True))
    )


@router.post("/teachers/{user_id}/retakes")
def allow_retake(
    user_id: int,
    body: TeacherRetakeIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[TeachersAdminService, Depends(deps.get_teachers_admin_service)],
) -> AdminTeacherCardOut:
    # Площадка — из тела, а не из `Origin`: админка одна на обе, и `platform_of`
    # отдал бы здесь первую площадку всегда (решение владельца).
    # Ответ — карточка целиком: экран перерисовывает вкладку «Тесты» ответом
    return AdminTeacherCardOut(
        **svc.allow_retake(admin, user_id, body.quiz_id, body.reason, body.platform)
    )


@router.delete("/enrollments/{enrollment_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_enrollment(
    enrollment_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[TeachersAdminService, Depends(deps.get_teachers_admin_service)],
) -> None:
    svc.revoke(enrollment_id)
