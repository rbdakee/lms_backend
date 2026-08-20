"""Редактор курса и его программы: список версий, поля курса, дерево модулей.

Отдельно от `CoursesService` потому, что смотрит в другую сторону. Тот
показывает курс площадке — только опубликованные версии, только видимые
элементы. Здесь наоборот: правят черновик, и скрытый элемент нужно видеть,
иначе его нечем достать обратно.

Любая правка курса или его программы двигает `updated_at`: для методиста
курс — это дерево целиком, и столбец «Изменён» в списке без этого врал бы.
"""

from app.adapters.db.models import Course, Lesson, Module, Quiz, Task
from app.adapters.db.repos import CategoryRepo, CourseAdminRepo
from app.application.courses import cover_url
from app.application.files import safe_name
from app.application.ports import StoragePort
from app.config import Settings
from app.domain.content import is_blank_html
from app.domain.errors import (
    CourseInUseError,
    FieldError,
    ModuleInUseError,
    NotFoundError,
    VersionExistsError,
)
from app.domain.image import HEAD_SIZE, image_mime
from app.domain.plural import plural
from app.domain.program import LESSON_KINDS, item_key

# Что берёт обложка — то же, что логотип платформы: картинка любого формата,
# включая svg. Раздаётся она с теми же заголовками безопасности.
NOT_AN_IMAGE = "Нужна картинка: PNG, JPEG, GIF, WEBP или SVG"

# Статусы, в которых курс показывают людям: их смена проверяет минимум,
# без которого карточку нечем нарисовать.
PUBLIC_STATUSES = ("planned", "open", "closed")

# Поля курса, у которых null в PATCH — это «не прислано»: колонка обязательная.
PLAIN_FIELDS = (
    "title",
    "short",
    "full",
    "category_id",
    "hours",
    "status",
    "strict_order",
    "cert_require_lessons",
    "cert_require_tasks",
    "cert_require_module_quizzes",
    "cert_require_final_quiz",
)

# Поля, у которых null — значение: «цена по запросу», «даты нет». Обложки
# здесь нет: она приходит объектом и ложится в две колонки — см. patch.
NULLABLE_FIELDS = ("duration_text", "price", "starts_at")

# Поля, которые переезжают в языковую версию и в дубликат. Цена, дата старта
# и статус — не переезжают: цену на казахскую версию ставят отдельно, и молча
# продублировать её нельзя.
COPIED_FIELDS = (
    "title",
    "short",
    "full",
    "cover_key",
    "cover_name",
    # Старый адрес-ссылка переезжает вместе с остальным: у копии курса 13
    # обложка обязана остаться той же самой
    "cover",
    "category_id",
    "hours",
    "duration_text",
    "strict_order",
    "cert_require_lessons",
    "cert_require_tasks",
    "cert_require_module_quizzes",
    "cert_require_final_quiz",
)

# Название версии на её же языке — так его читает админ в тексте ошибки.
LANG_VERSIONS = {"ru": "Русская версия", "kz": "Қазақша нұсқа"}

# Что именно держит элемент от удаления — по виду элемента (BACKEND_NOTES,
# раздел 10). Дальше такой элемент прячут: is_hidden есть у всех трёх видов.
BLOCK_REASONS = {
    "video": "has_progress",
    "text": "has_progress",
    "quiz": "has_attempts",
    "task": "has_submissions",
}

# Пункты чек-листа про наполнение: запланированный курс публикуют пустым,
# чтобы собирать заявки до готовности (DESIGN_BRIEF, раздел 9).


def lesson_ready(lesson: Lesson) -> bool:
    """Бейдж «готов» у урока: у видеоурока есть ссылка, у текстового — непустая
    разметка. Нужен и редактору урока — признак там тот же самый."""
    if lesson.kind == "video":
        return bool(lesson.video_url)
    return not is_blank_html((lesson.body or {}).get("html", ""))


def task_ready(task: Task) -> bool:
    """Задание готово, когда написано условие: без него человеку нечего делать."""
    return not is_blank_html((task.statement or {}).get("html", ""))


def program_item(item: dict, *, is_hidden: bool, is_ready: bool, has_data: bool) -> dict:
    """Элемент дерева программы глазами админа.

    К полям, которые видит учитель, добавляются три признака вместо статуса
    прохождения: скрыт, готов, есть чужие данные. Общий на редакторы курса,
    урока, теста и задания — форма элемента в контракте одна.

    `is_hidden` приходит настоящий у всех трёх видов: админ должен видеть,
    что он спрятал, иначе скрытое нечем достать обратно.
    """
    return {**item, "is_hidden": is_hidden, "is_ready": is_ready, "has_data": has_data}


def program_minutes(program: list[dict]) -> int:
    """«Всего по программе» внизу дерева: сумма по видимым элементам —
    скрытый элемент не занимает времени ни у кого."""
    return sum(
        item["time_required_min"]
        for module in program
        for item in module["items"]
        if not item["is_hidden"]
    )


def _checked_title(raw: str) -> str:
    title = raw.strip()
    if not title:
        raise FieldError("title", "Без названия курс не сохранить")
    return title


def _version_conflict(lang: str, existing: Course) -> VersionExistsError:
    """409 с id уже существующей версии: на неё уводит переключатель РУС|ҚАЗ."""
    return VersionExistsError(f"{LANG_VERSIONS[lang]} у этого курса уже есть", existing.id)


def _readiness(course: Course, program: list[dict]) -> dict:
    """Чек-лист вкладки «Публикация»: `code` — для ветвления, `text` — готовая
    строка, `items` — названия, которых не хватает, `blocking` — закрывает ли
    невыполненный пункт кнопку «Открыть набор»."""
    all_items = [item for module in program for item in module["items"]]
    items = [item for item in all_items if not item["is_hidden"]]
    hidden = len(all_items) - len(items)
    empty_lessons = [
        item["title"] for item in items if item["kind"] in LESSON_KINDS and not item["is_ready"]
    ]
    empty_quizzes = [
        item["title"] for item in items if item["kind"] == "quiz" and not item["is_ready"]
    ]
    # Пока видимых элементов нет, проверять было нечего, и «нарушений не
    # нашлось» читается как «проверено и хорошо» — это враньё: заготовка
    # элемента заводится скрытой, и у собранного курса такое бывает.
    if empty_lessons:
        lessons_text = (
            f"У {len(empty_lessons)} "
            f"{plural(len(empty_lessons), 'урока', 'уроков', 'уроков')} нет содержимого"
        )
    elif items:
        lessons_text = "Уроков без содержимого нет"
    else:
        lessons_text = "Уроки не проверялись — в программе нет видимых элементов"
    if empty_quizzes:
        quizzes_text = (
            f"В {len(empty_quizzes)} "
            f"{plural(len(empty_quizzes), 'тесте', 'тестах', 'тестах')} нет вопросов"
        )
    elif items:
        quizzes_text = "Во всех тестах есть вопросы"
    else:
        quizzes_text = "Тесты не проверялись — в программе нет видимых элементов"
    if items:
        program_text = (
            f"В программе {len(items)} "
            f"{plural(len(items), 'элемент', 'элемента', 'элементов')}"
        )
    elif hidden:
        # Шесть элементов на соседней вкладке и «нет ни одного» здесь — админ
        # решит, что программа потерялась, а она вся скрыта.
        program_text = f"В программе нет ни одного видимого элемента: скрыто {hidden}"
    else:
        program_text = "В программе нет ни одного элемента"
    # Обложка есть — загруженная картинка или доставшийся от прежних времён
    # адрес-ссылка: в каталоге и та и другая рисуются одинаково
    has_cover = bool(course.cover_key or course.cover)
    checks = [
        {
            "code": "cover",
            "ok": has_cover,
            # Единственный пункт, который набор не держит: без картинки курс
            # в каталоге выглядит бедно, но людей в него пускать это не мешает
            "blocking": False,
            "text": (
                "Обложка загружена"
                if has_cover
                else "Обложки нет — в каталоге курс будет без картинки"
            ),
            "items": [],
        },
        {
            "code": "hours",
            "ok": course.hours >= 1,
            "blocking": True,
            "text": "Объём курса указан" if course.hours >= 1 else "Объём курса не указан",
            "items": [],
        },
        {**_starts_at_check(course), "blocking": True, "items": []},
        {
            "code": "empty_lessons",
            "ok": not empty_lessons,
            "blocking": True,
            "text": lessons_text,
            "items": empty_lessons,
        },
        {
            "code": "empty_quizzes",
            "ok": not empty_quizzes,
            "blocking": True,
            "text": quizzes_text,
            "items": empty_quizzes,
        },
        {
            "code": "program",
            "ok": bool(items),
            "blocking": True,
            "text": program_text,
            "items": [],
        },
    ]
    # Два флага — две кнопки вкладки «Публикация», и требования у них разные.
    # Открыть набор можно только готовому курсу, но считается это по одним
    # блокирующим пунктам: неблокирующий остаётся предупреждением в чек-листе
    # и кнопку не держит. Запланированный публикуют пустым, ради того он
    # и нужен — собирать заявки до того, как курс готов (DESIGN_BRIEF,
    # раздел 9). Поэтому у второго условие одно: дата старта, именно она
    # рисует бейдж «Старт 1 сентября».
    return {
        "can_open": all(check["ok"] for check in checks if check["blocking"]),
        "can_plan": course.starts_at is not None,
        "items": checks,
    }


def _check_tree(
    known_modules: set[int],
    known_items: set[tuple[str, int]],
    sent_modules: list[int],
    sent_items: list[tuple[str, int]],
) -> None:
    """Дерево приходит целиком, иначе не меняется ничего.

    Правило простое: пока экран не знал о новом уроке, перетаскивание не
    должно этот урок потерять.
    """
    if len(set(sent_modules)) != len(sent_modules) or len(set(sent_items)) != len(sent_items):
        raise FieldError("modules", "В дереве есть повторы")
    foreign = len(set(sent_modules) - known_modules) + len(set(sent_items) - known_items)
    if foreign:
        raise FieldError("modules", f"В дереве есть чужие элементы: {foreign}")
    missing_modules = len(known_modules - set(sent_modules))
    if missing_modules:
        raise FieldError("modules", f"В дереве не хватает модулей курса: {missing_modules}")
    missing_items = len(known_items - set(sent_items))
    if missing_items:
        raise FieldError("modules", f"В дереве не хватает элементов курса: {missing_items}")


def _starts_at_check(course: Course) -> dict:
    """Дата старта обязательна ровно у запланированного курса: именно она
    рисует бейдж «Старт 1 сентября»."""
    if course.status != "planned":
        return {
            "code": "starts_at",
            "ok": True,
            "text": "Дата старта не нужна — курс не запланирован",
        }
    if course.starts_at is not None:
        return {"code": "starts_at", "ok": True, "text": "Дата старта указана"}
    return {
        "code": "starts_at",
        "ok": False,
        "text": "У запланированного курса дата старта обязательна",
    }


class CoursesAdminService:
    def __init__(
        self,
        courses: CourseAdminRepo,
        categories: CategoryRepo,
        storage: StoragePort,
        cfg: Settings,
    ):
        self.courses = courses
        # Категории с сессии 7б живут в таблице: справочник правит админ,
        # и сверять category_id больше не с чем, кроме неё
        self.categories = categories
        # Хранилище нужно обложке: что объект есть и что это картинка, сервер
        # берёт у него, а не у браузера
        self.storage = storage
        self.cfg = cfg

    # -- GET /admin/courses ---------------------------------------------

    def admin_list(
        self,
        *,
        status: str | None,
        lang: str | None,
        q: str | None,
        offset: int,
        limit: int,
    ) -> dict:
        rows, total = self.courses.page(
            status=status, lang=lang, q=q, offset=offset, limit=limit
        )
        if not rows:
            return {"items": [], "total": total}
        ids = [course.id for course in rows]
        modules = self.courses.modules_count(ids)
        lessons = self.courses.lessons_count(ids)
        leads = self.courses.open_leads_count(ids)
        enrollments = self.courses.enrollment_counts(ids)
        versions = self.courses.group_versions([course.group_id for course in rows])
        items = []
        for course in rows:
            students, completed = enrollments.get(course.id, (0, 0))
            items.append(
                {
                    "id": course.id,
                    "group_id": course.group_id,
                    "lang": course.lang,
                    "title": course.title,
                    "cover": cover_url(course, self.cfg.public_base_url),
                    "category_id": course.category_id,
                    "hours": course.hours,
                    "price": course.price,
                    "status": course.status,
                    "starts_at": course.starts_at,
                    "modules_count": modules.get(course.id, 0),
                    "lessons_count": lessons.get(course.id, 0),
                    "open_leads_count": leads.get(course.id, 0),
                    "students_count": students,
                    "completed_count": completed,
                    "updated_at": course.updated_at,
                    "versions": self._versions_out(course, versions),
                }
            )
        return {"items": items, "total": total}

    # -- POST /admin/courses ---------------------------------------------

    def create(self, fields: dict) -> dict:
        title = _checked_title(fields["title"])
        self._check_category(fields["category_id"])
        # Своя группа: вторая языковая версия заводится отдельной кнопкой
        course = self.courses.create(
            group_id=self.courses.next_group_id(),
            lang=fields["lang"],
            title=title,
            category_id=fields["category_id"],
            hours=fields["hours"],
        )
        return self._card_out(course)

    # -- GET /admin/courses/{id} -----------------------------------------

    def card(self, course_id: int) -> dict:
        return self._card_out(self._found(course_id))

    # -- PATCH /admin/courses/{id} ---------------------------------------

    def patch(self, course_id: int, fields: dict) -> dict:
        course = self._found(course_id)
        if fields.get("title") is not None:
            fields = {**fields, "title": _checked_title(fields["title"])}
        if "category_id" in fields and fields["category_id"] is not None:
            self._check_category(fields["category_id"])
        for field in PLAIN_FIELDS:
            if fields.get(field) is not None:
                setattr(course, field, fields[field])
        for field in NULLABLE_FIELDS:
            if field in fields:
                setattr(course, field, fields[field])
        # null — снять обложку; в остальном это key и name из ответа POST /files
        if "cover" in fields:
            course.cover_key, course.cover_name = self._cover(fields["cover"])
            # Старый адрес-ссылка гаснет вместе с этим: иначе «снять обложку»
            # у курса, заведённого до сессии 7в, не убирало бы ничего, а новая
            # картинка ждала бы своей очереди за прежней ссылкой
            course.cover = None
        # Проверяется то, что получилось, а не то, что прислали: публикация
        # одним запросом вместе с недостающим полем проходит
        if course.status in PUBLIC_STATUSES:
            self._check_publishable(course)
        self.courses.touch(course.id)
        return self._card_out(course)

    # -- DELETE /admin/courses/{id} --------------------------------------

    def delete(self, course_id: int) -> None:
        course = self._found(course_id)
        usage = self.courses.usage_counts(course.id)
        if any(usage.values()):
            raise CourseInUseError(**usage)
        self.courses.delete_course(course.id)

    # -- POST /admin/courses/{id}/versions -------------------------------

    def add_version(self, course_id: int, lang: str, copy_program: bool) -> dict:
        source = self._found(course_id)
        if lang == source.lang:
            raise FieldError("lang", "Это язык самого курса — выберите другой")
        existing = self.courses.version_in_group(source.group_id, lang)
        if existing is not None:
            raise _version_conflict(lang, existing)
        version = self.courses.create_version(
            group_id=source.group_id, lang=lang, **self._copied_fields(source)
        )
        if version is None:
            # Проверка выше и вставка не атомарны: соседний запрос успевает
            # между ними. Данные спасает уникальный индекс (group_id, lang),
            # а наружу обязано уйти то же 409 с id чужой версии
            raise _version_conflict(lang, self.courses.version_in_group(source.group_id, lang))
        if copy_program:
            self.courses.copy_program(source.id, version.id)
        return self._card_out(version)

    # -- POST /admin/courses/{id}/duplicate ------------------------------

    def duplicate(self, course_id: int) -> dict:
        source = self._found(course_id)
        # Новая группа, а не второй язык: это другой курс
        copy = self.courses.create(
            group_id=self.courses.next_group_id(),
            lang=source.lang,
            **{**self._copied_fields(source), "title": f"{source.title} (копия)"},
        )
        self.courses.copy_program(source.id, copy.id)
        return self._card_out(copy)

    # -- POST /admin/courses/{id}/modules --------------------------------

    def add_module(self, course_id: int, title: str) -> dict:
        course = self._found(course_id)
        title = title.strip()
        if not title:
            raise FieldError("title", "Без названия модуль не сохранить")
        module = self.courses.create_module(course.id, title)
        self.courses.touch(course.id)
        return {"id": module.id, "title": module.title, "items": []}

    # -- PATCH /admin/modules/{id} ---------------------------------------

    def rename_module(self, module_id: int, title: str) -> dict:
        module = self._found_module(module_id)
        title = title.strip()
        if not title:
            raise FieldError("title", "Без названия модуль не сохранить")
        module.title = title
        self.courses.touch(module.course_id)
        return self._module_out(module)

    # -- DELETE /admin/modules/{id} --------------------------------------

    def delete_module(self, module_id: int) -> None:
        module = self._found_module(module_id)
        blockers = [
            {
                "kind": item["kind"],
                "id": item["id"],
                "title": item["title"],
                "reason": BLOCK_REASONS[item["kind"]],
            }
            for item in self._module_out(module)["items"]
            if item["has_data"]
        ]
        if blockers:
            raise ModuleInUseError(blockers)
        self.courses.delete_modules([module.id])
        self.courses.touch(module.course_id)

    # -- PUT /admin/courses/{id}/program_order ---------------------------

    def reorder(self, course_id: int, modules: list[dict]) -> dict:
        course = self._found(course_id)
        known_modules = {module.id: module for module in self.courses.modules(course.id)}
        known_items: dict[tuple[str, int], Lesson | Quiz | Task] = {
            ("lesson", lesson.id): lesson for lesson in self.courses.lessons(course.id)
        }
        known_items.update({("quiz", quiz.id): quiz for quiz in self.courses.quizzes(course.id)})
        known_items.update({("task", task.id): task for task in self.courses.tasks(course.id)})

        sent_modules = [module["id"] for module in modules]
        sent_items = [item_key(item) for module in modules for item in module["items"]]
        _check_tree(set(known_modules), set(known_items), sent_modules, sent_items)

        for position, sent in enumerate(modules):
            module = known_modules[sent["id"]]
            module.order_index = position
            for index, sent_item in enumerate(sent["items"]):
                # Элемент, оказавшийся в чужом items, переезжает в этот модуль
                row = known_items[item_key(sent_item)]
                row.module_id = module.id
                row.order_index = index
        self.courses.touch(course.id)
        program = self._program(course.id)
        return {"program": program, "program_minutes": program_minutes(program)}

    # -- проверки ---------------------------------------------------------

    def _cover(self, value: dict | None) -> tuple[str | None, str | None]:
        """Ключ объекта в хранилище и имя файла; null убирает обложку.

        Проверок две, и порядок у них тот же, что у картинок настроек: сперва
        объект в хранилище есть (иначе админ увидит пустой блок вместо только
        что загруженного файла), и только потом — что это картинка. Формат
        опознаётся по байтам объекта, а не по присланному имени: имя сочиняет
        клиент, и договор, названный «обложка.jpg», получил бы публичный адрес
        раздачи.

        Размеры в пикселях не проверяются: «16:9, минимум 640×360» из брифа —
        подсказка человеку на экране. Измерить их нечем, кроме библиотеки
        картинок, а её здесь нет.

        Имя хранится рядом с ключом: ключи POST /files случайные нарочно,
        и админу досталось бы «9f3c1a7e4b2d8c05.jpg» вместо «Обложка.jpg».
        Байты при снятии обложки остаются: порт storage умеет писать, читать
        и мерить, но не удалять.
        """
        if value is None:
            return None, None
        if self.storage.size(value["key"]) is None:
            raise NotFoundError("Загруженный файл не найден — загрузите его заново")
        # Начала объекта хватает на любую сигнатуру: дальше смотреть нечего
        head = next(iter(self.storage.read(value["key"])), b"")[:HEAD_SIZE]
        if not (image_mime(head) or "").startswith("image/"):
            raise FieldError("cover", NOT_AN_IMAGE)
        return value["key"], safe_name(value["name"])

    def _check_category(self, category_id: int) -> None:
        """Категория берётся из таблицы, а не из константы: справочник правит
        админ, и удалить категорию, на которой висит курс, ему уже не дадут."""
        if self.categories.by_id(category_id) is None:
            raise FieldError("category_id", "Такой категории нет")

    def _check_publishable(self, course: Course) -> None:
        """Минимум, без которого курс нельзя показывать людям. Наполненность
        программы не проверяется нигде: запланированный курс для того и публикуют
        пустым."""
        if not course.title.strip():
            raise FieldError("title", "Без названия курс не опубликовать")
        if course.hours < 1:
            raise FieldError("hours", "Объём курса в часах обязателен")
        self._check_category(course.category_id)
        if course.status == "planned" and course.starts_at is None:
            raise FieldError("starts_at", "У запланированного курса дата старта обязательна")

    # -- сборка ответа ---------------------------------------------------

    def _found(self, course_id: int) -> Course:
        course = self.courses.by_id(course_id)
        if course is None:
            raise NotFoundError("Курс не найден")
        return course

    def _found_module(self, module_id: int) -> Module:
        module = self.courses.module_by_id(module_id)
        if module is None:
            raise NotFoundError("Модуль не найден")
        return module

    @staticmethod
    def _copied_fields(source: Course) -> dict:
        """Поля, переезжающие в языковую версию и в дубликат. Новая строка
        всегда черновик: показывать её людям решают отдельно."""
        return {field: getattr(source, field) for field in COPIED_FIELDS} | {
            "status": "draft",
            "price": None,
            "starts_at": None,
        }

    @staticmethod
    def _versions_out(course: Course, versions: dict[int, list[Course]]) -> list[dict]:
        """Соседние версии той же группы; себя в список не включаем — на себя
        переключаться некуда."""
        return [
            {"id": other.id, "lang": other.lang, "status": other.status}
            for other in versions.get(course.group_id, [])
            if other.id != course.id
        ]

    def _card_out(self, course: Course) -> dict:
        program = self._program(course.id)
        students, _ = self.courses.enrollment_counts([course.id]).get(course.id, (0, 0))
        return {
            "id": course.id,
            "group_id": course.group_id,
            "lang": course.lang,
            "title": course.title,
            "short": course.short,
            "full": course.full,
            "cover": cover_url(course, self.cfg.public_base_url),
            "category_id": course.category_id,
            "hours": course.hours,
            "duration_text": course.duration_text,
            "price": course.price,
            "status": course.status,
            "starts_at": course.starts_at,
            "strict_order": course.strict_order,
            "cert_require_lessons": course.cert_require_lessons,
            "cert_require_tasks": course.cert_require_tasks,
            "cert_require_module_quizzes": course.cert_require_module_quizzes,
            "cert_require_final_quiz": course.cert_require_final_quiz,
            "created_at": course.created_at,
            "updated_at": course.updated_at,
            # По нему экран решает, показывать ли предупреждение о правках
            # курса, где уже учатся
            "has_students": students > 0,
            "program_minutes": program_minutes(program),
            "versions": self._versions_out(
                course, self.courses.group_versions([course.group_id])
            ),
            "program": program,
            "readiness": _readiness(course, program),
        }

    def _module_out(self, module: Module) -> dict:
        """Модуль в форме элемента дерева: ответ переименования, он же —
        источник признаков `has_data` при удалении."""
        program = self._program(module.course_id)
        return next(item for item in program if item["id"] == module.id)

    def _program(self, course_id: int) -> list[dict]:
        """Дерево программы глазами админа: то же, что у учителя, но скрытые
        элементы на месте, а вместо статуса прохождения — три признака."""
        lessons = self.courses.lessons(course_id)
        quizzes = self.courses.quizzes(course_id)
        tasks = self.courses.tasks(course_id)
        questions = self.courses.questions_count([quiz.id for quiz in quizzes])
        progress = self.courses.progress_counts([lesson.id for lesson in lessons])
        attempts = self.courses.attempt_counts([quiz.id for quiz in quizzes])
        submissions = self.courses.submission_counts([task.id for task in tasks])

        by_module: dict[int, list[tuple[int, dict]]] = {}
        for lesson in lessons:
            by_module.setdefault(lesson.module_id, []).append(
                (
                    lesson.order_index,
                    program_item(
                        {
                            "kind": lesson.kind,
                            "id": lesson.id,
                            "title": lesson.title,
                            "time_required_min": lesson.time_required_min,
                            "duration_label": lesson.duration_label,
                        },
                        is_hidden=lesson.is_hidden,
                        is_ready=lesson_ready(lesson),
                        has_data=progress.get(lesson.id, 0) > 0,
                    ),
                )
            )
        for quiz in quizzes:
            by_module.setdefault(quiz.module_id, []).append(
                (
                    quiz.order_index,
                    program_item(
                        {
                            "kind": "quiz",
                            "id": quiz.id,
                            "title": quiz.title,
                            "time_required_min": quiz.time_required_min,
                            "questions_count": questions.get(quiz.id, 0),
                            "time_limit_min": quiz.time_limit_min,
                            "pass_score": quiz.pass_score,
                            "is_final": quiz.is_final,
                        },
                        is_hidden=quiz.is_hidden,
                        is_ready=questions.get(quiz.id, 0) > 0,
                        has_data=attempts.get(quiz.id, 0) > 0,
                    ),
                )
            )
        for task in tasks:
            by_module.setdefault(task.module_id, []).append(
                (
                    task.order_index,
                    program_item(
                        {
                            "kind": "task",
                            "id": task.id,
                            "title": task.title,
                            "time_required_min": task.time_required_min,
                            "submit_format": task.submit_format,
                        },
                        is_hidden=task.is_hidden,
                        is_ready=task_ready(task),
                        has_data=submissions.get(task.id, 0) > 0,
                    ),
                )
            )
        return [
            {
                "id": module.id,
                "title": module.title,
                "items": [
                    item
                    for _, item in sorted(by_module.get(module.id, []), key=lambda pair: pair[0])
                ],
            }
            for module in self.courses.modules(course_id)
        ]
