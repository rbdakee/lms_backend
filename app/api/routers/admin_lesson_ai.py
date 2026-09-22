"""Доступ к «Жоспару» в админке: кому выдан, сколько израсходовал, выдача
и снятие. Слой тонкий — логика в `app/application/lesson_ai_access.py`.

Все ручки за `get_current_admin` зависимостью роутера. Выключателя продукта
(`require_lesson_ai`) здесь нет нарочно: доступ выдают после оплаты, а оплата
не ждёт, пока продукт включат, — выданный заранее доступ заработает сам,
когда выключатель повернут.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.adapters.db.models import User
from app.api import deps
from app.api.pagination import PageParams, page_out
from app.api.schemas_lesson_ai import (
    LessonAccessCardOut,
    LessonAccessGrantIn,
    LessonAccessPageOut,
)
from app.application.lesson_ai_access import LessonAccessService

router = APIRouter(prefix="/admin/lesson_ai", dependencies=[Depends(deps.get_current_admin)])

Access = Annotated[LessonAccessService, Depends(deps.get_lesson_access_service)]
Admin = Annotated[User, Depends(deps.get_current_admin)]


@router.get("/access")
def access_list(
    params: Annotated[PageParams, Depends()],
    svc: Access,
    status: Annotated[Literal["active", "expired", "revoked"] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> LessonAccessPageOut:
    data = svc.admin_list(status=status, q=q, offset=params.offset, limit=params.per_page)
    return LessonAccessPageOut(**page_out(data["items"], data["total"], params))


@router.get("/access/{user_id}")
def access_card(user_id: int, svc: Access) -> LessonAccessCardOut:
    return LessonAccessCardOut(**svc.card(user_id))


@router.put("/access/{user_id}")
def grant(
    user_id: int, body: LessonAccessGrantIn, admin: Admin, svc: Access
) -> LessonAccessCardOut:
    # Выдать, продлить и выдать заново после снятия — одно действие (L7, запись 3)
    return LessonAccessCardOut(
        **svc.grant(admin, user_id, expires_on=body.expires_on, paid=body.paid, note=body.note)
    )


@router.delete("/access/{user_id}", status_code=204)
def revoke(user_id: int, admin: Admin, svc: Access) -> None:
    svc.revoke(admin, user_id)
