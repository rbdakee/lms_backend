"""Инструменты чата сбора заказа — ровно два, и оба только читают
опубликованный справочник (TEACHER_AI_BRIEF, раздел 6).

| Инструмент | Что делает |
|---|---|
| `find_objectives` | цели обучения по тексту: код наизусть не помнит никто |
| `get_objective` | цель по коду, если учитель его назвал |

Больше инструментов нет, и это граница по коду, а не по промпту: у модели
чата нет ни нашей базы, ни генерации материалов, ни чего-либо из реестра
админских действий — этот модуль его не импортирует. Заказ модель
не записывает вовсе: его пишет сервер (`app/application/lesson_ai_chat.py`).

Ответ инструмента — строки справочника **дословно**, с номером строки.
Экран рисует найденные цели карточками из этого ответа, и учитель
подтверждает строку базы, а не пересказ модели (раздел 1).
"""

from dataclasses import dataclass
from typing import Any

from app.adapters.db.lesson_ai_repos import LessonChatRepo, ObjectiveRow
from app.domain.lesson_ai import objective_code_in, search_stems

FIND = "find_objectives"
GET = "get_objective"

# Больше пяти карточек учитель не читает: он ищет свою, а не изучает программу.
# Потолок — на случай «покажи ещё»
FIND_LIMIT = 5
FIND_LIMIT_MAX = 10
LANGS = ("ru", "kz")

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": FIND,
            "description": (
                "Найти цели обучения в опубликованной школьной программе по словам из их"
                " формулировки. Возвращает строки справочника с кодом и номером `id`."
                " Пиши запрос словами, какими цели записаны в программе:"
                " «сложение вычитание двузначных чисел без перехода через десяток»,"
                " а не «складываем двузначные»."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Слова из формулировки цели или код цели.",
                    },
                    "grade": {
                        "type": ["integer", "null"],
                        "description": "Класс, если учитель его назвал; иначе null.",
                    },
                    "lang": {
                        "type": ["string", "null"],
                        "enum": ["ru", "kz", None],
                        "description": (
                            "Редакция программы: ru — русская, kz — казахская. null — обе."
                            " Слова запроса должны быть на языке этой редакции."
                        ),
                    },
                    "limit": {
                        "type": ["integer", "null"],
                        "description": (
                            f"Сколько целей вернуть, до {FIND_LIMIT_MAX}; null — {FIND_LIMIT}."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": GET,
            "description": (
                "Взять цель обучения по коду вида 2.1.2.6 из опубликованной программы."
                " Возвращает строку справочника в каждой редакции, где цель есть."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Код цели, четыре числа через точку.",
                    },
                    "lang": {
                        "type": ["string", "null"],
                        "enum": ["ru", "kz", None],
                        "description": "Редакция программы; null — обе.",
                    },
                },
                "required": ["code"],
            },
        },
    },
]


@dataclass
class OrderCtx:
    """Кто зовёт инструмент. Учитель и его чат — больше чату знать нечего."""

    repo: LessonChatRepo
    user_id: int
    chat_id: int


class OrderTools:
    """Два инструмента в том виде, в каком их ждёт общий цикл (`ai_loop.Toolbox`)."""

    def schemas(self) -> list[dict]:
        return SCHEMAS

    def call(self, name: str, args: dict, ctx: OrderCtx) -> dict:
        if name == FIND:
            return find_objectives(ctx, args)
        if name == GET:
            return get_objective(ctx, args)
        # Модель попросила то, чего у неё нет, — например, «сгенерировать план»
        # или админский инструмент из чужого промпта. Отказ читает она сама
        return _refusal("unknown_tool", f"Инструмента {name} нет. Доступны: {FIND}, {GET}.")


def find_objectives(ctx: OrderCtx, args: dict) -> dict:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return _refusal("bad_arguments", "Нужен запрос — слова из формулировки цели")
    grade = args.get("grade")
    if grade is not None and (not isinstance(grade, int) or not 1 <= grade <= 12):
        return _refusal("bad_arguments", "Класс — число от 1 до 12 или null")
    lang = args.get("lang")
    if lang is not None and lang not in LANGS:
        return _refusal("bad_arguments", "Редакция — ru, kz или null")
    limit = args.get("limit") or FIND_LIMIT
    if not isinstance(limit, int) or limit < 1:
        return _refusal("bad_arguments", f"Сколько целей — число от 1 до {FIND_LIMIT_MAX}")
    stems = search_stems(query)
    code = objective_code_in(query)
    if not stems and not code:
        return _refusal("bad_arguments", "В запросе нет ни одного слова из формулировки цели")
    rows = ctx.repo.find_objectives(
        stems, code=code, grade=grade, lang=lang, limit=min(limit, FIND_LIMIT_MAX)
    )
    return {"items": [objective_item(row) for row in rows]}


def get_objective(ctx: OrderCtx, args: dict) -> dict:
    raw = args.get("code")
    code = objective_code_in(raw) if isinstance(raw, str) else None
    if code is None:
        return _refusal("bad_arguments", "Код цели — четыре числа через точку, например 2.1.2.6")
    lang = args.get("lang")
    if lang is not None and lang not in LANGS:
        return _refusal("bad_arguments", "Редакция — ru, kz или null")
    rows = ctx.repo.objectives_by_code(code, lang)
    if not rows:
        return _refusal("not_found", f"Цели {code} в опубликованной программе нет")
    return {"items": [objective_item(row) for row in rows]}


def objective_item(row: ObjectiveRow) -> dict[str, Any]:
    """Цель так, как её видят и модель, и карточка на экране: строка
    справочника дословно, с кодом и пометкой из приказа."""
    objective, program, subject, section, subsection = row
    return {
        "id": objective.id,
        "code": objective.code,
        "text": objective.text,
        "marker": objective.marker,
        "grade": objective.grade,
        "lang": program.lang,
        "subject_code": subject.code,
        "section": section,
        "subsection": subsection,
    }


def _refusal(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}
