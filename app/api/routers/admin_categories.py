from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import AdminCategoriesOut, AdminCategoryIn, AdminCategoryOut
from app.application.categories import CategoriesService

router = APIRouter(prefix="/admin")


@router.get("/categories")
def admin_categories(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CategoriesService, Depends(deps.get_categories_service)],
) -> AdminCategoriesOut:
    return AdminCategoriesOut(**svc.admin_list())


@router.post("/categories")
def create_category(
    body: AdminCategoryIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CategoriesService, Depends(deps.get_categories_service)],
) -> AdminCategoryOut:
    return AdminCategoryOut(**svc.create(body.title))


@router.patch("/categories/{category_id}")
def rename_category(
    category_id: int,
    body: AdminCategoryIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CategoriesService, Depends(deps.get_categories_service)],
) -> AdminCategoryOut:
    # Тело то же, что у создания: у категории правится одно название
    return AdminCategoryOut(**svc.rename(category_id, body.title))


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(
    category_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CategoriesService, Depends(deps.get_categories_service)],
) -> None:
    svc.delete(category_id)
