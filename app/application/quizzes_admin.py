"""Редактор теста: настройки, вопросы с вариантами и правильные ответы.

Отдельно от `QuizzesService` потому, что смотрит в другую сторону. Тому тест —
экран учителя: доступ по enrollment, единственная попытка и вопросы без
правильных ответов. Здесь тест правят, поэтому приходит и скрытый тест,
и тест черновика, а `is_correct` с пояснением уходят наружу — это
единственное место во всём контракте, где они уходят вообще.

Запрет из `BACKEND_NOTES`, раздел 10, держится тоже здесь: вопрос, попавший
в состав попытки, не редактируется и не удаляется — создают новый, а этот
прячут. Настроек теста запрет не касается: `passed` и `score` завершённых
попыток посчитаны на старом проходном балле и такими остаются, поэтому
поднять `pass_score` можно и у теста, который уже проходили.
"""

from app.adapters.db.models import Course, Module, Option, Question, Quiz
from app.adapters.db.repos import CourseAdminRepo, QuizAdminRepo
from app.domain.errors import (
    FieldError,
    FinalQuizExistsError,
    HasAttemptsError,
    NotFoundError,
)
from app.domain.plural import plural

# Поля теста, у которых null в PATCH — это «не прислано»: колонка обязательная.
PLAIN_FIELDS = (
    "title",
    "is_final",
    "pass_score",
    "shuffle",
    "show_review",
    "retakable",
    "time_required_min",
    "is_hidden",
)

# Поля вопроса с той же оговоркой; explanation и options — отдельно: у первого
# null значение, у второго null означает «варианты не прислали».
QUESTION_PLAIN_FIELDS = ("type", "text", "points", "is_hidden")

# Сколько вариантов у вопроса имеет смысл: один — не выбор, одиннадцать
# не прочитываются глазами.
MIN_OPTIONS = 2
MAX_OPTIONS = 10

# «Несколько правильных» из двух вариантов — это вопрос с одним ответом
# наоборот, и человек отвечает на него по той же логике.
MIN_MULTI_OPTIONS = 3


def _checked_title(raw: str) -> str:
    title = raw.strip()
    if not title:
        raise FieldError("title", "Без названия тест не сохранить")
    return title


def _checked_text(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise FieldError("text", "Без текста вопрос не сохранить")
    return text


def _checked_options(type_: str, options: list[dict]) -> list[dict]:
    """Правила зачёта проверяются на паре «тип вопроса — варианты»: тип без
    вариантов ничего не значит, а варианты без типа не проверить.

    Частичных баллов нет (`BACKEND_NOTES`, раздел 4), поэтому у `multi`
    «правильных хотя бы один» — не мягкость, а нижняя граница смысла:
    зачёт всё равно только за полностью верный набор.

    Возвращает варианты с обрезанными пробелами: ведущий пробел не виден
    глазами, а сравнение текстов на экране ломает.
    """
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise FieldError("options", f"Вариантов должно быть от {MIN_OPTIONS} до {MAX_OPTIONS}")
    cleaned = [
        {"text": option["text"].strip(), "is_correct": option["is_correct"]}
        for option in options
    ]
    if any(not option["text"] for option in cleaned):
        raise FieldError("options", "Вариант без текста не сохранить")
    correct = sum(1 for option in cleaned if option["is_correct"])
    if type_ == "single" and correct != 1:
        raise FieldError("options", "В вопросе с одним ответом правильный ровно один")
    if type_ == "multi":
        if not correct:
            raise FieldError("options", "В вопросе с несколькими ответами нужен правильный")
        if len(cleaned) < MIN_MULTI_OPTIONS:
            raise FieldError(
                "options",
                f"В вопросе с несколькими ответами вариантов"
                f" не меньше {MIN_MULTI_OPTIONS}",
            )
    if type_ == "bool" and (len(cleaned) != 2 or correct != 1):
        raise FieldError("options", "У вопроса «да/нет» два варианта и один правильный")
    return cleaned


def _question_out(question: Question, options: list[Option], has_attempts: bool) -> dict:
    """Вопрос с правильными ответами и пояснением. Учительский GET /quizzes/{id}
    не отдаёт ни того, ни другого: права проверяются на сервере, а не тем,
    что экран лежит на другом домене."""
    return {
        "id": question.id,
        "type": question.type,
        "text": question.text,
        "explanation": question.explanation,
        "points": question.points,
        "is_hidden": question.is_hidden,
        # Попадал ли этот вопрос в состав хотя бы одной попытки: по нему экран
        # рисует замок, не дожидаясь 409
        "has_attempts": has_attempts,
        "options": [
            {"id": option.id, "text": option.text, "is_correct": option.is_correct}
            for option in options
        ],
    }


class QuizzesAdminService:
    def __init__(self, quizzes: QuizAdminRepo, courses: CourseAdminRepo):
        self.quizzes = quizzes
        self.courses = courses

    # -- POST /admin/modules/{id}/quizzes --------------------------------

    def create(self, module_id: int, fields: dict) -> dict:
        module = self._found_module(module_id)
        if fields["is_final"]:
            self._check_final(module.course_id)
        # Заготовка заводится скрытой: вопросов у неё ещё нет, а пустой тест
        # в открытом курсе входит в условия сертификата и отдаёт 409 quiz_empty
        # на попытку — документ по курсу перестал бы получать кто бы то ни было
        quiz = self.quizzes.create(
            module_id=module.id,
            title=_checked_title(fields["title"]),
            is_final=fields["is_final"],
            pass_score=fields["pass_score"],
            time_required_min=fields["time_required_min"],
            order_index=self.courses.next_order_index(module.id),
            is_hidden=True,
        )
        course = self.courses.by_id(module.course_id)
        self.courses.touch(course.id)
        return self._card_out(quiz, module, course)

    # -- GET /admin/quizzes/{id} -----------------------------------------

    def card(self, quiz_id: int) -> dict:
        return self._card_out(*self._found(quiz_id))

    # -- PATCH /admin/quizzes/{id} ---------------------------------------

    def patch(self, quiz_id: int, fields: dict) -> dict:
        quiz, module, course = self._found(quiz_id)
        if fields.get("title") is not None:
            fields = {**fields, "title": _checked_title(fields["title"])}
        if fields.get("is_final"):
            self._check_final(course.id, exclude_id=quiz.id)
        # Настройки правятся и у теста с попытками: запрет раздела 10 касается
        # вопросов, а не правил. Уже посчитанные passed и score здесь не
        # трогаются нигде — иначе поднятие проходного отобрало бы у людей
        # зачёт, а вместе с ним и выданный сертификат
        for field in PLAIN_FIELDS:
            if fields.get(field) is not None:
                setattr(quiz, field, fields[field])
        # null — таймера нет: переключатель «Таймер» и есть выбор между null
        # и числом, поэтому здесь null значение, а не «не прислано»
        if "time_limit_min" in fields:
            quiz.time_limit_min = fields["time_limit_min"]
        self.courses.touch(course.id)
        return self._card_out(quiz, module, course)

    # -- DELETE /admin/quizzes/{id} --------------------------------------

    def delete(self, quiz_id: int) -> None:
        quiz, _, course = self._found(quiz_id)
        # Тест, который кто-то проходил, не удаляется, а скрывается: вместе
        # с ним ушли бы чужие баллы, а по ним выдан сертификат
        attempts = self._attempts_count(quiz.id)
        if attempts:
            raise HasAttemptsError(
                f"У теста {attempts} {plural(attempts, 'попытка', 'попытки', 'попыток')}"
                " — его можно скрыть, но не удалить",
                attempts,
            )
        self.quizzes.delete(quiz.id)
        self.courses.touch(course.id)

    # -- POST /admin/quizzes/{id}/questions ------------------------------

    def add_question(self, quiz_id: int, fields: dict) -> dict:
        quiz, _, course = self._found(quiz_id)
        options = _checked_options(fields["type"], fields["options"])
        # Вопрос вместе с вариантами, одним телом: варианта без вопроса
        # не бывает, и двумя запросами это оставляло бы в базе вопросы
        # без ответов, когда второй запрос не дошёл
        question = self.quizzes.create_question(
            quiz_id=quiz.id,
            type=fields["type"],
            text=_checked_text(fields["text"]),
            explanation=fields["explanation"],
            points=fields["points"],
            order_index=self.quizzes.next_question_order(quiz.id),
        )
        self.quizzes.replace_options(question.id, options)
        self.courses.touch(course.id)
        return self._question_card(question)

    # -- PATCH /admin/quiz_questions/{id} --------------------------------

    def patch_question(self, question_id: int, fields: dict) -> dict:
        question, course = self._found_question(question_id)
        # Запрос, в котором нет ничего, кроме is_hidden, не проверяется вовсе:
        # иначе выполнить совет из текста 409 — «создайте новый, а этот
        # скройте» — было бы нечем
        if set(fields) - {"is_hidden"}:
            # Попытки спрашиваются до правки вариантов, а не после: варианты
            # приходят полным списком и заменяют прежние, и обратный порядок
            # стёр бы то, по чему посчитан чужой балл
            attempts = self.quizzes.question_attempts_count(question.id)
            if attempts:
                raise HasAttemptsError(
                    f"Вопрос был в {attempts} "
                    f"{plural(attempts, 'попытке', 'попытках', 'попытках')}"
                    " — создайте новый, а этот скройте",
                    attempts,
                )
            # Проверяется то, что получится, а не то, что было: смена типа
            # вместе с новыми вариантами проходит одним запросом, а смена типа
            # в одиночку — проверяется на тех вариантах, что уже лежат
            sent = fields.get("options")
            options = _checked_options(
                fields.get("type") or question.type,
                sent if sent is not None else self._stored_options(question.id),
            )
            if sent is not None:
                fields = {**fields, "options": options}
        if fields.get("text") is not None:
            fields = {**fields, "text": _checked_text(fields["text"])}
        for field in QUESTION_PLAIN_FIELDS:
            if fields.get(field) is not None:
                setattr(question, field, fields[field])
        # null — пояснения у вопроса нет, блок на экране не рисуется
        if "explanation" in fields:
            question.explanation = fields["explanation"]
        if fields.get("options") is not None:
            self.quizzes.replace_options(question.id, fields["options"])
        self.courses.touch(course.id)
        return self._question_card(question)

    # -- DELETE /admin/quiz_questions/{id} -------------------------------

    def delete_question(self, question_id: int) -> None:
        question, course = self._found_question(question_id)
        attempts = self.quizzes.question_attempts_count(question.id)
        if attempts:
            raise HasAttemptsError(
                f"Вопрос был в {attempts} "
                f"{plural(attempts, 'попытке', 'попытках', 'попытках')}"
                " — создайте новый, а этот скройте",
                attempts,
            )
        self.quizzes.delete_question(question.id)
        self.courses.touch(course.id)

    # -- сборка ответа ----------------------------------------------------

    def _found(self, quiz_id: int) -> tuple[Quiz, Module, Course]:
        found = self.quizzes.with_course_and_module(quiz_id)
        if found is None:
            raise NotFoundError("Тест не найден")
        return found

    def _found_module(self, module_id: int) -> Module:
        module = self.courses.module_by_id(module_id)
        if module is None:
            raise NotFoundError("Модуль не найден")
        return module

    def _found_question(self, question_id: int) -> tuple[Question, Course]:
        question = self.quizzes.question_by_id(question_id)
        if question is None:
            raise NotFoundError("Вопрос не найден")
        return question, self.courses.course_of_item("quiz", question.quiz_id)

    def _check_final(self, course_id: int, *, exclude_id: int | None = None) -> None:
        """Итоговый тест в курсе один: отчёт считает средний балл по итоговому
        и берёт первый попавшийся, а чек-лист сертификата пишет «Сдать итоговый
        тест» в единственном числе.

        Строка курса берётся на запись до проверки: частичным уникальным
        индексом эту единственность не выразить — `course_id` у теста нет,
        он в двух джойнах, — и без блокировки два одновременных запроса
        оба прочитали бы «итогового нет».
        """
        self.courses.lock(course_id)
        existing = self.quizzes.final_in_course(course_id, exclude_id=exclude_id)
        if existing is not None:
            raise FinalQuizExistsError(existing.title, existing.id)

    def _attempts_count(self, quiz_id: int) -> int:
        return self.courses.attempt_counts([quiz_id]).get(quiz_id, 0)

    def _stored_options(self, question_id: int) -> list[dict]:
        """Варианты вопроса в той же форме, в какой они приходят с экрана:
        так их проверяет то же правило, что и присланные."""
        return [
            {"text": option.text, "is_correct": option.is_correct}
            for option in self.quizzes.options([question_id]).get(question_id, [])
        ]

    def _question_card(self, question: Question) -> dict:
        """Вопрос как элемент `questions[]` — ответ создания и правки.
        `has_attempts` считается заново: у скрытого правкой одного `is_hidden`
        он остаётся true, и замок на экране никуда не денется."""
        return _question_out(
            question,
            self.quizzes.options([question.id]).get(question.id, []),
            self.quizzes.question_attempts_count(question.id) > 0,
        )

    def _card_out(self, quiz: Quiz, module: Module, course: Course) -> dict:
        questions = self.quizzes.questions(quiz.id)
        options = self.quizzes.options([question.id for question in questions])
        attempted = self.quizzes.attempted_question_ids(quiz.id)
        return {
            "id": quiz.id,
            "module_id": quiz.module_id,
            # Хлебные крошки шапки редактора; lang рисует метку языка —
            # переключателя языка в тесте нет
            "course": {"id": course.id, "lang": course.lang, "title": course.title},
            "module": {"id": module.id, "title": module.title},
            "title": quiz.title,
            "is_final": quiz.is_final,
            "pass_score": quiz.pass_score,
            "time_limit_min": quiz.time_limit_min,
            "shuffle": quiz.shuffle,
            "show_review": quiz.show_review,
            "retakable": quiz.retakable,
            "time_required_min": quiz.time_required_min,
            "is_hidden": quiz.is_hidden,
            "has_attempts": self._attempts_count(quiz.id) > 0,
            # Скрытые вопросы не считаются, как не считаются и у учителя
            "max_score": sum(
                question.points for question in questions if not question.is_hidden
            ),
            "questions": [
                _question_out(
                    question,
                    options.get(question.id, []),
                    question.id in attempted,
                )
                for question in questions
            ],
        }
