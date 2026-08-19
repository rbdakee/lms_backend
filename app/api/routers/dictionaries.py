from typing import Annotated

from fastapi import APIRouter, Depends

from app.api import deps
from app.api.schemas import DictionariesOut
from app.application.categories import CategoriesService
from app.domain import dictionaries

router = APIRouter()


@router.get("/dictionaries")
def get_dictionaries(
    svc: Annotated[CategoriesService, Depends(deps.get_categories_service)],
) -> DictionariesOut:
    # Публично: категории нужны фильтру каталога, а каталог открытый.
    # Регионы и предметы остаются константами — их редактора нет
    return DictionariesOut(
        regions=dictionaries.REGIONS,
        subjects=dictionaries.SUBJECTS,
        categories=svc.public_list(),
    )
