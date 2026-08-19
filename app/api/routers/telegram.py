"""Привязка Telegram-бота: три ручки настроек и вебхук самого Telegram.

Вебхук стоит рядом с ними, а не в `settings`, потому что это не экран:
у него нет входа, а подлинность доказывает заголовок с секретом.
"""

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import TelegramBindCodeOut
from app.application.telegram_bind import TelegramBindService

log = logging.getLogger("telegram")

router = APIRouter()
admin_router = APIRouter(prefix="/admin/settings/telegram")

# Потолок на тело апдейта. Больше 4096 знаков в сообщении Telegram не берёт,
# файлы приезжают ссылками, а не байтами, — самый жирный апдейт (сообщение
# с разметкой и вложенным `reply_to_message`) не дотягивает и до десятков
# килобайт. 64 КБ — запас поверх этого; всё, что толще, до разбора JSON
# не доходит вовсе, иначе двадцатимегабайтное тело читается целиком в память
# и разбирается.
MAX_BODY_BYTES = 64 * 1024


@router.post("/telegram/webhook", dependencies=[Depends(deps.verify_telegram_secret)])
async def telegram_webhook(
    request: Request,
    svc: Annotated[TelegramBindService, Depends(deps.get_telegram_bind_service)],
) -> dict:
    """Сюда стучится сам Telegram. Отвечает 200 на что угодно, кроме неверного
    секрета: на любой другой ответ Telegram повторяет доставку по нарастающей,
    и один неудачный разбор превращается в бесконечный поток.

    Единственный `async def` роутер сервиса — только чтобы прочитать сырые
    байты, не занимая поток. Всё остальное — разбор, база и отправка в бот —
    уходит в threadpool: на цикле событий синхронная работа встаёт поперёк
    всех запросов сразу, а два одновременных апдейта с одним кодом вешали
    процесс насмерть (INSERT берёт блокировку строки, второй такой же
    блокирует сам поток цикла, и коммит первого некому запланировать).
    Петля самоподдерживающаяся: Telegram переотправляет апдейт именно тогда,
    когда вебхук отвечал долго.
    """
    # Тело читается кусками и обрывается на потолке: `request.body()` втянул
    # бы в память и двадцать мегабайт. Слишком толстое просто не разбираем,
    # но отвечаем тем же 200 — свой код ответа означал бы переотправку того же
    # тела по нарастающей. В лог уходит факт и потолок, самого тела там быть
    # не должно: это переписка людей
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            log.warning("Тело вебхука больше %s байт — не разбираем", MAX_BODY_BYTES)
            return {}
    await run_in_threadpool(_handle, svc, body)
    return {}


def _handle(svc: TelegramBindService, body: bytes) -> None:
    """Разбор и обработка апдейта — целиком вне цикла событий.

    JSON читается здесь же: невалидное тело — такой же неудачный разбор,
    как остальные, и отвечать на него надо тем же 200.
    """
    try:
        svc.handle_update(json.loads(body))
    except Exception:
        # Ни тела запроса, ни текста исключения: там переписка людей
        log.warning("Обновление от Telegram разобрать не удалось")


@admin_router.post("/bind_code")
def telegram_bind_code(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[TelegramBindService, Depends(deps.get_telegram_bind_service)],
) -> TelegramBindCodeOut:
    return TelegramBindCodeOut(**svc.bind_code())


@admin_router.post("/test", status_code=status.HTTP_204_NO_CONTENT)
def telegram_test(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[TelegramBindService, Depends(deps.get_telegram_bind_service)],
) -> None:
    svc.send_test()


@admin_router.post("/unbind", status_code=status.HTTP_204_NO_CONTENT)
def telegram_unbind(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[TelegramBindService, Depends(deps.get_telegram_bind_service)],
) -> None:
    svc.unbind()
