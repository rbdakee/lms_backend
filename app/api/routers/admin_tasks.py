from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import AdminTaskIn, AdminTaskOut, AdminTaskPatchIn
from app.application.tasks_admin import TasksAdminService

router = APIRouter(prefix="/admin")


@router.post("/modules/{module_id}/tasks")
def create_task(
    module_id: int,
    body: AdminTaskIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[TasksAdminService, Depends(deps.get_tasks_admin_service)],
) -> AdminTaskOut:
    # Ответ — редактор задания целиком: фронт сразу уводит на него
    return AdminTaskOut(**svc.create(module_id, body.model_dump()))


@router.get("/tasks/{task_id}")
def admin_task(
    task_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[TasksAdminService, Depends(deps.get_tasks_admin_service)],
) -> AdminTaskOut:
    return AdminTaskOut(**svc.card(task_id))


@router.patch("/tasks/{task_id}")
def patch_task(
    task_id: int,
    body: AdminTaskPatchIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[TasksAdminService, Depends(deps.get_tasks_admin_service)],
) -> AdminTaskOut:
    return AdminTaskOut(**svc.patch(task_id, body.model_dump(exclude_unset=True)))


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[TasksAdminService, Depends(deps.get_tasks_admin_service)],
) -> None:
    svc.delete(task_id)
