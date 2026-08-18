"""Правила программы курса: что пройдено, что закрыто и сколько сделано.

Статусы сайдбара и счётчики «N из M» — одна логика: прогресс это агрегат
по тем же статусам. Два независимых подсчёта разошлись бы, а человек читает
разные числа на двух экранах одного курса как ошибку.
"""

DONE = "done"
LOCKED = "locked"
AVAILABLE = "available"

# Урок в программе идёт своим kind содержимого; тест и задание — quiz и task.
LESSON_KINDS = ("video", "text")


def item_key(item: dict) -> tuple[str, int]:
    """Ключ элемента с типом: id у урока, теста и задания свои, и id=1
    бывает у всех троих."""
    kind = item["kind"]
    return ("lesson" if kind in LESSON_KINDS else kind, item["id"])


def apply_statuses(
    program: list[dict], done_keys: set[tuple[str, int]], *, strict_order: bool
) -> list[dict]:
    """Копия программы, где у каждого элемента есть `status`.

    Копия, а не отметка на месте: та же собранная программа уходит на страницу
    курса, где статусов нет вовсе.

    Закрывают элемент два правила, и они складываются: строгий порядок курса
    и итоговый тест, ждущий всех уроков.
    """
    lessons_left = any(
        item_key(item) not in done_keys
        for module in program
        for item in module["items"]
        if item["kind"] in LESSON_KINDS
    )
    # Строгий порядок закрывает всё после первого непройденного элемента,
    # сам он открыт.
    after_undone = False
    result = []
    for module in program:
        items = []
        for item in module["items"]:
            if item_key(item) in done_keys:
                # done считается раньше locked: пройденный элемент закрытым не станет
                items.append({**item, "status": DONE})
                continue
            # Итоговый тест ждёт все видимые уроки курса независимо от strict_order
            final_locked = item["kind"] == "quiz" and item["is_final"] and lessons_left
            status = LOCKED if after_undone or final_locked else AVAILABLE
            items.append({**item, "status": status})
            after_undone = after_undone or strict_order
        result.append({**module, "items": items})
    return result


def progress_of(program: list[dict]) -> dict:
    """Счётчики курса по всем видимым элементам программы — блок `access.granted`.

    `next_lesson` — первый непройденный элемент в сквозном порядке; `kind`
    у него обязателен, иначе кнопка «Продолжить» не знает, какой экран открывать.
    """
    items = [item for module in program for item in module["items"]]
    done_count = sum(1 for item in items if item["status"] == DONE)
    following = next((item for item in items if item["status"] != DONE), None)
    return {
        "done_count": done_count,
        "total_count": len(items),
        "progress_percent": round(done_count * 100 / len(items)) if items else 0,
        "next_lesson": (
            {"id": following["id"], "title": following["title"], "kind": following["kind"]}
            if following is not None
            else None
        ),
    }
