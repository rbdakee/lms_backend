"""Проверка живости для балансировщика.

Отвечает не «процесс запущен», а «запросы обслуживаются»: без похода в базу
ручка отвечала `200` и тогда, когда всё остальное отдавало `500`, —
балансировщик не отличал сломанный инстанс от живого, а раскатка считала
сломанную версию здоровой и продолжала катить.
"""

import logging

from fastapi import APIRouter
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.domain.errors import NotReadyError

log = logging.getLogger("app")

router = APIRouter()

# Сколько ждём базу, прежде чем считать сервис неготовым. Балансировщик
# опрашивает часто, и висеть здесь дольше собственного интервала опроса
# — значит копить занятые потоки.
CONNECT_TIMEOUT_SEC = 3

_engine = None


def _health_engine():
    """Своё соединение, мимо общего пула.

    Пул рабочих запросов конечен, и `connect()` из него ждёт свободного
    места: под нагрузкой ручка отвечала бы `503` «база не отвечает» при
    живой базе — и балансировщик снимал бы с трафика здоровый инстанс
    ровно тогда, когда он нужен. Отсюда NullPool: соединение на проверку
    своё, чужую очередь оно не занимает и в чужую не встаёт.
    """
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_settings().database_url,
            poolclass=NullPool,
            connect_args={"connect_timeout": CONNECT_TIMEOUT_SEC},
        )
    return _engine


@router.get("/health")
def health() -> dict:
    # Объяснение — комментарием, а не докстрингом: докстринг обработчика
    # FastAPI печатает в OpenAPI как description, а схему держим неподвижной —
    # по ней фронт генерирует типы.
    try:
        with _health_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as err:
        # Ни строки подключения, ни пароля: в лог уходит только факт
        log.warning("База не отвечает — сервис не готов")
        raise NotReadyError() from err
    return {"status": "ok"}
