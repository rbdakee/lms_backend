"""Редактор урока: заготовка, содержимое, ссылка на видео и материалы.

Отдельно от `LessonsService` потому, что смотрит в другую сторону. Тому урок —
экран учителя: доступ по enrollment, отметка «пройден» и подписанная ссылка
вместо `video_url`. Здесь урок правят, поэтому приходит и скрытый урок,
и урок черновика, а ссылку на видео админ видит как есть — без неё поле
редактора пустое.

Любая правка урока и его материалов двигает `updated_at` курса: для методиста
курс — это дерево целиком, и столбец «Изменён» в списке без этого врал бы.
"""

from app.adapters.db.models import Course, Lesson, LessonFile, Module
from app.adapters.db.repos import CourseAdminRepo, LessonAdminRepo
from app.application.courses_admin import lesson_ready
from app.application.files import safe_name
from app.application.ports import StoragePort
from app.domain.content import normalize_youtube, sanitize_html
from app.domain.errors import FieldError, HasProgressError, NotFoundError
from app.domain.submission import mime_of

# Поля урока, у которых null в PATCH — это «не прислано»: колонка обязательная.
PLAIN_FIELDS = ("title", "kind", "time_required_min", "is_hidden")


def _checked_title(raw: str) -> str:
    title = raw.strip()
    if not title:
        raise FieldError("title", "Без названия урок не сохранить")
    return title


def _body(value: dict | None) -> dict | None:
    """Разметку чистит сервер: что вернулось из PATCH, то и лежит в базе —
    иначе автор не увидит, что его разметку почистили, и будет чинить одно
    и то же во второй раз. null — стереть заметку под видео."""
    return None if value is None else {"html": sanitize_html(value["html"])}


def _video_url(value: str | None) -> str | None:
    """Ссылка приводится к одному написанию: иначе одно и то же видео лежит
    в базе четырьмя разными строками и правкой его не найти.

    Пустая строка — это «ссылки нет»: очищенное поле формы приходит именно
    так, и отбивать его как «не похоже на YouTube» значило бы врать —
    поле не заполнено, а не заполнено неверно.
    """
    if value is None or not value.strip():
        return None
    url = normalize_youtube(value.strip())
    if url is None:
        raise FieldError("video_url", "Не похоже на ссылку YouTube — проверьте адрес")
    return url


def _check_content(lesson: Lesson) -> None:
    """Вид урока задаёт, что обязательно (BACKEND_NOTES, раздел 2): у видеоурока
    ссылка, у текстового — непустая разметка.

    Проверяется тот же признак, что рисует бейдж «готов» в дереве программы:
    два независимых определения готовности разошлись бы, и урок оказался бы
    сохранён черновиком, который чек-лист считает заполненным.
    """
    if lesson_ready(lesson):
        return
    if lesson.kind == "video":
        raise FieldError("video_url", "Без ссылки видеоурок не сохранится")
    raise FieldError("body", "Без текста текстовый урок не сохранится")


def _file_out(file: LessonFile) -> dict:
    """Материал урока наружу. Ключа хранилища здесь нет, как и везде: он
    уходит только тому, кто прямо сейчас загрузил файл (POST /files)."""
    return {
        "id": file.id,
        "name": file.name,
        "size_bytes": file.size_bytes,
        "mime": file.mime,
    }


class LessonsAdminService:
    def __init__(
        self, lessons: LessonAdminRepo, courses: CourseAdminRepo, storage: StoragePort
    ):
        self.lessons = lessons
        self.courses = courses
        self.storage = storage

    # -- POST /admin/modules/{id}/lessons --------------------------------

    def create(self, module_id: int, fields: dict) -> dict:
        module = self._found_module(module_id)
        # Заготовка обходит проверку вида урока нарочно: в момент создания
        # ссылки ещё нет, она появится в редакторе. Это единственное место,
        # где урок сохраняется пустым, — и потому он заводится скрытым:
        # в курсе, где уже учатся, пустой урок открылся бы учителю сразу
        # и не проиграл бы ничего. Показывают его снятием is_hidden
        lesson = self.lessons.create(
            module_id=module.id,
            title=_checked_title(fields["title"]),
            kind=fields["kind"],
            time_required_min=fields["time_required_min"],
            order_index=self.courses.next_order_index(module.id),
            is_hidden=True,
        )
        course = self.courses.by_id(module.course_id)
        self.courses.touch(course.id)
        return self._card_out(lesson, module, course)

    # -- GET /admin/lessons/{id} -----------------------------------------

    def card(self, lesson_id: int) -> dict:
        return self._card_out(*self._found(lesson_id))

    # -- PATCH /admin/lessons/{id} ---------------------------------------

    def patch(self, lesson_id: int, fields: dict) -> dict:
        lesson, module, course = self._found(lesson_id)
        if fields.get("title") is not None:
            fields = {**fields, "title": _checked_title(fields["title"])}
        for field in PLAIN_FIELDS:
            if fields.get(field) is not None:
                setattr(lesson, field, fields[field])
        if "body" in fields:
            lesson.body = _body(fields["body"])
        if "video_url" in fields:
            lesson.video_url = _video_url(fields["video_url"])
        if "duration_label" in fields:
            lesson.duration_label = fields["duration_label"]
        # Проверяется то, что получилось, а не то, что было: смена вида урока
        # вместе с новым содержимым проходит одним запросом, смена вида
        # в одиночку — отбивается. Запрос, в котором нет ничего, кроме
        # is_hidden, не проверяется вовсе: иначе спрятать урок с прогрессом
        # было бы нечем, а это ровно то, что предлагает текст 409
        if set(fields) - {"is_hidden"}:
            _check_content(lesson)
        self.courses.touch(course.id)
        return self._card_out(lesson, module, course)

    # -- DELETE /admin/lessons/{id} --------------------------------------

    def delete(self, lesson_id: int) -> None:
        lesson, _, course = self._found(lesson_id)
        # Урок, который кто-то прошёл, не удаляется, а скрывается: у сорока
        # человек «12 из 18» превратилось бы в «12 из 17», а у кого-то
        # условия сертификата выполнились бы сами собой
        progress = self._progress_count(lesson.id)
        if progress:
            raise HasProgressError(progress)
        self.lessons.delete(lesson.id)
        self.courses.touch(course.id)

    # -- POST /admin/lessons/{id}/files -----------------------------------

    def add_file(self, lesson_id: int, key: str, name: str) -> dict:
        lesson, _, course = self._found(lesson_id)
        # Размер сервер берёт из хранилища, тип — из имени: присланному
        # браузером верить нечего, а объекта под ключом может уже не быть
        size = self.storage.size(key)
        if size is None:
            raise NotFoundError("Загруженный файл не найден — загрузите его заново")
        name = safe_name(name)
        file = self.lessons.add_file(
            lesson_id=lesson.id, name=name, url=key, size_bytes=size, mime=mime_of(name)
        )
        self.courses.touch(course.id)
        return _file_out(file)

    # -- DELETE /admin/lesson_files/{id} ----------------------------------

    def delete_file(self, file_id: int) -> None:
        file = self.lessons.file_by_id(file_id)
        if file is None:
            raise NotFoundError("Файл не найден")
        course = self.courses.course_of_item("lesson", file.lesson_id)
        self.lessons.delete_file(file.id)
        self.courses.touch(course.id)

    # -- сборка ответа ----------------------------------------------------

    def _found(self, lesson_id: int) -> tuple[Lesson, Module, Course]:
        found = self.lessons.with_course_and_module(lesson_id)
        if found is None:
            raise NotFoundError("Урок не найден")
        return found

    def _found_module(self, module_id: int) -> Module:
        module = self.courses.module_by_id(module_id)
        if module is None:
            raise NotFoundError("Модуль не найден")
        return module

    def _progress_count(self, lesson_id: int) -> int:
        return self.courses.progress_counts([lesson_id]).get(lesson_id, 0)

    def _card_out(self, lesson: Lesson, module: Module, course: Course) -> dict:
        return {
            "id": lesson.id,
            "module_id": lesson.module_id,
            # Хлебные крошки шапки редактора; lang рисует метку языка —
            # переключателя языка в уроке нет
            "course": {"id": course.id, "lang": course.lang, "title": course.title},
            "module": {"id": module.id, "title": module.title},
            "title": lesson.title,
            "kind": lesson.kind,
            "body": lesson.body,
            # Учителю ссылку не отдают никогда — только подписанный playback;
            # админ её редактирует, и без неё поле пустое
            "video_url": lesson.video_url,
            "duration_label": lesson.duration_label,
            "time_required_min": lesson.time_required_min,
            "is_hidden": lesson.is_hidden,
            "is_ready": lesson_ready(lesson),
            "has_data": self._progress_count(lesson.id) > 0,
            "files": [_file_out(file) for file in self.lessons.files(lesson.id)],
        }
