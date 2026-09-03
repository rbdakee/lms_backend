from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.adapters.db.models import User
from app.api import deps
from app.api.pagination import PageParams, page_out
from app.api.schemas import (
    AdminLeadOut,
    AdminLeadsPageOut,
    AdminOverviewOut,
    AdminReportOut,
    AdminSubmissionCardOut,
    AdminSubmissionsPageOut,
    EnrollmentIn,
    EnrollmentOut,
    LeadPatchIn,
    SubmissionReviewIn,
)
from app.application.leads import LeadsService
from app.application.overview import OverviewService
from app.application.reports import ReportsService
from app.application.submissions_admin import SubmissionsAdminService
from app.domain.errors import ValidationAppError
from app.domain.platform import PLATFORMS

router = APIRouter(prefix="/admin")

LEAD_FILTER_STATUSES = {"new", "contacted", "paid", "granted", "declined", "open"}


def platform_filter(raw: str | None) -> str | None:
    """Фильтр площадки, один вид у всех админских списков: `?platform=p1`.

    Одно значение, а не список: админка одна на обе площадки, и фильтр —
    выбор одной из них. Параметра нет — обе, как и было. Разбор здесь,
    а не `Literal` в сигнатуре: валидатор FastAPI ответил бы английским
    текстом, а этот отказ читает админ (решение оркестратора, раздел 6).
    """
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if value not in PLATFORMS:
        raise ValidationAppError(f"Неизвестная площадка: {value}")
    return value


def _csv(raw: str | None) -> list[str] | None:
    """Значения фильтра через запятую; пустой параметр — то же, что без него."""
    if raw is None:
        return None
    values = [v.strip() for v in raw.split(",") if v.strip()]
    return values or None


def _statuses(raw: str | None) -> list[str] | None:
    values = _csv(raw)
    for v in values or []:
        if v not in LEAD_FILTER_STATUSES:
            raise ValidationAppError(f"Неизвестный статус заявки: {v}")
    return values


def _course_ids(raw: str | None) -> list[int] | None:
    values = _csv(raw)
    if values is None:
        return None
    try:
        return [int(v) for v in values]
    except ValueError:
        raise ValidationAppError("course_id — числа через запятую") from None


@router.get("/leads")
def admin_leads(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    params: Annotated[PageParams, Depends()],
    svc: Annotated[LeadsService, Depends(deps.get_leads_service)],
    # Оба фильтра принимают и одно значение, и список через запятую:
    # status=new,contacted — мультивыбор на экране заявок.
    # open — псевдостатус «в работе» (new | contacted | paid), сочетается
    # с остальными объединением
    status: Annotated[str | None, Query()] = None,
    course_id: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    # Одно значение; параметра нет — обе площадки
    platform: Annotated[str | None, Query()] = None,
) -> AdminLeadsPageOut:
    data = svc.admin_list(
        statuses=_statuses(status),
        course_ids=_course_ids(course_id),
        q=q,
        platform=platform_filter(platform),
        offset=params.offset,
        limit=params.per_page,
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
        **svc.grant(
            admin, body.user_id, body.course_id, body.paid, body.note, body.platform
        )
    )


@router.get("/submissions")
def admin_submissions(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    params: Annotated[PageParams, Depends()],
    svc: Annotated[SubmissionsAdminService, Depends(deps.get_submissions_admin_service)],
    # pending по умолчанию — это и есть очередь; all — архив и всё подряд
    status: Annotated[Literal["pending", "accepted", "rework", "all"], Query()] = "pending",
    course_id: Annotated[int | None, Query()] = None,
    platform: Annotated[str | None, Query()] = None,
) -> AdminSubmissionsPageOut:
    data = svc.admin_list(
        status=None if status == "all" else status,
        course_id=course_id,
        platform=platform_filter(platform),
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


@router.get("/overview")
def admin_overview(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[OverviewService, Depends(deps.get_overview_service)],
    # Общий фильтр экрана: он действует и на счётчики, и на списки,
    # и на справочные числа — вторых колонок у показателей нет
    platform: Annotated[str | None, Query()] = None,
) -> AdminOverviewOut:
    # Весь дашборд одним ответом: три счётчика, три списка и справочные числа
    return AdminOverviewOut(**svc.overview(platform_filter(platform)))


@router.get("/reports/{course_id}")
def admin_report(
    course_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    params: Annotated[PageParams, Depends()],
    svc: Annotated[ReportsService, Depends(deps.get_reports_service)],
    # Поиск по ФИО: в таблице участников их восемь сотен
    q: Annotated[str | None, Query(max_length=100)] = None,
    # Фильтр действует на сводку, воронку и таблицу участников разом
    platform: Annotated[str | None, Query()] = None,
) -> AdminReportOut:
    data = svc.report(
        course_id,
        platform=platform_filter(platform),
        q=q,
        offset=params.offset,
        limit=params.per_page,
    )
    # Сводка и воронка приходят целиком, страницами режется только таблица
    participants = page_out(data["participants"]["items"], data["participants"]["total"], params)
    return AdminReportOut(**{**data, "participants": participants})
