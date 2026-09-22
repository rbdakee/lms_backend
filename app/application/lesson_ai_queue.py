"""Очередь генерации «Жоспара»: взять задание, собрать материал, закрыть задание.

Очередь не знает, как устроен ни один вид материала: генератор вида
подключается снаружи (`MaterialGenerator`), и L5 добавляет виды, не трогая
этот файл.

Одно задание — три коротких транзакции и долгий вызов модели между ними,
вне всякой транзакции:

1. взять задание (`claim`) и сразу закоммитить — соседний воркер его больше
   не видит;
2. прочитать из базы всё, что нужно генератору (`prepare`), и закрыть чтение;
3. вызвать модель — десятки секунд, соединение с базой в это время не занято;
4. записать результат или отказ, если задание всё ещё наше.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session as DbSession

from app.adapters.db.lesson_ai_repos import LessonAiJobRepo
from app.adapters.db.models import LessonKit
from app.domain.lesson_ai import MAX_ATTEMPTS, JobError, retry_delay

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Generated:
    """Готовый материал: документ целиком и во что он обошёлся."""

    content: dict
    tokens_in: int = 0
    tokens_out: int = 0


class MaterialGenerator(Protocol):
    """Генератор одного вида материала.

    `prepare` читает базу и отказывает, если генерировать нельзя
    (`JobError`); `generate` зовёт модель и в базу не ходит вовсе —
    поэтому долгий вызов идёт без открытой транзакции.
    """

    model: str

    def prepare(self, db: DbSession, kit: LessonKit) -> Any: ...

    def generate(self, prepared: Any) -> Generated: ...


class JobRunner:
    def __init__(
        self,
        session: Callable[[], DbSession],
        generators: Mapping[str, MaterialGenerator],
        *,
        lease_sec: int,
    ):
        self.session = session
        self.generators = generators
        self.lease_sec = lease_sec

    def run_next(self) -> bool:
        """Взять и выполнить одно задание. False — брать было нечего."""
        with self.session() as db:
            job = LessonAiJobRepo(db).claim(self.lease_sec)
            if job is None:
                return False
            job_id, attempt, kind, kit_id = job.id, job.attempts, job.kind, job.kit_id
            db.commit()
        self._run(job_id, attempt, kind, kit_id)
        return True

    def _run(self, job_id: int, attempt: int, kind: str, kit_id: int) -> None:
        if attempt > MAX_ATTEMPTS:
            # Сюда приходит только подобранное по истёкшей аренде: обычный
            # отказ закрывает задание сам, не дожидаясь лишней попытки
            self._fail(job_id, attempt, JobError("interrupted", retryable=False))
            return
        generator = self.generators.get(kind)
        if generator is None:
            # Вид, которого генератор не умеет, отбивает ещё ручка создания
            # комплекта; дойти сюда он может только по нашей ошибке
            log.error("Задание %s: нет генератора для вида %s", job_id, kind)
            self._fail(job_id, attempt, JobError("internal_error", retryable=False))
            return
        try:
            with self.session() as db:
                kit = db.get(LessonKit, kit_id)
                user_id = kit.user_id
                prepared = generator.prepare(db, kit)
            result = generator.generate(prepared)
        except JobError as exc:
            self._fail(job_id, attempt, exc, model=generator.model)
            return
        except Exception:
            log.exception("Задание %s (%s) упало", job_id, kind)
            self._fail(job_id, attempt, JobError("internal_error", retryable=False))
            return

        with self.session() as db:
            repo = LessonAiJobRepo(db)
            job = repo.owned(job_id, attempt)
            if job is None:
                log.warning(
                    "Задание %s: попытка %s опоздала, результат не записан", job_id, attempt
                )
                return
            repo.add_usage(
                job, tokens_in=result.tokens_in, tokens_out=result.tokens_out, model=generator.model
            )
            repo.done(job, result.content, user_id=user_id)
            db.commit()

    def _fail(self, job_id: int, attempt: int, exc: JobError, *, model: str | None = None) -> None:
        with self.session() as db:
            repo = LessonAiJobRepo(db)
            job = repo.owned(job_id, attempt)
            if job is None:
                log.warning("Задание %s: попытка %s опоздала, отказ не записан", job_id, attempt)
                return
            if model is not None:
                repo.add_usage(
                    job, tokens_in=exc.tokens_in, tokens_out=exc.tokens_out, model=model
                )
            delay = retry_delay(attempt) if exc.retryable else None
            if delay is None:
                log.warning("Задание %s: отказ %s, попыток больше нет", job_id, exc.code)
                repo.fail(job, exc.code)
            else:
                log.info("Задание %s: отказ %s, повтор через %s с", job_id, exc.code, delay)
                repo.retry(job, exc.code, delay)
            db.commit()
