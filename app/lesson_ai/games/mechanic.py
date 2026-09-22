"""Карточка игровой механики и загрузка каталога.

Каталог — данные, а не код: по файлу `.toml` на механику в `catalog/`. Добавить
или убрать механику — добавить или убрать файл; ни загрузчик, ни отбор имён
механик не знают и не должны знать (решение владельца, 22.09.2026). Карточки —
строительные блоки для модели, а не меню: схема `game` от каталога не зависит
и остаётся широкой (TEACHER_AI_BRIEF, раздел 4.3; `L5_games_db_design.md`).

Модель карточки строгая: неизвестное поле, значение вне перечисления,
пропущенный уровень в `difficulty_scaling` — ошибка при загрузке, то есть при
старте воркеров, а не тихо кривая игра. Имена полей равны будущим колонкам
таблицы `lesson_game_mechanic` (дизайн, раздел 5.2): переезд в базу — одна
миграция данных, модель остаётся той же.

Перечисления здесь — те, по которым фильтрует и печатает код. Семейство
механики (`family`) и темы (`math_topics`) — открытые слаги: новое семейство
не должно требовать правки кода, оно нужно отбору только для разнообразия.
"""

import tomllib
from datetime import date
from functools import cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

CATALOG_DIR = Path(__file__).parent / "catalog"

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

Engagement = Literal["calm", "active", "full_game"]
Phase = Literal["start", "middle", "end", "whole_lesson"]
Grouping = Literal["individual", "pairs", "small_groups", "teams", "whole_class"]
PrintUnit = Literal["per_student", "per_pair", "per_group", "per_team", "per_class"]
ContentShape = Literal[
    "short_answer",
    "pairs",
    "true_false",
    "multi_step",
    "ordering",
    "classification",
    "spatial",
    "measurement",
    "money",
    "open_response",
]
Prop = Literal[
    "die", "counters", "scissors", "glue", "envelopes", "pencils", "timer", "ruler", "magnets"
]
ServerAsset = Literal["die_net", "tokens", "diploma", "stickers", "money"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DifficultyScaling(_Strict):
    """Уровень класса — не фильтр, а масштаб: механика та же, меняется
    наполнение. Модели уходит одна строка — уровня заказа."""

    basic: Line
    standard: Line
    advanced: Line


class LangNotes(_Strict):
    ru: str = ""
    kz: str = ""


class PrintItem(_Strict):
    name: Line
    per: PrintUnit
    # Словами: «по числу детей», «20–30» — число зависит от класса и урока
    count: Line
    layout: Literal["a4_sheet", "card_grid", "board_a4", "board_a3_split", "strip"]
    cut: Literal["none", "grid", "contour"]
    double_sided: bool = False


class Mechanic(_Strict):
    # -- кто она
    id: Slug
    title: Line
    family: Slug
    status: Literal["draft", "active", "retired"]
    summary: Line
    # -- по чему отбирают
    subjects: list[Slug] = Field(min_length=1)
    content_shapes: list[ContentShape] = Field(min_length=1)
    math_topics: list[Slug] = []
    grade_min: int = Field(ge=1, le=11)
    grade_max: int = Field(ge=1, le=11)
    # Первый — родной уровень механики
    engagement_levels: list[Engagement] = Field(min_length=1)
    level_shift: str = ""
    lesson_phases: list[Phase] = Field(min_length=1)
    duration_min: int = Field(ge=1)
    duration_max: int = Field(ge=1)
    grouping: list[Grouping] = Field(min_length=1)
    print_unit: PrintUnit
    group_size_min: int = Field(ge=1)
    group_size_max: int = Field(ge=1)
    rules_complexity: Literal["simple", "medium", "complex"]
    student_reading: Literal["none", "short", "sentences"]
    movement: Literal["seated", "at_board", "around_class"]
    noise: Literal["quiet", "voiced", "loud"]
    competition: Literal["none", "individual", "team"]
    teacher_role: Literal["host", "referee", "facilitator", "observer"]
    # -- что уходит модели
    scoring: Line
    difficulty_scaling: DifficultyScaling
    in_class_differentiation: str = ""
    self_check: Literal["answer_key", "self_revealing", "peer_check", "teacher_check"]
    invariants: list[Line] = []
    model_generates: list[Line] = Field(min_length=1)
    pitfalls: list[Line] = []
    combines_with: list[Slug] = []
    station_ready: bool
    lang_notes: LangNotes = LangNotes()
    lang_sensitive: bool
    # -- что печатать
    print_items: list[PrintItem] = Field(min_length=1)
    props: list[Prop] = []
    server_assets: list[ServerAsset] = []
    prep_effort: Literal["none", "low", "medium", "high"]
    replayability: Literal["single_use", "replayable", "reusable_frame"]
    # -- откуда и кто проверил
    source: Line
    reviewed_by: str = ""
    reviewed_at: date | None = None
    export_shape: Literal["quiz_4_options", "true_false", "matching", "none"]

    @model_validator(mode="after")
    def _consistent(self):
        if self.grade_min > self.grade_max:
            raise ValueError("grade_min больше grade_max")
        if self.duration_min > self.duration_max:
            raise ValueError("duration_min больше duration_max")
        if self.group_size_min > self.group_size_max:
            raise ValueError("group_size_min больше group_size_max")
        # Тихая механика с беготней по классу — ошибка каталога, а не игра:
        # учитель, выбравший «спокойно», её не ждёт (дизайн, раздел 2.1)
        if self.engagement_levels[0] == "calm" and self.movement == "around_class":
            raise ValueError("родной уровень calm, а movement = around_class")
        return self


class CatalogError(ValueError):
    """Каталог не собрался: карточка кривая или ссылается на несуществующую."""


def load_catalog(directory: Path = CATALOG_DIR) -> tuple[Mechanic, ...]:
    """Все карточки каталога, в порядке имён файлов.

    Слаг — вечный ключ: он пишется в материал и в статистику. Поэтому имя
    файла обязано совпадать с `id` — иначе переименованный файл молча дал бы
    вторую механику под старым слагом.
    """
    mechanics: list[Mechanic] = []
    for path in sorted(directory.glob("*.toml")):
        with path.open("rb") as file:
            try:
                mechanic = Mechanic.model_validate(tomllib.load(file))
            except (tomllib.TOMLDecodeError, ValueError) as exc:
                raise CatalogError(f"{path.name}: {exc}") from None
        if mechanic.id != path.stem:
            raise CatalogError(f"{path.name}: id «{mechanic.id}» не совпадает с именем файла")
        mechanics.append(mechanic)
    ids = {mechanic.id for mechanic in mechanics}
    for mechanic in mechanics:
        missing = [other for other in mechanic.combines_with if other not in ids]
        if missing:
            raise CatalogError(f"{mechanic.id}: combines_with ссылается на {missing}")
    return tuple(mechanics)


@cache
def catalog() -> tuple[Mechanic, ...]:
    """Каталог в памяти процесса: карточки — килобайты, читаются один раз.
    Правка карточки — перезапуск сервиса, как любая правка кода."""
    return load_catalog()
