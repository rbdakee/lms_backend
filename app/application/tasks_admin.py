"""Редактор задания: условие, файл-шаблон и правила сдачи.

Отдельно от `TasksService` потому, что смотрит в другую сторону. Тому задание —
экран учителя: доступ по enrollment, история сдач и кнопка «Отправить». Здесь
задание правят, поэтому приходит и скрытое задание, и задание черновика,
а вместо истории приходят три админских признака.

Условие чистит сервер по тому же белому списку, что и текст урока: что
вернулось из `PATCH`, то и лежит в базе. Любая правка двигает `updated_at`
курса — для методиста курс это дерево целиком, и столбец «Изменён» в списке
без этого врал бы.
"""

from app.adapters.db.models import Course, Module, Task
from app.adapters.db.repos import CourseAdminRepo, TaskAdminRepo
from app.application.courses_admin import task_ready
from app.application.files import safe_name
from app.application.ports import StoragePort
from app.application.tasks import template_file_out
from app.config import Settings
from app.domain.content import sanitize_html
from app.domain.errors import FieldError, HasSubmissionsError, NotFoundError

# Поля задания, у которых null в PATCH — это «не прислано»: колонка обязательная.
# Пустой allowed_ext здесь не мешает — это список, а не null.
PLAIN_FIELDS = (
    "title",
    "submit_format",
    "allowed_ext",
    "max_size_mb",
    "time_required_min",
    "is_hidden",
)


def _checked_title(raw: str) -> str:
    title = raw.strip()
    if not title:
        raise FieldError("title", "Без названия задание не сохранить")
    return title


def _statement(value: dict) -> dict:
    """Разметку чистит сервер, как и у урока: что вернулось из PATCH, то и лежит
    в базе — иначе автор не увидит, что его разметку почистили, и будет чинить
    одно и то же во второй раз.

    Пустое условие — законное состояние: заготовка из окна «Добавить
    в программу» ровно такая, и дерево показывает её как «черновик».
    """
    return {"html": sanitize_html(value["html"])}


def _allowed_ext(raw: list[str]) -> list[str]:
    """Расширения без точки и в нижнем регистре — ровно в том виде, в каком их
    сравнивает сдача работы. Иначе `.PDF` из формы не совпадёт ни с чем,
    а в тексте отказа человек прочитает «можно: .PDF» и не поймёт, что не так.

    Пустой список — ограничения нет.
    """
    cleaned: list[str] = []
    for ext in raw:
        one = ext.strip().lower().lstrip(".")
        if one and one not in cleaned:
            cleaned.append(one)
    return cleaned


class TasksAdminService:
    def __init__(
        self,
        tasks: TaskAdminRepo,
        courses: CourseAdminRepo,
        storage: StoragePort,
        cfg: Settings,
    ):
        self.tasks = tasks
        self.courses = courses
        self.storage = storage
        self.cfg = cfg

    # -- POST /admin/modules/{id}/tasks ----------------------------------

    def create(self, module_id: int, fields: dict) -> dict:
        module = self._found_module(module_id)
        # Условие пустое: колонка обязательная, а текст появится в редакторе,
        # куда фронт уводит сразу после создания. Заготовка при этом скрыта —
        # задание без условия в открытом курсе учителю показывать нечем
        task = self.tasks.create(
            module_id=module.id,
            title=_checked_title(fields["title"]),
            statement={"html": ""},
            time_required_min=fields["time_required_min"],
            order_index=self.courses.next_order_index(module.id),
            is_hidden=True,
        )
        course = self.courses.by_id(module.course_id)
        self.courses.touch(course.id)
        return self._card_out(task, module, course)

    # -- GET /admin/tasks/{id} -------------------------------------------

    def card(self, task_id: int) -> dict:
        return self._card_out(*self._found(task_id))

    # -- PATCH /admin/tasks/{id} -----------------------------------------

    def patch(self, task_id: int, fields: dict) -> dict:
        task, module, course = self._found(task_id)
        if fields.get("title") is not None:
            fields = {**fields, "title": _checked_title(fields["title"])}
        if fields.get("allowed_ext") is not None:
            fields = {**fields, "allowed_ext": _allowed_ext(fields["allowed_ext"])}
        if fields.get("max_size_mb") is not None:
            self._check_max_size(fields["max_size_mb"])
        for field in PLAIN_FIELDS:
            if fields.get(field) is not None:
                setattr(task, field, fields[field])
        if fields.get("statement") is not None:
            task.statement = _statement(fields["statement"])
        # null — убрать шаблон; в остальном это key из ответа POST /files
        if "template_file" in fields:
            task.template_file, task.template_file_name = self._template(fields["template_file"])
        self.courses.touch(course.id)
        return self._card_out(task, module, course)

    # -- DELETE /admin/tasks/{id} ----------------------------------------

    def delete(self, task_id: int) -> None:
        task, _, course = self._found(task_id)
        # Задание со сдачами не удаляется, а скрывается: работы людей
        # ссылались бы в пустоту, а очередь проверки собирается джойном
        # по заданию
        submissions = self._submissions_count(task.id)
        if submissions:
            raise HasSubmissionsError(submissions)
        self.tasks.delete(task.id)
        self.courses.touch(course.id)

    # -- сборка ответа ----------------------------------------------------

    def _found(self, task_id: int) -> tuple[Task, Module, Course]:
        found = self.tasks.with_course_and_module(task_id)
        if found is None:
            raise NotFoundError("Задание не найдено")
        return found

    def _found_module(self, module_id: int) -> Module:
        module = self.courses.module_by_id(module_id)
        if module is None:
            raise NotFoundError("Модуль не найден")
        return module

    def _check_max_size(self, max_size_mb: int) -> None:
        """Потолок стоит у загрузки (POST /files), и задание не может обещать
        больше, чем примет сервер: человек собрал бы работу и получил отказ
        уже на отправке."""
        if max_size_mb > self.cfg.upload_max_mb:
            raise FieldError(
                "max_size_mb", f"Больше {self.cfg.upload_max_mb} МБ сервер не примет"
            )

    def _template(self, value: dict | None) -> tuple[str | None, str | None]:
        """Ключ объекта в хранилище и имя, которое увидит учитель; null убирает
        шаблон у задания.

        Имя хранится отдельной колонкой, а не выводится из ключа: ключи
        POST /files случайные нарочно, и учителю досталось бы
        «9f3c1a7e4b2d8c05.docx» вместо «Шаблон дескрипторов.docx».

        Байты при отвязке остаются: порт storage умеет писать, читать и мерить,
        но не удалять. Ключа, под которым объекта нет, не сохраняем — иначе
        админ увидит пустой блок вместо только что приложенного файла.
        """
        if value is None:
            return None, None
        if self.storage.size(value["key"]) is None:
            raise NotFoundError("Загруженный файл не найден — загрузите его заново")
        return value["key"], safe_name(value["name"])

    def _submissions_count(self, task_id: int) -> int:
        return self.courses.submission_counts([task_id]).get(task_id, 0)

    def _card_out(self, task: Task, module: Module, course: Course) -> dict:
        return {
            "id": task.id,
            "module_id": task.module_id,
            # Хлебные крошки шапки редактора; lang рисует метку языка —
            # переключателя языка в задании нет
            "course": {"id": course.id, "lang": course.lang, "title": course.title},
            "module": {"id": module.id, "title": module.title},
            "title": task.title,
            "statement": task.statement,
            # Так же, как учителю: имя, размер и тип, без ключа хранилища
            "template_file": template_file_out(self.storage, task),
            "submit_format": task.submit_format,
            "allowed_ext": task.allowed_ext,
            "max_size_mb": task.max_size_mb,
            "time_required_min": task.time_required_min,
            "is_hidden": task.is_hidden,
            "is_ready": task_ready(task),
            "has_data": self._submissions_count(task.id) > 0,
        }
