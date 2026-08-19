"""Попытка теста в режиме предпросмотра — в памяти процесса.

Раздел 12 BACKEND_NOTES требует, чтобы в предпросмотре результаты теста
«считались в памяти и выбрасывались»: в базу не уходит ни попытки, ни ответа,
и протекать физически нечему.

Ограничение принято сознательно и честно называется: попытка не переживает
перезапуск сервиса и не работает, когда воркеров несколько, — соседний
процесс её просто не увидит. Режим ручной и одноразовый, а платой за
надёжность была бы та самая запись в базу, ради отсутствия которой всё
и затевалось (CONTRACT, сессия 6).
"""

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.adapters.db.repos import now_utc

# Забытая попытка убирается по сроку: без него память процесса растёт
# на каждый вход в режим и не отдаёт ничего обратно.
TTL = timedelta(hours=6)

# id попыток предпросмотра отрицательные: настоящие приходят из BigInteger
# последовательности и всегда положительны, спутать их нельзя, а форма ответа
# (целое число) для экрана остаётся прежней.
_ids = itertools.count(-1, -1)


@dataclass
class PreviewAttempt:
    """Снимок попытки: ровно те поля, что сценарий читает у строки quiz_attempt,
    и под теми же именами — тогда счёт, таймер и разбор считаются общим кодом."""

    id: int
    user_id: int
    quiz_id: int
    # Порядок вопросов фиксируется на старте, как и у настоящей попытки:
    # разбор обязан показать то, что человек решал.
    question_order: list[int]
    started_at: datetime
    # question_id -> выбранные варианты
    answers: dict[int, list[int]] = field(default_factory=dict)
    finished_at: datetime | None = None
    score: int = 0
    passed: bool = False
    # Попытка предпросмотра одна и ни с какой другой за зачёт не соревнуется
    is_counted: bool = True


class PreviewAttemptStore:
    """Попытки всех сессий предпросмотра. Ключ — id сессии, а не пользователя:
    режим включается в конкретной сессии, и попытка принадлежит ей."""

    def __init__(self, ttl: timedelta = TTL):
        self.ttl = ttl
        self._items: dict[str, PreviewAttempt] = {}

    def get(self, session_id: str) -> PreviewAttempt | None:
        self._purge()
        return self._items.get(session_id)

    def start(
        self, session_id: str, *, user_id: int, quiz_id: int, question_order: list[int]
    ) -> PreviewAttempt:
        """Новая попытка вместо прежней: попытка у сессии одна, и держать
        историю предпросмотра незачем — она всё равно выбрасывается."""
        self._purge()
        attempt = PreviewAttempt(
            id=next(_ids),
            user_id=user_id,
            quiz_id=quiz_id,
            question_order=list(question_order),
            started_at=now_utc(),
        )
        self._items[session_id] = attempt
        return attempt

    def drop(self, session_id: str) -> None:
        self._items.pop(session_id, None)

    def _purge(self) -> None:
        expired = [
            key
            for key, attempt in self._items.items()
            if now_utc() - attempt.started_at > self.ttl
        ]
        for key in expired:
            del self._items[key]


class SessionAttempts:
    """Ручка на попытку одной сессии: сценарию про id сессии знать незачем,
    он получает её от deps так же, как репозитории."""

    def __init__(self, store: PreviewAttemptStore, session_id: str):
        self.store = store
        self.session_id = session_id

    def get(self) -> PreviewAttempt | None:
        return self.store.get(self.session_id)

    def start(self, *, user_id: int, quiz_id: int, question_order: list[int]) -> PreviewAttempt:
        return self.store.start(
            self.session_id, user_id=user_id, quiz_id=quiz_id, question_order=question_order
        )
