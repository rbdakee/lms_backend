"""Экран задания: условие, файл-шаблон и сдача работы.

Рамка доступа та же, что у урока: всё требует входа и действующего
enrollment по курсу задания. Каждая отправка — новая строка, история не
переписывается: учитель видит, что присылал, и что ему на это ответили.

Ключ хранилища наружу не уходит нигде. В базе у файла работы лежит именно
он, а на экран отдаётся постоянная ссылка, байты по которой раздаются
с проверкой сессии — в отличие от материалов урока, где право доказывает
подпись (BACKEND_NOTES, раздел 9).
"""

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import quote

from app.adapters.db.models import Course, Submission, Task, User
from app.adapters.db.repos import (
    SUBMISSION_ACCEPTED,
    SUBMISSION_PENDING,
    SUBMISSION_REWORK,
    EnrollmentRepo,
    SubmissionRepo,
    TaskRepo,
    now_utc,
)
from app.application.files import BAD_LINK, safe_name
from app.application.ports import StoragePort, TelegramPort
from app.config import Settings
from app.domain.errors import (
    FieldError,
    ForbiddenError,
    NotFoundError,
    SubmissionPendingError,
    TaskAcceptedError,
)
from app.domain.signed_link import is_valid, sign
from app.domain.submission import ext_of, file_url, mime_of

log = logging.getLogger("tasks")

TASK_DENIED = "Задание доступно после выдачи доступа к курсу"
SUBMISSION_DENIED = "Работа доступна автору и администратору"
# Ещё не сдавал — «none»: сдач нет, а состояние экрану нужно.
NO_SUBMISSION = "none"
# Сдать можно, пока работа не у админа и задание не закрыто зачётом.
CAN_SUBMIT_STATUSES = (NO_SUBMISSION, SUBMISSION_REWORK)
MAX_FILES = 10

# Что сказать про пустую сдачу — зависит от того, что задание вообще принимает.
EMPTY_SUBMISSION = {
    "text": ("text", "Напишите ответ"),
    "file": ("files", "Приложите файл"),
    "both": ("text", "Напишите ответ или приложите файл"),
}


def _template_path(task_id: int, name: str) -> str:
    """Путь раздачи шаблона — он же строка, которую подписываем: у nginx
    в подпись идёт `$uri`, то есть путь без запроса и уже раскодированный."""
    return f"/files/task/{task_id}/{safe_name(name)}"


def template_file_out(storage: StoragePort, task: Task) -> dict | None:
    """Шаблон задания для экрана; null — шаблона нет.

    Ключ в базе есть, а объекта в хранилище нет — считаем, что шаблона нет:
    это недоделанное наполнение курса, а не сбой, и падать экран не должен.
    """
    if not task.template_file:
        return None
    size = storage.size(task.template_file)
    if size is None:
        return None
    name = PurePosixPath(task.template_file).name
    return {"name": name, "size_bytes": size, "mime": mime_of(name)}


def files_out(submission: Submission, base_url: str) -> list[dict]:
    """Файлы работы наружу: снимок метаданных плюс постоянная ссылка вместо
    ключа хранилища, который в этом снимке лежит под `url`."""
    return [
        {
            "name": file["name"],
            "size_bytes": file["size_bytes"],
            "mime": file["mime"],
            "url": file_url(base_url, submission.id, index, file["name"]),
        }
        for index, file in enumerate(submission.files)
    ]


def submission_out(submission: Submission, base_url: str) -> dict:
    """Сдача как элемент истории. reviewed_by здесь нет и не будет: комментарий
    проверяющего подписан на экране просто «Администратор»."""
    return {
        "id": submission.id,
        "status": submission.status,
        "created_at": submission.created_at,
        "text": submission.text,
        "files": files_out(submission, base_url),
        "comment": submission.comment,
        "reviewed_at": submission.reviewed_at,
    }


class TasksService:
    def __init__(
        self,
        tasks: TaskRepo,
        submissions: SubmissionRepo,
        enrollments: EnrollmentRepo,
        storage: StoragePort,
        telegram: TelegramPort,
        cfg: Settings,
        commit: Callable[[], None],
        preview_course_id: int | None,
    ):
        self.tasks = tasks
        self.submissions = submissions
        self.enrollments = enrollments
        self.storage = storage
        self.telegram = telegram
        self.cfg = cfg
        # Курс, который админ смотрит «как учитель»: по нему ничего не пишется
        self.preview_course_id = preview_course_id
        # Работа обязана быть в базе до того, как уйдёт в Telegram: бот
        # недоступен, а работа всё равно в очереди проверки.
        self.commit = commit

    # -- GET /tasks/{id} ------------------------------------------------

    def task_page(self, user: User, task_id: int) -> dict:
        task, _ = self._accessible(user, task_id)
        history = self.submissions.history_for(user.id, task.id)
        # Свежие сверху, значит статус задания — статус первой в списке
        status = history[0].status if history else NO_SUBMISSION
        return {
            "id": task.id,
            "module_id": task.module_id,
            "title": task.title,
            # JSON как его положил админ: форма условия фиксируется в сессии 7
            "statement": task.statement,
            "template_file": template_file_out(self.storage, task),
            "submit_format": task.submit_format,
            "allowed_ext": task.allowed_ext,
            "max_size_mb": task.max_size_mb,
            "time_required_min": task.time_required_min,
            "status": status,
            "can_submit": status in CAN_SUBMIT_STATUSES,
            "submissions": [
                submission_out(submission, self.cfg.public_base_url) for submission in history
            ],
        }

    # -- файл-шаблон ----------------------------------------------------

    def template_link(self, user: User, task_id: int) -> dict:
        """Подписанная ссылка на шаблон — как у материалов урока: короткий срок
        жизни, фронт запрашивает её по клику."""
        task, _ = self._accessible(user, task_id)
        template = template_file_out(self.storage, task)
        if template is None:
            # Шаблона нет — нечего и подписывать; на экране кнопки тоже нет
            raise NotFoundError("Файл не найден")

        # Секунды, а не микросекунды: в подпись уходит unix-время, и фронту
        # показываем ровно тот момент, до которого ссылка живёт
        expires = int((now_utc() + timedelta(minutes=self.cfg.file_url_ttl_min)).timestamp())
        path = _template_path(task.id, template["name"])
        signature = sign(path, expires, self.cfg.storage_secret)
        return {
            "url": f"{self.cfg.public_base_url}{quote(path)}?e={expires}&s={signature}",
            "expires_at": datetime.fromtimestamp(expires, UTC),
        }

    def template_content(
        self, task_id: int, filename: str, expires: int, signature: str
    ) -> tuple[str, str, int, Iterator[bytes]]:
        """Байты шаблона по подписанной ссылке. Сессии здесь нет сознательно:
        право на файл доказывает подпись, и в бою этот путь заберёт nginx."""
        path = _template_path(task_id, filename)
        now = int(now_utc().timestamp())
        if not is_valid(path, expires, signature, self.cfg.storage_secret, now=now):
            raise ForbiddenError(BAD_LINK)

        task = self.tasks.by_id(task_id)
        if task is None or not task.template_file:
            raise NotFoundError("Файл не найден")
        size = self.storage.size(task.template_file)
        if size is None:
            # Ключ в базе есть, объекта нет: для скачивающего это тот же 404
            raise NotFoundError("Файл не найден")
        name = PurePosixPath(task.template_file).name
        return name, mime_of(name), size, self.storage.read(task.template_file)

    # -- файлы сданной работы -------------------------------------------

    def submission_file(
        self, user: User, submission_id: int, index: int
    ) -> tuple[dict, int, Iterator[bytes]]:
        """Байты файла работы. Подписи здесь нет: ссылка постоянная, и право
        доказывает сессия — свою работу открывает автор, любую — админ."""
        submission = self.submissions.by_id(submission_id)
        if submission is None:
            raise NotFoundError("Файл не найден")
        if submission.user_id != user.id and not user.is_admin:
            raise ForbiddenError(SUBMISSION_DENIED)
        if not 0 <= index < len(submission.files):
            raise NotFoundError("Файл не найден")

        file = submission.files[index]
        size = self.storage.size(file["url"])
        if size is None:
            raise NotFoundError("Файл не найден")
        return file, size, self.storage.read(file["url"])

    # -- POST /tasks/{id}/submissions -----------------------------------

    def submit(self, user: User, task_id: int, text: str | None, files: list[dict]) -> dict:
        task, course = self._accessible(user, task_id)
        last = self.submissions.last_for(user.id, task.id)
        if last is not None and last.status == SUBMISSION_PENDING:
            raise SubmissionPendingError()
        if last is not None and last.status == SUBMISSION_ACCEPTED:
            raise TaskAcceptedError()

        text = (text or "").strip() or None
        preview = course.id == self.preview_course_id
        snapshot = self._snapshot(task, text, files, preview=preview)
        if preview:
            # Ранний выход после проверок состава: они и есть то, ради чего
            # админ жмёт «Отправить» в предпросмотре. Ни строки в submission,
            # ни сообщения в бот (BACKEND_NOTES, раздел 12). Объект создан
            # в памяти и в сессию SQLAlchemy не добавлен; id=0 — ссылки на его
            # файлы никуда не ведут, потому что и файлов нет
            return submission_out(
                Submission(
                    id=0,
                    user_id=user.id,
                    task_id=task.id,
                    text=text,
                    files=snapshot,
                    status=SUBMISSION_PENDING,
                    created_at=now_utc(),
                ),
                self.cfg.public_base_url,
            )
        submission = self.submissions.create(user.id, task.id, text=text, files=snapshot)
        if submission is None:
            # Гонка двух одновременных отправок: вторую pending не пустила база
            raise SubmissionPendingError()
        self.commit()
        try:
            # Без ФИО и телефона: кто именно сдал, админ увидит в карточке
            self.telegram.notify_admins(
                f"Работа №{submission.id} на проверку:"
                f" задание „{task.title}“, курс „{course.title}“"
            )
        except Exception:
            log.exception("Telegram-уведомление о работе %s не ушло", submission.id)
        return submission_out(submission, self.cfg.public_base_url)

    def _snapshot(
        self, task: Task, text: str | None, files: list[dict], *, preview: bool = False
    ) -> list[dict]:
        """Проверки состава и файлов — здесь, а не при загрузке: POST /files
        не знает, в какое задание файл поедет.

        Метаданные снимаются с хранилища: присланным размеру и типу верить
        нечего, а имя берётся из запроса — под ним человек файл и узнает.
        В предпросмотре снимать их неоткуда: загрузка там тоже no-op, объекта
        под ключом нет, — поэтому размер считается нулевым, а состав и формат
        проверяются как обычно (BACKEND_NOTES, раздел 12).
        """
        self._check_format(task, text, files)
        if len(files) > MAX_FILES:
            raise FieldError("files", f"Можно приложить не больше {MAX_FILES} файлов")

        allowed = {ext.lower().lstrip(".") for ext in task.allowed_ext}
        limit_bytes = task.max_size_mb * 1024 * 1024
        snapshot = []
        for file in files:
            name = safe_name(file["name"])
            size = 0 if preview else self.storage.size(file["key"])
            if size is None:
                raise FieldError("files", "Файл не найден — загрузите заново")
            ext = ext_of(name)
            if allowed and ext not in allowed:
                raise FieldError(
                    "files",
                    f"Формат .{ext} не принимается — можно: {', '.join(task.allowed_ext)}",
                )
            if size > limit_bytes:
                raise FieldError("files", f"Файл больше {task.max_size_mb} МБ")
            # В базе у файла лежит ключ хранилища; наружу вместо него уходит
            # постоянная ссылка — см. files_out
            snapshot.append(
                {"name": name, "url": file["key"], "size_bytes": size, "mime": mime_of(name)}
            )
        return snapshot

    @staticmethod
    def _check_format(task: Task, text: str | None, files: list[dict]) -> None:
        """submit_format решает состав работы: лишнее — такая же ошибка формы,
        как и пустая сдача, и подсвечивается на том поле, где оно лишнее."""
        if task.submit_format == "text" and files:
            raise FieldError("files", "Задание сдаётся текстом — файл прикладывать не нужно")
        if task.submit_format == "file" and text:
            raise FieldError("text", "Задание сдаётся файлом — текст не нужен")
        if not text and not files:
            raise FieldError(*EMPTY_SUBMISSION[task.submit_format])

    # -- рамка доступа ---------------------------------------------------

    def _accessible(self, user: User, task_id: int) -> tuple[Task, Course]:
        """Порядок проверок один на все эндпоинты задания: существование, потом
        доступ, — поэтому у чужого курса приходит 403, а не 404."""
        found = self.tasks.visible_with_course(task_id)
        if found is None:
            raise NotFoundError("Задание не найдено")
        task, course = found
        if self.enrollments.active_for(user.id, course.id) is None:
            raise ForbiddenError(TASK_DENIED)
        return task, course
