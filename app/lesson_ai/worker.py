"""Воркеры очереди генерации — потоки в процессе API.

Отдельного процесса нет нарочно: бриф разрешает очередь своей таблицей,
но без нового сервиса в деплое (TEACHER_AI_BRIEF, раздел 7). Потоки, а не
корутины: генерация — синхронный SQLAlchemy и синхронный клиент провайдера,
в цикле событий им делать нечего.

Процессов API может стать больше одного — очередь от этого не ломается:
задание берётся `SKIP LOCKED`, и два воркера одно и то же не возьмут.
"""

import logging
import threading

from sqlalchemy.orm import Session as DbSession

from app.adapters.ai.openai_chat import OpenAiChat
from app.adapters.db.base import get_engine
from app.application.lesson_ai_queue import JobRunner
from app.config import Settings
from app.lesson_ai.kinds import generators

log = logging.getLogger(__name__)


class Workers:
    def __init__(self, runner: JobRunner, count: int, poll_sec: float):
        self.runner = runner
        self.count = count
        self.poll_sec = poll_sec
        self._stop = threading.Event()

    def start(self) -> None:
        for number in range(1, self.count + 1):
            threading.Thread(target=self._loop, name=f"lesson_ai-{number}", daemon=True).start()
        log.info("Жоспар: воркеров генерации запущено: %s", self.count)

    def stop(self) -> None:
        # Поток посреди генерации не ждём: модель может думать минутами, а
        # задание, брошенное на полпути, подберёт следующий запуск по
        # истёкшей аренде — это и есть её назначение
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.runner.run_next()
            except Exception:
                # База моргнула — воркер не умирает, а пробует снова через паузу:
                # мёртвый поток молча останавливал бы очередь до перезапуска
                log.exception("Жоспар: воркер не смог взять задание")
                worked = False
            if not worked:
                self._stop.wait(self.poll_sec)


def lease_seconds(cfg: Settings) -> int:
    """Аренда взятого задания — дольше самой долгой честной генерации.

    Клиент провайдера повторяет запрос один раз (`max_retries=1`), значит
    честная генерация длится не дольше двух таймаутов. Минута сверху —
    на чтение и запись в базу. Аренда короче — живое задание подбирал бы
    второй воркер, и за один материал платили бы дважды.
    """
    return cfg.lesson_ai_timeout_sec * 2 + 60


def start_workers(cfg: Settings) -> Workers:
    ai = OpenAiChat(cfg, model=cfg.lesson_ai_model, timeout_sec=cfg.lesson_ai_timeout_sec)
    runner = JobRunner(
        lambda: DbSession(get_engine()),
        generators(ai, cfg.lesson_ai_model),
        lease_sec=lease_seconds(cfg),
    )
    workers = Workers(runner, cfg.lesson_ai_workers, cfg.lesson_ai_poll_sec)
    workers.start()
    return workers
