"""Экран сверки справочника учебной программы. Слой тонкий: разбор запроса,
вызов сценария, сериализация — логика в `app/application/curriculum_admin.py`.

Все ручки за `get_current_admin` зависимостью уровня роутера: справочник
вычитывает владелец, учителю здесь нет ничего (CONTRACT.md, раздел
«Справочник учебной программы»).
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.adapters.db.models import User
from app.api import deps
from app.api.pagination import PageParams, page_out
from app.api.schemas_curriculum import (
    CurriculumIssueOut,
    CurriculumIssuesPageOut,
    CurriculumObjectiveOut,
    CurriculumObjectivePatchIn,
    CurriculumObjectivesPageOut,
    CurriculumProgramCardOut,
    CurriculumProgramOut,
    CurriculumProgramsPageOut,
)
from app.application.curriculum_admin import CurriculumAdminService

router = APIRouter(
    prefix="/admin/curriculum", dependencies=[Depends(deps.get_current_admin)]
)


@router.get("/programs")
def programs(
    params: Annotated[PageParams, Depends()],
    svc: Annotated[CurriculumAdminService, Depends(deps.get_curriculum_admin_service)],
    subject_code: Annotated[str | None, Query(max_length=50)] = None,
    lang: Annotated[Literal["ru", "kz"] | None, Query()] = None,
    status: Annotated[Literal["loaded", "published"] | None, Query()] = None,
) -> CurriculumProgramsPageOut:
    data = svc.programs(
        subject_code=subject_code,
        lang=lang,
        status=status,
        offset=params.offset,
        limit=params.per_page,
    )
    return CurriculumProgramsPageOut(**page_out(data["items"], data["total"], params))


@router.get("/programs/{program_id}")
def program(
    program_id: int,
    svc: Annotated[CurriculumAdminService, Depends(deps.get_curriculum_admin_service)],
) -> CurriculumProgramCardOut:
    return CurriculumProgramCardOut(**svc.program(program_id))


@router.get("/programs/{program_id}/objectives")
def objectives(
    program_id: int,
    params: Annotated[PageParams, Depends()],
    svc: Annotated[CurriculumAdminService, Depends(deps.get_curriculum_admin_service)],
    grade: Annotated[int | None, Query(ge=1, le=12)] = None,
    # Код подраздела, как в приказе: «1.1»
    subsection: Annotated[str | None, Query(max_length=20)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    has_image: Annotated[bool | None, Query()] = None,
    edited: Annotated[bool | None, Query()] = None,
) -> CurriculumObjectivesPageOut:
    data = svc.objectives(
        program_id,
        grade=grade,
        subsection=subsection,
        q=q,
        has_image=has_image,
        edited=edited,
        offset=params.offset,
        limit=params.per_page,
    )
    return CurriculumObjectivesPageOut(**page_out(data["items"], data["total"], params))


@router.patch("/objectives/{objective_id}")
def edit_objective(
    objective_id: int,
    body: CurriculumObjectivePatchIn,
    svc: Annotated[CurriculumAdminService, Depends(deps.get_curriculum_admin_service)],
) -> CurriculumObjectiveOut:
    # Ответ — цель целиком, в форме элемента списка: экран перерисовывает
    # строку ответом, вместе с пересчитанным has_image
    return CurriculumObjectiveOut(**svc.edit_objective(objective_id, body.text))


@router.get("/programs/{program_id}/issues")
def issues(
    program_id: int,
    params: Annotated[PageParams, Depends()],
    svc: Annotated[CurriculumAdminService, Depends(deps.get_curriculum_admin_service)],
    severity: Annotated[Literal["error", "warning"] | None, Query()] = None,
    resolved: Annotated[bool | None, Query()] = None,
) -> CurriculumIssuesPageOut:
    data = svc.issues(
        program_id,
        severity=severity,
        resolved=resolved,
        offset=params.offset,
        limit=params.per_page,
    )
    return CurriculumIssuesPageOut(**page_out(data["items"], data["total"], params))


@router.post("/issues/{issue_id}/resolve")
def resolve_issue(
    issue_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CurriculumAdminService, Depends(deps.get_curriculum_admin_service)],
) -> CurriculumIssueOut:
    return CurriculumIssueOut(**svc.resolve_issue(admin, issue_id))


@router.delete("/issues/{issue_id}/resolve")
def unresolve_issue(
    issue_id: int,
    svc: Annotated[CurriculumAdminService, Depends(deps.get_curriculum_admin_service)],
) -> CurriculumIssueOut:
    # Не 204: экран перерисовывает строку той же формой, что и после разбора
    return CurriculumIssueOut(**svc.unresolve_issue(issue_id))


@router.post("/programs/{program_id}/publish")
def publish(
    program_id: int,
    svc: Annotated[CurriculumAdminService, Depends(deps.get_curriculum_admin_service)],
) -> CurriculumProgramOut:
    return CurriculumProgramOut(**svc.publish(program_id))


@router.post("/programs/{program_id}/unpublish")
def unpublish(
    program_id: int,
    svc: Annotated[CurriculumAdminService, Depends(deps.get_curriculum_admin_service)],
) -> CurriculumProgramOut:
    return CurriculumProgramOut(**svc.unpublish(program_id))
