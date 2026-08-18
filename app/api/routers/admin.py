from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.adapters.db.models import User
from app.api import deps
from app.api.pagination import PageParams, page_out
from app.api.schemas import (
    AdminLeadOut,
    AdminLeadsPageOut,
    AdminSubmissionCardOut,
    AdminSubmissionsPageOut,
    EnrollmentIn,
    EnrollmentOut,
    LeadPatchIn,
    SubmissionReviewIn,
)
from app.application.leads import LeadsService
from app.application.submissions_admin import SubmissionsAdminService

router = APIRouter(prefix="/admin")


@router.get("/leads")
def admin_leads(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    params: Annotated[PageParams, Depends()],
    svc: Annotated[LeadsService, Depends(deps.get_leads_service)],
    # open — псевдостатус «в работе»: new | contacted | paid
    status: Annotated[
        Literal["new", "contacted", "paid", "granted", "declined", "open"] | None, Query()
    ] = None,
    course_id: Annotated[int | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> AdminLeadsPageOut:
    data = svc.admin_list(
        status=status, course_id=course_id, q=q, offset=params.offset, limit=params.per_page
    )
    return AdminLeadsPageOut(**page_out(data["items"], data["total"], params))


@router.patch("/leads/{lead_id}")
def patch_lead(
    lead_id: int,
    body: LeadPatchIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[LeadsService, Depends(deps.get_leads_service)],
) -> AdminLeadOut:
    return AdminLeadOut(**svc.admin_patch(lead_id, body.model_dump(exclude_unset=True)))


@router.post("/enrollments")
def grant_enrollment(
    body: EnrollmentIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[LeadsService, Depends(deps.get_leads_service)],
) -> EnrollmentOut:
    return EnrollmentOut(
        **svc.grant(admin, body.user_id, body.course_id, body.paid, body.note)
    )


@router.get("/submissions")
def admin_submissions(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    params: Annotated[PageParams, Depends()],
    svc: Annotated[SubmissionsAdminService, Depends(deps.get_submissions_admin_service)],
    # pending по умолчанию — это и есть очередь; all — архив и всё подряд
    status: Annotated[Literal["pending", "accepted", "rework", "all"], Query()] = "pending",
    course_id: Annotated[int | None, Query()] = None,
) -> AdminSubmissionsPageOut:
    data = svc.admin_list(
        status=None if status == "all" else status,
        course_id=course_id,
        offset=params.offset,
        limit=params.per_page,
    )
    return AdminSubmissionsPageOut(**page_out(data["items"], data["total"], params))


@router.get("/submissions/{submission_id}")
def admin_submission(
    submission_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[SubmissionsAdminService, Depends(deps.get_submissions_admin_service)],
) -> AdminSubmissionCardOut:
    return AdminSubmissionCardOut(**svc.card(submission_id))


@router.post("/submissions/{submission_id}/review")
def review_submission(
    submission_id: int,
    body: SubmissionReviewIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[SubmissionsAdminService, Depends(deps.get_submissions_admin_service)],
) -> AdminSubmissionCardOut:
    # Ответ — обновлённая карточка целиком: «сохранить и перейти к следующей»
    # фронт делает по уже загруженной очереди, отдельного эндпоинта нет
    return AdminSubmissionCardOut(
        **svc.review(admin, submission_id, body.verdict, body.comment)
    )
