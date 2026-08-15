from fastapi import APIRouter

from app.api.schemas import DictionariesOut
from app.domain import dictionaries

router = APIRouter()


@router.get("/dictionaries")
def get_dictionaries() -> DictionariesOut:
    # Публично: категории нужны фильтру каталога, а каталог открытый
    return DictionariesOut(
        regions=dictionaries.REGIONS,
        subjects=dictionaries.SUBJECTS,
        categories=dictionaries.CATEGORIES,
    )
