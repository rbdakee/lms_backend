from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.adapters.db.models import Session, User
from app.api import deps
from app.api.schemas import PreviewEnterIn
from app.application.preview import PreviewService

# Ручки админские, а смотрит админ клиентское приложение: кука сессии одна
# на оба домена, и флаг режима виден обоим (CONTRACT, сессия 6).
router = APIRouter(prefix="/admin/preview")


@router.post("/enter", status_code=status.HTTP_204_NO_CONTENT)
def enter_preview(
    body: PreviewEnterIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    session: Annotated[Session, Depends(deps.get_current_session)],
    svc: Annotated[PreviewService, Depends(deps.get_preview_service)],
) -> None:
    # Ответ пустой: о режиме клиентское приложение узнаёт из GET /me
    svc.enter(session, body.course_id)


@router.post("/exit", status_code=status.HTTP_204_NO_CONTENT)
def exit_preview(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    session: Annotated[Session, Depends(deps.get_current_session)],
    svc: Annotated[PreviewService, Depends(deps.get_preview_service)],
) -> None:
    # Тела нет и повторный вызов вне режима — тоже 204
    svc.exit(session)
