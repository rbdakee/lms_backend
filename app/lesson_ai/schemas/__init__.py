"""Схемы ответа модели — по файлу на вид материала — и перевод pydantic-модели
в JSON-схему строгого режима провайдера.

Одна pydantic-модель даёт обе вещи сразу: схему, по которой модель отвечает,
и проверку того, что она ответила. Два описания одной формы разошлись бы
с первой правки.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Строка, в которой есть что-то кроме пробелов. Строгий режим провайдера
# гарантирует, что поле есть, но не что оно не пустое: пустое ловит проверка
# ответа, и тогда материал генерируется заново.
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# Блоки, которые кладёт сервер у всех видов, кроме плана и ключей: раздел
# программы и цель обучения с кодом, дословно из справочника. Модель их
# не пишет — поля кода в схемах нет ни у одного вида (раздел 4.1, правило 2).
SERVER_BLOCKS = ("section", "learning_objectives")


class Strict(BaseModel):
    # Лишнее поле в ответе — ответ не по форме: так модель не может
    # дописать от себя ни код цели, ни ещё один блок
    model_config = ConfigDict(extra="forbid")


class Task(Strict):
    """Задача вместе с ответом — в одной схеме, в один момент.

    Модель, решающая чужой список задач, ошибается заметно чаще той, что
    пишет ответ к своей же задаче (раздел 4.2). Ключи с ответами собирает
    из этих полей сервер, без модели: любой объект с `text` и `answer`
    внутри группы (`TaskGroup`) попадает в ключи.
    """

    text: Text = Field(
        description=(
            "Условие задания так, как его прочтёт ученик: пример, задача, вопрос."
            " Конкретно, с числами."
        )
    )
    answer: Text = Field(
        description=(
            "Верный ответ к этому заданию. Для задачи — ответ и краткое решение"
            " (действие и результат); для открытого задания — образец ответа."
            " Проверь ответ дважды: по нему учитель проверяет тетради."
        )
    )


class TaskGroup(Strict):
    """Группа заданий — карточка, вариант, уровень. Ключи с ответами
    печатают её заголовок и номера заданий внутри неё."""

    title: Text = Field(description="Заголовок группы, как он напечатан на листе.")
    tasks: list[Task] = Field(min_length=1, description="Задания группы по порядку.")


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
