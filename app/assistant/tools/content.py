"""Отзывы, вопросы под уроками и справочник категорий.

Три области с одним читателем: админ разбирает то, что написали живые люди.
Отсюда главное правило файла — **ответ человеку ассистент не публикует**.
Текст он вправе написать, а поставить его от имени академии решает админ,
поэтому `review_reply` и `question_answer` помечены `confirm` наравне
с удалением (`ASSISTANT_BRIEF`, раздел 6).

Автор отзыва и автор вопроса здесь — `Учитель #418` и ничего больше: списки
маскированы всегда, без исключений (раздел 5). Сценарии отдают ФИО тремя
полями — до ответа инструмента оно не доходит.

Премодерации отзывов в продукте нет: отзыв виден сразу, и «одобрить» или
«скрыть» его нечем — только ответить или удалить (`CLAUDE.md`).
"""

from app.adapters.db.models import Course, Lesson, ThreadMessage
from app.adapters.db.repos import ReviewRepo, ThreadMessageRepo
from app.api import deps
from app.application.categories import CategoriesService
from app.application.questions import QuestionsService
from app.application.reviews_admin import ReviewsAdminService
from app.assistant.masking import teacher_ref
from app.assistant.registry import Arg, ToolCtx, ToolError, platform_arg, preview, tool
from app.domain.platform import PLATFORMS
from app.domain.plural import plural

# Потолок строк в ответе инструмента — тот же, что у курсов. Не постраничность:
# ассистенту нужен вывод, а не страница; за второй он сходит с узким фильтром.
LIST_LIMIT = 50

# Сколько знаков чужого текста показывает карточка подтверждения. Админ читает
# её глазами перед кнопкой, и простыня на две тысячи знаков там не читается.
PREVIEW_TEXT = 160


def reviews_service(ctx: ToolCtx) -> ReviewsAdminService:
    """Тот же сервис, что стоит за `/admin/reviews`.

    Сборка берётся у `deps`, а не повторяется здесь: второй такой набор
    репозиториев рано или поздно разъехался бы с тем, которым пользуется
    админка, и ассистент читал бы не то, что видит человек.
    """
    return deps.get_reviews_admin_service(db=ctx.db)


def questions_service(ctx: ToolCtx) -> QuestionsService:
    """Тот же сервис, что стоит за `/admin/questions` и за ответом под уроком.

    Предпросмотра у ассистента нет вовсе — он не входит от имени учителя
    (`ASSISTANT_BRIEF`, раздел 12), поэтому репозитории настоящие: `None`
    вместо предпросматриваемого курса.
    """
    return deps.get_questions_service(
        db=ctx.db,
        thread_limiter=deps.get_thread_limiter(),
        enrollments=deps.get_enrollments(db=ctx.db, preview=None),
        preview_course_id=None,
        repos=deps.get_visibility_repos(db=ctx.db, preview_course_id=None),
    )


def categories_service(ctx: ToolCtx) -> CategoriesService:
    return deps.get_categories_service(db=ctx.db)


# -- общее ----------------------------------------------------------------


def teacher_out(user_id: int) -> dict:
    """Автор строки: номер и подпись, больше ничего.

    Даже школы и региона здесь нет, хотя `masked_teacher` их отдаёт: в ленте
    отзывов и в очереди вопросов они ни на что не влияют, а персданных
    в контексте OpenAI от них становится больше.
    """
    return {"user_id": user_id, "ref": teacher_ref(user_id)}


def when(value) -> str | None:
    """Время строкой ISO, а не объектом.

    Ответ инструмента ложится в `assistant_message.tool_result` — это JSONB,
    и `datetime` туда не сериализуется вовсе.
    """
    return value.isoformat() if value is not None else None


def counts_by_platform(count) -> dict[str, int]:
    """Числа по каждой площадке — считаются всегда, был фильтр или нет.

    «12 отзывов» без площадки — неверный ответ (`ASSISTANT_BRIEF`, раздел 4).
    Счётчик берётся тем же сценарием с `limit=0`: точное число, а не то,
    что уместилось на странице.
    """
    return {code: count(code) for code in PLATFORMS}


def short(text: str) -> str:
    """Чужой текст в карточке подтверждения — куском, а не целиком."""
    text = " ".join(text.split())
    return text if len(text) <= PREVIEW_TEXT else text[:PREVIEW_TEXT] + "…"


# -- отзывы ---------------------------------------------------------------


@tool(
    name="reviews_list",
    danger="read",
    description=(
        "Лента отзывов о курсах: оценка, текст, курс, площадка, дата и ответ"
        " академии, если он уже написан. Премодерации в продукте нет — отзыв"
        " виден сразу, и «одобрить» или «скрыть» его нечем: либо ответить"
        " через review_reply, либо удалить через review_delete. Отзывы свои"
        " у каждой площадки: без фильтра приходят обе, и в ответе видно,"
        " сколько отзывов на какой."
    ),
    args={
        "course_id": Arg("integer", "Только отзывы об этом курсе"),
        "rating": Arg("integer", "Только отзывы с такой оценкой, от 1 до 5"),
        "platform": platform_arg(
            "Только отзывы этой площадки. Не задана — обе, и числа по каждой"
            " видны отдельно."
        ),
    },
)
def reviews_list(
    ctx: ToolCtx,
    course_id: int | None = None,
    rating: int | None = None,
    platform: str | None = None,
) -> dict:
    service = reviews_service(ctx)

    def count(code: str) -> int:
        return service.admin_list(
            course_id=course_id, rating=rating, platform=code, offset=0, limit=0
        )["total"]

    data = service.admin_list(
        course_id=course_id, rating=rating, platform=platform, offset=0, limit=LIST_LIMIT
    )
    return {
        "total": data["total"],
        "by_platform": counts_by_platform(count),
        "shown": len(data["items"]),
        "truncated": data["total"] > len(data["items"]),
        "items": [_review_row(item) for item in data["items"]],
    }


@tool(
    name="review_card",
    danger="read",
    description=(
        "Один отзыв по его идентификатору — тем же составом, что и строка"
        " ленты. Нужен, когда идентификатор уже известен: с экрана отзывов"
        " или из прошлого вызова, а лента до него не доходит фильтрами."
        " Удалённый отзыв не открывается: отвечать на него негде."
    ),
    args={"review_id": Arg("integer", "Идентификатор отзыва", required=True)},
)
def review_card(ctx: ToolCtx, review_id: int) -> dict:
    review, course = _found_review(ctx, review_id)
    return {
        "id": review.id,
        "platform": review.platform,
        "rating": review.rating,
        "text": review.text,
        "created_at": when(review.created_at),
        "course": {"id": course.id, "lang": course.lang, "title": course.title},
        "teacher": teacher_out(review.user_id),
        "reply": (
            {"text": review.reply_text, "created_at": when(review.reply_at)}
            if review.reply_text is not None
            else None
        ),
    }


@tool(
    name="review_reply",
    danger="confirm",
    description=(
        "Ответить на отзыв от имени академии. Ответ у отзыва один: повторный"
        " вызов заменяет прежний текст, вторых уровней у него не бывает."
        " Текст пишет ассистент, публикует администратор — до кнопки это"
        " черновик, и называть его утверждённым нельзя. Отзыв читает учитель:"
        " второй площадки в тексте не существует, а на казахском текст идёт"
        " с обязательной вычиткой."
    ),
    args={
        "review_id": Arg("integer", "Идентификатор отзыва", required=True),
        "text": Arg("string", "Текст ответа, не длиннее 2000 знаков", required=True),
    },
)
def review_reply(ctx: ToolCtx, review_id: int, text: str) -> dict:
    # Сценарий возвращает элемент ленты с ФИО автора — наружу уходит строка,
    # собранная тем же маскирующим кодом, что и вся лента
    return _review_row(reviews_service(ctx).reply(ctx.admin, review_id, text))


@preview("review_reply")
def review_reply_preview(ctx: ToolCtx, review_id: int, text: str) -> str:
    review, course = _found_review(ctx, review_id)
    again = " Прежний ответ будет заменён." if review.reply_text is not None else ""
    return (
        f"Опубликует ответ академии на отзыв #{review.id} ({review.rating} из 5)"
        f" о курсе «{course.title}», площадка {review.platform}."
        f" Автор — {teacher_ref(review.user_id)}.{again}"
        f" Текст: «{short(text)}»"
    )


@tool(
    name="review_delete",
    danger="confirm",
    description=(
        "Удалить отзыв. Применяется к брани и спаму, а не к низкой оценке:"
        " удалённый отзыв пропадает и со страницы курса, и из средней оценки,"
        " и вернуть его нечем. Недовольный отзыв — случай для review_reply."
    ),
    args={"review_id": Arg("integer", "Идентификатор отзыва", required=True)},
)
def review_delete(ctx: ToolCtx, review_id: int) -> dict:
    reviews_service(ctx).delete(ctx.admin, review_id)
    return {"review_id": review_id, "deleted": True}


@preview("review_delete")
def review_delete_preview(ctx: ToolCtx, review_id: int) -> str:
    review, course = _found_review(ctx, review_id)
    return (
        f"Удалит отзыв #{review.id} ({review.rating} из 5) о курсе"
        f" «{course.title}», площадка {review.platform}, автор"
        f" {teacher_ref(review.user_id)}. Отзыв исчезнет со страницы курса"
        f" и из средней оценки; вернуть его нечем."
        f" Текст: «{short(review.text)}»"
    )


def _found_review(ctx: ToolCtx, review_id: int):
    """Отзыв вместе с курсом или отказ, который прочитает модель.

    Сценария «один отзыв по id» нет — у ленты только фильтры, — поэтому
    строка берётся тем же репозиторием, которым её берёт `ReviewsAdminService`,
    и с той же проверкой: удалённый отзыв для админа тоже не существует.
    """
    found = ReviewRepo(ctx.db).with_author_and_course(review_id)
    if found is None or found[0].deleted_at is not None:
        raise ToolError(
            "not_found", f"Отзыва #{review_id} нет: он удалён или его никогда не было"
        )
    review, _author, course = found
    return review, course


def _review_row(item: dict) -> dict:
    """Строка ленты без автора: сценарий отдаёт ФИО, школу и город, сюда
    доезжает только номер."""
    reply = item["reply"]
    return {
        "id": item["id"],
        "platform": item["platform"],
        "rating": item["rating"],
        "text": item["text"],
        "created_at": when(item["created_at"]),
        "course": item["course"],
        "teacher": teacher_out(item["teacher"]["id"]),
        "reply": (
            {"text": reply["text"], "created_at": when(reply["created_at"])}
            if reply is not None
            else None
        ),
    }


# -- вопросы под уроками ---------------------------------------------------


@tool(
    name="questions_list",
    danger="read",
    description=(
        "Очередь вопросов, которые учителя задали под уроками: текст, курс,"
        " урок с номером, площадка и уже написанные ответы. answered=false —"
        " только те, где никто ещё не ответил; с этого экран и открывается."
        " Ветку одного вопроса целиком показывает question_thread, отвечает"
        " question_answer. Вопросы свои у каждой площадки: без фильтра"
        " приходят обе, и числа по каждой видны отдельно."
    ),
    args={
        "answered": Arg(
            "boolean",
            "false — только вопросы без ответа, true — только отвеченные."
            " Не задан — все. Ответ автора самому себе вопрос не закрывает.",
        ),
        "course_id": Arg("integer", "Только вопросы по этому курсу"),
        "q": Arg("string", "Поиск по тексту вопроса"),
        "platform": platform_arg(
            "Только вопросы этой площадки. Не задана — обе, и числа по каждой"
            " видны отдельно."
        ),
    },
)
def questions_list(
    ctx: ToolCtx,
    answered: bool | None = None,
    course_id: int | None = None,
    q: str | None = None,
    platform: str | None = None,
) -> dict:
    service = questions_service(ctx)

    def count(code: str) -> int:
        return service.admin_list(
            answered=answered, course_id=course_id, q=q, platform=code, offset=0, limit=0
        )["total"]

    data = service.admin_list(
        answered=answered,
        course_id=course_id,
        q=q,
        platform=platform,
        offset=0,
        limit=LIST_LIMIT,
    )
    return {
        "total": data["total"],
        "by_platform": counts_by_platform(count),
        "shown": len(data["items"]),
        "truncated": data["total"] > len(data["items"]),
        "items": [_question_row(item) for item in data["items"]],
    }


@tool(
    name="question_thread",
    danger="read",
    description=(
        "Ветка одного вопроса целиком: сам вопрос, все ответы по порядку"
        " и номер того, кто каждый из них написал — отвечает и академия,"
        " и другие учителя с доступом к курсу. Нужен перед question_answer:"
        " в очереди видно, что ответ есть, а здесь — что именно уже сказали."
        " Адресуется любое сообщение ветки, показывается ветка от её вопроса."
    ),
    args={
        "message_id": Arg(
            "integer", "Идентификатор вопроса или любого ответа в его ветке", required=True
        )
    },
)
def question_thread(ctx: ToolCtx, message_id: int) -> dict:
    root = _root_of(ctx, _found_message(ctx, message_id))
    lesson, course = _where(ctx, root)
    replies = ThreadMessageRepo(ctx.db).replies_for([root.id]).get(root.id, [])
    return {
        "id": root.id,
        "platform": root.platform,
        "text": root.text,
        "created_at": when(root.created_at),
        "teacher": teacher_out(root.user_id),
        "course": {"id": course.id, "lang": course.lang, "title": course.title},
        "lesson": {"id": lesson.id, "title": lesson.title},
        "replies": [
            {
                "id": reply.id,
                "text": reply.text,
                # Ответ академии и ответ коллеги — разные вещи: по первому
                # видно, закрыт ли вопрос для админа
                "from_academy": author.is_admin,
                "teacher": teacher_out(reply.user_id),
                "created_at": when(reply.created_at),
            }
            for reply, author in replies
        ],
    }


@tool(
    name="question_answer",
    danger="confirm",
    description=(
        "Ответить учителю на вопрос под уроком от имени академии. Адресуется"
        " любое сообщение ветки — ответ всё равно встанет под её вопросом"
        " и на площадке этого вопроса, а не на какой-то другой. Текст пишет"
        " ассистент, публикует администратор. Правильных ответов теста"
        " в тексте быть не должно: спросивший про тест чаще всего собирается"
        " его пересдавать — объясняй принцип и говори, где в уроке смотреть."
    ),
    args={
        "message_id": Arg(
            "integer", "Идентификатор вопроса или любого ответа в его ветке", required=True
        ),
        "text": Arg("string", "Текст ответа, не длиннее 2000 знаков", required=True),
    },
)
def question_answer(ctx: ToolCtx, message_id: int, text: str) -> dict:
    root = _root_of(ctx, _found_message(ctx, message_id))
    # Площадка берётся у самого вопроса: ассистента зовут не с площадки,
    # а ответ обязан лечь в ту ленту, где вопрос задан. Сценарий приходит
    # к тому же решению через parent.platform — здесь оно сказано вслух
    posted = questions_service(ctx).add_message(
        ctx.admin, root.lesson_id, text, root.id, root.platform
    )
    # Сценарий отдаёт author_name — имя самого админа; наружу оно не идёт
    return {
        "question_id": root.id,
        "message_id": posted["id"],
        "platform": root.platform,
        "created_at": when(posted["created_at"]),
    }


@preview("question_answer")
def question_answer_preview(ctx: ToolCtx, message_id: int, text: str) -> str:
    root = _root_of(ctx, _found_message(ctx, message_id))
    lesson, course = _where(ctx, root)
    count = len(ThreadMessageRepo(ctx.db).replies_for([root.id]).get(root.id, []))
    already = (
        f" В ветке уже {count} {plural(count, 'ответ', 'ответа', 'ответов')}."
        if count
        else " Ответов в ветке пока нет."
    )
    return (
        f"Опубликует ответ академии под вопросом #{root.id}"
        f" ({teacher_ref(root.user_id)}) — урок «{lesson.title}» курса"
        f" «{course.title}», площадка {root.platform}.{already}"
        f" Текст: «{short(text)}»"
    )


@tool(
    name="question_delete",
    danger="confirm",
    description=(
        "Удалить сообщение под уроком — и вопрос, и отдельный ответ."
        " Удаление вопроса уносит из ленты всю ветку вместе с ответами;"
        " удаление ответа оставляет вопрос на месте. Ответить вместо"
        " удаления — question_answer."
    ),
    args={
        "message_id": Arg("integer", "Идентификатор вопроса или ответа", required=True)
    },
)
def question_delete(ctx: ToolCtx, message_id: int) -> dict:
    questions_service(ctx).delete_message(ctx.admin, message_id)
    return {"message_id": message_id, "deleted": True}


@preview("question_delete")
def question_delete_preview(ctx: ToolCtx, message_id: int) -> str:
    message = _found_message(ctx, message_id)
    lesson, course = _where(ctx, message)
    where = f"урок «{lesson.title}» курса «{course.title}», площадка {message.platform}"
    if message.parent_id is not None:
        return (
            f"Удалит ответ #{message.id} в ветке вопроса #{message.parent_id}"
            f" ({where}). Сам вопрос останется на месте."
            f" Текст: «{short(message.text)}»"
        )
    count = len(ThreadMessageRepo(ctx.db).replies_for([message.id]).get(message.id, []))
    return (
        f"Удалит вопрос #{message.id} ({teacher_ref(message.user_id)}, {where})"
        f" и уберёт из ленты {count} {plural(count, 'ответ', 'ответа', 'ответов')}"
        f" под ним. Текст: «{short(message.text)}»"
    )


def _found_message(ctx: ToolCtx, message_id: int) -> ThreadMessage:
    """Сообщение ветки или отказ, который прочитает модель.

    Сценария «одно сообщение по id» нет: очередь ходит фильтрами и страницами,
    а сюда приходят с карточки, где идентификатор уже известен. Строка
    берётся тем же репозиторием, что и у `QuestionsService`.
    """
    message = ThreadMessageRepo(ctx.db).by_id(message_id)
    if message is None or message.deleted_at is not None:
        raise ToolError(
            "not_found", f"Сообщения #{message_id} нет: оно удалено или его никогда не было"
        )
    return message


def _root_of(ctx: ToolCtx, message: ThreadMessage) -> ThreadMessage:
    """Вопрос ветки. Адресуется её корень — тем же приёмом, что у удаления
    сообщения: ответ ложится под вопрос, а не под соседний ответ, второго
    уровня вложенности в треде нет."""
    if message.parent_id is None:
        return message
    return _found_message(ctx, message.parent_id)


def _where(ctx: ToolCtx, message: ThreadMessage) -> tuple[Lesson, Course]:
    """Урок и курс сообщения — для карточки и для текста подтверждения.

    Напрямую, а не `visible_with_course`: вопрос разбирают и по скрытому
    курсу, и очередь админа его показывает (`ThreadMessageRepo.admin_page`).
    """
    lesson = ctx.db.get(Lesson, message.lesson_id)
    course = ctx.db.get(Course, message.course_id)
    if lesson is None or course is None:
        raise ToolError(
            "not_found", f"У сообщения #{message.id} не нашлось ни урока, ни курса"
        )
    return lesson, course


def _question_row(item: dict) -> dict:
    """Строка очереди без автора и без имён отвечавших.

    У ответов сценарий отдаёт `author_name` и не отдаёт идентификатора,
    поэтому здесь от них остаётся текст и признак «это академия». Номера
    отвечавших видно в question_thread.
    """
    return {
        "id": item["id"],
        "platform": item["platform"],
        "text": item["text"],
        "created_at": when(item["created_at"]),
        "teacher": teacher_out(item["teacher"]["id"]),
        "course": item["course"],
        "lesson": item["lesson"],
        "replies_count": len(item["replies"]),
        "replies": [
            {
                "id": reply["id"],
                "text": reply["text"],
                "from_academy": reply["author_is_admin"],
                "created_at": when(reply["created_at"]),
            }
            for reply in item["replies"]
        ],
    }


# -- категории -------------------------------------------------------------


@tool(
    name="categories_list",
    danger="read",
    description=(
        "Справочник категорий каталога: название, порядок и сколько курсов"
        " в каждой, считая черновики. По этому числу видно, можно ли"
        " категорию удалить: с курсами она не удаляется. Категория одна"
        " на обе площадки — своей у каждой нет. Отсюда берут category_id"
        " для заведения и правки курса."
    ),
)
def categories_list(ctx: ToolCtx) -> dict:
    # Без потолка и без числа по площадкам: категорий единицы, справочник
    # общий, и постранично его не рисует даже экран настроек
    items = categories_service(ctx).admin_list()["items"]
    return {"total": len(items), "items": items}


@tool(
    name="category_create",
    danger="write",
    description=(
        "Завести категорию каталога. Название уникально: две одинаковые"
        " строки в фильтре каталога неразличимы, и повтор отбивается отказом."
        " Новая категория встаёт в конец списка — порядок правится руками"
        " на экране настроек, инструмента под него нет."
    ),
    args={"title": Arg("string", "Название категории", required=True)},
)
def category_create(ctx: ToolCtx, title: str) -> dict:
    return categories_service(ctx).create(title)


@tool(
    name="category_rename",
    danger="write",
    description=(
        "Переименовать категорию. Это единственное, что у неё правится:"
        " порядок задаётся при создании, а число курсов считается, а не"
        " хранится. Название видно в фильтре каталога на обеих площадках."
    ),
    args={
        "category_id": Arg("integer", "Идентификатор категории", required=True),
        "title": Arg("string", "Новое название", required=True),
    },
)
def category_rename(ctx: ToolCtx, category_id: int, title: str) -> dict:
    return categories_service(ctx).rename(category_id, title)


@tool(
    name="category_delete",
    danger="confirm",
    description=(
        "Удалить категорию. Категория, на которой висит хоть один курс,"
        " не удаляется вовсе: категория у курса обязательна, и осиротевший"
        " курс исчез бы из каталога. Сначала переведи курсы в другую."
    ),
    args={"category_id": Arg("integer", "Идентификатор категории", required=True)},
)
def category_delete(ctx: ToolCtx, category_id: int) -> dict:
    categories_service(ctx).delete(category_id)
    return {"category_id": category_id, "deleted": True}


@preview("category_delete")
def category_delete_preview(ctx: ToolCtx, category_id: int) -> str:
    service = categories_service(ctx)
    found = next(
        (item for item in service.admin_list()["items"] if item["id"] == category_id), None
    )
    if found is None:
        raise ToolError("not_found", f"Категории #{category_id} нет")
    count = found["courses_count"]
    text = f"Удалит категорию #{category_id} «{found['title']}» из справочника каталога."
    if count:
        text += (
            f" Не удалится: в ней {count} {plural(count, 'курс', 'курса', 'курсов')}"
            f" — сначала переведите их в другую."
        )
    return text
