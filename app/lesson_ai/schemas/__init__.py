"""Схемы ответа модели — по файлу на вид материала — и перевод pydantic-модели
в JSON-схему строгого режима провайдера.

Одна pydantic-модель даёт обе вещи сразу: схему, по которой модель отвечает,
и проверку того, что она ответила. Два описания одной формы разошлись бы
с первой правки.
"""

from typing import Annotated

from pydantic import BaseModel, StringConstraints

# Строка, в которой есть что-то кроме пробелов. Строгий режим провайдера
# гарантирует, что поле есть, но не что оно не пустое: пустое ловит проверка
# ответа, и тогда материал генерируется заново.
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# Ключи JSON-схемы, которые строгий режим принимает наверняка. Остальное —
# `title`, `minLength`, `minItems` — к модели не уходит: часть ключей строгий
# режим отвергает целиком, и запрос падал бы, а не отвечал хуже. Ограничения
# при этом не теряются — их держит pydantic-модель на проверке ответа.
_KEEP = {"type", "description", "items", "enum", "anyOf"}


def strict_schema(model: type[BaseModel]) -> dict:
    """JSON-схема строгого режима из pydantic-модели.

    Ссылки (`$ref`) разворачиваются на месте: рядом со ссылкой нельзя
    оставить описание поля, а без описания модель не знает, что в блок писать.
    У каждого объекта все поля обязательны и лишних нет — это условие
    строгого режима, и оно же не даёт модели дописать блок от себя
    (например, код цели обучения).
    """
    root = model.model_json_schema()
    return _strict(root, root.get("$defs", {}))


def _strict(node: dict, defs: dict) -> dict:
    ref = node.get("$ref")
    if ref is not None:
        # Описание поля главнее описания модели, на которую оно ссылается
        node = {**defs[ref.rsplit("/", 1)[-1]], **{k: v for k, v in node.items() if k != "$ref"}}
    out: dict = {}
    for key, value in node.items():
        if key == "properties":
            out["properties"] = {name: _strict(sub, defs) for name, sub in value.items()}
        elif key == "items":
            out["items"] = _strict(value, defs)
        elif key == "anyOf":
            out["anyOf"] = [_strict(sub, defs) for sub in value]
        elif key in _KEEP:
            out[key] = value
    if out.get("type") == "object":
        out["required"] = list(out.get("properties", {}))
        out["additionalProperties"] = False
    return out
