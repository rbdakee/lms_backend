"""Отбор строительных блоков для игры: две-три механики из каталога под заказ.

Путь «Б» из `L5_games_db_design.md` (раздел 4): сервер фильтрует карточки
по заказу, ранжирует и кладёт две-три в **сообщение пользователя**, а не
в системный промпт. Системный промпт игры один на всех и берётся провайдером
из кэша; переменный каталог в нём ломал бы кэш на каждом вызове, а весь
каталог целиком модель читала бы как меню и выбирала одну позицию — ровно
то, что бриф запрещает (раздел 4.3).

Три карточки — из разных семейств: основная, вторая — из тех, с чем основная
скрещивается, третья — с соседнего уровня вовлечённости, как источник чужого
хода. Случайность внутри ранга — с зерном от номера комплекта: жалобу можно
разобрать, повторив отбор, а разные комплекты получают разное.

Имён механик здесь нет и быть не должно: всё, что отбор знает о механике, —
поля её карточки.
"""

import random
from collections.abc import Iterable

from app.lesson_ai.context import LessonBrief
from app.lesson_ai.games.mechanic import Mechanic, catalog

PICK = 3

# Соседний уровень — откуда брать третью карточку, «чужой ход»
NEIGHBOUR = {"calm": "active", "active": "calm", "full_game": "active"}

# Подписи перечислений для модели: слаг ей ничего не скажет
TEACHER_ROLE = {
    "host": "ведущий: читает вслух, ведёт ход",
    "referee": "судья: проверяет ответы и следит за правилами",
    "facilitator": "помощник: ходит между группами, подсказывает",
    "observer": "наблюдатель: дети играют сами",
}
STUDENT_READING = {
    "none": "дети почти не читают — правила голосом учителя, на карточках числа и значки",
    "short": "дети читают короткие подписи",
    "sentences": "дети читают задания целыми предложениями",
}
PER = {
    "per_student": "на каждого ученика",
    "per_pair": "на пару",
    "per_group": "на группу",
    "per_team": "на команду",
    "per_class": "на класс",
}

FRAME = (
    "Строительные блоки для игры — механики из каталога, проверенные на бумаге"
    " и ножницах. Это не меню: не выбирай одну из них целиком. Скрести,"
    " переименуй под сюжет урока, привяжи к цели и к плану урока. Игра, легшая"
    " на одну механику без изменений, выйдет скучной. Инварианты и грабли"
    " механики, которую берёшь в основу, соблюдай."
)


def references(brief: LessonBrief, seed: int) -> tuple[str, tuple[str, ...]]:
    """Текст для сообщения модели и слаги отданных механик.

    Пустой каталог или пустой фильтр — не отказ: игра генерируется и без
    опоры, как до каталога.
    """
    picked = pick(catalog(), brief, seed)
    if not picked:
        return "", ()
    cards = "\n\n".join(_card(mechanic, brief) for mechanic in picked)
    return f"{FRAME}\n\n{cards}", tuple(mechanic.id for mechanic in picked)


def pick(mechanics: Iterable[Mechanic], brief: LessonBrief, seed: int) -> list[Mechanic]:
    rng = random.Random(seed)
    level = brief.engagement_level
    main = _ranked([m for m in mechanics if _fits(m, brief, level)], brief, level, rng)
    if not main:
        return []
    chosen = [main[0]]
    families = {main[0].family}

    # Вторая — из тех, с чем основная скрещивается; нет таких — следующая по рангу
    partners = [m for m in main[1:] if m.id in main[0].combines_with]
    second = _first_new(partners, families) or _first_new(main[1:], families)
    if second is not None:
        chosen.append(second)
        families.add(second.family)

    # Третья — с соседнего уровня: не для копирования, а как чужой ход
    neighbour = NEIGHBOUR[level]
    near = _ranked(
        [m for m in mechanics if m not in chosen and _fits(m, brief, neighbour)],
        brief,
        neighbour,
        rng,
    )
    third = _first_new(near, families) or _first_new([m for m in main if m not in chosen], families)
    if third is not None:
        chosen.append(third)
    return chosen[:PICK]


def _fits(mechanic: Mechanic, brief: LessonBrief, level: str) -> bool:
    """Жёсткий фильтр (дизайн, раздел 4.3, шаг 1)."""
    if mechanic.status == "retired":
        return False
    if "any" not in mechanic.subjects and brief.subject_code not in mechanic.subjects:
        return False
    if not mechanic.grade_min <= brief.grade <= mechanic.grade_max:
        return False
    # На весь урок-игру проходят и механики-станции: как часть квеста
    if level not in mechanic.engagement_levels and not (
        level == "full_game" and mechanic.station_ready
    ):
        return False
    # Первоклассник правил не прочтёт и сложных не удержит
    if brief.grade == 1 and (
        mechanic.student_reading == "sentences" or mechanic.rules_complexity == "complex"
    ):
        return False
    # Механика на словах языка без казахских заметок на казахский заказ не идёт
    return not (brief.lang == "kz" and mechanic.lang_sensitive and not mechanic.lang_notes.kz)


def _ranked(
    mechanics: list[Mechanic], brief: LessonBrief, level: str, rng: random.Random
) -> list[Mechanic]:
    """Мягкое ранжирование (шаг 2). Перемешивание до устойчивой сортировки
    даёт случайность только внутри одного ранга."""
    shuffled = list(mechanics)
    rng.shuffle(shuffled)
    return sorted(shuffled, key=lambda m: -_score(m, brief, level))


def _score(mechanic: Mechanic, brief: LessonBrief, level: str) -> int:
    score = 0
    if mechanic.engagement_levels[0] == level:
        score += 3
    elif level in mechanic.engagement_levels:
        score += 2
    # Проверенная методистом — выше черновика
    if mechanic.status == "active":
        score += 1
    # Фаза, в которую игра встаёт: весь урок у full_game, середина у остальных
    phase = "whole_lesson" if level == "full_game" else "middle"
    if phase in mechanic.lesson_phases:
        score += 1
    # Классу, который догоняет программу, не нужны вдобавок сложные правила
    if brief.difficulty_level == "basic":
        score += {"simple": 1, "medium": 0, "complex": -2}[mechanic.rules_complexity]
    return score


def _first_new(mechanics: Iterable[Mechanic], families: set[str]) -> Mechanic | None:
    return next((m for m in mechanics if m.family not in families), None)


def _card(mechanic: Mechanic, brief: LessonBrief) -> str:
    """Карточка для модели: только то, что ей нужно (шаг 5). Фильтровые поля
    уже сработали, служебные и то, что рисует сервер, ей не нужны."""
    lines = [
        f"### {mechanic.title}",
        mechanic.summary.strip(),
        f"Очки и победа: {mechanic.scoring}",
    ]
    if mechanic.engagement_levels[0] != brief.engagement_level and mechanic.level_shift:
        lines.append(f"Как перевести на нужный уровень: {mechanic.level_shift.strip()}")
    elif brief.engagement_level not in mechanic.engagement_levels:
        lines.append("Годится как станция или фрагмент внутри игры на весь урок.")
    scaling = getattr(mechanic.difficulty_scaling, brief.difficulty_level)
    lines.append(f"Под уровень класса: {scaling}")
    if mechanic.in_class_differentiation:
        lines.append(f"Разным детям в одном классе: {mechanic.in_class_differentiation}")
    lines.append(f"Учитель в игре — {TEACHER_ROLE[mechanic.teacher_role]}.")
    lines.append(f"Чтение: {STUDENT_READING[mechanic.student_reading]}.")
    lines += _list("Модель придумывает", mechanic.model_generates)
    lines += _list("Обязано сойтись", mechanic.invariants)
    lines += _list("Грабли", mechanic.pitfalls)
    lines += _list(
        "Печать",
        [f"{item.name} — {PER[item.per]}, {item.count}" for item in mechanic.print_items],
    )
    note = getattr(mechanic.lang_notes, brief.lang)
    if note:
        lines.append(f"Язык: {note}")
    return "\n".join(lines)


def _list(title: str, items: list[str]) -> list[str]:
    if not items:
        return []
    return [f"{title}:"] + [f"- {item}" for item in items]
