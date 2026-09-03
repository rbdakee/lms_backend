from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

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

# Пять минут, а не сутки, как у брендинга: адрес обложки у курса один и тот же,
# и заменивший картинку админ смотрит на результат тут же, в редакторе.
COVER_CACHE = "public, max-age=300"

# Обложкой кладут и svg, а внутри svg бывает <script>. Открытый прямым адресом,
# он выполнился бы на домене API — том самом, чью куку сессии делят оба фронта.
# В <img> картинка от этих заголовков не страдает.
COVER_SECURITY = {
    "x-content-type-options": "nosniff",
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'",
}


@router.get("")
def catalog(
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[CoursesService, Depends(deps.get_courses_service)],
) -> CatalogOut:
    # Без пагинации: каталог маленький, фильтры на фронте
    return CatalogOut(items=svc.catalog(platform))


@router.get("/{course_id}")
def course_page(
    course_id: int,
    user: Annotated[User | None, Depends(deps.get_current_user_optional)],
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[CoursesService, Depends(deps.get_courses_service)],
) -> CoursePageOut:
    return CoursePageOut(**svc.course_page(course_id, user, platform))


@router.get("/{course_id}/cover")
def course_cover(
    course_id: int,
    svc: Annotated[CoursesService, Depends(deps.get_courses_service)],
) -> StreamingResponse:
    # Без входа: обложка стоит в каталоге, который открывают все.
    # content-disposition не ставим — картинка стоит в <img>, а не скачивается
    mime, size, chunks = svc.cover(course_id)
    return StreamingResponse(
        chunks,
        media_type=mime,
        headers={
            "content-length": str(size),
            "cache-control": COVER_CACHE,
            **COVER_SECURITY,
        },
    )


@router.get("/{course_id}/program")
def course_program(
    course_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[CoursesService, Depends(deps.get_courses_service)],
) -> ProgramOut:
    # Программа со статусами — сайдбар экрана урока, только своим учителям
    return ProgramOut(**svc.program_page(user, course_id, platform))


@router.get("/{course_id}/reviews")
def course_reviews(
    course_id: int,
    params: Annotated[PageParams, Depends()],
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[CoursesService, Depends(deps.get_courses_service)],
) -> ReviewsPageOut:
    data = svc.reviews_page(course_id, params.offset, params.per_page, platform)
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
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[CoursesService, Depends(deps.get_courses_service)],
) -> ReviewOut:
    return ReviewOut(**svc.add_review(user, course_id, body.rating, body.text, platform))


@router.post("/{course_id}/lead")
def create_lead(
    course_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[LeadsService, Depends(deps.get_leads_service)],
) -> LeadOut:
    # Тело пустое: телефон и ФИО уже в профиле
    return LeadOut(**svc.create_lead(user, course_id, platform))
