"""Карточка методических рекомендаций и загрузка каталога.

Каталог — данные, а не код: по файлу `.toml` на пакет в `catalog/`.
Пакет собран из методрекомендаций НАО им. Алтынсарина (оценивание музыки,
труда и ИЗО; письменные работы по языковым предметам). Добавить или
исправить рекомендацию — правка файла, промпты не трогаются.

Модель карточки строгая: неизвестное поле или не тот формат — ошибка
при загрузке, то есть при старте воркеров, а не тихо кривой метод
в сообщении модели.

Файлы каталога — данные, извлечённые из документов: формулировки
критериев и дескрипторов сохранены дословно. Содержимое правится
руками; при следующем обновлении документов его пересобирают.
"""

import tomllib
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict

CATALOG_DIR = Path(__file__).parent / "catalog"

Line = str


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MethodPack(_Strict):
    """Один пакет методических рекомендаций.

    `subjects` — коды предметов справочника (`music`, `labor`, `art`,
    `russian`, …): по ним пакет подбирается под заказ. `kinds` — виды
    материала, к которым рекомендации относятся.
    """

    id: str
    source: Line
    subjects: tuple[str, ...]
    kinds: tuple[str, ...]
    summary: Line
    generation_notes: tuple[Line, ...] = ()
    assessment_structure: tuple[Line, ...] = ()
    formative: tuple[Line, ...] = ()
    summative: tuple[Line, ...] = ()
    task_examples: tuple[Line, ...] = ()


def load_catalog(directory: Path = CATALOG_DIR) -> tuple[MethodPack, ...]:
    packs: list[MethodPack] = []
    for path in sorted(directory.glob("*.toml")):
        with path.open("rb") as file:
            try:
                pack = MethodPack.model_validate(tomllib.load(file))
            except Exception as exc:
                raise ValueError(f"{path.name}: {exc}") from exc
        packs.append(pack)
    return tuple(packs)


@cache
def catalog() -> tuple[MethodPack, ...]:
    return load_catalog()
