from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import StreamingResponse

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import FileLinkOut, UploadedFileOut
from app.application.files import FilesService
from app.application.tasks import TasksService
from app.domain.errors import NoFileError

router = APIRouter(prefix="/files")


@router.post("")
def upload_file(
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[FilesService, Depends(deps.get_files_service)],
    file: Annotated[UploadFile | None, File()] = None,
) -> UploadedFileOut:
    # Достаточно входа: файл ещё ни к какому курсу не привязан. Поле объявлено
    # необязательным нарочно — «поля нет» и «файл пустой» человек видит одним
    # случаем, и ответ у них один
    if file is None:
        raise NoFileError()
    return UploadedFileOut(**svc.upload(file.filename or "", file.file))


@router.get("/lesson/{file_id}/{filename}")
def download_lesson_file(
    file_id: int,
    filename: str,
    svc: Annotated[FilesService, Depends(deps.get_files_service)],
    e: int = 0,
    s: str = "",
) -> StreamingResponse:
    # Сессия здесь не проверяется: право на файл доказывает подпись, а в бою
    # этот путь заберёт nginx и до приложения запрос не дойдёт вовсе
    file, size, chunks = svc.content(file_id, filename, e, s)
    return StreamingResponse(
        chunks,
        media_type=file.mime,
        headers={
            "content-length": str(size),
            # filename* — имя как есть: у материалов оно кириллическое
            "content-disposition": f"attachment; filename*=UTF-8''{quote(file.name)}",
        },
    )


@router.get("/task/{task_id}/{filename}")
def download_task_template(
    task_id: int,
    filename: str,
    svc: Annotated[TasksService, Depends(deps.get_tasks_service)],
    e: int = 0,
    s: str = "",
) -> StreamingResponse:
    # Шаблон задания раздаётся как материал урока: сессии нет, право на файл
    # доказывает подпись, и в бою этот путь заберёт nginx
    name, mime, size, chunks = svc.template_content(task_id, filename, e, s)
    return StreamingResponse(
        chunks,
        media_type=mime,
        headers={
            "content-length": str(size),
            "content-disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        },
    )


@router.get("/submission/{submission_id}/{index}/{filename}")
def download_submission_file(
    submission_id: int,
    index: int,
    filename: str,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[TasksService, Depends(deps.get_tasks_service)],
) -> StreamingResponse:
    # Подписи здесь нет: ссылка постоянная, а право доказывает сессия — работу
    # отдаём её автору и админам. filename в пути нужен только для красивого
    # имени при сохранении, сервер его не читает
    file, size, chunks = svc.submission_file(user, submission_id, index)
    return StreamingResponse(
        chunks,
        media_type=file["mime"],
        headers={
            "content-length": str(size),
            # filename* — имя как есть: работы приходят с кириллическими именами
            "content-disposition": f"attachment; filename*=UTF-8''{quote(file['name'])}",
        },
    )


@router.get("/{file_id}")
def lesson_file_link(
    file_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[FilesService, Depends(deps.get_files_service)],
) -> FileLinkOut:
    # Файл не проксируем: отдаём подписанную ссылку, фронт открывает её сам
    return FileLinkOut(**svc.link(user, file_id, platform))
