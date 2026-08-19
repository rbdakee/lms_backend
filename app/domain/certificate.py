"""Правила сертификата: номер документа и чек-лист условий.

Как и в тесте, сюда приходят готовые числа и множества: домену незачем знать,
откуда взялись «12 из 18» — из программы курса или из отметок учителя.
"""

import re
import secrets
from datetime import datetime

from app.domain.kz_time import ALMATY

NOT_STARTED = "not_started"
IN_PROGRESS = "in_progress"
DONE = "done"

# Порядок словаря — порядок строк чек-листа на экране: он один и в редакторе
# курса, и у учителя (DESIGN_BRIEF, «Условия сертификата»).
CONDITION_LABELS = {
    "lessons": "Пройти все уроки",
    "tasks": "Сдать все задания",
    "module_quizzes": "Сдать все тесты модулей",
    "final_quiz": "Сдать итоговый тест",
}

# 32 знака без похожих начертаний: номер диктуют по телефону и перебивают
# с бумаги, поэтому нет 0/O и 1/I (CONTRACT, сессия 6).
NUMBER_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
NUMBER_TAIL_LEN = 6

# Ищем по любым буквам и цифрам, а не только по алфавиту номера: сменится
# алфавит — старые номера должны продолжать находиться.
_NUMBER_RE = re.compile(rf"^KZ(\d{{4}})([0-9A-Z]{{{NUMBER_TAIL_LEN}}})$")


def condition_status(done_count: int | None, total_count: int) -> str:
    """Состояние строки чек-листа.

    Счётчиков нет — строка всегда «не начато»: до выдачи доступа это просто
    список требований.

    Условие, под которым нет ни одного элемента, выполненным НЕ считается.
    Иначе флаг, выставленный до наполнения курса, раздал бы сертификаты всем
    желающим; а урок «с чьим-то прогрессом не удаляется, а скрывается» — и
    скрытие последнего непройденного урока схлопнуло бы условие в выполненное.
    Курс совсем без условий — другой случай: там список пуст, и строк тут нет.
    """
    if done_count is None:
        return NOT_STARTED
    if total_count == 0:
        return NOT_STARTED
    if done_count >= total_count:
        return DONE
    return IN_PROGRESS if done_count else NOT_STARTED


def build_conditions(
    enabled: set[str],
    totals: dict[str, int],
    done: dict[str, int] | None,
    *,
    final_pass_score: int | None,
) -> list[dict]:
    """Чек-лист из включённых у курса условий, в порядке экрана.

    `done` = None — счётчиков нет: человек не вошёл или доступа к курсу ещё нет.
    """
    conditions = []
    for code, label in CONDITION_LABELS.items():
        if code not in enabled:
            continue
        total_count = totals.get(code, 0)
        done_count = done.get(code, 0) if done is not None else None
        condition = {
            "code": code,
            "label": label,
            "status": condition_status(done_count, total_count),
            "done_count": done_count,
            "total_count": total_count,
        }
        if code == "final_quiz":
            # Процент только у итогового: у тестов модулей он свой у каждого,
            # и одним числом строку «Сдать все тесты модулей» не подписать
            condition["pass_score"] = final_pass_score
        conditions.append(condition)
    return conditions


def all_done(conditions: list[dict]) -> bool:
    """Курс, где не включено ни одного условия, выдаёт сертификат сразу:
    пустой чек-лист выполнен."""
    return all(condition["status"] == DONE for condition in conditions)


def generate_number(now: datetime) -> str:
    """Номер вида `KZ-2026-XB7K2M`.

    Год — по казахстанскому времени: в 01:00 по Алматы 1 января выданный
    документ не должен уйти прошлым годом. Случайная часть из `secrets`,
    а не из `random`: последовательный номер перебирается скриптом за минуту,
    и весь реестр становится публичным (BACKEND_NOTES, раздел 6).
    """
    tail = "".join(secrets.choice(NUMBER_ALPHABET) for _ in range(NUMBER_TAIL_LEN))
    return f"KZ-{now.astimezone(ALMATY).year}-{tail}"


def canonical_number(raw: str) -> str | None:
    """Номер с бумаги в том виде, в каком он лежит в базе.

    Комиссия перебивает номер руками, поэтому регистр и разделители не в счёт:
    `kz2026xb7k2m` — тот же документ. None — набор, которого в реестре быть
    не может, и ходить за ним в базу незачем.
    """
    cleaned = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
    match = _NUMBER_RE.match(cleaned)
    return f"KZ-{match.group(1)}-{match.group(2)}" if match is not None else None
