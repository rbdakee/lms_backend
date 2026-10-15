"""Инструменты чата подготовки урока — только читают опубликованный справочник
(TEACHER_AI_BRIEF, раздел 6).

| Инструмент | Что делает |
|---|---|
| `list_subjects` | какие предметы и классы есть в программе и на каком языке написаны цели |
| `browse_objectives` | компактный список целей класса по разделам: смысловой выбор на любом языке |
| `find_objectives` | цели по словам (несколько вариантов запроса сразу) |
| `get_objective` | цель по коду, если учитель его назвал |
| `show_objectives` | показать учителю выбранные цели карточками |

Больше инструментов нет, и это граница по коду, а не по промпту: у модели
чата нет ни нашей базы, ни генерации материалов, ни чего-либо из реестра
админских действий — этот модуль его не импортирует. Урок модель
не записывает вовсе: его пишет сервер (`app/application/lesson_ai_chat.py`).

Ответ с ключом `items` — строки справочника **дословно**, с номером строки.
Экран рисует найденные цели карточками из этого ответа, и учитель
подтверждает строку базы, а не пересказ модели (раздел 1). Поэтому
`browse_objectives` ключа `items` не отдаёт: это список для выбора моделью, а карточки
получаются только у тех целей, которые она затем показала `show_objectives`.
"""

import json
from dataclasses import dataclass
from typing import Any

from app.adapters.curriculum.parser import objectives_language, subject_title_kz
from app.adapters.db.lesson_ai_repos import LessonChatRepo, ObjectiveRow
from app.domain.lesson_ai import objective_code_in, search_stems

FIND = "find_objectives"
GET = "get_objective"
SUBJECTS = "list_subjects"
BROWSE = "browse_objectives"
SHOW = "show_objectives"

# Больше пяти карточек учитель не читает: он ищет свою, а не изучает программу.
# Потолок — на случай «покажи ещё»
FIND_LIMIT = 5
FIND_LIMIT_MAX = 10
LANGS = ("ru", "kz")
QUERIES_MAX = 5
SHOW_MAX = 10
# Цель в списке обзора усекается: модели нужен смысл, чтобы выбрать, а полный
# текст она получит карточкой
BROWSE_TEXT_MAX = 120
# Список класса больше этого (≈3 тыс. токенов) отдаётся сначала разделами,
# потом целями выбранного раздела
BROWSE_MAX_CHARS = 8000
LANGUAGE_NAMES = {
    "ru": "русском",
    "kz": "казахском",
    "en": "английском",
    "de": "немецком",
    "fr": "французском",
}

# Один и тот же фрагмент у обоих инструментов: описание — часть кэшируемого начала запроса
SUBJECT_PROPERTY = {
    "type": ["string", "null"],
    "description": (
        "Код предмета из справочника (например math, music, russian_lang_l1), если"
        " учитель назвал предмет или он известен из прежнего ответа инструмента;"
        " иначе null. Выдуманный код — ошибка unknown_subject."
    ),
}

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": SUBJECTS,
            "description": (
                "Какие предметы есть в опубликованной программе: код, название, классы"
                " по редакциям и `objectives_language` — язык, на котором написан текст"
                " целей (ru, kz, en, de, fr; у иностранных языков цели на изучаемом языке"
                " в обеих редакциях). Зови, когда учитель не назвал предмет или класс,"
                " либо поиск ничего не нашёл: так видно, чего в программе нет."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": BROWSE,
            "description": (
                "Список целей одного класса предмета, сгруппированный по разделам: код,"
                " номер `id` и усечённый текст. Бери, когда поиск ничего не дал или учитель"
                " описал урок общими словами: выбери подходящие по смыслу сам, на любом"
                " языке, и покажи их учителю инструментом show_objectives. Список большого"
                " класса приходит сначала разделами (`sections` с `count`) — повтори вызов"
                " с `section`, чтобы получить цели раздела."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject_code": SUBJECT_PROPERTY,
                    "grade": {"type": "integer", "description": "Класс."},
                    "lang": {
                        "type": ["string", "null"],
                        "enum": ["ru", "kz", None],
                        "description": (
                            "Редакция программы; null — язык класса, если он назван,"
                            " иначе русская."
                        ),
                    },
                    "section": {
                        "type": ["string", "null"],
                        "description": "Название раздела из прежнего ответа; иначе null.",
                    },
                },
                "required": ["subject_code", "grade"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SHOW,
            "description": (
                "Показать учителю выбранные цели карточками, чтобы он отметил нужные."
                " Принимает номера `id` из browse_objectives; возвращает строки справочника"
                " дословно. Выдуманный номер не покажется."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": f"Номера целей, до {SHOW_MAX}.",
                    }
                },
                "required": ["ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": FIND,
            "description": (
                "Найти цели обучения в опубликованной школьной программе по словам из их"
                " формулировки. Поиск лексический, по основам слов: язык запроса должен"
                " совпасть с языком текста целей (`objectives_language` из list_subjects),"
                " а язык чата не важен. Переведи запрос учителя на язык целей и дай"
                " несколько вариантов в `queries` (синонимы, основы слов, русский, казахский,"
                " английский) — результаты объединяются без повторов. Возвращает строки"
                " справочника с кодом и номером `id`. Нашлось пусто — в ответе `hint`:"
                " следуй ему, а не проси у учителя код или точную формулировку."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": (
                            f"До {QUERIES_MAX} вариантов запроса: слова из формулировки цели,"
                            " на языке целей."
                        ),
                    },
                    "query": {
                        "type": ["string", "null"],
                        "description": "Один запрос или код цели; то же, что один элемент queries.",
                    },
                    "grade": {
                        "type": ["integer", "null"],
                        "description": "Класс, если учитель его назвал; иначе null.",
                    },
                    "lang": {
                        "type": ["string", "null"],
                        "enum": ["ru", "kz", None],
                        "description": "Редакция: ru — русская, kz — казахская. null — обе.",
                    },
                    "subject_code": SUBJECT_PROPERTY,
                    "limit": {
                        "type": ["integer", "null"],
                        "description": (
                            f"Сколько целей вернуть, до {FIND_LIMIT_MAX}; null — {FIND_LIMIT}."
                        ),
                    },
                },
                "required": [],
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
                " Один и тот же код есть в программах многих предметов. Если предмет"
                " не передан, а код встречается в нескольких, цели не возвращаются:"
                " приходит `ambiguous` со списком предметов (`subjects`: код и название)."
                " Тогда спроси учителя, какой это предмет, и повтори вызов с `subject_code`;"
                " выбирать предмет за учителя нельзя."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Код цели, четыре числа через точку.",
                    },
                    "subject_code": SUBJECT_PROPERTY,
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
    # Язык обучения класса, если он уже назван: по нему выбирается редакция
    # в обзоре, когда модель её не указала
    lang: str | None = None


class OrderTools:
    """Инструменты в том виде, в каком их ждёт общий цикл (`ai_loop.Toolbox`)."""

    def schemas(self) -> list[dict]:
        return SCHEMAS

    def call(self, name: str, args: dict, ctx: OrderCtx) -> dict:
        if name == FIND:
            return find_objectives(ctx, args)
        if name == GET:
            return get_objective(ctx, args)
        if name == SUBJECTS:
            return list_subjects(ctx)
        if name == BROWSE:
            return browse_objectives(ctx, args)
        if name == SHOW:
            return show_objectives(ctx, args)
        # Модель попросила то, чего у неё нет, — например, «сгенерировать план»
        # или админский инструмент из чужого промпта. Отказ читает она сама
        return _refusal(
            "unknown_tool",
            f"Инструмента {name} нет. Доступны: {SUBJECTS}, {BROWSE}, {SHOW}, {FIND}, {GET}.",
        )


def find_objectives(ctx: OrderCtx, args: dict) -> dict:
    queries = _queries(args)
    if queries is None:
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
    limit = min(limit, FIND_LIMIT_MAX)
    subject_code, refusal = _subject_arg(ctx, args)
    if refusal:
        return refusal
    found: dict[int, ObjectiveRow] = {}
    searched = False
    for query in queries:
        stems = search_stems(query)
        code = objective_code_in(query)
        if not stems and not code:
            continue
        searched = True
        for row in ctx.repo.find_objectives(
            stems, code=code, grade=grade, lang=lang, limit=limit, subject_code=subject_code
        ):
            found.setdefault(row[0].id, row)
    if not searched:
        return _refusal("bad_arguments", "В запросе нет ни одного слова из формулировки цели")
    items = [objective_item(row) for row in list(found.values())[:limit]]
    if items:
        return {"items": items}
    return {"items": [], "hint": _empty_hint(ctx, subject_code, grade, lang)}


def _queries(args: dict) -> list[str] | None:
    """Варианты запроса из `queries` и старого `query` — без пустых и повторов."""
    raw = args.get("queries")
    found = [q for q in raw if isinstance(q, str)] if isinstance(raw, list) else []
    if isinstance(args.get("query"), str):
        found.append(args["query"])
    unique = list(dict.fromkeys(q.strip() for q in found if q.strip()))[:QUERIES_MAX]
    return unique or None


def _language_note(subject_code: str, editions: list[str]) -> str:
    languages = list(dict.fromkeys(objectives_language(subject_code, ed) for ed in editions))
    return " и ".join(LANGUAGE_NAMES[lang] for lang in languages)


def _empty_hint(
    ctx: OrderCtx, subject_code: str | None, grade: int | None, lang: str | None
) -> str:
    """Что делать, когда поиск пуст: подсказка для модели, а не отказ. Код и
    точную формулировку у учителя просить нельзя (решение владельца 15.10.2026)."""
    if subject_code is None:
        return (
            "Ничего не найдено. Не проси у учителя код или формулировку: вызови"
            f" {SUBJECTS}, затем {BROWSE} по предмету и классу — или спроси ровно один"
            " недостающий параметр, предмет или класс."
        )
    by_edition = ctx.repo.published_grades().get(subject_code, {})
    editions = [ed for ed in by_edition if lang in (None, ed)] or list(by_edition) or ["ru"]
    parts = []
    available = sorted({g for ed in editions for g in by_edition.get(ed, [])})
    if grade is not None and available and grade not in available:
        parts.append(
            f"В {grade} классе этого предмета в программе нет, есть классы: "
            + ", ".join(map(str, available))
            + ". Скажи об этом учителю и предложи один из них."
        )
    parts.append(
        f"Цели предмета написаны на {_language_note(subject_code, editions)} языке"
        f" (`objectives_language`): повтори запрос на нём, с основами слов, или вызови {BROWSE}"
        " по предмету и классу. Код у учителя не проси."
    )
    return " ".join(parts)


def list_subjects(ctx: OrderCtx) -> dict:
    grades = ctx.repo.published_grades()
    items = []
    for code, title, _lo, _hi, langs in ctx.repo.published_subjects():
        items.append(
            {
                "code": code,
                "title": title,
                "title_kz": subject_title_kz(code, title),
                "grades": grades.get(code, {}),
                "objectives_language": {ed: objectives_language(code, ed) for ed in langs},
            }
        )
    return {"items": items}


def browse_objectives(ctx: OrderCtx, args: dict) -> dict:
    grade = args.get("grade")
    if not isinstance(grade, int) or isinstance(grade, bool) or not 1 <= grade <= 12:
        return _refusal("bad_arguments", "Класс — число от 1 до 12")
    lang = args.get("lang")
    if lang is not None and lang not in LANGS:
        return _refusal("bad_arguments", "Редакция — ru, kz или null")
    subject_code, refusal = _subject_arg(ctx, args)
    if refusal:
        return refusal
    if subject_code is None:
        return _refusal("bad_arguments", "Нужен код предмета — его даёт list_subjects")
    by_edition = ctx.repo.published_grades().get(subject_code, {})
    lang = lang or (ctx.lang if ctx.lang in by_edition else None)
    if lang is None:
        lang = "ru" if "ru" in by_edition else next(iter(by_edition), "ru")
    available = by_edition.get(lang, [])
    if grade not in available:
        return {
            "sections": [],
            "hint": (
                f"В {grade} классе этого предмета ({lang}) в программе нет"
                + (f", есть классы: {', '.join(map(str, available))}" if available else "")
                + ". Скажи об этом учителю и предложи один из них."
            ),
        }
    sections: list[dict] = []
    for row in ctx.repo.grade_objectives(grade, lang, subject_code):
        objective, title = row[0], row[3]
        if not sections or sections[-1]["title"] != title:
            sections.append({"title": title, "items": []})
        text = " ".join(objective.text.split())
        if len(text) > BROWSE_TEXT_MAX:
            text = text[: BROWSE_TEXT_MAX - 1].rstrip() + "…"
        sections[-1]["items"].append({"id": objective.id, "code": objective.code, "text": text})
    out: dict[str, Any] = {
        "subject_code": subject_code,
        "grade": grade,
        "lang": lang,
        "objectives_language": objectives_language(subject_code, lang),
    }
    wanted = args.get("section")
    if isinstance(wanted, str) and wanted.strip():
        picked = [s for s in sections if s["title"].casefold() == wanted.strip().casefold()]
        if not picked:
            return _refusal(
                "not_found",
                "Такого раздела нет. Разделы: " + "; ".join(s["title"] for s in sections),
            )
        return {**out, "sections": picked}
    if len(json.dumps(sections, ensure_ascii=False)) > BROWSE_MAX_CHARS:
        return {
            **out,
            "sections": [{"title": s["title"], "count": len(s["items"])} for s in sections],
            "message": (
                "Целей в классе много: выбери раздел по смыслу и повтори вызов с `section`."
            ),
        }
    return {**out, "sections": sections}


def show_objectives(ctx: OrderCtx, args: dict) -> dict:
    raw = args.get("ids")
    ids = [i for i in raw if isinstance(i, int) and not isinstance(i, bool)] if isinstance(
        raw, list
    ) else []
    ids = list(dict.fromkeys(ids))[:SHOW_MAX]
    if not ids:
        return _refusal("bad_arguments", "Нужны номера целей из browse_objectives")
    found = ctx.repo.objectives_by_ids(ids)
    return {"items": [objective_item(found[i]) for i in ids if i in found]}


def get_objective(ctx: OrderCtx, args: dict) -> dict:
    raw = args.get("code")
    code = objective_code_in(raw) if isinstance(raw, str) else None
    if code is None:
        return _refusal("bad_arguments", "Код цели — четыре числа через точку, например 2.1.2.6")
    lang = args.get("lang")
    if lang is not None and lang not in LANGS:
        return _refusal("bad_arguments", "Редакция — ru, kz или null")
    subject_code, refusal = _subject_arg(ctx, args)
    if refusal:
        return refusal
    rows = ctx.repo.objectives_by_code(code, lang, subject_code)
    if not rows:
        return _refusal("not_found", f"Цели {code} в опубликованной программе нет")
    subjects = {row[2].code: row[2].title for row in rows}
    if subject_code is None and len(subjects) > 1:
        # Код общий для нескольких предметов: карточек нет, пока учитель не назвал
        # предмет, — иначе в заказ попадёт цель чужой программы
        return {
            "ambiguous": True,
            "message": (
                f"Код {code} есть в нескольких предметах. Спроси учителя, какой это предмет,"
                " и повтори вызов с subject_code."
            ),
            "subjects": [{"code": c, "title": t} for c, t in sorted(subjects.items())],
        }
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


def _subject_arg(ctx: OrderCtx, args: dict) -> tuple[str | None, dict | None]:
    """Предмет из аргументов: (код, None) или (None, отказ). Незнакомый код —
    отказ, а не пустая выдача: «целей нет» модель прочла бы как ответ учителю."""
    subject_code = args.get("subject_code")
    if subject_code is None:
        return None, None
    if not isinstance(subject_code, str) or not ctx.repo.subject_known(subject_code):
        return None, _refusal("unknown_subject", "Такого предмета в программе нет")
    return subject_code, None


def _refusal(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}
