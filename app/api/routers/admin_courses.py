from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.adapters.db.models import User
from app.api import deps
from app.api.pagination import PageParams, page_out
from app.api.schemas import (
    AdminCourseCardOut,
    AdminCourseIn,
    AdminCoursePatchIn,
    AdminCoursesPageOut,
    AdminProgramModuleOut,
    AdminProgramOut,
    CourseVersionIn,
    ModuleIn,
    ProgramOrderIn,
)
from app.application.courses_admin import CoursesAdminService

router = APIRouter(prefix="/admin")


@router.get("/courses")
def admin_courses(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    params: Annotated[PageParams, Depends()],
    svc: Annotated[CoursesAdminService, Depends(deps.get_courses_admin_service)],
    # Фильтр видимости каталога к списку не применяется: черновик и скрытая
    # версия — обычные строки, их и правят
    status: Annotated[
        Literal["draft", "planned", "open", "closed", "hidden"] | None, Query()
    ] = None,
    lang: Annotated[Literal["ru", "kz"] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> AdminCoursesPageOut:
    data = svc.admin_list(
        status=status, lang=lang, q=q, offset=params.offset, limit=params.per_page
    )
    return AdminCoursesPageOut(**page_out(data["items"], data["total"], params))


@router.post("/courses")
def create_course(
    body: AdminCourseIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CoursesAdminService, Depends(deps.get_courses_admin_service)],
) -> AdminCourseCardOut:
    return AdminCourseCardOut(**svc.create(body.model_dump()))


@router.get("/courses/{course_id}")
def admin_course(
    course_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CoursesAdminService, Depends(deps.get_courses_admin_service)],
) -> AdminCourseCardOut:
    return AdminCourseCardOut(**svc.card(course_id))


@router.patch("/courses/{course_id}")
def patch_course(
    course_id: int,
    body: AdminCoursePatchIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CoursesAdminService, Depends(deps.get_courses_admin_service)],
) -> AdminCourseCardOut:
    return AdminCourseCardOut(**svc.patch(course_id, body.model_dump(exclude_unset=True)))


@router.delete("/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_course(
    course_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CoursesAdminService, Depends(deps.get_courses_admin_service)],
) -> None:
    svc.delete(course_id)


@router.post("/courses/{course_id}/versions")
def create_version(
    course_id: int,
    body: CourseVersionIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CoursesAdminService, Depends(deps.get_courses_admin_service)],
) -> AdminCourseCardOut:
    # Ответ — редактор новой версии: фронт сразу уводит на неё
    return AdminCourseCardOut(**svc.add_version(course_id, body.lang, body.copy_program))


@router.post("/courses/{course_id}/duplicate")
def duplicate_course(
    course_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CoursesAdminService, Depends(deps.get_courses_admin_service)],
) -> AdminCourseCardOut:
    return AdminCourseCardOut(**svc.duplicate(course_id))


@router.post("/courses/{course_id}/modules")
def create_module(
    course_id: int,
    body: ModuleIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CoursesAdminService, Depends(deps.get_courses_admin_service)],
) -> AdminProgramModuleOut:
    return AdminProgramModuleOut(**svc.add_module(course_id, body.title))


@router.patch("/modules/{module_id}")
def patch_module(
    module_id: int,
    body: ModuleIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CoursesAdminService, Depends(deps.get_courses_admin_service)],
) -> AdminProgramModuleOut:
    return AdminProgramModuleOut(**svc.rename_module(module_id, body.title))


@router.delete("/modules/{module_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_module(
    module_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CoursesAdminService, Depends(deps.get_courses_admin_service)],
) -> None:
    svc.delete_module(module_id)


@router.put("/courses/{course_id}/program_order")
def program_order(
    course_id: int,
    body: ProgramOrderIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CoursesAdminService, Depends(deps.get_courses_admin_service)],
) -> AdminProgramOut:
    # Экран перерисовывает дерево ответом сервера, а не своим оптимистичным
    return AdminProgramOut(**svc.reorder(course_id, body.model_dump()["modules"]))
