"""Файлы: загрузка в приватное хранилище, ссылка на материал урока
и раздача байтов по этой ссылке.

Публичного адреса у файла нет нигде: в базе лежит `key` объекта, наружу
уходит подписанная ссылка на 15 минут. Иначе материалы платного курса
разлетаются пересылкой одной строки (BACKEND_NOTES, раздел 9).
"""

import mimetypes
import re
import secrets
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import PurePosixPath
from typing import BinaryIO
from urllib.parse import quote

from app.adapters.db.models import LessonFile, User
from app.adapters.db.repos import EnrollmentRepo, LessonRepo, now_utc
from app.application.ports import StoragePort
from app.config import Settings
from app.domain.errors import (
    FileTooLargeError,
    ForbiddenError,
    NoFileError,
    NotFoundError,
)
from app.domain.signed_link import is_valid, sign

CHUNK_SIZE = 1024 * 1024
DEFAULT_MIME = "application/octet-stream"
# Ссылка недействительна и ссылка просрочена — для человека одно и то же:
# фронт открывает playback/файл заново, а не разбирает причину
BAD_LINK = "Ссылка устарела — откройте файл заново"


def _safe_name(raw: str) -> str:
    """Имя, как его прислал браузер, очищенное от путей: старые браузеры шлют
    путь целиком, и `C:\\Users\\Айгуль\\памятка.pdf` — это имя файла."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or "file"


def _new_key(name: str) -> str:
    """Ключ объекта в хранилище. Имя от человека в него не попадает: случайный
    hex снимает и совпадения имён, и попытки увести путь из каталога.
    Дата — по UTC, чтобы раскладка по каталогам не зависела от машины."""
    ext = re.sub(r"[^a-z0-9]", "", PurePosixPath(name).suffix.lower())[:10]
    day = now_utc().strftime("%Y/%m/%d")
    return f"uploads/{day}/{secrets.token_hex(8)}" + (f".{ext}" if ext else "")


def _chunks(stream: BinaryIO) -> Iterator[bytes]:
    while chunk := stream.read(CHUNK_SIZE):
        yield chunk


def _capped(chunks: Iterable[bytes], limit: int, max_mb: int) -> Iterator[bytes]:
    """Обрывает загрузку на превышении: чтобы узнать, что файл больше 20 МБ,
    не нужно принять все его 20 МБ."""
    size = 0
    for chunk in chunks:
        size += len(chunk)
        if size > limit:
            raise FileTooLargeError(max_mb)
        yield chunk


def _download_path(file_id: int, name: str) -> str:
    """Путь раздачи — он же строка, которую подписываем: у nginx в подпись
    идёт `$uri`, то есть путь без запроса и уже раскодированный."""
    return f"/files/lesson/{file_id}/{_safe_name(name)}"


class FilesService:
    def __init__(
        self,
        lessons: LessonRepo,
        enrollments: EnrollmentRepo,
        storage: StoragePort,
        cfg: Settings,
    ):
        self.lessons = lessons
        self.enrollments = enrollments
        self.storage = storage
        self.cfg = cfg

    def upload(self, filename: str, stream: BinaryIO) -> dict:
        """Загруженный файл ни к чему не привязан: привязка происходит в том
        запросе, куда `key` передаётся дальше (сдача работы, материал урока)."""
        name = _safe_name(filename)
        chunks = _chunks(stream)
        first = next(chunks, b"")
        if not first:
            raise NoFileError()

        key = _new_key(name)
        size = self.storage.save(
            key,
            _capped(
                chain([first], chunks),
                self.cfg.upload_max_mb * 1024 * 1024,
                self.cfg.upload_max_mb,
            ),
        )
        return {
            "key": key,
            "name": name,
            # Тип определяет сервер: присланному в запросе верить нечего
            "mime": mimetypes.guess_type(name)[0] or DEFAULT_MIME,
            "size_bytes": size,
        }

    def link(self, user: User, file_id: int) -> dict:
        """Ссылка на материал урока. Порядок проверок общий: существование,
        потом доступ, — поэтому у чужого курса приходит 403, а не 404."""
        found = self.lessons.file_with_course(file_id)
        if found is None:
            raise NotFoundError("Файл не найден")
        file, course = found
        if self.enrollments.active_for(user.id, course.id) is None:
            raise ForbiddenError("Материалы доступны учителям с доступом к курсу")

        # Секунды, а не микросекунды: в подпись уходит unix-время, и фронту
        # показываем ровно тот момент, до которого ссылка живёт
        expires = int((now_utc() + timedelta(minutes=self.cfg.file_url_ttl_min)).timestamp())
        expires_at = datetime.fromtimestamp(expires, UTC)
        path = _download_path(file.id, file.name)
        signature = sign(path, expires, self.cfg.storage_secret)
        return {
            "url": f"{self.cfg.public_base_url}{quote(path)}?e={expires}&s={signature}",
            "expires_at": expires_at,
        }

    def content(
        self, file_id: int, filename: str, expires: int, signature: str
    ) -> tuple[LessonFile, int, Iterator[bytes]]:
        """Байты по подписанной ссылке. Сессии здесь нет сознательно: право
        на файл доказывает подпись, и в бою этот путь заберёт nginx, который
        про сессии не знает."""
        path = _download_path(file_id, filename)
        now = int(now_utc().timestamp())
        if not is_valid(path, expires, signature, self.cfg.storage_secret, now=now):
            raise ForbiddenError(BAD_LINK)

        file = self.lessons.file_by_id(file_id)
        if file is None:
            raise NotFoundError("Файл не найден")
        size = self.storage.size(file.url)
        if size is None:
            # Строка в базе есть, объекта нет: для скачивающего это тот же 404
            raise NotFoundError("Файл не найден")
        return file, size, self.storage.read(file.url)
