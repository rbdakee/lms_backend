"""Черновик ответа учителю: «Ответить с помощью ИИ» у вопроса и у отзыва.

**Это не чат** (`ASSISTANT_BRIEF`, раздел 11). Один вызов: без истории, без
инструментов, без цикла и без памяти. Ассистент ничего не спрашивает, ничего
не уточняет и никуда не эскалирует — данных мало, значит пишет самый
осторожный полезный ответ, а не вопрос админу.

**Контекст собирает сервер, а не модель.** Состав фиксирован таблицей раздела
11 и от хода разговора не зависит: модель ничего не дозапрашивает, потому что
дозапрашивать ей нечем.

Обе ручки ничего не меняют — только возвращают текст. Отправляет его админ
той же кнопкой «Ответить», что была там и раньше: правило «ответ живому
человеку публикует админ» соблюдено самой формой экрана.
"""

import logging

from sqlalchemy.orm import Session as DbSession

from app.adapters.ai.openai_chat import AiUnavailableError
from app.adapters.db.models import Course, Lesson, Quiz, ThreadMessage, User
from app.adapters.db.repos import (
    CourseAdminRepo,
    CourseRepo,
    QuizAdminRepo,
    ReviewRepo,
    ThreadMessageRepo,
)
from app.application.ports import AiMessage, AiPort
from app.application.program import build_program
from app.application.quizzes_admin import QuizzesAdminService
from app.application.ratelimit import SlidingWindowLimiter
from app.assistant.masking import teacher_ref
from app.assistant.prompt import build_draft_system
from app.domain.brands import brand
from app.domain.errors import AiFailedError, NotFoundError, RateLimitedError

log = logging.getLogger(__name__)

# Слова, по которым сервер решает, что вопрос про тест, и кладёт в контекст
# тест модуля вместе с правильными ответами и разъяснениями. Список короткий
# и нарочно: тест — самая дорогая часть контекста, и класть его в каждый
# вопрос про видео значит платить за него всегда.
QUIZ_WORDS = (
    "тест", "теста", "тесте", "тестом", "тесты", "тестов",
    "балл", "баллы", "баллов", "попытк", "пересдач", "правильн", "ответ",
    # Казахские формы: спросили по-казахски — контекст нужен тот же
    "тестті", "тестте", "тесттің", "сынақ", "дұрыс жауап", "балл алу",
)


class DraftReplyService:
    def __init__(
        self,
        db: DbSession,
        ai: AiPort,
        *,
        limiter: SlidingWindowLimiter,
    ):
        self.db = db
        self.ai = ai
        self.limiter = limiter

    # -- POST /admin/thread_messages/{id}/draft_reply -----------------------

    def for_question(self, admin: User, message_id: int) -> dict:
        """Черновик ответа на вопрос под уроком.

        Адресуется корень ветки — тот же путь, что у уже существующего
        `DELETE /admin/thread_messages/{id}`.
        """
        message = ThreadMessageRepo(self.db).by_id(message_id)
        if message is None or message.deleted_at is not None:
            raise NotFoundError("Вопрос не найден")
        if message.parent_id is not None:
            # Отвечают на вопрос, а не на ответ под ним: ветка одна и корень
            # у неё один
            message = ThreadMessageRepo(self.db).by_id(message.parent_id)
            if message is None or message.deleted_at is not None:
                raise NotFoundError("Вопрос не найден")
        course = self.db.get(Course, message.course_id)
        lesson = self.db.get(Lesson, message.lesson_id) if message.lesson_id else None
        if course is None:
            raise NotFoundError("Курс вопроса не найден")

        parts: list[str] = []
        have: list[str] = []
        missing: list[str] = []
        parts.append(self._course_block(course, have, missing))
        parts.append(self._program_block(course, have))
        if lesson is not None:
            parts.append(self._lesson_block(lesson, have, missing))
            quiz = self._quiz_for(message, lesson)
            if quiz is not None:
                parts.append(self._quiz_block(quiz, have, missing))
        parts.append(self._thread_block(message, have))
        # Площадка — своя у вопроса, а не у запроса: админка одна на обе,
        # и отвечать надо голосом той площадки, где вопрос задан
        return self._ask(admin, message.platform, "question", parts, have, missing)

    # -- POST /admin/reviews/{id}/draft_reply -------------------------------

    def for_review(self, admin: User, review_id: int) -> dict:
        found = ReviewRepo(self.db).with_author_and_course(review_id)
        if found is None or found[0].deleted_at is not None:
            raise NotFoundError("Отзыв не найден")
        review, _, course = found

        parts: list[str] = []
        have: list[str] = []
        missing: list[str] = []
        parts.append(self._course_block(course, have, missing))
        parts.append(self._program_block(course, have))
        parts.append(
            "## Отзыв\n\n"
            f"Оценка: {review.rating} из 5. Автор: {teacher_ref(review.user_id)}.\n\n"
            f"{review.text.strip() or '(без текста)'}"
        )
        have.append("оценки и текста отзыва")
        # Площадка у отзыва своя: отвечаем голосом той площадки, в чьём
        # каталоге отзыв виден (PLATFORMS_BRIEF, решение 3)
        return self._ask(admin, review.platform, "review", parts, have, missing)

    # -- сборка контекста ---------------------------------------------------

    def _course_block(self, course: Course, have: list[str], missing: list[str]) -> str:
        lines = [f"Название: {course.title}", f"Язык: {course.lang}", f"Объём: {course.hours} ч"]
        if course.short.strip() or course.full.strip():
            lines.append(f"Описание: {course.short.strip()} {course.full.strip()}".strip())
            have.append("описания курса")
        else:
            missing.append("у курса нет описания")
        return "## Курс\n\n" + "\n".join(lines)

    def _program_block(self, course: Course, have: list[str]) -> str:
        program = build_program(CourseRepo(self.db), course.id)
        have.append("программы курса")
        lines = []
        for module in program:
            lines.append(f"- {module['title']}")
            for item in module["items"]:
                lines.append(f"  - [{item['kind']}] {item['title']}")
        return "## Программа\n\n" + ("\n".join(lines) or "(программа пуста)")

    def _lesson_block(self, lesson: Lesson, have: list[str], missing: list[str]) -> str:
        have.append(f"урока «{lesson.title}»")
        lines = [f"Название: {lesson.title}", f"Вид: {lesson.kind}"]
        text = _lesson_text(lesson)
        if text:
            lines.append(f"\nТекст урока:\n{text}")
        else:
            missing.append("у урока нет текста")
        return "## Урок, к которому задан вопрос\n\n" + "\n".join(lines)

    def _quiz_for(self, message: ThreadMessage, lesson: Lesson) -> Quiz | None:
        """Тест модуля — только когда вопрос и правда про тест.

        Решает сервер по словам вопроса, а не модель: класть тест с ответами
        в каждый вопрос про видео значит платить за него всегда, а угадывать
        модели нечем — она контекст не дозапрашивает.
        """
        text = message.text.lower()
        if not any(word in text for word in QUIZ_WORDS):
            return None
        quizzes = [
            quiz
            for quiz in CourseAdminRepo(self.db).quizzes(message.course_id)
            if quiz.module_id == lesson.module_id
        ]
        return quizzes[0] if quizzes else None

    def _quiz_block(self, quiz: Quiz, have: list[str], missing: list[str]) -> str:
        """Вопросы, правильные ответы и разъяснения — целиком.

        Модель их видит, но в текст не переносит: правило написано в промпте
        черновика отдельным пунктом, потому что учитель, спросивший про тест,
        чаще всего идёт пересдавать.
        """
        card = QuizzesAdminService(
            quizzes=QuizAdminRepo(self.db), courses=CourseAdminRepo(self.db)
        ).card(quiz.id)
        lines = [f"Название: {card['title']}", f"Проходной балл: {card['pass_score']}"]
        explained = 0
        for number, question in enumerate(card["questions"], start=1):
            lines.append(f"\n{number}. {question['text']}")
            for option in question["options"]:
                mark = "верный" if option["is_correct"] else "неверный"
                lines.append(f"   - ({mark}) {option['text']}")
            if question.get("explanation"):
                explained += 1
                lines.append(f"   Разъяснение: {question['explanation']}")
        if explained:
            have.append("теста с разъяснениями")
        else:
            have.append("теста")
            missing.append("разъяснений в тесте нет")
        return (
            "## Тест этого модуля\n\nПравильные ответы видны тебе, но в ответ"
            " учителю они не переносятся.\n\n" + "\n".join(lines)
        )

    def _thread_block(self, root: ThreadMessage, have: list[str]) -> str:
        replies = ThreadMessageRepo(self.db).replies_for([root.id]).get(root.id, [])
        have.append("переписки в этой ветке")
        lines = [f"{teacher_ref(root.user_id)}: {root.text.strip()}"]
        for message, author in replies:
            who = "Администратор" if author.is_admin else teacher_ref(author.id)
            lines.append(f"{who}: {message.text.strip()}")
        return "## Вопрос и переписка под ним\n\n" + "\n\n".join(lines)

    # -- вызов --------------------------------------------------------------

    def _ask(
        self,
        admin: User,
        platform: str,
        kind: str,
        parts: list[str],
        have: list[str],
        missing: list[str],
    ) -> dict:
        # Лимит тот же, что у чата, и счётчик тот же: черновики и разговор
        # тратят один и тот же месячный счёт
        wait = self.limiter.hit(str(admin.id))
        if wait:
            raise RateLimitedError(
                "Слишком много обращений к ассистенту — подождите немного",
                retry_after_sec=wait,
            )
        try:
            reply = self.ai.complete(
                system=build_draft_system(brand(platform), kind=kind),
                # Один вызов и одно сообщение: истории у черновика нет,
                # инструментов тоже — модель ничего не дозапрашивает
                messages=[AiMessage(role="user", content="\n\n".join(parts))],
            )
        except AiUnavailableError:
            raise AiFailedError() from None
        return {"text": reply.text.strip(), "built_from": _built_from(have, missing)}


def _lesson_text(lesson: Lesson) -> str:
    """Текст урока из `body`. Хранится он деревом блоков, а модели нужен
    читаемый текст — собираем строки, не пытаясь воспроизвести вёрстку."""
    body = lesson.body
    if not body:
        return ""
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key, value in node.items():
                # Ключи вроде `type` и `level` — разметка, а не текст
                if key in ("text", "items", "content", "blocks", "caption"):
                    walk(value)
    walk(body)
    return "\n".join(line.strip() for line in found if line and line.strip())


def _built_from(have: list[str], missing: list[str]) -> str:
    """Строка «из чего собрано». Это не вопрос админу и не требование
    что-то заполнить: видно, на чём стоит текст."""
    text = "Собрано из " + ", ".join(have) + "."
    if missing:
        text += " " + ", ".join(missing).capitalize() + "."
    return text


