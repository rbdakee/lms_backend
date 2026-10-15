"""Каталог картинок для презентации и поиск по нему.

Каталог — данные, а не код: `catalog.toml` и PNG в `files/`, по образцу каталога
игр (`games/catalog`). Модель картинок не рисует, а называет ключ; ключ, которого
здесь нет, — не ошибка ответа, а слайд без картинки (решение владельца 15.10.2026,
TEACHER_AI_BRIEF, «Закрыто»). Загрузка строгая: запись без файла или дубль ключа —
ошибка при старте, а не пустое место в чужой презентации.
"""

import logging
import tomllib
from functools import cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
FILES = ROOT / "files"

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]


class Image(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Slug
    file: str
    src: str
    category: Slug
    ru: list[str]
    kz: list[str]
    en: list[str]

    @model_validator(mode="after")
    def _file_exists(self):
        if not (FILES / self.file).is_file():
            raise ValueError(f"нет файла {self.file}")
        return self


@cache
def catalog() -> dict[str, Image]:
    raw = tomllib.loads((ROOT / "catalog.toml").read_text(encoding="utf-8"))
    found: dict[str, Image] = {}
    for item in raw["image"]:
        image = Image.model_validate(item)
        if image.key in found:
            raise ValueError(f"ключ картинки повторяется: {image.key}")
        found[image.key] = image
    return found


def lookup(key: str) -> Image | None:
    return catalog().get(key.strip().lower())


def png(key: str) -> bytes | None:
    image = lookup(key)
    return (FILES / image.file).read_bytes() if image else None


def known_or_empty(key: str) -> str:
    """Ключ слайда после проверки: свой — как есть, чужой — пусто с записью
    в лог. Сам ключ в лог не идёт: его могла написать модель по тексту урока."""
    key = key.strip().lower()
    if not key:
        return ""
    if key in catalog():
        return key
    log.warning("презентация: ключа картинки нет в каталоге, слайд без картинки")
    return ""


def index() -> str:
    """Список ключей по разделам — для системного промпта. Он один на все заказы,
    поэтому кладётся в систему, а не в сообщение: провайдер берёт начало запроса
    из кэша, а каталог меняется только вместе с кодом."""
    groups: dict[str, list[str]] = {}
    for image in catalog().values():
        groups.setdefault(image.category, []).append(image.key)
    return "\n".join(f"{category}: {', '.join(keys)}" for category, keys in groups.items())


def search(query: str, limit: int = 8) -> list[Image]:
    """Поиск для помощника в чате: слово или часть слова на ru, kz или en.
    Совпадение с началом слова выше, чем с серединой; без ИИ и индексов —
    каталог на сотни строк."""
    words = [w for w in query.lower().replace(",", " ").split() if w]
    if not words:
        return []
    scored = []
    for image in catalog().values():
        terms = [image.key.replace("_", " "), *image.ru, *image.kz, *image.en]
        score = 0
        for word in words:
            if any(t == word for t in terms):
                score += 3
            elif any(t.startswith(word) for t in terms):
                score += 2
            elif any(word in t for t in terms):
                score += 1
        if score:
            scored.append((-score, image.key, image))
    return [image for *_, image in sorted(scored, key=lambda x: x[:2])[:limit]]
