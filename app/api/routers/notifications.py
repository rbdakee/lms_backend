from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.adapters.db.models import User
from app.api import deps
from app.api.pagination import PageParams, page_out
from app.api.schemas import NotificationsPageOut, NotificationsReadIn
from app.application.notifications import NotificationsService

router = APIRouter(prefix="/notifications")


@router.get("")
def notifications(
    user: Annotated[User, Depends(deps.get_current_user)],
    params: Annotated[PageParams, Depends()],
    svc: Annotated[NotificationsService, Depends(deps.get_notifications_service)],
) -> NotificationsPageOut:
    # Панель колокольчика берёт ?per_page=4 и получает и список, и счётчик
    data = svc.page(user, params.offset, params.per_page)
    return NotificationsPageOut(
        **page_out(data["items"], data["total"], params), unread_count=data["unread_count"]
    )


@router.post("/read", status_code=status.HTTP_204_NO_CONTENT)
def read_notifications(
    body: NotificationsReadIn,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[NotificationsService, Depends(deps.get_notifications_service)],
) -> None:
    svc.mark_read(user, body.ids, body.all)
