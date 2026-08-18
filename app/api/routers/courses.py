from typing import Annotated

from fastapi import APIRouter, Depends

from app.adapters.db.models import User
from app.api import deps
from app.api.pagination import PageParams, page_out
from app.api.schemas import (
    CatalogOut,
    CoursePageOut,
    LeadOut,
    ProgramOut,
    ReviewIn,
    ReviewOut,
    ReviewsPageOut,
)
from app.application.courses import CoursesService
from app.application.leads import LeadsService

router = APIRouter(prefix="/courses")


@router.get("")
def catalog(
    svc: Annotated[CoursesService, Depends(deps.get_courses_service)],
) -> CatalogOut:
    # Без пагинации: каталог маленький, фильтры на фронте
    return CatalogOut(items=svc.catalog())


@router.get("/{course_id}")
def course_page(
    course_id: int,
    user: Annotated[User | None, Depends(deps.get_current_user_optional)],
    svc: Annotated[CoursesService, Depends(deps.get_courses_service)],
) -> CoursePageOut:
    return CoursePageOut(**svc.course_page(course_id, user))


@router.get("/{course_id}/program")
def course_program(
    course_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[CoursesService, Depends(deps.get_courses_service)],
) -> ProgramOut:
    # Программа со статусами — сайдбар экрана урока, только своим учителям
    return ProgramOut(**svc.program_page(user, course_id))


@router.get("/{course_id}/reviews")
def course_reviews(
    course_id: int,
    params: Annotated[PageParams, Depends()],
    svc: Annotated[CoursesService, Depends(deps.get_courses_service)],
) -> ReviewsPageOut:
    data = svc.reviews_page(course_id, params.offset, params.per_page)
    return ReviewsPageOut(
        **page_out(data["items"], data["total"], params),
        rating=data["rating"],
        breakdown=data["breakdown"],
    )


@router.post("/{course_id}/reviews")
def add_review(
    course_id: int,
    body: ReviewIn,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[CoursesService, Depends(deps.get_courses_service)],
) -> ReviewOut:
    return ReviewOut(**svc.add_review(user, course_id, body.rating, body.text))


@router.post("/{course_id}/lead")
def create_lead(
    course_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[LeadsService, Depends(deps.get_leads_service)],
) -> LeadOut:
    # Тело пустое: телефон и ФИО уже в профиле
    return LeadOut(**svc.create_lead(user, course_id))
