from typing import Annotated

from fastapi import APIRouter, Depends

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import AdminAdminIn, AdminAdminOut, AdminAdminsOut
from app.application.admins import AdminsService

router = APIRouter(prefix="/admin")


@router.get("/admins")
def admin_admins(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[AdminsService, Depends(deps.get_admins_service)],
) -> AdminAdminsOut:
    # Телефоны админов — персональные данные: эндпоинт только для админа,
    # как и весь остальной /admin
    return AdminAdminsOut(**svc.admin_list(admin))


@router.post("/admins")
def add_admin(
    body: AdminAdminIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[AdminsService, Depends(deps.get_admins_service)],
) -> AdminAdminOut:
    return AdminAdminOut(**svc.add(admin, body.phone))
