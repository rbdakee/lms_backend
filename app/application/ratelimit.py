"""Счётчик частоты запросов — в памяти процесса.

Отдельного кэша в продукте нет по решению владельца, а uvicorn поднимается
одним процессом: словарь в памяти и есть весь нужный механизм. Цена ошибки
здесь мала — лимит защищает `playback` от превращения в качалку курса,
а не деньги и не единственную попытку теста. Перезапуск сервиса счётчики
обнуляет, и это допустимо.

Часы вынесены параметром: тест не может переждать минуту, а окно должно
проверяться по-настоящему.
"""

import time
from collections import deque
from collections.abc import Callable
from math import ceil


class SlidingWindowLimiter:
    def __init__(
        self, limit: int, window_sec: int, clock: Callable[[], float] = time.monotonic
    ):
        self.limit = limit
        self.window_sec = window_sec
        self.clock = clock
        # Ключ — обычно user_id; очередь хранит времена запросов в окне,
        # то есть не больше limit значений на активного человека
        self.hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> int:
        """0 — можно; иначе через сколько секунд повторять."""
        now = self.clock()
        hits = self.hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window_sec:
            hits.popleft()
        if not hits:
            # Ключ публичного лимита задаёт клиент: остывшие адреса нужно
            # выбрасывать, иначе словарь растёт до перезапуска сервиса.
            # Текущий ключ не трогаем — в него сейчас пишется этот запрос
            self._forget_cold(now, keep=key)
        if len(hits) >= self.limit:
            # Освободится, когда из окна выйдет самый старый запрос
            return max(1, ceil(self.window_sec - (now - hits[0])))
        hits.append(now)
        return 0

    def _forget_cold(self, now: float, *, keep: str) -> None:
        for key, hits in list(self.hits.items()):
            if key == keep:
                continue
            if not hits or now - hits[-1] >= self.window_sec:
                del self.hits[key]
