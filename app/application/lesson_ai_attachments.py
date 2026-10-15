"""Вложения в чат «Жоспара» (решение владельца 16.10.2026): принять файл,
снять с него выжимку один раз, показать учителю и отдать разговору.

Выжимка снимается **в фоне**, а не в запросе загрузки: разбор
двадцатистраничного PDF — минута модели, и держать загрузку открытой
на это время нельзя. Очередь здесь — та же мысль, что у генерации
(`lesson_ai_queue.py`), но не её таблица: `lesson_ai_job` привязана к
комплекту и материалу, а у вложения нет ни того, ни другого. Воркер берёт
старейшее `processing`-вложение под замком строки и держит замок до конца
разбора: замок на десятки секунд разовой операции дешевле таблицы аренд
вокруг одного статуса. Процесс умер посреди разбора — замок упал вместе
с соединением, и после рестарта вложение разбирается заново.

Провал разбора — состояние вложения (`failed` + код), а не отказ учителю:
файл уже принят, и учитель видит у скрепки «не получилось», может удалить
его и приложить заново. Персонального в логе не появляется ни в каком виде:
имя файла лежит в базе, исключения провайдера в лог уходят обезличенными
(`OpenAiChat`), здесь же — только коды и номера строк.
"""

import logging
import secrets
from collections.abc import Callable
from typing import Protocol

from sqlalchemy.orm import Session as DbSession

from app.adapters.ai.openai_chat import AiUnavailableError, MediaUnsupportedError
from app.adapters.db.lesson_ai_repos import LessonAttachmentRepo, LessonChatRepo, LessonUsageRepo
from app.adapters.db.models import LessonAiChat, User, now_utc
from app.application.ports import AiMedia, AiPort, StoragePort
from app.domain.errors import (
    AttachmentLimitError,
    AttachmentPagesError,
    AttachmentTooBigError,
    AttachmentTypeError,
    FieldError,
    NotFoundError,
)
from app.domain.lesson_ai import ATTACHMENT_SUMMARY_MAX
from app.lesson_ai import attachments as file_formats
from app.lesson_ai.prompts import attachment as prompt

log = logging.getLogger(__name__)

# Читаем загрузку кусками и не держим в памяти больше лимита: лишний
# мегабайт — запас, чтобы отличить «ровно на лимите» от «чуть больше»
CHUNK = 1024 * 1024


class StorageFactory(Protocol):
    def __call__(self) -> StoragePort: ...


class LessonAttachmentService:
    def __init__(
        self,
        db: DbSession,
        storage: StoragePort,
        ai: AiPort,
        *,
        max_files: int,
        max_mb: int,
        max_pdf_pages: int,
        model: str,
    ):
        self.db = db
        self.storage = storage
        self.ai = ai
        self.max_files = max_files
        self.max_mb = max_mb
        self.max_pdf_pages = max_pdf_pages
        # Какой моделью снималась выжимка — в учёт расхода: настройка
        # меняется, а записанная строка остаётся честной
        self.model = model
        self.repo = LessonAttachmentRepo(db)

    # -- приём --------------------------------------------------------------

    def upload(self, user: User, chat_id: int, filename: str, stream) -> dict:
        """Один файл multipart'ом. Все проверки — до записи в хранилище:
        отклонённый файл не оставляет после себя ни объекта, ни строки."""
        chat = self._own_chat(user, chat_id)
        if self.repo.count_in_chat(chat.id) >= self.max_files:
            raise AttachmentLimitError(self.max_files)
        data = self._read(stream)
        if file_formats.is_heic(data):
            # HEIC в браузерах не приходит: iPhone конвертирует его в JPEG
            # при выборе файла. Значит файл прислали мимо экрана — просим
            # человека, а не тащим конвертацию на сервер
            raise AttachmentTypeError(
                "Фото в формате HEIC не принимается — сохраните его как JPG"
                " и приложите заново"
            )
        kind = file_formats.sniff(data)
        if kind is None:
            raise AttachmentTypeError(
                "Такой файл не принимается: приложите фото (JPG, PNG, WEBP),"
                " PDF, документ Word или презентацию PowerPoint"
            )
        pages = file_formats.pdf_pages(data) if kind == "pdf" else None
        if pages is not None and pages > self.max_pdf_pages:
            raise AttachmentPagesError(self.max_pdf_pages, pages)
        mime = (
            file_formats.image_mime(data) if kind == "image" else file_formats.MIME_BY_KIND[kind]
        )
        row = self.repo.add(
            user_id=user.id,
            chat_id=chat.id,
            kind=kind,
            mime=mime,
            filename=_safe_name(filename),
            size_bytes=len(data),
            pages=pages,
            storage_key=f"lesson_ai/{now_utc():%Y/%m/%d}/{secrets.token_hex(8)}",
            status="processing",
        )
        self.storage.save(row.storage_key, [data])
        return self.out(row)

    def _read(self, stream) -> bytes:
        """Байты загрузки с потолком размера, проверяемым по дороге: файл
        вдвое больше лимита не должен залетать в память целиком прежде,
        чем мы его отвергнем."""
        chunks = []
        size = 0
        limit = self.max_mb * 1024 * 1024
        while chunk := stream.read(CHUNK):
            size += len(chunk)
            if size > limit:
                raise AttachmentTooBigError(self.max_mb)
            chunks.append(chunk)
        if not chunks:
            raise FieldError("file", "Файл пустой")
        return b"".join(chunks)

    # -- учителю ------------------------------------------------------------

    def attachments(self, user: User, chat_id: int) -> dict:
        chat = self._own_chat(user, chat_id)
        return {"items": [self.out(row) for row in self.repo.in_chat(chat.id)]}

    def delete(self, user: User, chat_id: int, attachment_id: int) -> None:
        chat = self._own_chat(user, chat_id)
        row = self.repo.get(attachment_id)
        # Чужое и «нет такого» неотличимы: по номерам нельзя перебирать,
        # чьи файлы существуют
        if row is None or row.chat_id != chat.id or row.user_id != user.id:
            raise NotFoundError("Вложение не найдено")
        self.repo.delete(row)
        self.storage.delete(row.storage_key)

    def _own_chat(self, user: User, chat_id: int) -> LessonAiChat:
        chat = LessonChatRepo(self.db).chat(chat_id)
        if chat is None or chat.user_id != user.id:
            raise NotFoundError("Чат не найден")
        return chat

    def out(self, row) -> dict:
        return attachment_out(row)


def attachment_out(row) -> dict:
    """Строка вложения для фронта — одна форма у ручек вложений и у ленты
    чата. `summary` здесь нарочно нет: это материал для модели, экрану
    хватает статуса — гонять тысячи знаков выжимки на каждый опрос ленты
    незачем."""
    return {
        "id": row.id,
        "kind": row.kind,
        "mime": row.mime,
        "filename": row.filename,
        "size_bytes": row.size_bytes,
        "pages": row.pages,
        "status": row.status,
        "error_code": row.error_code,
        "created_at": row.created_at,
    }


def _safe_name(raw: str) -> str:
    """Имя, как его прислал браузер, без пути: `C:\\Users\\…\\памятка.pdf` —
    это имя файла, а не путь. В лог оно не идёт, в базе лежит для учителя."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:255] or "file"


class AttachmentRunner:
    """Фоновый разбор: взять `processing`-вложение, снять выжимку, закрыть.

    Одна транзакция на вложение; вызов модели происходит **под замком
    строки** — второй воркер это же вложение не возьмёт, а возьмёт следующее.
    """

    def __init__(
        self,
        session: Callable[[], DbSession],
        storage: StorageFactory,
        ai: AiPort,
        *,
        model: str,
    ):
        self.session = session
        self.storage = storage
        self.ai = ai
        self.model = model

    def run_next(self) -> bool:
        """Разобрать одно вложение. False — разбираемых не было."""
        with self.session() as db:
            row = LessonAttachmentRepo(db).next_pending()
            if row is None:
                return False
            self._summarize(db, row)
            db.commit()
        return True

    def _summarize(self, db: DbSession, row) -> None:
        data = b"".join(self.storage().read(row.storage_key))
        try:
            summary, tokens = self._extract(row, data)
        except Exception as exc:
            # Текст исключения в лог и в базу не идёт: в нём бывает
            # содержимое файла и переписка с провайдером
            log.warning("Вложение %s: разбор не удался (%s)", row.id, self._fail_code(exc))
            self._finish(db, row, None, self._fail_code(exc), (0, 0))
            return
        self._finish(db, row, file_formats.snip(summary, ATTACHMENT_SUMMARY_MAX), None, tokens)

    def _extract(self, row, data: bytes) -> tuple[str, tuple[int, int]]:
        """Выжимка по виду. docx и pptx — без модели; image и pdf — один
        мультимодальный запрос, сколько бы страниц в файле ни было."""
        extractor = file_formats.EXTRACTORS.get(row.kind)
        if extractor is not None:
            return extractor(data), (0, 0)
        reply = self.ai.describe_media(
            system=prompt.SYSTEM,
            media=AiMedia(mime=row.mime, data=data, filename=row.filename),
            instruction=prompt.INSTRUCTION,
        )
        return reply.text, (reply.tokens_in, reply.tokens_out)

    @staticmethod
    def _fail_code(exc: Exception) -> str:
        """Код отказа по исключению. «Провайдер файлы не умеет» отличён от
        «не ответил»: первое не лечится повторной попыткой, и учителю они
        отвечают разными словами."""
        if isinstance(exc, MediaUnsupportedError):
            return "attachment_unsupported"
        if isinstance(exc, AiUnavailableError):
            return "ai_failed"
        return "extract_failed"

    def _finish(
        self, db: DbSession, row, summary: str | None, error: str | None, tokens
    ) -> None:
        tokens_in, tokens_out = tokens
        row.status = "ready" if summary is not None else "failed"
        row.summary = summary
        row.error_code = error
        db.flush()
        # Расход — на учителя, владевшего файлом; у docx и pptx его нет,
        # и строка учёта не пишется вовсе (`LessonUsageRepo.add` молчит
        # на нулевых токенах)
        LessonUsageRepo(db).add(
            row.user_id,
            "attachment",
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            chat_id=row.chat_id,
            kind=row.kind,
        )
