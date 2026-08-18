"""Правила теста: счёт, серверный таймер и выбор зачётной попытки.

Счёт считается по снимку вопросов попытки, а не по текущему состоянию теста:
админ поправит вопрос — уже выставленный балл от этого не поедет. Поэтому
сюда приходят готовые числа и множества, а не строки базы.
"""

from datetime import datetime, timedelta
from math import ceil


def earned_points(points: int, correct_ids: set[int], chosen_ids: set[int]) -> int:
    """Балл за вопрос: только за полностью верный набор.

    Частичных баллов нет — «две галочки из трёх» это не половина знания,
    а неверный ответ (BACKEND_NOTES, раздел 4).
    """
    return points if chosen_ids == correct_ids else 0


def score_percent(score: int, max_score: int) -> int:
    """Процент от максимума, округлённый до целого: проходной балл в тесте
    задан процентами, а баллы вопросов бывают дробными по смыслу."""
    return round(score * 100 / max_score) if max_score else 0


def deadline(started_at: datetime, time_limit_min: int | None) -> datetime | None:
    """Момент, после которого ответы не принимаются. None — теста без лимита:
    такая попытка не истекает никогда."""
    if time_limit_min is None:
        return None
    return started_at + timedelta(minutes=time_limit_min)


def remaining_sec(started_at: datetime, time_limit_min: int | None, now: datetime) -> int | None:
    """Сколько осталось по часам сервера — от них рисуется таймер на экране.
    Часы клиента к делу не относятся: перевёл время — попытка не удлинилась.

    Вверх, а не вниз: ноль должен означать ровно то же, что `is_expired`, —
    иначе на последней секунде экран отправит на подсчёт ещё живую попытку.
    """
    limit = deadline(started_at, time_limit_min)
    if limit is None:
        return None
    return max(0, ceil((limit - now).total_seconds()))


def is_expired(started_at: datetime, time_limit_min: int | None, now: datetime) -> bool:
    limit = deadline(started_at, time_limit_min)
    return limit is not None and now >= limit


def timed_out(started_at: datetime, finished_at: datetime, time_limit_min: int | None) -> bool:
    """Автосдача по времени. Отдельной колонки нет: у истёкшей попытки
    finished_at ставится ровно на конец лимита, и это её и отличает."""
    limit = deadline(started_at, time_limit_min)
    return limit is not None and finished_at >= limit


def minutes_spent(started_at: datetime, finished_at: datetime) -> int:
    """Целые минуты за тестом, вниз: «12 минут» на экране результата."""
    return int((finished_at - started_at).total_seconds() // 60)


def review_available(retakable: bool, show_review: bool) -> bool:
    """У непересдаваемого теста разбор есть всегда: единственная попытка
    без объяснения ошибок ничему не учит. У пересдаваемого разбор —
    подсказка к следующей попытке, и её админ может выключить.
    """
    return show_review if retakable else True
