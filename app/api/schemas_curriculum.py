"""Схемы справочника учебной программы — отдельным файлом, а не в `schemas.py`.

Причина та же, что у схем ассистента: `schemas.py` — файл на две с лишним
тысячи строк, и дописывать в его конец очередной экран значит гарантированно
столкнуться в нём с параллельной сессией.

Формы ответов зафиксированы примерами в `CONTRACT.md`, раздел «Справочник
учебной программы»: фронт работает от них и не гадает.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel


class CurriculumProgramOut(BaseModel):
    """Программа в форме элемента списка. Ею же отвечают публикация
    и снятие публикации: экран перерисовывает строку ответом.

    `issues_open` — сколько подозрений ещё не разобрано, `errors_open` —
    из них ошибок. Пока `errors_open` больше нуля, публикация закрыта.
    """

    id: int
    subject_code: str
    subject_title: str
    lang: Literal["ru", "kz"]
    grade_from: int
    grade_to: int
    order_number: str
    order_date: date
    order_date_text: str
    source_url: str
    title: str
    status: Literal["loaded", "published"]
    imported_at: datetime
    published_at: datetime | None
    objectives_count: int
    issues_open: int
    errors_open: int


class CurriculumProgramsPageOut(BaseModel):
    items: list[CurriculumProgramOut]
    total: int
    page: int
    per_page: int


class CurriculumSubsectionOut(BaseModel):
    id: int
    code: str
    title: str
    objectives_count: int


class CurriculumSectionOut(BaseModel):
    """Раздел приказа. Уровня всего два — раздел и его подразделы,
    третьего у программы не бывает."""

    id: int
    code: str
    title: str
    subsections: list[CurriculumSubsectionOut]


class CurriculumProgramCardOut(CurriculumProgramOut):
    sections: list[CurriculumSectionOut]


class CurriculumObjectiveOut(BaseModel):
    """Цель обучения.

    `quarters` — четверти, в которых цель изучается; пустой список значит,
    что в долгосрочном плане приказа её нет (так бывает).
    `marker` — пометка из приказа (`**` — «реализуется частично»), пустая
    строка, если её нет. `edited_at` — когда текст правили руками;
    `null` — текст как в приказе.
    """

    id: int
    code: str
    grade: int
    index: int
    marker: str
    text: str
    quarters: list[int]
    has_image: bool
    edited_at: datetime | None
    subsection_code: str
    subsection_title: str


class CurriculumObjectivesPageOut(BaseModel):
    items: list[CurriculumObjectiveOut]
    total: int
    page: int
    per_page: int


class CurriculumObjectivePatchIn(BaseModel):
    """Правка формулировки. Кода, класса и подраздела здесь нет и не будет:
    они заданы приказом, и по коду цели завуч сверяет план урока."""

    model_config = {"extra": "forbid"}

    text: str


class CurriculumIssueOut(BaseModel):
    """Подозрение автопроверки импорта.

    `kind` — машинный код проверки: по нему фронт группирует список
    и подбирает подсказку. `objective_code` пуст у подозрений, которые
    относятся к программе целиком (`count_mismatch`, `empty_section`).
    `resolved_by` — ФИО админа, чтобы через месяц было видно, кто принял
    решение.
    """

    id: int
    kind: str
    severity: Literal["error", "warning"]
    objective_code: str | None
    message: str
    resolved_at: datetime | None
    resolved_by: str | None


class CurriculumIssuesPageOut(BaseModel):
    items: list[CurriculumIssueOut]
    total: int
    page: int
    per_page: int
