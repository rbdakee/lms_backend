"""Инструменты чата после генерации: правка материалов комплекта из разговора
(TEACHER_AI_BRIEF, раздел 1; решение владельца 15.10.2026: один чат — один
комплект, и «добавь слайд» говорят чату, а не нажимают звёздочку на блоке).

| Инструмент | Что делает |
|---|---|
| `get_material` | оглавление материала: блоки, сколько в них пунктов, заголовки |
| `get_block` | один блок целиком — текст по запросу, чтобы не раздувать токены |
| `rewrite_block` | переписать блок по просьбе — тем же механизмом, что кнопка ИИ |
| `add_item` | добавить пункт в блок-список (слайд, задание, шаг урока) |
| `replace_item` | заменить пункт блока-списка |
| `delete_item` | убрать пункт блока-списка |
| `set_objectives` | сменить набор целей урока: добавить, убрать, переставить |
| `regenerate_kit` | собрать материалы заново под текущие цели (все или выбранные виды) |
| `find_objectives`, `browse_objectives`, `show_objectives` | поиск новых целей, как в сборке |
| `find_image` | ключ картинки для слайда из каталога готовых изображений |

Своего пути к блокам у инструментов нет: каждая правка идёт через
`LessonMaterialsService` — ту же, что ручка кнопки ИИ и ручная правка учителя.
Поэтому проверка схемы вида, запись версии с пометкой источника (`regenerated`
или `edited`), пересборка ключей и замок на материал — общие, а не повторённые.
Блоки сервера (раздел программы, цель обучения с кодом) недоступны ни одному
инструменту: модель их не видит и не меняет.

Схема вида зафиксирована: блок — поле схемы, поэтому «новый слайд» — это новый
пункт в списке слайдов нужного этапа, а новый блок целиком завести нельзя.
Материал целиком заново собирает `regenerate_kit`: ставит генерацию в очередь,
старое содержимое остаётся в истории блоков, а блоки по одному переписывает
`rewrite_block`.

Результат успешной правки несёт `event` — его сервер положит в ленту событием,
по которому экран подсветит материал справа. Реестр админских действий сюда
не импортируется (раздел 6).
"""

from dataclasses import dataclass
from typing import Any

from app.adapters.db.lesson_ai_repos import LessonKitRepo
from app.adapters.db.models import LessonMaterial, User
from app.application.lesson_kits import LessonKitsService
from app.application.lesson_materials import LessonMaterialsService
from app.domain.lesson_ai import KIND_TITLES_RU, MATERIAL_KINDS, objective_codes_in
from app.lesson_ai import images, order_tools
from app.lesson_ai.context import strip_codes_deep

GET_MATERIAL = "get_material"
GET_BLOCK = "get_block"
REWRITE = "rewrite_block"
ADD = "add_item"
REPLACE = "replace_item"
DELETE = "delete_item"
SET_OBJECTIVES = "set_objectives"
FIND_IMAGE = "find_image"
REGENERATE = "regenerate_kit"
# Поиск целей — инструменты сборки заказа, без правок: читают справочник
SEARCH_TOOLS = (order_tools.SUBJECTS, order_tools.BROWSE, order_tools.SHOW, order_tools.FIND)

# Просьба учителя на кнопке ИИ — не длиннее 500 символов (`LessonBlockRegenerateIn`);
# у чата тот же потолок: инструкция уходит в тот же запрос к модели
INSTRUCTION_MAX = 500
# В оглавлении пункт показан заголовком или началом текста: по нему модель
# находит «слайд про сложение», а полный текст берёт `get_block`
PREVIEW_MAX = 60
# Слайды презентации лежат тремя блоками, а учитель считает их подряд
SLIDE_BLOCKS = ("slides_beginning", "slides_middle", "slides_end")

KIND_PROPERTY = {
    "type": "string",
    "enum": list(MATERIAL_KINDS),
    "description": "Вид материала комплекта: lesson_plan, presentation, tasks и так далее.",
}
BLOCK_PROPERTY = {
    "type": "string",
    "description": "Имя блока из оглавления материала, например slides_middle.",
}
INDEX_PROPERTY = {
    "type": "integer",
    "minimum": 1,
    "description": "Номер пункта в блоке, считая с единицы, как в оглавлении.",
}
ITEM_PROPERTY = {
    "type": ["object", "string"],
    "description": (
        "Новый пункт той же формы, что остальные пункты этого блока (посмотри блок"
        " через get_block): слайд — объект со всеми полями слайда (layout, title, lines,"
        " left_title, right_title, right_lines, fact, answer, image, animation, visual,"
        " notes — какое когда заполнять, сказано в описании слайда; чужие типу поля"
        " пустые); ключ картинки — только из find_image; пункт из строк — строка."
        " Текст — на языке комплекта. Кодов целей обучения в тексте нет."
    ),
}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


SCHEMAS = [
    _tool(
        GET_MATERIAL,
        "Оглавление готового материала комплекта: его блоки, сколько в каждом пунктов"
        " и заголовки пунктов. Текст блоков не возвращает — для него get_block.",
        {"kind": KIND_PROPERTY},
        ["kind"],
    ),
    _tool(
        GET_BLOCK,
        "Один блок материала целиком — его текущее содержимое. Берётся перед"
        " правкой, чтобы увидеть форму пунктов и не повторять уже сказанное.",
        {"kind": KIND_PROPERTY, "block": BLOCK_PROPERTY},
        ["kind", "block"],
    ),
    _tool(
        REWRITE,
        "Переписать блок по просьбе учителя: блок целиком, остальные не меняются."
        " Подходит для «сделай проще», «добавь больше игры», «перепиши середину»."
        " Просьбу передай словами учителя, коротко и конкретно.",
        {
            "kind": KIND_PROPERTY,
            "block": BLOCK_PROPERTY,
            "instruction": {
                "type": "string",
                "description": f"Что изменить, до {INSTRUCTION_MAX} символов.",
            },
        },
        ["kind", "block", "instruction"],
    ),
    _tool(
        ADD,
        "Добавить один пункт в блок-список: слайд в презентацию, задание на карточку,"
        " шаг в ход урока. Пункт пишешь сам, по форме соседних.",
        {
            "kind": KIND_PROPERTY,
            "block": BLOCK_PROPERTY,
            "item": ITEM_PROPERTY,
            "position": {
                "type": ["integer", "null"],
                "minimum": 1,
                "description": "На какое место поставить (считая с единицы); null — в конец блока.",
            },
        },
        ["kind", "block", "item"],
    ),
    _tool(
        REPLACE,
        "Заменить один пункт блока-списка новым той же формы.",
        {
            "kind": KIND_PROPERTY,
            "block": BLOCK_PROPERTY,
            "index": INDEX_PROPERTY,
            "item": ITEM_PROPERTY,
        },
        ["kind", "block", "index", "item"],
    ),
    _tool(
        DELETE,
        "Убрать один пункт из блока-списка. В блоке остаётся хотя бы один пункт.",
        {"kind": KIND_PROPERTY, "block": BLOCK_PROPERTY, "index": INDEX_PROPERTY},
        ["kind", "block", "index"],
    ),
]

SCHEMAS.append(
    _tool(
        SET_OBJECTIVES,
        "Сменить набор целей обучения урока: убрать лишние, добавить новые, поменять"
        " порядок. Передай полный новый набор — номера `id` целей в нужном порядке,"
        " включая те, что остаются. Номера бери из состояния комплекта (текущие цели)"
        " и из find_objectives, browse_objectives или show_objectives (новые). Цели"
        " только той же программы и того же класса, что у комплекта. Блоки «раздел»"
        " и «цели обучения» во всех материалах обновятся сами; остальное — критерии,"
        " цели урока, задания — ты не меняешь, а предлагаешь учителю переписать.",
        {
            "objective_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
                "description": "Полный новый набор целей в порядке учителя.",
            }
        },
        ["objective_ids"],
    )
)
SCHEMAS.append(
    _tool(
        REGENERATE,
        "Собрать материалы комплекта заново с ТЕКУЩИМИ целями и теми же параметрами урока"
        " (тема, уровни, пожелания). Без kinds — все материалы; с kinds — только эти виды."
        " Старое содержимое сохраняется в истории блоков. Идёт в очередь и занимает несколько"
        " минут; пересборка считается как один комплект из суточного лимита, поэтому запускай"
        " её только по согласию учителя. Пока комплект собирается, нельзя.",
        {
            "kinds": {
                "type": ["array", "null"],
                "items": KIND_PROPERTY,
                "description": "Какие виды пересобрать; null или пусто — все материалы.",
            }
        },
        [],
    )
)
# Описания инструментов поиска — те же, что у сборки заказа: часть кэшируемого начала запроса
SCHEMAS.extend(s for s in order_tools.SCHEMAS if s["function"]["name"] in SEARCH_TOOLS)
SCHEMAS.append(
    _tool(
        FIND_IMAGE,
        "Найти картинку для слайда в каталоге готовых изображений: по слову на русском,"
        " казахском или английском («яблоко», «алма», «rocket»). Возвращает ключи для"
        " поля image слайда. Картинок вне каталога нет, а выдуманный ключ слайд"
        " оставит без картинки — ключ бери только отсюда или из соседних слайдов.",
        {
            "query": {"type": "string", "description": "Слово или два: что нарисовано."},
        },
        ["query"],
    )
)


@dataclass
class EditCtx:
    """Кто зовёт инструмент и над каким комплектом. Комплект — тот, что привязан
    к чату; другого модель назвать не может, у инструментов нет номера комплекта."""

    svc: LessonMaterialsService
    kits: LessonKitRepo
    user: User
    kit_id: int | None
    # Смена целей — тем же сценарием, что и ручка экрана
    kits_svc: LessonKitsService
    # Поиск целей — контекст инструментов сборки: справочник, язык урока
    search: order_tools.OrderCtx


class EditTools:
    """Инструменты правки и поиска целей в том виде, в каком их ждёт общий цикл
    (`ai_loop.Toolbox`)."""

    def schemas(self) -> list[dict]:
        return SCHEMAS

    def call(self, name: str, args: dict, ctx: EditCtx) -> dict:
        if name in SEARCH_TOOLS:
            return order_tools.OrderTools().call(name, args, ctx.search)
        if name == FIND_IMAGE:
            return _find_image(args)
        handler = _HANDLERS.get(name)
        if handler is None and name not in (SET_OBJECTIVES, REGENERATE):
            names = ", ".join([*_HANDLERS, SET_OBJECTIVES, REGENERATE, FIND_IMAGE, *SEARCH_TOOLS])
            return _refusal("unknown_tool", f"Инструмента {name} нет. Доступны: {names}.")
        if ctx.kit_id is None:
            return _refusal("no_kit", "У этого чата нет комплекта — править нечего")
        if name == SET_OBJECTIVES:
            return _set_objectives(ctx, args)
        if name == REGENERATE:
            return _regenerate(ctx, args)
        material = _material(ctx, args.get("kind"))
        if isinstance(material, dict):
            return material
        return handler(ctx, material, args)


def _find_image(args: dict) -> dict:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return _refusal("bad_arguments", "Нужно слово: что должно быть на картинке")
    found = images.search(query.strip()[:80])
    return {
        "images": [{"key": i.key, "category": i.category, "ru": i.ru[:2]} for i in found],
        "note": "" if found else "Подходящей картинки нет: оставь поле image пустым",
    }


def _get_material(ctx: EditCtx, material: LessonMaterial, args: dict) -> dict:
    blocks = ctx.svc.blocks(ctx.user, material.id)["items"]
    return {
        "kind": material.kind,
        "blocks": [
            {
                "block": block["kind"],
                "editable": block["editable"],
                **_outline(block["content"], shown=block["editable"]),
            }
            for block in blocks
        ],
    }


def _get_block(ctx: EditCtx, material: LessonMaterial, args: dict) -> dict:
    block = _block(ctx, material, args.get("block"))
    if isinstance(block, dict) and "error" in block:
        return block
    if not block["editable"]:
        return _refusal("block_locked", "Этот блок берётся из программы и не правится")
    # Коды целей модели не показываем, как и при перегенерации блока: учитель
    # мог вписать код руками, а модель повторила бы его в новом пункте
    return {"block": block["kind"], "content": strip_codes_deep(block["content"])}


def _rewrite(ctx: EditCtx, material: LessonMaterial, args: dict) -> dict:
    block = _editable_block(ctx, material, args.get("block"))
    if "error" in block:
        return block
    instruction = args.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        return _refusal("bad_arguments", "Нужна просьба — что именно изменить в блоке")
    out = ctx.svc.regenerate(
        ctx.user, material.id, block["kind"], instruction.strip()[:INSTRUCTION_MAX]
    )
    return _changed(ctx, material, "rewritten", out, index=None)


def _add(ctx: EditCtx, material: LessonMaterial, args: dict) -> dict:
    block = _list_block(ctx, material, args.get("block"))
    if "error" in block:
        return block
    item = _item(args.get("item"))
    if "error" in item:
        return item
    items: list = list(block["content"])
    position = args.get("position")
    if position is None:
        position = len(items) + 1
    if (
        not isinstance(position, int)
        or isinstance(position, bool)
        or not 1 <= position <= len(items) + 1
    ):
        return _refusal("bad_arguments", f"Место — число от 1 до {len(items) + 1} или null")
    items.insert(position - 1, item["value"])
    out = ctx.svc.edit(ctx.user, material.id, block["kind"], items)
    return _changed(ctx, material, "item_added", out, index=position)


def _replace(ctx: EditCtx, material: LessonMaterial, args: dict) -> dict:
    block = _list_block(ctx, material, args.get("block"))
    if "error" in block:
        return block
    index = _index(args.get("index"), len(block["content"]))
    if isinstance(index, dict):
        return index
    item = _item(args.get("item"))
    if "error" in item:
        return item
    items: list = list(block["content"])
    items[index - 1] = item["value"]
    out = ctx.svc.edit(ctx.user, material.id, block["kind"], items)
    return _changed(ctx, material, "item_replaced", out, index=index)


def _delete(ctx: EditCtx, material: LessonMaterial, args: dict) -> dict:
    block = _list_block(ctx, material, args.get("block"))
    if "error" in block:
        return block
    index = _index(args.get("index"), len(block["content"]))
    if isinstance(index, dict):
        return index
    items: list = list(block["content"])
    del items[index - 1]
    # Пустой список схема вида не пропустит — отказ придёт от неё же
    out = ctx.svc.edit(ctx.user, material.id, block["kind"], items)
    return _changed(ctx, material, "item_deleted", out, index=index)


def _set_objectives(ctx: EditCtx, args: dict) -> dict:
    raw = args.get("objective_ids")
    ids = (
        [i for i in raw if isinstance(i, int) and not isinstance(i, bool)]
        if isinstance(raw, list)
        else []
    )
    if not ids or len(ids) != len(raw):
        return _refusal("bad_arguments", "Нужен список номеров целей (id), хотя бы один")
    # Ошибки набора (смешанный, не найден, не опубликован) — `AppError`: цикл
    # покажет их модели отказом, и она объяснит учителю
    kit = ctx.kits_svc.set_objectives(ctx.user, ctx.kit_id, ids)
    return {
        "ok": True,
        "objectives": kit["objectives"],
        "hint": (
            "Блоки «раздел» и «цели обучения» в материалах обновлены. Одним сообщением"
            " скажи, что изменилось, и предложи ровно два пути: «Пересобрать урок под"
            " новые цели» (рекомендуется) и «Поправить только связанное». Не спрашивай,"
            " что переписывать первым. Ничего не переписывай и не пересобирай до ответа."
        ),
        "event": {
            "type": "objectives_changed",
            "objectives": [{"id": o["id"], "code": o["code"]} for o in kit["objectives"]],
        },
    }


def _regenerate(ctx: EditCtx, args: dict) -> dict:
    raw = args.get("kinds")
    if raw is not None and (not isinstance(raw, list) or any(k not in MATERIAL_KINDS for k in raw)):
        return _refusal("bad_arguments", "Виды — список из: " + ", ".join(MATERIAL_KINDS))
    # Отказ лимита (429) и сборка (409) — `AppError`: цикл покажет их модели,
    # и она объяснит учителю
    kit = ctx.kits_svc.regenerate(ctx.user, ctx.kit_id, raw or None)
    return {
        "ok": True,
        "kinds": [m["kind"] for m in kit["materials"] if m["status"] == "queued"],
        "hint": (
            "Пересборка поставлена в очередь, займёт несколько минут; о готовности"
            " сообщит лента. Скажи об этом учителю одной-двумя фразами и не правь"
            " материалы, пока комплект собирается."
        ),
    }


_HANDLERS = {
    GET_MATERIAL: _get_material,
    GET_BLOCK: _get_block,
    REWRITE: _rewrite,
    ADD: _add,
    REPLACE: _replace,
    DELETE: _delete,
}


def _changed(
    ctx: EditCtx, material: LessonMaterial, action: str, block_out: dict, *, index: int | None
) -> dict:
    """Результат правки для модели и событие для ленты — одним ответом."""
    block = block_out["kind"]
    slide = _slide_number(ctx, material, block, index)
    event = {
        "type": "block_changed",
        "action": action,
        "material_id": material.id,
        "kind": material.kind,
        "block": block,
        "index": index,
        "slide": slide,
    }
    return {
        "ok": True,
        "block": block,
        "now": _outline(block_out["content"], shown=True),
        "event": event,
    }


def event_text(event: dict) -> str:
    """Подпись события словами — запасная, для экрана без своей вёрстки."""
    if event["type"] == "objectives_changed":
        return "Цели урока изменены: " + ", ".join(o["code"] for o in event["objectives"])
    title = KIND_TITLES_RU[event["kind"]]
    if event["action"] == "rewritten":
        return f"{title}: переписан блок «{event['block']}»"
    if event["slide"] is not None:
        place = f"слайд {event['slide']}"
    else:
        place = f"пункт {event['index']} блока «{event['block']}»"
    verb = {"item_added": "добавлен", "item_replaced": "изменён", "item_deleted": "удалён"}
    return f"{title}: {verb[event['action']]} {place}"


def _slide_number(
    ctx: EditCtx, material: LessonMaterial, block: str, index: int | None
) -> int | None:
    """Сквозной номер слайда презентации — так его называет учитель.
    Считается по блокам уже после правки: добавленный слайд сам занимает своё место."""
    if material.kind != "presentation" or block not in SLIDE_BLOCKS or index is None:
        return None
    before = 0
    for item in ctx.svc.blocks(ctx.user, material.id)["items"]:
        if item["kind"] == block:
            break
        if item["kind"] in SLIDE_BLOCKS:
            before += len(item["content"])
    # Впереди титульный слайд и «Сегодня на уроке» (блок goals) — это два слайда
    return 2 + before + index


def _outline(content: Any, *, shown: bool) -> dict:
    """Блок кратко: форма, число пунктов и заголовки. Закрытый блок — без
    содержимого вовсе."""
    if not shown:
        return {"type": "server"}
    if isinstance(content, list):
        return {"type": "list", "count": len(content), "items": [preview(i) for i in content]}
    if isinstance(content, dict):
        return {"type": "object", "keys": list(content)}
    return {"type": "text", "preview": preview(content)}


def preview(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("title", "teacher", "text", "name"):
            if isinstance(value.get(key), str):
                return clip(value[key])
        first = next((v for v in value.values() if isinstance(v, str)), "")
        return clip(first)
    return clip(value if isinstance(value, str) else str(value))


def clip(text: str) -> str:
    text = strip_codes_deep(" ".join(text.split()))
    return text if len(text) <= PREVIEW_MAX else text[: PREVIEW_MAX - 1] + "…"


def _material(ctx: EditCtx, kind: Any) -> LessonMaterial | dict:
    if kind not in MATERIAL_KINDS:
        return _refusal("bad_arguments", "Вид материала — один из: " + ", ".join(MATERIAL_KINDS))
    for material in ctx.kits.materials(ctx.kit_id):
        if material.kind == kind:
            if material.status != "ready":
                return _refusal(
                    "material_not_ready",
                    "Материал ещё собирается или не получился — править пока нечего",
                )
            return material
    return _refusal("no_material", f"В этом комплекте нет материала {kind}")


def _block(ctx: EditCtx, material: LessonMaterial, name: Any) -> dict:
    if not isinstance(name, str):
        return _refusal("bad_arguments", "Нужно имя блока из оглавления материала")
    for block in ctx.svc.blocks(ctx.user, material.id)["items"]:
        if block["kind"] == name:
            return block
    return _refusal("no_block", f"В материале нет блока {name}: сверься с оглавлением")


def _editable_block(ctx: EditCtx, material: LessonMaterial, name: Any) -> dict:
    block = _block(ctx, material, name)
    if "error" not in block and not block["editable"]:
        return _refusal("block_locked", "Этот блок берётся из программы и не правится")
    return block


def _list_block(ctx: EditCtx, material: LessonMaterial, name: Any) -> dict:
    block = _editable_block(ctx, material, name)
    if "error" not in block and not isinstance(block["content"], list):
        return _refusal(
            "not_a_list",
            "Блок не список пунктов — его можно только переписать целиком (rewrite_block)",
        )
    return block


def _item(value: Any) -> dict:
    if not isinstance(value, dict | str) or value in ("", {}):
        return _refusal(
            "bad_arguments", "Нужен пункт: объект той же формы, что соседние, или строка"
        )
    # Код цели в тексте модель могла только сочинить: ему неоткуда взяться
    if objective_codes_in(value):
        return _refusal("bad_arguments", "В тексте не должно быть кодов целей обучения")
    return {"value": value}


def _index(value: Any, size: int) -> int | dict:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= size:
        return _refusal("bad_arguments", f"Номер пункта — число от 1 до {size}")
    return value


def _refusal(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}
