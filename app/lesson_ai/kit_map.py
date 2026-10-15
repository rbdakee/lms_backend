"""Карта комплекта для режима правки: что в каждом виде материала и что в нём сейчас.

Два слоя, и оба не пишутся руками:

1. **Устройство видов** (`kinds_catalog`) — постоянное, часть системного промпта
   и потому кэшируемое. Блоки, их форма и подсказка берутся из схем видов
   (`app/lesson_ai/schemas`, `kinds.SPECS`): добавили блок в схему — он появился
   и здесь, расходиться нечему. Руками написано только «для чего вид» (`PURPOSE`):
   из схемы этого не вывести.
2. **Оглавление комплекта** (`kit_outline`) — меняющееся, в служебном сообщении
   перед репликой: по каждому материалу — блоки, число пунктов и короткая выжимка.
   Полный текст блока модель берёт `get_block`: здесь он раздул бы каждый запрос.
"""

import types
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from app.domain.lesson_ai import KIND_TITLES_RU, MATERIAL_KINDS
from app.lesson_ai.edit_tools import SLIDE_BLOCKS, preview
from app.lesson_ai.kinds import SPECS
from app.lesson_ai.prompts.order_chat import KIND_LABELS

# Для чего вид нужен учителю — из схемы не выводится
PURPOSE = {
    "lesson_plan": "краткосрочный план урока (КСП/ҚМЖ), который учитель сдаёт завучу",
    "presentation": "слайды к уроку: начало, середина, конец",
    "tasks": "карточки с заданиями на урок, по уровням",
    "homework": "домашнее задание",
    "assessment": "проверочная работа с дескрипторами",
    "game": "игра по теме урока",
    "answer_key": "ключи с ответами к заданиям, домашней и проверочной",
}
ANSWER_KEY_NOTE = (
    "собирает сервер из ответов в заданиях, домашней и проверочной; правка этих"
    " материалов пересобирает ключи сама, руками их не правят, блоки не правятся"
)
DESCRIPTION_MAX = 70
# Сколько пунктов блока показывать в оглавлении: остальные — счётчиком «ещё N».
# Оглавление не длиннее OUTLINE_MAX_CHARS (≈2,5 тыс. токенов): не влезло — выжимки
# урезаются ступенями, а структура (блоки и числа пунктов) остаётся
ITEMS_STEPS = (12, 6, 3, 1)
OUTLINE_MAX_CHARS = 10000
ITEM_PREVIEW_MAX = 50
MATERIAL_STATUS_LABELS = {
    "queued": "ждёт очереди",
    "generating": "собирается",
    "ready": "готов",
    "failed": "не получился",
}
SHAPES = {list: "список", dict: "объект", str: "текст", int: "число"}


def kinds_catalog() -> str:
    """Описание семи видов для системного промпта — из схем видов."""
    lines: list[str] = []
    for kind in MATERIAL_KINDS:
        head = f"- {kind} ({KIND_TITLES_RU[kind]}) — {PURPOSE[kind]}."
        spec = next((s for s in SPECS if s.kind == kind), None)
        if spec is None:
            lines.append(f"{head} Ключи {ANSWER_KEY_NOTE}.")
            continue
        lines.append(head)
        lines.append(
            "  Блоки сервера (не правятся): " + ", ".join(spec.server_blocks) + "."
        )
        for name, field in spec.draft.model_fields.items():
            shape = _shape(field.annotation)
            how = "add_item/replace_item/delete_item" if shape == "список" else "rewrite_block"
            lines.append(f"  · {name} — {shape}; {_first_sentence(field.description)} [{how}]")
    return "\n".join(lines)


def _shape(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin in (types.UnionType, Union):
        args = [a for a in get_args(annotation) if a is not type(None)]
        return _shape(args[0]) if args else "текст"
    if origin is not None:
        annotation = origin
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "объект"
    return SHAPES.get(annotation, "текст")


def _first_sentence(description: str | None) -> str:
    text = " ".join((description or "").split())
    for stop in (". ", ": ", " — "):
        cut = text.find(stop)
        if 0 < cut:
            text = text[:cut]
    if len(text) > DESCRIPTION_MAX:
        return text[: DESCRIPTION_MAX - 1].rstrip() + "…"
    return text.rstrip(" ,.;:—")


def kit_outline(materials: list[dict]) -> list[str]:
    """Строки оглавления всех материалов комплекта.

    `materials` — `{kind, status, blocks: [{name, content, server}]}`. Структура
    (какие блоки и сколько в них пунктов) показывается всегда, а выжимка
    урезается: слайды — заголовками со сквозными номерами, задания и шаги —
    началом текста.
    """
    lines: list[str] = []
    for shown in ITEMS_STEPS:
        lines = _outline(materials, shown)
        if sum(len(line) + 1 for line in lines) <= OUTLINE_MAX_CHARS:
            break
    return lines


def _outline(materials: list[dict], shown: int) -> list[str]:
    lines: list[str] = []
    for material in materials:
        title = KIND_LABELS[material["kind"]]
        status = MATERIAL_STATUS_LABELS[material["status"]]
        lines.append(f"  - {material['kind']} ({title}): {status}")
        # Впереди титульный слайд и «Сегодня на уроке» — слайды блоков идут с третьего
        slide = 3 if material["kind"] == "presentation" else None
        for block in material["blocks"]:
            if block["server"]:
                lines.append(f"    · {block['name']}: сервер, не правится")
                continue
            content = block["content"]
            numbered = slide is not None and block["name"] in SLIDE_BLOCKS
            first = slide if numbered else None
            lines.append("    · " + _block_line(block["name"], content, first, shown))
            if numbered and isinstance(content, list):
                slide += len(content)
    return lines


def _block_line(name: str, content: Any, first_slide: int | None, limit: int) -> str:
    if isinstance(content, list):
        shown = [_item(i, content[i]) if first_slide is None else _slide(first_slide, i, content[i])
                 for i in range(min(len(content), limit))]
        rest = f"; …ещё {len(content) - limit}" if len(content) > limit else ""
        return f"{name}: список, {len(content)}: " + "; ".join(shown) + rest
    if isinstance(content, dict):
        fields = ", ".join(
            f"{key} ({len(value)})" if isinstance(value, list) else key
            for key, value in content.items()
        )
        title = content.get("title")
        head = f" «{_short(title)}»" if isinstance(title, str) else ""
        return f"{name}: объект{head}, поля {fields}"
    return f"{name}: текст: {_short(preview(content))}"


def _item(index: int, value: Any) -> str:
    extra = ""
    if isinstance(value, dict) and isinstance(value.get("tasks"), list):
        extra = f" [{len(value['tasks'])} зад.]"
    return f"{index + 1}) {_short(preview(value))}{extra}"


def _slide(first: int, index: int, value: Any) -> str:
    return f"слайд {first + index} «{_short(preview(value))}»"


def _short(text: str) -> str:
    return text if len(text) <= ITEM_PREVIEW_MAX else text[: ITEM_PREVIEW_MAX - 1].rstrip() + "…"
