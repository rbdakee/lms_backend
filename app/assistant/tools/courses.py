"""Курсы: список, карточка, программа, удаление.

**Это образец, на который смотрят остальные файлы `tools/*.py`.** Здесь
собраны все приёмы, обязательные для каждого инструмента:

1. **Инструменты растут из сценариев `app/application/*`, а не из HTTP.**
   Бэкенд не стучится сам в себя: тот же сервис, что зовёт роутер, зовёт
   и ассистент. Адаптеры при этом выбираются там же, где для роутеров, —
   в `app/api/deps.py`: «выбор адаптера через конфигурацию, а не `if`
   в месте вызова» (`backend/CLAUDE.md`), и второй такой выбор рядом
   разъехался бы с первым.
2. **Площадка — аргумент, а не `Origin`.** Админка одна на обе площадки
   (`ASSISTANT_BRIEF`, решение 11), тот же приём уже применён
   в `app/api/routers/admin_teachers.py`. Аргумента нет — считаются обе,
   и в ответе это видно числами: «12 заявок» без площадки — брак.
3. **Описание — для модели, а не для человека:** когда применять и чем
   отличается от соседнего инструмента. «Список курсов» — бесполезное
   описание.
4. **Опасное — `confirm` с `@preview`.** Инструмент не исполняется, а даёт
   предложение; текст последствий считается по данным и с числами.
5. **Списки короткие.** Модель платит за каждый токен ответа, а админу
   нужен вывод, а не двести строк: список режется потолком и честно
   говорит, что срезан.
"""

from dataclasses import replace
from datetime import date

from app.adapters.db.repos import CategoryRepo, CourseAdminRepo, CoursePlatformRepo
from app.api import deps
from app.application.courses_admin import PUBLIC_STATUSES, CoursesAdminService
from app.assistant.registry import Arg, ToolCtx, ToolError, platform_arg, preview, tool
from app.config import get_settings
from app.domain.platform import PLATFORMS
from app.domain.plural import plural
from app.domain.program import LESSON_KINDS

# Потолок строк в ответе инструмента. Не постраничность: ассистенту нужен
# вывод, а не страница — за второй он сходит с более узким фильтром.
LIST_LIMIT = 50

# Сколько строк читается ради чисел по площадкам. Показываем LIST_LIMIT,
# а считаем по всем: «12 курсов, 9 на p1» обязано быть правдой и тогда,
# когда список срезан.
COUNT_LIMIT = 1000

COURSE_STATUSES = ["draft", "planned", "open", "closed", "hidden"]


def courses_service(ctx: ToolCtx) -> CoursesAdminService:
    """Тот же сервис, что стоит за `/admin/courses`.

    Хранилище берётся у `deps`, а не собирается заново: выбор между диском
    и бакетом делает конфигурация, и второй такой выбор здесь означал бы
    ассистента, который в бою читает не тот адаптер, что админка.
    """
    return CoursesAdminService(
        courses=CourseAdminRepo(ctx.db),
        platforms=CoursePlatformRepo(ctx.db),
        categories=CategoryRepo(ctx.db),
        storage=deps.get_storage(),
        cfg=get_settings(),
    )


@tool(
    name="courses_list",
    danger="read",
    description=(
        "Список курсов: название, язык, статус, площадки с ценами, сколько"
        " модулей и уроков, сколько учеников и открытых заявок. Отсюда берут"
        " course_id для всех остальных инструментов. Содержание курса —"
        " модули, уроки, тесты — сюда не входит, для него есть course_program."
    ),
    args={
        "status": Arg(
            "string",
            "Статус курса. draft — черновик, planned — запланирован,"
            " open — идёт набор, closed — набор закрыт, hidden — скрыт.",
            enum=COURSE_STATUSES,
        ),
        "lang": Arg("string", "Язык курса", enum=["ru", "kz"]),
        "platform": platform_arg(
            "Показать только курсы, выложенные на этой площадке. Не задан —"
            " все курсы, и в ответе видно, сколько их на каждой площадке."
        ),
        "q": Arg("string", "Поиск по названию"),
    },
)
def courses_list(
    ctx: ToolCtx,
    status: str | None = None,
    lang: str | None = None,
    platform: str | None = None,
    q: str | None = None,
) -> dict:
    # Берём с запасом, а режем потом: числа по площадкам обязаны считаться
    # по всем подходящим курсам, а не по показанной странице — иначе срезанный
    # список молча соврёт про «сколько их на p2». Курсов в системе десятки,
    # и второй выборки ради счёта здесь не нужно.
    data = courses_service(ctx).admin_list(
        status=status, lang=lang, q=q, offset=0, limit=COUNT_LIMIT
    )
    items = data["items"]
    if platform:
        items = [item for item in items if _on(item, platform)]
    # Числа по площадкам считаются всегда, даже когда фильтра не было:
    # без них «12 курсов» — неверный ответ (ASSISTANT_BRIEF, раздел 4)
    by_platform = {code: sum(1 for item in items if _on(item, code)) for code in PLATFORMS}
    cut = items[:LIST_LIMIT]
    return {
        "total": len(items),
        "by_platform": by_platform,
        "shown": len(cut),
        "truncated": len(items) > len(cut),
        "items": [_course_row(item) for item in cut],
    }


@tool(
    name="course_card",
    danger="read",
    description=(
        "Карточка одного курса: описания, объём в часах, категория, статус,"
        " площадки с ценами, условия сертификата и признак «по нему уже"
        " учатся». Программы здесь нет — она в course_program."
    ),
    args={"course_id": Arg("integer", "Идентификатор курса", required=True)},
)
def course_card(ctx: ToolCtx, course_id: int) -> dict:
    card = courses_service(ctx).card(course_id)
    return {
        field: card[field]
        for field in (
            "id", "lang", "title", "short", "full", "category_id", "hours",
            "duration_text", "platforms", "status", "starts_at", "strict_order",
            "cert_require_lessons", "cert_require_tasks",
            "cert_require_module_quizzes", "cert_require_final_quiz",
            "has_students", "program_minutes", "versions", "readiness",
        )
    }


@tool(
    name="course_program",
    danger="read",
    description=(
        "Содержание курса по порядку: модули, а в них уроки, тесты и задания"
        " с их идентификаторами. Отсюда берут lesson_id, quiz_id и task_id."
        " Текстов уроков и вопросов теста здесь нет — за ними идут в lesson_card"
        " и quiz_card."
    ),
    args={"course_id": Arg("integer", "Идентификатор курса", required=True)},
)
def course_program(ctx: ToolCtx, course_id: int) -> dict:
    card = courses_service(ctx).card(course_id)
    return {
        "course_id": card["id"],
        "title": card["title"],
        "program_minutes": card["program_minutes"],
        "modules": card["program"],
    }


@tool(
    name="course_delete",
    danger="confirm",
    description=(
        "Удалить курс целиком. Курс, по которому есть доступы, заявки или"
        " сертификаты, не удаляется вовсе — предложи снять его с публикации"
        " вместо удаления."
    ),
    args={"course_id": Arg("integer", "Идентификатор курса", required=True)},
)
def course_delete(ctx: ToolCtx, course_id: int) -> dict:
    courses_service(ctx).delete(course_id)
    return {"course_id": course_id, "deleted": True}


@preview("course_delete")
def course_delete_preview(ctx: ToolCtx, course_id: int) -> str:
    """Последствия с числами и с площадками — то, что админ читает над кнопкой.

    Считается по данным, а не по названию инструмента: «удалить курс» ничего
    не говорит, а «удалится курс „Оценивание“ с 4 модулями и 12 уроками,
    выложенный на p1 и p2» — говорит.
    """
    service = courses_service(ctx)
    card = service.card(course_id)
    usage = CourseAdminRepo(ctx.db).usage_counts(course_id)
    where = ", ".join(row["platform"] for row in card["platforms"]) or "нигде"
    lessons = sum(len(module["items"]) for module in card["program"])
    modules = len(card["program"])
    text = (
        f"Удалит курс #{course_id} «{card['title']}» ({card['lang']}):"
        f" {modules} {plural(modules, 'модуль', 'модуля', 'модулей')},"
        f" {lessons} {plural(lessons, 'элемент', 'элемента', 'элементов')} программы."
        f" Выложен: {where}."
    )
    if any(usage.values()):
        text += (
            f" Не удалится: доступов {usage['enrollments']}, заявок {usage['leads']},"
            f" сертификатов {usage['certificates']}."
        )
    return text


def _on(item: dict, platform: str) -> bool:
    return any(row["platform"] == platform for row in item["platforms"])


def _course_row(item: dict) -> dict:
    """Строка списка. Обложки и версий здесь нет: модель на них не смотрит,
    а токены они стоят."""
    return {
        field: item[field]
        for field in (
            "id", "lang", "title", "status", "platforms", "hours",
            "modules_count", "lessons_count", "open_leads_count",
            "students_count", "completed_count",
        )
    }


# -- заведение и правка курса --------------------------------------------

# Слова статуса для карточки подтверждения: «open → hidden» админ читает
# медленнее, чем «идёт набор → скрыт», а решает он по этой строке.
STATUS_WORDS = {
    "draft": "черновик",
    "planned": "запланирован",
    "open": "идёт набор",
    "closed": "набор закрыт",
    "hidden": "скрыт",
}


def required_platform(description: str) -> Arg:
    """Площадка, без которой вызов не имеет смысла: у публикации и цены «обе
    сразу» не бывает — это два бренда с разными ценами. Берётся у того же
    `platform_arg`, чтобы список кодов не разошёлся с остальными файлами."""
    return replace(platform_arg(description), required=True)


@tool(
    name="course_create",
    danger="write",
    description=(
        "Завести новый курс. Заводится черновиком, без программы и без единой"
        " площадки: модули добавляет module_add, статус меняет course_status,"
        " площадку с ценой — course_publish. Вторая языковая версия"
        " существующего курса заводится не здесь, а в course_add_version:"
        " курс одноязычный, и русская с казахской — два разных курса."
    ),
    args={
        "title": Arg("string", "Название курса", required=True),
        "lang": Arg("string", "Язык курса", enum=["ru", "kz"], required=True),
        "category_id": Arg(
            "integer", "Идентификатор категории из справочника категорий", required=True
        ),
        "hours": Arg("integer", "Объём курса в часах, от 1 до 999", required=True),
    },
)
def course_create(ctx: ToolCtx, title: str, lang: str, category_id: int, hours: int) -> dict:
    card = courses_service(ctx).create(
        {
            "title": title,
            "lang": lang,
            "category_id": category_id,
            "hours": _ranged("hours", hours, 1, 999),
        }
    )
    return _saved(card, ["title", "lang", "category_id", "hours"])


@tool(
    name="course_update",
    danger="write",
    description=(
        "Правка полей курса: название, короткое и полное описание, категория,"
        " объём в часах, длительность словами, строгий порядок прохождения"
        " и условия сертификата. Курс общий для обеих площадок — правка видна"
        " и на p1, и на p2, где он выложен. Статуса и цены здесь нет:"
        " они в course_status и course_publish."
    ),
    args={
        "course_id": Arg("integer", "Идентификатор курса", required=True),
        "title": Arg("string", "Название курса"),
        "short": Arg("string", "Короткое описание — строка под названием в каталоге"),
        "full": Arg("string", "Полное описание для страницы курса, размеченный текст"),
        "category_id": Arg("integer", "Категория из справочника"),
        "hours": Arg("integer", "Объём курса в часах, от 1 до 999"),
        "duration_text": Arg("string", "Длительность словами, например «6 недель»"),
        "strict_order": Arg(
            "boolean", "Строгий порядок: следующий элемент открывается после предыдущего"
        ),
        "cert_require_lessons": Arg("boolean", "Условие сертификата: пройти все уроки"),
        "cert_require_tasks": Arg("boolean", "Условие сертификата: сдать все задания"),
        "cert_require_module_quizzes": Arg(
            "boolean", "Условие сертификата: сдать тесты модулей"
        ),
        "cert_require_final_quiz": Arg("boolean", "Условие сертификата: сдать итоговый тест"),
    },
)
def course_update(
    ctx: ToolCtx,
    course_id: int,
    title: str | None = None,
    short: str | None = None,
    full: str | None = None,
    category_id: int | None = None,
    hours: int | None = None,
    duration_text: str | None = None,
    strict_order: bool | None = None,
    cert_require_lessons: bool | None = None,
    cert_require_tasks: bool | None = None,
    cert_require_module_quizzes: bool | None = None,
    cert_require_final_quiz: bool | None = None,
) -> dict:
    fields = {
        "title": title,
        "short": short,
        "full": full,
        "category_id": category_id,
        "hours": None if hours is None else _ranged("hours", hours, 1, 999),
        "duration_text": duration_text,
        "strict_order": strict_order,
        "cert_require_lessons": cert_require_lessons,
        "cert_require_tasks": cert_require_tasks,
        "cert_require_module_quizzes": cert_require_module_quizzes,
        "cert_require_final_quiz": cert_require_final_quiz,
    }
    fields = {name: value for name, value in fields.items() if value is not None}
    if not fields:
        raise ToolError("nothing_to_change", "Не задано ни одного поля — менять нечего")
    return _saved(courses_service(ctx).patch(course_id, fields), sorted(fields))


@tool(
    name="course_status",
    danger="confirm",
    description=(
        "Сменить статус курса: draft — черновик, planned — запланирован,"
        " open — идёт набор, closed — набор закрыт, hidden — скрыт. Это и есть"
        " публикация, отдельной кнопки «опубликовать» нет. Статус общий"
        " на обе площадки: скрытый курс уходит из каталога и p1, и p2 сразу."
        " Где именно курс выложен и почём — course_publish."
    ),
    args={
        "course_id": Arg("integer", "Идентификатор курса", required=True),
        "status": Arg("string", "Новый статус курса", enum=COURSE_STATUSES, required=True),
        "starts_at": Arg(
            "string", "Дата старта, ГГГГ-ММ-ДД. У запланированного курса обязательна."
        ),
    },
)
def course_status(
    ctx: ToolCtx, course_id: int, status: str, starts_at: str | None = None
) -> dict:
    fields: dict = {"status": status}
    if starts_at is not None:
        fields["starts_at"] = _date(starts_at)
    return _saved(courses_service(ctx).patch(course_id, fields), sorted(fields))


@preview("course_status")
def course_status_preview(
    ctx: ToolCtx, course_id: int, status: str, starts_at: str | None = None
) -> str:
    card = courses_service(ctx).card(course_id)
    where = _platforms_text(card)
    text = (
        f"Курс #{course_id} «{card['title']}»: {STATUS_WORDS[card['status']]}"
        f" → {STATUS_WORDS[status]}. Выложен: {where}."
    )
    was, becomes = card["status"] in PUBLIC_STATUSES, status in PUBLIC_STATUSES
    if was and not becomes:
        text += f" Карточка исчезнет из каталога: {where}."
    if becomes and not was:
        text += f" Карточка появится в каталоге: {where}."
    # Отказ по дате видно до кнопки, а не после: у запланированного курса
    # дата старта рисует бейдж «Старт 1 сентября» и потому обязательна
    if status == "planned" and starts_at is None and card["starts_at"] is None:
        text += " Дата старта не задана — без неё запланированный курс не сохранится."
    return text + _students_text(ctx, course_id)


@tool(
    name="course_publish",
    danger="confirm",
    description=(
        "Выложить курс на одной площадке и назначить там цену; если он на ней"
        " уже выложен — поменять цену. Публикация и цена свои у каждой площадки:"
        " p1 и p2 — разные бренды с разными ценами, и вызов трогает только"
        " названную. Выложить на обеих — два отдельных вызова. Убрать курс"
        " из каталога площадки — course_unpublish, сам статус — course_status."
    ),
    args={
        "course_id": Arg("integer", "Идентификатор курса", required=True),
        "platform": required_platform("Площадка, на которой курс выкладывается"),
        "price": Arg(
            "integer",
            "Цена в тенге. Не задана — у выложенного курса цена остаётся прежней,"
            " у нового будет «цена по запросу».",
        ),
    },
)
def course_publish(
    ctx: ToolCtx, course_id: int, platform: str, price: int | None = None
) -> dict:
    service = courses_service(ctx)
    rows = _platform_prices(service.card(course_id))
    # Набор площадок заменяется целиком, поэтому соседняя площадка едет
    # обратно как есть: иначе публикация на p2 снимала бы курс с p1
    rows[platform] = rows.get(platform) if price is None else _ranged("price", price, 0, 10**9)
    card = service.patch(course_id, {"platforms": _platforms_in(rows)})
    return _saved(card, ["platforms"])


@preview("course_publish")
def course_publish_preview(
    ctx: ToolCtx, course_id: int, platform: str, price: int | None = None
) -> str:
    card = courses_service(ctx).card(course_id)
    rows = _platform_prices(card)
    now = _money(price) if price is not None else _money(rows.get(platform))
    if platform in rows:
        return (
            f"Курс #{course_id} «{card['title']}» уже выложен на {platform}:"
            f" цена {_money(rows[platform])} → {now}. Публикация на другой"
            f" площадке не меняется. Сейчас выложен: {_platforms_text(card)}."
        )
    return (
        f"Выложит курс #{course_id} «{card['title']}» на площадке {platform},"
        f" цена {now}: он появится в каталоге {platform}. Статус курса —"
        f" {STATUS_WORDS[card['status']]}. Сейчас выложен: {_platforms_text(card)}."
    )


@tool(
    name="course_unpublish",
    danger="confirm",
    description=(
        "Убрать курс из каталога одной площадки: строка публикации с ценой"
        " на ней стирается. Доступ у тех, кто уже учится, при этом не"
        " отбирается — они доучиваются и получают сертификат. Публикация"
        " на второй площадке не трогается."
    ),
    args={
        "course_id": Arg("integer", "Идентификатор курса", required=True),
        "platform": required_platform("Площадка, с которой курс снимается"),
    },
)
def course_unpublish(ctx: ToolCtx, course_id: int, platform: str) -> dict:
    service = courses_service(ctx)
    rows = _platform_prices(service.card(course_id))
    if platform not in rows:
        raise ToolError(
            "not_published", f"Курс #{course_id} на площадке {platform} не выложен"
        )
    del rows[platform]
    card = service.patch(course_id, {"platforms": _platforms_in(rows)})
    return _saved(card, ["platforms"])


@preview("course_unpublish")
def course_unpublish_preview(ctx: ToolCtx, course_id: int, platform: str) -> str:
    card = courses_service(ctx).card(course_id)
    rows = _platform_prices(card)
    if platform not in rows:
        return f"Курс #{course_id} «{card['title']}» на {platform} не выложен — снимать нечего."
    left = [code for code in rows if code != platform] or ["нигде"]
    return (
        f"Снимет курс #{course_id} «{card['title']}» с площадки {platform}:"
        f" цена {_money(rows[platform])} там сотрётся, из каталога {platform}"
        f" курс исчезнет. Останется: {', '.join(left)}."
        + _students_text(ctx, course_id)
        + " Доступ у них остаётся."
    )


@tool(
    name="course_add_version",
    danger="write",
    description=(
        "Завести вторую языковую версию курса: русскую у казахского или"
        " наоборот. Версия заводится отдельным курсом-черновиком, без площадок"
        " и без цены. Тексты в ней не переводятся сами: машинный перевод"
        " выкладывать нельзя, его вычитывает человек. Копия курса на том же"
        " языке — это не версия, а course_duplicate."
    ),
    args={
        "course_id": Arg("integer", "Идентификатор исходного курса", required=True),
        "lang": Arg("string", "Язык новой версии", enum=["ru", "kz"], required=True),
        "copy_program": Arg(
            "boolean", "Скопировать структуру программы заготовкой (по умолчанию нет)"
        ),
    },
)
def course_add_version(
    ctx: ToolCtx, course_id: int, lang: str, copy_program: bool = False
) -> dict:
    card = courses_service(ctx).add_version(course_id, lang, copy_program)
    return _saved(card, ["lang"]) | {"source_course_id": course_id}


@tool(
    name="course_duplicate",
    danger="write",
    description=(
        "Скопировать курс целиком вместе с программой: получается отдельный"
        " курс-черновик «… (копия)» на том же языке, без площадок и цены."
        " Нужна вторая языковая версия того же курса — course_add_version:"
        " она остаётся в той же языковой группе, а копия заводит свою."
    ),
    args={"course_id": Arg("integer", "Идентификатор курса-источника", required=True)},
)
def course_duplicate(ctx: ToolCtx, course_id: int) -> dict:
    card = courses_service(ctx).duplicate(course_id)
    return _saved(card, ["title"]) | {"source_course_id": course_id}


# -- модули и порядок программы ------------------------------------------


@tool(
    name="module_add",
    danger="write",
    description=(
        "Добавить модуль в конец программы курса. Модуль — только заголовок:"
        " уроки, тесты и задания кладут в него lesson_create, quiz_create"
        " и task_create, а порядок меняет program_move."
    ),
    args={
        "course_id": Arg("integer", "Идентификатор курса", required=True),
        "title": Arg("string", "Название модуля", required=True),
    },
)
def module_add(ctx: ToolCtx, course_id: int, title: str) -> dict:
    module = courses_service(ctx).add_module(course_id, title)
    return {"course_id": course_id, "module_id": module["id"], "title": module["title"]}


@tool(
    name="module_rename",
    danger="write",
    description=(
        "Переименовать модуль. Содержимое модуля не трогается — переносит"
        " уроки между модулями program_move."
    ),
    args={
        "module_id": Arg("integer", "Идентификатор модуля", required=True),
        "title": Arg("string", "Новое название модуля", required=True),
    },
)
def module_rename(ctx: ToolCtx, module_id: int, title: str) -> dict:
    module = courses_service(ctx).rename_module(module_id, title)
    return {"module_id": module["id"], "title": module["title"]}


@tool(
    name="module_delete",
    danger="confirm",
    description=(
        "Удалить модуль вместе со всеми уроками, тестами и заданиями в нём."
        " Модуль, в котором есть элемент с чужими данными — пройденный урок,"
        " тест с попытками, задание со сдачами, — не удаляется вовсе: такие"
        " элементы прячут через lesson_update, quiz_update и task_update."
    ),
    args={"module_id": Arg("integer", "Идентификатор модуля", required=True)},
)
def module_delete(ctx: ToolCtx, module_id: int) -> dict:
    courses_service(ctx).delete_module(module_id)
    return {"module_id": module_id, "deleted": True}


@preview("module_delete")
def module_delete_preview(ctx: ToolCtx, module_id: int) -> str:
    module = _module(ctx, module_id)
    card = courses_service(ctx).card(module.course_id)
    items = next(
        (row["items"] for row in card["program"] if row["id"] == module_id), []
    )
    kinds = {
        "уроков": sum(1 for item in items if item["kind"] in LESSON_KINDS),
        "тестов": sum(1 for item in items if item["kind"] == "quiz"),
        "заданий": sum(1 for item in items if item["kind"] == "task"),
    }
    what = ", ".join(f"{count} {name}" for name, count in kinds.items() if count)
    text = (
        f"Удалит модуль «{module.title}» курса #{module.course_id} «{card['title']}»"
        f" и всё, что в нём: {what or 'ни одного элемента'}."
        f" Курс выложен: {_platforms_text(card)}."
    )
    blocked = [item["title"] for item in items if item["has_data"]]
    if blocked:
        text += (
            f" Не удалится: по курсу уже учатся, и у {len(blocked)}"
            f" {plural(len(blocked), 'элемента', 'элементов', 'элементов')} есть"
            f" чужие данные ({', '.join(blocked[:3])})."
        )
    return text


@tool(
    name="program_move",
    danger="write",
    description=(
        "Переставить элемент программы: модуль на другое место в курсе либо"
        " урок, тест или задание внутри модуля или в соседний модуль."
        " Дерево программы курса сервер собирает сам — присылать его целиком"
        " не нужно. Идентификаторы берут в course_program."
    ),
    args={
        "course_id": Arg("integer", "Идентификатор курса", required=True),
        "kind": Arg(
            "string",
            "Что переставляем: module — модуль, lesson — урок любого вида,"
            " quiz — тест, task — задание",
            enum=["module", "lesson", "quiz", "task"],
            required=True,
        ),
        "item_id": Arg("integer", "Идентификатор модуля или элемента", required=True),
        "position": Arg("integer", "Новое место, считая с 1. Не задано — в конец"),
        "to_module_id": Arg(
            "integer", "Перенести элемент в этот модуль. Не задан — остаётся в своём"
        ),
    },
)
def program_move(
    ctx: ToolCtx,
    course_id: int,
    kind: str,
    item_id: int,
    position: int | None = None,
    to_module_id: int | None = None,
) -> dict:
    service = courses_service(ctx)
    card = service.card(course_id)
    # Сценарий принимает дерево целиком и отбивает неполное: экран шлёт его
    # из своего состояния, а у ассистента состояния нет — читаем перед правкой
    tree = [
        {
            "id": module["id"],
            "items": [{"kind": item["kind"], "id": item["id"]} for item in module["items"]],
        }
        for module in card["program"]
    ]
    if kind == "module":
        if to_module_id is not None:
            raise ToolError("bad_argument", "Модуль внутрь модуля не переносится")
        moved = next((row for row in tree if row["id"] == item_id), None)
        if moved is None:
            raise ToolError("not_found", f"В курсе #{course_id} нет модуля #{item_id}")
        tree.remove(moved)
        tree.insert(_slot(position, len(tree)), moved)
    else:
        found = _find_item(tree, kind, item_id)
        if found is None:
            raise ToolError(
                "not_found", f"В курсе #{course_id} нет элемента {kind} #{item_id}"
            )
        source, item = found
        target = source if to_module_id is None else _module_in(tree, to_module_id)
        if target is None:
            raise ToolError("not_found", f"В курсе #{course_id} нет модуля #{to_module_id}")
        source["items"].remove(item)
        target["items"].insert(_slot(position, len(target["items"])), item)
    data = service.reorder(course_id, tree)
    return {
        "course_id": course_id,
        "moved": {"kind": kind, "id": item_id},
        "program": [_module_row(module) for module in data["program"]],
    }


# -- сборка ответов и текстов --------------------------------------------


def _saved(card: dict, changed: list[str]) -> dict:
    """Что вернуть после правки: чем курс стал, а не карточка целиком —
    её ассистент возьмёт course_card, если она вдруг понадобится."""
    return {
        "course_id": card["id"],
        "title": card["title"],
        "lang": card["lang"],
        "status": card["status"],
        "platforms": card["platforms"],
        "has_students": card["has_students"],
        "changed": changed,
    }


def _module_row(module: dict) -> dict:
    return {
        "id": module["id"],
        "title": module["title"],
        "items": [
            {"kind": item["kind"], "id": item["id"], "title": item["title"]}
            for item in module["items"]
        ],
    }


def _platform_prices(card: dict) -> dict[str, int | None]:
    """Публикация курса как {площадка: цена}. Набор в сценарии заменяется
    целиком, поэтому правка одной площадки — это чтение всех и запись всех."""
    return {row["platform"]: row["price"] for row in card["platforms"]}


def _platforms_in(rows: dict[str, int | None]) -> list[dict]:
    return [{"platform": code, "price": rows[code]} for code in PLATFORMS if code in rows]


def _platforms_text(card: dict) -> str:
    """Где курс выложен и почём — строкой для карточки подтверждения:
    предложение опасного действия обязано называть площадку."""
    rows = _platform_prices(card)
    return ", ".join(f"{code} ({_money(price)})" for code, price in rows.items()) or "нигде"


def _students_text(ctx: ToolCtx, course_id: int) -> str:
    """«По курсу уже учатся» — то, из-за чего половина правок отбивается
    (BACKEND_NOTES, раздел 10). В предложении это идёт числом, а не намёком."""
    students, _ = CourseAdminRepo(ctx.db).enrollment_counts([course_id]).get(course_id, (0, 0))
    return f" По курсу учатся {students}." if students else " Учеников по курсу нет."


def _money(price: int | None) -> str:
    if price is None:
        return "цена по запросу"
    return f"{price:,} ₸".replace(",", " ")


def _module(ctx: ToolCtx, module_id: int):
    module = CourseAdminRepo(ctx.db).module_by_id(module_id)
    if module is None:
        raise ToolError("not_found", f"Модуля #{module_id} нет")
    return module


def _find_item(tree: list[dict], kind: str, item_id: int) -> tuple[dict, dict] | None:
    """Элемент и модуль, в котором он лежит. Урок ищется по обоим видам:
    в дереве у него стоит video или text, а модель называет его lesson."""
    kinds = LESSON_KINDS if kind == "lesson" else (kind,)
    for module in tree:
        for item in module["items"]:
            if item["id"] == item_id and item["kind"] in kinds:
                return module, item
    return None


def _module_in(tree: list[dict], module_id: int) -> dict | None:
    return next((row for row in tree if row["id"] == module_id), None)


def _slot(position: int | None, size: int) -> int:
    """Место, считая с 1: модель регулярно присылает 0 или число больше длины,
    и отбивать за это вызов дороже, чем поставить элемент в край."""
    if position is None:
        return size
    return max(0, min(size, position - 1))


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError("bad_argument", f"Дата пишется как ГГГГ-ММ-ДД, пришло {value!r}") from None


def _ranged(name: str, value: int, low: int, high: int) -> int:
    """Границы, которые на экране держит схема запроса. Инструмент идёт мимо
    неё, а курс на 100 000 часов потом правится руками."""
    if not low <= value <= high:
        raise ToolError("bad_argument", f"{name}: бывает от {low} до {high}, пришло {value}")
    return value
