from typing import Annotated

from fastapi import APIRouter, Depends

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import FileLinkOut, SubmissionIn, SubmissionOut, TaskOut
from app.application.tasks import TasksService

router = APIRouter(prefix="/tasks")


@router.get("/{task_id}")
def task(
    task_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[TasksService, Depends(deps.get_tasks_service)],
) -> TaskOut:
    return TaskOut(**svc.task_page(user, task_id))


@router.get("/{task_id}/template_file")
def task_template_file(
    task_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[TasksService, Depends(deps.get_tasks_service)],
) -> FileLinkOut:
    # Файл не проксируем: отдаём подписанную ссылку, как у материалов урока
    return FileLinkOut(**svc.template_link(user, task_id))


@router.post("/{task_id}/submissions")
def submit_task(
    task_id: int,
    body: SubmissionIn,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[TasksService, Depends(deps.get_tasks_service)],
) -> SubmissionOut:
    # Файлы уже загружены через POST /files: сюда приходят key и имя, а размер
    # и тип сервер снимает с хранилища сам
    files = [file.model_dump() for file in body.files]
    return SubmissionOut(**svc.submit(user, task_id, body.text, files))
