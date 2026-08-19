from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.adapters.db.models import User
from app.api import deps
from app.api.pagination import PageParams, page_out
from app.api.schemas import AdminReviewOut, AdminReviewsPageOut, ReviewReplyIn
from app.application.reviews_admin import ReviewsAdminService

router = APIRouter(prefix="/admin")


@router.get("/reviews")
def admin_reviews(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    params: Annotated[PageParams, Depends()],
    svc: Annotated[ReviewsAdminService, Depends(deps.get_reviews_admin_service)],
    course_id: Annotated[int | None, Query()] = None,
    # Звёзды: экран открывается без фильтра, отдельно смотрят единицы и двойки
    rating: Annotated[int | None, Query(ge=1, le=5)] = None,
) -> AdminReviewsPageOut:
    data = svc.admin_list(
        course_id=course_id, rating=rating, offset=params.offset, limit=params.per_page
    )
    return AdminReviewsPageOut(**page_out(data["items"], data["total"], params))


@router.post("/reviews/{review_id}/reply")
def reply_to_review(
    review_id: int,
    body: ReviewReplyIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[ReviewsAdminService, Depends(deps.get_reviews_admin_service)],
) -> AdminReviewOut:
    # Ответ — отзыв в форме элемента ленты: экран перерисовывает строку
    return AdminReviewOut(**svc.reply(admin, review_id, body.text))


@router.delete("/reviews/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_review(
    review_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[ReviewsAdminService, Depends(deps.get_reviews_admin_service)],
) -> None:
    svc.delete(admin, review_id)
