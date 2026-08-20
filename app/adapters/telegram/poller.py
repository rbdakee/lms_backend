"""Поллинг апдейтов вместо вебхука — только для локальной разработки.

`TELEGRAM_UPDATES=poll`: `localhost:8000` снаружи не виден, Telegram до него
не достучится, поэтому сервис сам вытягивает апдейты через `getUpdates`
и прогоняет их через ту же `TelegramBindService.handle_update`, что и вебхук
(`app/api/routers/telegram.py`) — разбор один и тот же, разница только
в том, откуда берётся апдейт. В бою этот файл не участвует вовсе:
`api.domain.kz` публичный, `TELEGRAM_UPDATES` остаётся `webhook`.

Фоновая задача одна на процесс и живёт, пока жив сервис (`app/main.py`,
`lifespan`) — не очередь и не воркер: воркер один и тот же `uvicorn`
процесс, задача просто крутится в его собственном цикле событий.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.adapters.db.base import get_engine
from app.adapters.db.repos import SettingRepo
from app.adapters.telegram.bot import API_BASE, TelegramBot
from app.application.telegram_bind import TelegramBindService
from app.config import Settings

log = logging.getLogger("telegram")

# Долгий опрос: пока апдейтов нет, запрос просто висит на стороне Telegram
# и возвращается пустым по истечении этого времени — без него цикл долбил бы
# API раз в секунду вхолостую. Клиентский таймаут — с запасом поверх него,
# иначе сокет обрывается раньше, чем успевает ответить сам Telegram.
LONG_POLL_SEC = 25
CLIENT_TIMEOUT_SEC = LONG_POLL_SEC + 10
# Пауза после сетевой ошибки — не долбить Telegram чаще, чем раз в это время
RETRY_PAUSE_SEC = 5


async def run_poller(cfg: Settings) -> None:
    """Цикл поллинга. Останавливается отменой задачи снаружи (`lifespan`
    на выключении сервиса) — своего условия остановки у цикла нет."""
    log.info("Telegram: поллинг апдейтов вместо вебхука (TELEGRAM_UPDATES=poll)")
    offset: int | None = None
    try:
        while True:
            try:
                updates = await run_in_threadpool(_get_updates, cfg.telegram_bot_token, offset)
            except Exception:
                log.warning(
                    "Telegram: getUpdates не удался, пробую снова через %sс", RETRY_PAUSE_SEC
                )
                await asyncio.sleep(RETRY_PAUSE_SEC)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                await run_in_threadpool(_handle_one, cfg, upd)
    except asyncio.CancelledError:
        log.info("Telegram: поллинг остановлен")
        raise


def _get_updates(token: str, offset: int | None) -> list[dict]:
    params = f"timeout={LONG_POLL_SEC}"
    if offset is not None:
        params += f"&offset={offset}"
    request = urllib.request.Request(f"{API_BASE}/bot{token}/getUpdates?{params}")
    try:
        with urllib.request.urlopen(request, timeout=CLIENT_TIMEOUT_SEC) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as err:
        log.warning("Telegram ответил %s на getUpdates", err.code)
        raise
    if not (isinstance(body, dict) and body.get("ok")):
        raise RuntimeError("Telegram не отдал апдейты")
    return body.get("result", [])


def _handle_one(cfg: Settings, update: dict) -> None:
    """Один апдейт — своя сессия базы: цикл не должен держать соединение
    занятым между двумя опросами, а два апдейта подряд — делить одну сессию.
    """
    db = Session(get_engine())
    try:
        svc = TelegramBindService(
            settings=SettingRepo(db),
            telegram=TelegramBot(cfg.telegram_bot_token, None),
            cfg=cfg,
            commit=db.commit,
        )
        svc.handle_update(update)
        db.commit()
    except Exception:
        db.rollback()
        # Ни апдейта, ни текста исключения: там переписка людей — то же
        # правило, что и у вебхука
        log.warning("Telegram: апдейт из поллинга разобрать не удалось")
    finally:
        db.close()
