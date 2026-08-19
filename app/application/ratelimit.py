"""Счётчик частоты запросов — в памяти процесса.

Отдельного кэша в продукте нет по решению владельца, а uvicorn поднимается
одним процессом: словарь в памяти и есть весь нужный механизм. Цена ошибки
здесь мала — лимит защищает `playback` от превращения в качалку курса,
а не деньги и не единственную попытку теста. Перезапуск сервиса счётчики
обнуляет, и это допустимо.

Часы вынесены параметром: тест не может переждать минуту, а окно должно
проверяться по-настоящему.
"""

import threading
import time
from collections import OrderedDict, deque
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
        # то есть не больше limit значений на активного человека.
        # Порядок словаря — по времени последнего запроса, свежие в хвосте:
        # остывшие тогда лежат подряд в голове, и уборке хватает головы
        # вместо обхода всего словаря.
        self.hits: OrderedDict[str, deque[float]] = OrderedDict()
        # Лимитер зовут из нескольких потоков threadpool сразу, и стоит он
        # на горячем пути playback и публичной проверки. Замок здесь дешевле
        # ошибки: без него сосед выносит ключ ровно между тем, как ключ
        # заведён, и тем, как в него записан запрос, — запрос обслужен,
        # но не посчитан, и потолок протекает тем сильнее, чем больше нагрузка.
        self._lock = threading.Lock()

    def hit(self, key: str) -> int:
        """0 — можно; иначе через сколько секунд повторять."""
        now = self.clock()
        with self._lock:
            hits = self.hits.get(key)
            while hits and now - hits[0] >= self.window_sec:
                hits.popleft()
            if not hits:
                # Ключ публичного лимита задаёт клиент: остывшие адреса нужно
                # выбрасывать, иначе словарь растёт до перезапуска сервиса.
                # Уборка идёт до того, как ключ заведён: из-под собственного
                # запроса вынести его теперь нечему
                self._forget_cold(now)
                hits = self.hits.setdefault(key, deque())
            if len(hits) >= self.limit:
                # Освободится, когда из окна выйдет самый старый запрос
                return max(1, ceil(self.window_sec - (now - hits[0])))
            hits.append(now)
            # Ключ уходит в хвост: словарь держится по времени последнего
            # запроса, и на этом стоит вся дешевизна уборки
            self.hits.move_to_end(key)
            return 0

    def _forget_cold(self, now: float) -> None:
        """Снимает остывшие ключи с головы словаря.

        Стоит столько, сколько вынесла, а не сколько ключей всего: на первом
        живом ключе обход кончается, потому что за ним живые все. Обход
        целиком стоил здесь 14.5 секунды CPU на 20000 адресах за окно —
        и заказывал эту работу посторонний, ключ публичной проверки задаёт
        клиент.
        """
        while self.hits:
            key = next(iter(self.hits))
            hits = self.hits[key]
            if hits and now - hits[-1] < self.window_sec:
                break
            del self.hits[key]
