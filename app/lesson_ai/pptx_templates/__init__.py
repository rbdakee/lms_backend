"""Библиотека шаблонов презентации — каталог и его сверка с `library.pptx`
(PPTX_TEMPLATES_SPEC, раздел 1).

Библиотека — данные, а не код: `catalog.toml` и `library.pptx` в каталоге
пакета (по образцу `lesson_ai/images`). Модель выбирает шаблон ключом, а
сервер собирает слайд из заготовки; расхождение каталога с файлом ловится
здесь, один раз, а не в середине сборки чужого урока.

Путь к библиотеке — настройка `PPTX_TEMPLATES_DIR`; пустая значит «рядом
с кодом». Библиотеки нет — вид `presentation` работает по-старому
(предупреждение при старте, `check_start`); библиотека есть, но битая —
отказ при старте: половина слотов не соберётся молча.

Пока библиотеки в репозитории нет (её делает дизайн параллельно), проверки
каталога гоняются в тестах на библиотеке-фикстуре (`tests/fixtures/`).
"""

import logging
import tomllib
from functools import cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.config import get_settings

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
LIBRARY_FILE = "library.pptx"
CATALOG_FILE = "catalog.toml"

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]

# kind слота — что в него вписывает исполнитель (раздел 1 спеки)
SLOT_KINDS = ("text", "bullets", "number", "image")
# Где уместен шаблон — те же этапы, что у блоков слайдов презентации
STAGES = ("beginning", "middle", "end")


class Slot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Slug
    kind: Literal["text", "bullets", "number", "image"]
    max_chars: int | None = None
    max_items: int | None = None
    required: bool = False
    # Нижняя граница кегля при переполнении; пусто — обычные 24 pt (спека, п. 6)
    min_pt: int | None = Field(default=None, ge=10, le=72)


class DeckTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Slug
    slide: int = Field(ge=1)
    title: str
    purpose: str
    stage: list[Literal["beginning", "middle", "end"]]
    slots: list[Slot]
    # Имена украшений, которые сценарий может удалить, — без префикса `deco:`
    removable: list[str] = Field(default_factory=list)
    # Украшения-спутники: имя украшения → условия «за какими слотами оно
    # следует». Условие — пара (слот, N): слот пуст или пунктов в нём меньше N.
    # Украшение уходит вместе со слотом, когда не выполнилось ни одно условие;
    # модель об этом не просит — исполнитель делает сам (спека, п. 4)
    follows: dict[str, tuple[tuple[str, int | None], ...]] = Field(default_factory=dict)
    # Порядок появления по клику по умолчанию: имена слотов
    default_order: list[str] = Field(default_factory=list)


@cache
def load(directory: Path) -> dict[str, DeckTemplate]:
    """Каталог со сверкой фигур: каждый слот обязан быть фигурой `slot:<name>`
    на своём слайде, каждое украшение — `deco:<name>`. Ошибка здесь — при
    старте, а не на слайде у учителя."""
    raw = tomllib.loads((directory / CATALOG_FILE).read_text(encoding="utf-8"))
    found: dict[str, DeckTemplate] = {}
    for item in raw.get("template") or []:
        template = DeckTemplate(
            key=item["key"],
            slide=item["slide"],
            title=item["title"],
            purpose=item["purpose"],
            stage=item["stage"],
            slots=item.get("slot") or [],
            removable=(item.get("deco") or {}).get("removable") or [],
            follows=_parse_follows((item.get("deco") or {}).get("follows") or {}),
            default_order=(item.get("animation") or {}).get("default") or [],
        )
        if template.key in found:
            raise ValueError(f"ключ шаблона повторяется: {template.key}")
        found[template.key] = template
    if not found:
        raise ValueError("каталог пуст: ни одного шаблона")
    _check_shapes(directory, found)
    return found


def _parse_follows(raw: dict) -> dict[str, tuple[tuple[str, int | None], ...]]:
    """`follows = { picture_bg = "picture", row4 = ["left:4", "right:4"] }` —
    значения TOML приводятся к парам (слот, минимум пунктов)."""
    follows = {}
    for deco, value in raw.items():
        if isinstance(value, str):
            value = [value]
        follows[deco] = tuple(_condition(str(c)) for c in value)
    return follows


def _condition(value: str) -> tuple[str, int | None]:
    name, _, count = value.partition(":")
    if not count:
        return name, None
    if not count.isdigit() or int(count) < 1:
        raise ValueError(f"в deco.follows условие {value!r}: число пунктов должно быть целым ≥ 1")
    return name, int(count)


def _check_shapes(directory: Path, found: dict[str, DeckTemplate]) -> None:
    from pptx import Presentation

    deck = Presentation(directory / LIBRARY_FILE)
    for template in found.values():
        if template.slide > len(deck.slides):
            raise ValueError(f"{template.key}: в library.pptx нет слайда {template.slide}")
        names = {shape.name for shape in deck.slides[template.slide - 1].shapes}
        for slot in template.slots:
            if f"slot:{slot.name}" not in names:
                raise ValueError(
                    f"{template.key}: на слайде {template.slide} нет фигуры slot:{slot.name}"
                )
        for name in template.removable:
            if f"deco:{name}" not in names:
                raise ValueError(
                    f"{template.key}: на слайде {template.slide} нет фигуры deco:{name}"
                )
        slots = {slot.name: slot for slot in template.slots}
        for deco, conditions in template.follows.items():
            if f"deco:{deco}" not in names:
                raise ValueError(
                    f"{template.key}: на слайде {template.slide} нет фигуры deco:{deco}"
                )
            for name, at_least in conditions:
                slot = slots.get(name)
                if slot is None:
                    raise ValueError(
                        f"{template.key}: в deco.follows имя {name!r} — не слот шаблона"
                    )
                if at_least and (slot.kind != "bullets" or at_least > (slot.max_items or 0)):
                    raise ValueError(
                        f"{template.key}: в deco.follows «{name}:{at_least}» — число пунктов"
                        " только для bullets и не больше max_items"
                    )
        known = {slot.name for slot in template.slots}
        for name in template.default_order:
            if name not in known:
                raise ValueError(
                    f"{template.key}: в animation.default имя {name!r} — не слот шаблона"
                )


def root() -> Path:
    """Каталог библиотеки этой установки: настройка или каталог рядом с кодом."""
    custom = get_settings().pptx_templates_dir
    return Path(custom) if custom else ROOT


def available(directory: Path | None = None) -> bool:
    """Библиотека читается целиком? Битая приравнена к отсутствующей:
    вид работает по-старому, а причину назовёт `check_start`."""
    try:
        load(directory or root())
    except Exception:  # нет файла, битый томl, кривые фигуры
        return False
    return True


def check_start() -> None:
    """Проверка при старте сервиса. Библиотеки нет — предупреждение и старый
    путь (это штатный деплой, пока дизайн не сдал библиотеку); библиотека
    битая — отказ: молча потерянные слоты дороже упавшего старта.
    Зовётся из `check_providers`; импорт там локальный — модуль сам читает
    настройки, и при старте `app.config` ещё в сборке."""
    path = root()
    try:
        load(path)
    except FileNotFoundError:
        log.warning(
            "PPTX_TEMPLATES_DIR: библиотеки шаблонов презентации нет (%s) —"
            " презентация собирается по-старому, восемью типами слайдов",
            path,
        )
    except Exception as exc:
        raise RuntimeError(f"библиотека шаблонов презентации {path} не читается: {exc}") from exc


def index(found: dict[str, DeckTemplate]) -> str:
    """Каталог словами для модели — компактно, один на все заказы: постоянная
    часть промпта, провайдер берёт её из кэша. Лимиты слотов названы, чтобы
    модель держала текст коротким, а не для отсечки: превышение — мельче
    кегль и предупреждение, не отказ."""
    lines = []
    for template in found.values():
        slots = ", ".join(_slot_label(slot) for slot in template.slots)
        stages = ",".join(template.stage)
        lines.append(
            f"- {template.key} — {template.title} [{stages}]: {template.purpose}. Слоты: {slots}"
        )
    return "\n".join(lines)


def _slot_label(slot: Slot) -> str:
    label = f"{slot.name} ({slot.kind}"
    if slot.max_items:
        label += f", до {slot.max_items}"
    if slot.max_chars:
        label += f", до {slot.max_chars} зн."
    if slot.required:
        label += ", обязательно"
    return label + ")"
