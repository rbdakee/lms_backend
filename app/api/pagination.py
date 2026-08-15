"""Постраничные списки — один вид на все эндпоинты:

    запрос:  ?page=1&per_page=20
    ответ:   {"items": [...], "total": 137, "page": 1, "per_page": 20}

Пустая страница — это items: [] с настоящим total, а не ошибка.
"""

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel


class PageParams(BaseModel):
    page: Annotated[int, Query(ge=1)] = 1
    per_page: Annotated[int, Query(ge=1, le=100)] = 20

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


def page_out(items: list, total: int, params: PageParams) -> dict:
    return {
        "items": items,
        "total": total,
        "page": params.page,
        "per_page": params.per_page,
    }
