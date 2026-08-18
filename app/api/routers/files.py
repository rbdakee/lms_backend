from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import StreamingResponse

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import FileLinkOut, UploadedFileOut
from app.application.files import FilesService
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


@router.get("/{file_id}")
def lesson_file_link(
    file_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[FilesService, Depends(deps.get_files_service)],
) -> FileLinkOut:
    # Файл не проксируем: отдаём подписанную ссылку, фронт открывает её сам
    return FileLinkOut(**svc.link(user, file_id))
