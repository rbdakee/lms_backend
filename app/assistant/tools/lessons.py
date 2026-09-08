"""Уроки, тесты с вопросами и задания: чтение и правка содержимого курса.

Форма та же, что в `tools/courses.py` (образец), и правила оттуда же:
инструменты растут из сценариев `app/application/*`, адаптеры берутся
у `app/api/deps.py`, опасное — `confirm` с `@preview`, где есть числа
и площадки.

Своё здесь одно — **курс, по которому уже учатся** (`BACKEND_NOTES`,
раздел 10). Урок с чужим прогрессом, тест с попытками и задание со сдачами
не удаляются, а прячутся; вопрос, попавший в состав попытки, не правится
вовсе. Сценарии это уже отбивают, и переписывать их проверки здесь нечем:
дело инструмента — донести отказ до модели и сказать в предложении, что
по курсу учатся, до того как админ нажмёт «Выполнить».

Загрузки файлов урока и файла-шаблона задания в реестре нет вовсе: файл
ассистенту взять неоткуда — он не браузер и в хранилище ничего не кладёт.
"""

from app.adapters.db.models import Question
from app.adapters.db.repos import (
    CourseAdminRepo,
    CoursePlatformRepo,
    LessonAdminRepo,
    QuizAdminRepo,
    TaskAdminRepo,
)
from app.api import deps
from app.application.lessons_admin import LessonsAdminService
from app.application.quizzes_admin import QuizzesAdminService
from app.application.tasks_admin import TasksAdminService
from app.assistant.registry import Arg, ToolCtx, ToolError, preview, tool
from app.config import get_settings
from app.domain.plural import plural

# Границы чисел, которые на экране держит схема запроса: инструмент идёт
# мимо неё, а тест с проходным баллом 900 потом правится руками.
LIMITS = {
    "pass_score": (1, 100),
    "points": (1, 100),
    "time_limit_min": (1, 600),
    "time_required_min": (0, 600),
    "max_size_mb": (1, 1024),
}


def lessons_service(ctx: ToolCtx) -> LessonsAdminService:
    """Тот же сценарий, что стоит за `/admin/lessons/{id}`; хранилище —
    у `deps`, а не собранное заново (см. `courses_service` в courses.py)."""
    return LessonsAdminService(
        lessons=LessonAdminRepo(ctx.db),
        courses=CourseAdminRepo(ctx.db),
        storage=deps.get_storage(),
    )


def quizzes_service(ctx: ToolCtx) -> QuizzesAdminService:
    return QuizzesAdminService(quizzes=QuizAdminRepo(ctx.db), courses=CourseAdminRepo(ctx.db))


def tasks_service(ctx: ToolCtx) -> TasksAdminService:
    return TasksAdminService(
        tasks=TaskAdminRepo(ctx.db),
        courses=CourseAdminRepo(ctx.db),
        storage=deps.get_storage(),
        cfg=get_settings(),
    )


# -- урок ------------------------------------------------------------------


@tool(
    name="lesson_card",
    danger="read",
    description=(
        "Карточка урока целиком: текст урока, ссылка на видео, прикреплённые"
        " материалы, требуемое время, скрыт ли он и проходил ли его кто-нибудь."
        " В course_program текстов нет — за ними идут сюда. Идентификатор урока"
        " берут в course_program."
    ),
    args={"lesson_id": Arg("integer", "Идентификатор урока", required=True)},
)
def lesson_card(ctx: ToolCtx, lesson_id: int) -> dict:
    return lessons_service(ctx).card(lesson_id)


@tool(
    name="lesson_create",
    danger="write",
    description=(
        "Создать урок в модуле. Урок встаёт в конец модуля заготовкой"
        " и — обязательно — скрытым: содержимого у него ещё нет, а в курсе,"
        " где учатся, пустой урок открылся бы людям сразу. Текст или ссылку"
        " на видео кладёт lesson_update, он же снимает is_hidden; место"
        " в программе меняет program_move."
    ),
    args={
        "module_id": Arg("integer", "Модуль, в который кладётся урок", required=True),
        "title": Arg("string", "Название урока", required=True),
        "kind": Arg(
            "string",
            "Вид урока: video — видео с YouTube, text — текстовый урок",
            enum=["video", "text"],
            required=True,
        ),
        "time_required_min": Arg(
            "integer", "Требуемое время в минутах, 0–600. По умолчанию 0"
        ),
    },
)
def lesson_create(
    ctx: ToolCtx, module_id: int, title: str, kind: str, time_required_min: int = 0
) -> dict:
    card = lessons_service(ctx).create(
        module_id,
        {
            "title": title,
            "kind": kind,
            "time_required_min": _ranged("time_required_min", time_required_min),
        },
    )
    return _lesson_out(card, ["title", "kind", "time_required_min"])


@tool(
    name="lesson_update",
    danger="write",
    description=(
        "Правка урока: название, вид, текст, ссылка на видео, длительность"
        " и требуемое время. Здесь же урок прячут и достают обратно —"
        " is_hidden. Видеоурок не сохранится без ссылки, текстовый — без"
        " текста, но один is_hidden проходит всегда: так прячут урок, который"
        " нельзя удалить из-за чужого прогресса."
    ),
    args={
        "lesson_id": Arg("integer", "Идентификатор урока", required=True),
        "title": Arg("string", "Название урока"),
        "kind": Arg("string", "Вид урока", enum=["video", "text"]),
        "body_html": Arg(
            "string",
            "Текст урока разметкой. Лишние теги сервер вырежет сам."
            " У текстового урока без него не сохранить",
        ),
        "video_url": Arg("string", "Ссылка на YouTube; сервер приводит её к одному виду"),
        "duration_label": Arg("string", "Длительность видео словами, например «12 мин»"),
        "time_required_min": Arg("integer", "Требуемое время в минутах, 0–600"),
        "is_hidden": Arg(
            "boolean", "true — спрятать урок от учеников, false — показать снова"
        ),
    },
)
def lesson_update(
    ctx: ToolCtx,
    lesson_id: int,
    title: str | None = None,
    kind: str | None = None,
    body_html: str | None = None,
    video_url: str | None = None,
    duration_label: str | None = None,
    time_required_min: int | None = None,
    is_hidden: bool | None = None,
) -> dict:
    fields = _checked(_fields(
        title=title,
        kind=kind,
        # Разметка приходит строкой, а в сценарий уходит объектом: форма
        # `{"html": ...}` зафиксирована контрактом, а модели плоская строка
        # понятнее вложенного объекта, которого она не увидит в схеме
        body=None if body_html is None else {"html": body_html},
        video_url=video_url,
        duration_label=duration_label,
        time_required_min=_maybe("time_required_min", time_required_min),
        is_hidden=is_hidden,
    ))
    return _lesson_out(lessons_service(ctx).patch(lesson_id, fields), sorted(fields))


@tool(
    name="lesson_delete",
    danger="confirm",
    description=(
        "Удалить урок. Урок, который кто-то уже прошёл, не удаляется вовсе:"
        " у людей «12 из 18» превратилось бы в «12 из 17», а условия"
        " сертификата выполнились бы сами собой. Такой урок прячут —"
        " lesson_update с is_hidden."
    ),
    args={"lesson_id": Arg("integer", "Идентификатор урока", required=True)},
)
def lesson_delete(ctx: ToolCtx, lesson_id: int) -> dict:
    lessons_service(ctx).delete(lesson_id)
    return {"lesson_id": lesson_id, "deleted": True}


@preview("lesson_delete")
def lesson_delete_preview(ctx: ToolCtx, lesson_id: int) -> str:
    card = lessons_service(ctx).card(lesson_id)
    progress = CourseAdminRepo(ctx.db).progress_counts([lesson_id]).get(lesson_id, 0)
    text = (
        f"Удалит урок #{lesson_id} «{card['title']}» из модуля"
        f" «{card['module']['title']}»." + _course_note(ctx, card["course"])
    )
    if progress:
        text += f" Не удалится: урок прошли {progress} — его можно только скрыть."
    return text


# -- тест ------------------------------------------------------------------


@tool(
    name="quiz_card",
    danger="read",
    description=(
        "Карточка теста целиком: настройки, вопросы по порядку, все варианты"
        " ответа с пометкой правильных и разъяснения к вопросам. Правильные"
        " ответы видит только админ — в тексте, который прочитает учитель,"
        " их не называют. Идентификатор теста берут в course_program."
    ),
    args={"quiz_id": Arg("integer", "Идентификатор теста", required=True)},
)
def quiz_card(ctx: ToolCtx, quiz_id: int) -> dict:
    return quizzes_service(ctx).card(quiz_id)


@tool(
    name="quiz_create",
    danger="write",
    description=(
        "Создать тест в модуле. Тест встаёт в конец модуля заготовкой без"
        " вопросов и скрытым: пустой тест в открытом курсе входит в условия"
        " сертификата и не даётся пройти. Вопросы добавляет quiz_question_add,"
        " показывает тест quiz_update с is_hidden. Итоговый тест в курсе"
        " может быть только один."
    ),
    args={
        "module_id": Arg("integer", "Модуль, в который кладётся тест", required=True),
        "title": Arg("string", "Название теста", required=True),
        "pass_score": Arg("integer", "Проходной балл в процентах, 1–100", required=True),
        "is_final": Arg(
            "boolean", "Итоговый тест курса. По умолчанию нет — обычный тест модуля"
        ),
        "time_required_min": Arg("integer", "Требуемое время в минутах, 0–600"),
    },
)
def quiz_create(
    ctx: ToolCtx,
    module_id: int,
    title: str,
    pass_score: int,
    is_final: bool = False,
    time_required_min: int = 0,
) -> dict:
    card = quizzes_service(ctx).create(
        module_id,
        {
            "title": title,
            "pass_score": _ranged("pass_score", pass_score),
            "is_final": is_final,
            "time_required_min": _ranged("time_required_min", time_required_min),
        },
    )
    return _quiz_out(card, ["title", "pass_score", "is_final", "time_required_min"])


@tool(
    name="quiz_update",
    danger="write",
    description=(
        "Настройки теста: название, проходной балл, таймер, перемешивание,"
        " показ разбора, пересдаваемость, требуемое время, признак итогового"
        " и is_hidden. Настройки правятся и у теста, который уже проходили:"
        " запрет касается вопросов, а не правил, и посчитанные баллы"
        " от нового проходного не меняются. Вопросы правит quiz_question_update."
    ),
    args={
        "quiz_id": Arg("integer", "Идентификатор теста", required=True),
        "title": Arg("string", "Название теста"),
        "is_final": Arg("boolean", "Итоговый тест курса; второго такого в курсе не бывает"),
        "pass_score": Arg("integer", "Проходной балл в процентах, 1–100"),
        "time_limit_min": Arg(
            "integer", "Таймер в минутах, 1–600. 0 — снять таймер совсем"
        ),
        "shuffle": Arg("boolean", "Перемешивать вопросы"),
        "show_review": Arg("boolean", "Показывать разбор ответов после теста"),
        "retakable": Arg("boolean", "Разрешить пересдачу; по умолчанию попытка одна"),
        "time_required_min": Arg("integer", "Требуемое время в минутах, 0–600"),
        "is_hidden": Arg("boolean", "true — спрятать тест от учеников, false — показать"),
    },
)
def quiz_update(
    ctx: ToolCtx,
    quiz_id: int,
    title: str | None = None,
    is_final: bool | None = None,
    pass_score: int | None = None,
    time_limit_min: int | None = None,
    shuffle: bool | None = None,
    show_review: bool | None = None,
    retakable: bool | None = None,
    time_required_min: int | None = None,
    is_hidden: bool | None = None,
) -> dict:
    fields = _fields(
        title=title,
        is_final=is_final,
        pass_score=_maybe("pass_score", pass_score),
        shuffle=shuffle,
        show_review=show_review,
        retakable=retakable,
        time_required_min=_maybe("time_required_min", time_required_min),
        is_hidden=is_hidden,
    )
    if time_limit_min is not None:
        # У сценария null — это «таймера нет», а не «не прислали», но null
        # от модели реестр отбрасывает вместе с незаданными аргументами.
        # Поэтому «снять таймер» приходит нулём — числом, которое не бывает
        # настоящим таймером
        fields["time_limit_min"] = (
            None if time_limit_min == 0 else _ranged("time_limit_min", time_limit_min)
        )
    _checked(fields)
    return _quiz_out(quizzes_service(ctx).patch(quiz_id, fields), sorted(fields))


@tool(
    name="quiz_delete",
    danger="confirm",
    description=(
        "Удалить тест вместе с вопросами и вариантами. Тест, который кто-то"
        " проходил, не удаляется вовсе: вместе с ним ушли бы чужие баллы,"
        " а по ним выдан сертификат. Такой тест прячут — quiz_update"
        " с is_hidden."
    ),
    args={"quiz_id": Arg("integer", "Идентификатор теста", required=True)},
)
def quiz_delete(ctx: ToolCtx, quiz_id: int) -> dict:
    quizzes_service(ctx).delete(quiz_id)
    return {"quiz_id": quiz_id, "deleted": True}


@preview("quiz_delete")
def quiz_delete_preview(ctx: ToolCtx, quiz_id: int) -> str:
    card = quizzes_service(ctx).card(quiz_id)
    attempts = CourseAdminRepo(ctx.db).attempt_counts([quiz_id]).get(quiz_id, 0)
    what = "итоговый тест" if card["is_final"] else "тест"
    text = (
        f"Удалит {what} #{quiz_id} «{card['title']}» из модуля"
        f" «{card['module']['title']}» и {len(card['questions'])}"
        f" {plural(len(card['questions']), 'вопрос', 'вопроса', 'вопросов')} в нём."
        + _course_note(ctx, card["course"])
    )
    if attempts:
        text += (
            f" Не удалится: по тесту {attempts}"
            f" {plural(attempts, 'попытка', 'попытки', 'попыток')} —"
            " его можно только скрыть."
        )
    return text


# -- вопросы теста ---------------------------------------------------------


@tool(
    name="quiz_question_add",
    danger="write",
    description=(
        "Добавить вопрос в тест вместе с вариантами ответа: варианты приходят"
        " списком строк, а correct — номера правильных, считая с 1. Частичных"
        " баллов нет: зачёт идёт только за полностью верный набор. Вопрос"
        " встаёт в конец теста."
    ),
    args={
        "quiz_id": Arg("integer", "Идентификатор теста", required=True),
        "type": Arg(
            "string",
            "single — один правильный ответ, multi — несколько (вариантов"
            " не меньше трёх), bool — да/нет (ровно два варианта)",
            enum=["single", "multi", "bool"],
            required=True,
        ),
        "text": Arg("string", "Текст вопроса", required=True),
        "options": Arg(
            "array", "Варианты ответа по порядку, от 2 до 10", items="string", required=True
        ),
        "correct": Arg(
            "array",
            "Номера правильных вариантов, считая с 1",
            items="integer",
            required=True,
        ),
        "explanation": Arg(
            "string", "Разъяснение к вопросу: его видит ученик в разборе после теста"
        ),
        "points": Arg("integer", "Баллы за вопрос, 1–100. По умолчанию 1"),
    },
)
def quiz_question_add(
    ctx: ToolCtx,
    quiz_id: int,
    type: str,
    text: str,
    options: list[str],
    correct: list[int],
    explanation: str | None = None,
    points: int = 1,
) -> dict:
    question = quizzes_service(ctx).add_question(
        quiz_id,
        {
            "type": type,
            "text": text,
            "options": _options(options, correct),
            "explanation": explanation,
            "points": _ranged("points", points),
        },
    )
    return _question_out(question, ["type", "text", "options", "points"])


@tool(
    name="quiz_question_update",
    danger="write",
    description=(
        "Правка вопроса теста. Вопрос, который уже попадал в чью-то попытку,"
        " не правится вовсе — разбор показал бы человеку не то, что он видел;"
        " вместо правки создают новый вопрос, а этот прячут is_hidden"
        " (он проходит и у вопроса с попытками). Варианты присылаются полным"
        " списком вместе с correct и заменяют прежние целиком."
    ),
    args={
        "question_id": Arg("integer", "Идентификатор вопроса", required=True),
        "type": Arg("string", "Тип вопроса", enum=["single", "multi", "bool"]),
        "text": Arg("string", "Текст вопроса"),
        "explanation": Arg("string", "Разъяснение к вопросу"),
        "points": Arg("integer", "Баллы за вопрос, 1–100"),
        "options": Arg(
            "array", "Новые варианты ответа целиком, от 2 до 10", items="string"
        ),
        "correct": Arg(
            "array", "Номера правильных вариантов, считая с 1", items="integer"
        ),
        "is_hidden": Arg("boolean", "true — спрятать вопрос, false — показать снова"),
    },
)
def quiz_question_update(
    ctx: ToolCtx,
    question_id: int,
    type: str | None = None,
    text: str | None = None,
    explanation: str | None = None,
    points: int | None = None,
    options: list[str] | None = None,
    correct: list[int] | None = None,
    is_hidden: bool | None = None,
) -> dict:
    if options is None and correct is not None:
        raise ToolError(
            "bad_argument",
            "Варианты присылаются полным списком: correct без options непонятен",
        )
    fields = _checked(_fields(
        type=type,
        text=text,
        explanation=explanation,
        points=_maybe("points", points),
        options=None if options is None else _options(options, correct),
        is_hidden=is_hidden,
    ))
    question = quizzes_service(ctx).patch_question(question_id, fields)
    return _question_out(question, sorted(fields))


@tool(
    name="quiz_question_delete",
    danger="confirm",
    description=(
        "Удалить вопрос теста вместе с вариантами. Вопрос, попадавший в чью-то"
        " попытку, не удаляется вовсе: по нему посчитан чужой балл. Такой"
        " вопрос прячут — quiz_question_update с is_hidden."
    ),
    args={"question_id": Arg("integer", "Идентификатор вопроса", required=True)},
)
def quiz_question_delete(ctx: ToolCtx, question_id: int) -> dict:
    quizzes_service(ctx).delete_question(question_id)
    return {"question_id": question_id, "deleted": True}


@preview("quiz_question_delete")
def quiz_question_delete_preview(ctx: ToolCtx, question_id: int) -> str:
    question = _question(ctx, question_id)
    card = quizzes_service(ctx).card(question.quiz_id)
    attempts = QuizAdminRepo(ctx.db).question_attempts_count(question_id)
    text = (
        f"Удалит вопрос #{question_id} «{_short(question.text)}» из теста"
        f" #{question.quiz_id} «{card['title']}»: в тесте останется"
        f" {len(card['questions']) - 1}"
        f" {plural(len(card['questions']) - 1, 'вопрос', 'вопроса', 'вопросов')}."
        + _course_note(ctx, card["course"])
    )
    if attempts:
        text += (
            f" Не удалится: вопрос был в {attempts}"
            f" {plural(attempts, 'попытке', 'попытках', 'попытках')} —"
            " его можно только скрыть."
        )
    return text


# -- задание ---------------------------------------------------------------


@tool(
    name="task_card",
    danger="read",
    description=(
        "Карточка задания целиком: условие текстом, формат сдачи, разрешённые"
        " расширения файлов, потолок размера, файл-шаблон, скрыто ли задание"
        " и есть ли по нему сданные работы. Идентификатор берут"
        " в course_program."
    ),
    args={"task_id": Arg("integer", "Идентификатор задания", required=True)},
)
def task_card(ctx: ToolCtx, task_id: int) -> dict:
    return tasks_service(ctx).card(task_id)


@tool(
    name="task_create",
    danger="write",
    description=(
        "Создать задание в модуле. Задание встаёт в конец модуля заготовкой"
        " с пустым условием и скрытым: задание без условия человеку нечего"
        " делать. Условие пишет task_update, он же показывает задание."
    ),
    args={
        "module_id": Arg("integer", "Модуль, в который кладётся задание", required=True),
        "title": Arg("string", "Название задания", required=True),
        "time_required_min": Arg("integer", "Требуемое время в минутах, 0–600"),
    },
)
def task_create(ctx: ToolCtx, module_id: int, title: str, time_required_min: int = 0) -> dict:
    card = tasks_service(ctx).create(
        module_id,
        {"title": title, "time_required_min": _ranged("time_required_min", time_required_min)},
    )
    return _task_out(card, ["title", "time_required_min"])


@tool(
    name="task_update",
    danger="write",
    description=(
        "Правка задания: название, условие, формат сдачи, разрешённые"
        " расширения файлов, потолок размера, требуемое время и is_hidden."
        " Файл-шаблон отсюда не меняется — файл берётся только с экрана."
        " Задание со сдачами не удаляется, но прячется здесь."
    ),
    args={
        "task_id": Arg("integer", "Идентификатор задания", required=True),
        "title": Arg("string", "Название задания"),
        "statement_html": Arg(
            "string", "Условие задания разметкой; лишние теги сервер вырежет сам"
        ),
        "submit_format": Arg(
            "string",
            "Как сдают работу: text — текстом, file — файлом, both — и так и так",
            enum=["text", "file", "both"],
        ),
        "allowed_ext": Arg(
            "array",
            "Разрешённые расширения без точки, например pdf, docx."
            " Пустой список — принимается любое",
            items="string",
        ),
        "max_size_mb": Arg("integer", "Потолок размера файла в мегабайтах"),
        "time_required_min": Arg("integer", "Требуемое время в минутах, 0–600"),
        "is_hidden": Arg("boolean", "true — спрятать задание, false — показать снова"),
    },
)
def task_update(
    ctx: ToolCtx,
    task_id: int,
    title: str | None = None,
    statement_html: str | None = None,
    submit_format: str | None = None,
    allowed_ext: list[str] | None = None,
    max_size_mb: int | None = None,
    time_required_min: int | None = None,
    is_hidden: bool | None = None,
) -> dict:
    fields = _checked(_fields(
        title=title,
        statement=None if statement_html is None else {"html": statement_html},
        submit_format=submit_format,
        allowed_ext=allowed_ext,
        max_size_mb=_maybe("max_size_mb", max_size_mb),
        time_required_min=_maybe("time_required_min", time_required_min),
        is_hidden=is_hidden,
    ))
    return _task_out(tasks_service(ctx).patch(task_id, fields), sorted(fields))


@tool(
    name="task_delete",
    danger="confirm",
    description=(
        "Удалить задание. Задание, на которое сдали хотя бы одну работу,"
        " не удаляется вовсе: чужие работы ссылались бы в пустоту, а очередь"
        " проверки собирается по заданию. Такое задание прячут — task_update"
        " с is_hidden."
    ),
    args={"task_id": Arg("integer", "Идентификатор задания", required=True)},
)
def task_delete(ctx: ToolCtx, task_id: int) -> dict:
    tasks_service(ctx).delete(task_id)
    return {"task_id": task_id, "deleted": True}


@preview("task_delete")
def task_delete_preview(ctx: ToolCtx, task_id: int) -> str:
    card = tasks_service(ctx).card(task_id)
    submissions = CourseAdminRepo(ctx.db).submission_counts([task_id]).get(task_id, 0)
    text = (
        f"Удалит задание #{task_id} «{card['title']}» из модуля"
        f" «{card['module']['title']}»." + _course_note(ctx, card["course"])
    )
    if submissions:
        text += (
            f" Не удалится: на задание сдали {submissions} работ —"
            " его можно только скрыть."
        )
    return text


# -- сборка ответов и текстов ----------------------------------------------


def _fields(**values) -> dict:
    """Только присланное: незаданный аргумент — это «не трогай поле», а не
    «поставь пусто»."""
    return {name: value for name, value in values.items() if value is not None}


def _checked(fields: dict) -> dict:
    """Пустой набор — отказ: иначе ассистент отчитается о правке, которой
    не было. Отдельно от `_fields`, потому что у теста поле собирается
    после него — снятый таймер приходит нулём."""
    if not fields:
        raise ToolError("nothing_to_change", "Не задано ни одного поля — менять нечего")
    return fields


def _lesson_out(card: dict, changed: list[str]) -> dict:
    """Урок после правки — без текста и без материалов: их ассистент только что
    прислал сам, и возвращать их обратно значит платить за них дважды."""
    return {
        "lesson_id": card["id"],
        "module_id": card["module_id"],
        "course_id": card["course"]["id"],
        "title": card["title"],
        "kind": card["kind"],
        "is_hidden": card["is_hidden"],
        "is_ready": card["is_ready"],
        "has_data": card["has_data"],
        "changed": changed,
    }


def _quiz_out(card: dict, changed: list[str]) -> dict:
    return {
        "quiz_id": card["id"],
        "module_id": card["module_id"],
        "course_id": card["course"]["id"],
        "title": card["title"],
        "is_final": card["is_final"],
        "pass_score": card["pass_score"],
        "time_limit_min": card["time_limit_min"],
        "is_hidden": card["is_hidden"],
        "has_attempts": card["has_attempts"],
        "questions_count": len(card["questions"]),
        "changed": changed,
    }


def _question_out(question: dict, changed: list[str]) -> dict:
    return {
        "question_id": question["id"],
        "type": question["type"],
        "text": question["text"],
        "points": question["points"],
        "is_hidden": question["is_hidden"],
        "has_attempts": question["has_attempts"],
        "options": [
            {"text": option["text"], "is_correct": option["is_correct"]}
            for option in question["options"]
        ],
        "changed": changed,
    }


def _task_out(card: dict, changed: list[str]) -> dict:
    return {
        "task_id": card["id"],
        "module_id": card["module_id"],
        "course_id": card["course"]["id"],
        "title": card["title"],
        "submit_format": card["submit_format"],
        "is_hidden": card["is_hidden"],
        "is_ready": card["is_ready"],
        "has_data": card["has_data"],
        "changed": changed,
    }


def _options(options: list[str], correct: list[int] | None) -> list[dict]:
    """Варианты и номера правильных — в форму сценария. Плоские список строк
    и список номеров вместо списка объектов не от бедности: схема аргумента
    объектов внутри массива не описывает, а невыразимую схему модель угадывает
    как придётся."""
    if not correct:
        raise ToolError(
            "missing_argument",
            "Не сказано, какие варианты правильные: correct — их номера, считая с 1",
        )
    outside = sorted(number for number in correct if not 1 <= number <= len(options))
    if outside:
        raise ToolError(
            "bad_argument",
            f"В correct номера {outside}, а вариантов {len(options)} — считают с 1",
        )
    return [
        {"text": text, "is_correct": number in correct}
        for number, text in enumerate(options, start=1)
    ]


def _course_note(ctx: ToolCtx, course: dict) -> str:
    """Хвост карточки подтверждения: курс, его площадки и ученики.

    Элемент программы лежит в курсе, а курс выложен на площадках со своими
    ценами — без этого «удалит урок» звучит одинаково у черновика и у курса,
    по которому учатся на обеих площадках.
    """
    rows = CoursePlatformRepo(ctx.db).list_for(course["id"])
    where = ", ".join(f"{row.platform} ({_money(row.price)})" for row in rows) or "нигде"
    students, _ = (
        CourseAdminRepo(ctx.db).enrollment_counts([course["id"]]).get(course["id"], (0, 0))
    )
    return (
        f" Курс #{course['id']} «{course['title']}» выложен: {where};"
        f" учатся {students}."
    )


def _question(ctx: ToolCtx, question_id: int) -> Question:
    question = QuizAdminRepo(ctx.db).question_by_id(question_id)
    if question is None:
        raise ToolError("not_found", f"Вопроса #{question_id} нет")
    return question


def _short(text: str, limit: int = 60) -> str:
    """Текст вопроса в карточке подтверждения: она читается глазами, и абзац
    в ней хуже одной строки."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _money(price: int | None) -> str:
    if price is None:
        return "цена по запросу"
    return f"{price:,} ₸".replace(",", " ")


def _maybe(name: str, value: int | None) -> int | None:
    return None if value is None else _ranged(name, value)


def _ranged(name: str, value: int) -> int:
    low, high = LIMITS[name]
    if not low <= value <= high:
        raise ToolError("bad_argument", f"{name}: бывает от {low} до {high}, пришло {value}")
    return value
