"""Экран теста: состояние, прохождение попытки и разбор.

Здесь ошибка стоит человеку единственной попытки, поэтому три вещи держатся
жёстко: одна зачётная попытка — частичным индексом базы, время — часами
сервера, состав вопросов — снимком в попытке. Всё остальное подстраивается
под них.
"""

import random

from app.adapters.db.models import Course, Option, Question, Quiz, QuizAttempt, User
from app.adapters.db.repos import (
    AttemptRepo,
    CertificateRepo,
    CourseRepo,
    EnrollmentRepo,
    ProgressRepo,
    QuizRepo,
    now_utc,
)
from app.application.preview_quiz import PreviewAttempt, SessionAttempts
from app.application.program import build_program, with_statuses
from app.domain.errors import (
    AttemptFinishedError,
    AttemptNotFinishedError,
    AttemptUsedError,
    CertificateIssuedError,
    ForbiddenError,
    NotFoundError,
    QuizEmptyError,
    ReviewUnavailableError,
    TimeExpiredError,
    ValidationAppError,
)
from app.domain.program import LOCKED, item_key
from app.domain.quiz import (
    deadline,
    earned_points,
    is_expired,
    minutes_spent,
    remaining_sec,
    review_available,
    score_percent,
    timed_out,
)

QUIZ_DENIED = "Тест доступен после выдачи доступа к курсу"
# Замок программы у теста — отказ, а не только вид на экране: попытка
# единственная и не возвращается (решение владельца, сессия 8)
QUIZ_LOCKED = "Тест откроется, когда будут пройдены предыдущие элементы курса"

# Попытка бывает строкой базы и снимком в памяти (режим предпросмотра): счёт,
# таймер и разбор одинаковы для обеих, поля у них называются одинаково.
AnyAttempt = QuizAttempt | PreviewAttempt

# single и bool — один вариант ответа; multi — сколько угодно.
SINGLE_CHOICE_TYPES = ("single", "bool")


class QuizzesService:
    def __init__(
        self,
        quizzes: QuizRepo,
        attempts: AttemptRepo,
        enrollments: EnrollmentRepo,
        certificates: CertificateRepo,
        courses: CourseRepo,
        progress: ProgressRepo,
        preview_course_id: int | None,
        preview_attempts: SessionAttempts | None,
    ):
        self.quizzes = quizzes
        self.attempts = attempts
        self.enrollments = enrollments
        self.certificates = certificates
        # Программа курса и прогресс — только ради замка на старте попытки:
        # статус элемента считает та же функция, что рисует его на экране,
        # иначе экран и сервер разошлись бы в понимании «закрыт»
        self.courses = courses
        self.progress = progress
        # Курс, который админ смотрит «как учитель», и попытка этой сессии
        # в памяти процесса: в базу в режиме не уходит ничего
        self.preview_course_id = preview_course_id
        self.preview_attempts = preview_attempts

    # -- GET /quizzes/{id} ----------------------------------------------

    def quiz_page(self, user: User, quiz_id: int, platform: str) -> dict:
        quiz, course = self._accessible(user, quiz_id, platform)
        questions = self.quizzes.visible_questions(quiz.id)
        if self._preview(course):
            # В предпросмотре история — это единственная попытка из памяти,
            # и та пока не завершена (BACKEND_NOTES, раздел 12)
            attempt = self._preview_attempt(quiz)
            finished: list[AnyAttempt] = (
                [attempt] if attempt is not None and attempt.finished_at is not None else []
            )
            state = self._preview_state(quiz, attempt)
        else:
            finished = list(self.attempts.finished_for(user.id, quiz.id, platform))
            state = self._state(user, quiz, course, finished, platform)
        return {
            "id": quiz.id,
            "module_id": quiz.module_id,
            "title": quiz.title,
            "is_final": quiz.is_final,
            "pass_score": quiz.pass_score,
            "time_limit_min": quiz.time_limit_min,
            "retakable": quiz.retakable,
            "show_review": quiz.show_review,
            "time_required_min": quiz.time_required_min,
            # Числа теста видны до старта, сами вопросы — нет: иначе тест
            # изучают, не тратя единственную попытку
            "questions_count": len(questions),
            "max_score": sum(question.points for question in questions),
            "state": state,
            "attempts": self._history(quiz, finished),
        }

    def _state(
        self,
        user: User,
        quiz: Quiz,
        course: Course,
        finished: list[AnyAttempt],
        platform: str,
    ) -> dict:
        active = self.attempts.active_for(user.id, quiz.id, platform)
        if active is not None:
            # Активная попытка главнее прочего: экран возвращает человека в неё
            return {"status": "in_progress", "attempt": self._attempt_out(active, quiz)}
        has_certificate = (
            self.certificates.active_for(user.id, course.id, platform) is not None
        )
        # Замок программы входит в «можно начать» тем же расчётом, каким
        # его проверяет старт: иначе экран рисует живую кнопку, а нажатие даёт 403 —
        # человек видит открытый тест и получает отказ
        blocked = has_certificate or self._is_locked(user, quiz, course, platform)
        if not finished:
            return {"status": "not_started", "can_start": not blocked}
        return {
            "status": "finished",
            "result": self._result_out(self._counted(finished), quiz),
            "can_retake": quiz.retakable and not blocked,
            "review_available": review_available(quiz.retakable, quiz.show_review),
        }

    @staticmethod
    def _counted(finished: list[AnyAttempt]) -> AnyAttempt:
        """Зачётная попытка — та, что помечена is_counted. Пометки нет только
        у истории, оставшейся от отзыва сертификата: там показываем последнюю."""
        return next(
            (attempt for attempt in finished if attempt.is_counted),
            finished[-1],
        )

    def _history(self, quiz: Quiz, finished: list[AnyAttempt]) -> list[dict]:
        max_scores = self.attempts.max_scores(finished)
        return [
            {
                "id": attempt.id,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
                "minutes_spent": minutes_spent(attempt.started_at, attempt.finished_at),
                "score": attempt.score,
                "max_score": max_scores[attempt.id],
                "score_percent": score_percent(attempt.score, max_scores[attempt.id]),
                "passed": attempt.passed,
                "timed_out": timed_out(
                    attempt.started_at, attempt.finished_at, quiz.time_limit_min
                ),
                "is_counted": attempt.is_counted,
            }
            for attempt in finished
        ]

    # -- POST /quizzes/{id}/quiz_attempts --------------------------------

    def start(self, user: User, quiz_id: int, platform: str) -> dict:
        quiz, course = self._accessible(user, quiz_id, platform)
        if self._preview(course):
            return self._preview_start(user, quiz)
        active = self.attempts.active_for(user.id, quiz.id, platform)
        if active is not None:
            # Идемпотентность по активной попытке: двойной клик по «Начать тест»
            # возвращает ту же попытку с сохранёнными ответами
            return self._attempt_out(active, quiz)
        self._check_unlocked(user, quiz, course, platform)
        if self.certificates.active_for(user.id, course.id, platform) is not None:
            raise CertificateIssuedError()
        if not quiz.retakable and self.attempts.finished_for(user.id, quiz.id, platform):
            raise AttemptUsedError()

        order = self._question_order(quiz)
        # У непересдаваемого попытка сразу зачётная: вторую не пустит индекс
        attempt = self.attempts.create(
            user.id, quiz.id, order, platform, is_counted=not quiz.retakable
        )
        if attempt is None:
            # Гонку двойного старта выиграл соседний запрос — отвечаем его
            # попыткой, чтобы клик не выглядел ошибкой и не съел вторую
            active = self.attempts.active_for(user.id, quiz.id, platform)
            if active is not None:
                return self._attempt_out(active, quiz)
            attempt = self.attempts.counted_for(user.id, quiz.id, platform)
            if attempt is None or attempt.finished_at is not None:
                raise AttemptUsedError()
        return self._attempt_out(attempt, quiz)

    def _is_locked(self, user: User, quiz: Quiz, course: Course, platform: str) -> bool:
        """Закрыт ли тест замком программы — строгим порядком курса или
        правилом итогового теста, ждущего все уроки.

        Статус спрашивается у той же функции, что рисует программу на экране:
        два отдельных расчёта разошлись бы, и человек получал бы отказ
        на тесте, который экран показывает открытым.
        """
        program = with_statuses(
            self.progress, course, user.id, build_program(self.courses, course.id), platform
        )
        return any(
            item["status"] == LOCKED and item_key(item) == ("quiz", quiz.id)
            for module in program
            for item in module["items"]
        )

    def _check_unlocked(self, user: User, quiz: Quiz, course: Course, platform: str) -> None:
        """Тест не начинается раньше своего черёда.

        Замок программы до этого был правилом показа: экран рисовал его,
        а сервер отдавал попытку любому, кто позвал ручку мимо экрана.
        Уроки и задания так и остались правилом показа — заглянувший вперёд
        ничего не теряет, — а вот попытка единственная и не возвращается:
        сгоревшая не в свой черёд стоит человеку курса.
        """
        if self._is_locked(user, quiz, course, platform):
            raise ForbiddenError(QUIZ_LOCKED)

    def _question_order(self, quiz: Quiz) -> list[int]:
        """Снимок состава попытки: что и в каком порядке человек будет решать."""
        questions = self.quizzes.visible_questions(quiz.id)
        if not questions or not sum(question.points for question in questions):
            # Пустой max_score сделал бы процент бессмысленным, а попытку —
            # потраченной впустую: тест из вопросов по нулю баллов не сдаётся
            # ни при каком ответе
            raise QuizEmptyError()
        order = [question.id for question in questions]
        if quiz.shuffle:
            random.shuffle(order)
        return order

    # -- POST /quiz_attempts/{id}/answers --------------------------------

    def answer(
        self,
        user: User,
        attempt_id: int,
        question_id: int,
        option_ids: list[int],
        platform: str,
    ) -> None:
        attempt, quiz, _ = self._own_attempt(user, attempt_id, platform)
        if attempt.finished_at is not None:
            raise AttemptFinishedError()
        if is_expired(attempt.started_at, quiz.time_limit_min, now_utc()):
            # Ответ не сохраняется вовсе: после дедлайна попытка неизменна
            raise TimeExpiredError()

        question = self.quizzes.questions_by_ids([question_id]).get(question_id)
        if question is None or question_id not in attempt.question_order:
            raise self._invalid("question_id", "Вопрос не из этой попытки")
        if question.type in SINGLE_CHOICE_TYPES and len(option_ids) > 1:
            raise self._invalid("option_ids", "Вопрос принимает один вариант ответа")
        known = {
            option.id for option in self.quizzes.options([question_id]).get(question_id, [])
        }
        if not set(option_ids) <= known:
            raise self._invalid("option_ids", "Вариант не из этого вопроса")

        # Пустой список — снятый ответ, а не отсутствие строки: человек передумал
        if isinstance(attempt, PreviewAttempt):
            # Ответ ложится в память процесса и уйдёт вместе с режимом
            attempt.answers[question_id] = list(option_ids)
        else:
            self.attempts.save_answer(attempt.id, question_id, option_ids)

    @staticmethod
    def _invalid(field: str, message: str) -> ValidationAppError:
        return ValidationAppError(
            "Проверьте заполнение полей",
            details={"fields": [{"field": field, "message": message}]},
        )

    # -- POST /quiz_attempts/{id}/finish ---------------------------------

    def finish(self, user: User, attempt_id: int, platform: str) -> dict:
        attempt, quiz, _ = self._own_attempt(user, attempt_id, platform)
        if attempt.finished_at is not None:
            # Идемпотентность: повторный клик по «Завершить» отдаёт тот же
            # результат и не сдвигает finished_at
            return self._result_out(attempt, quiz)

        questions, options = self._snapshot(attempt)
        chosen = {question_id: set(ids) for question_id, ids in self._answers(attempt)}
        score = 0
        for question_id in attempt.question_order:
            question = questions.get(question_id)
            if question is None:
                continue
            correct = {opt.id for opt in options.get(question_id, []) if opt.is_correct}
            score += earned_points(question.points, correct, chosen.get(question_id, set()))
        max_score = sum(
            questions[qid].points for qid in attempt.question_order if qid in questions
        )

        now = now_utc()
        limit = deadline(attempt.started_at, quiz.time_limit_min)
        # Время вышло — попытка закрывается концом лимита, а не моментом клика:
        # иначе «автосдача» приписала бы человеку минуты, которых у него не было
        attempt.finished_at = limit if limit is not None and now >= limit else now
        attempt.score = score
        attempt.passed = score_percent(score, max_score) >= quiz.pass_score
        if quiz.retakable and not isinstance(attempt, PreviewAttempt):
            # У попытки в памяти зачёт переключать не с чем: она там одна
            self.attempts.switch_counted(attempt)
        return self._result_out(attempt, quiz)

    # -- GET /quiz_attempts/{id}/review ----------------------------------

    def review(self, user: User, attempt_id: int, platform: str) -> dict:
        attempt, quiz, _ = self._own_attempt(user, attempt_id, platform)
        if attempt.finished_at is None:
            raise AttemptNotFinishedError()
        if not review_available(quiz.retakable, quiz.show_review):
            raise ReviewUnavailableError()

        questions, options = self._snapshot(attempt)
        chosen = {question_id: set(ids) for question_id, ids in self._answers(attempt)}
        items = []
        # Порядок попытки, а не теста: человек должен увидеть то же, что решал
        for question_id in attempt.question_order:
            question = questions.get(question_id)
            if question is None:
                continue
            question_options = options.get(question_id, [])
            picked = chosen.get(question_id, set())
            correct = {opt.id for opt in question_options if opt.is_correct}
            items.append(
                {
                    "id": question.id,
                    "type": question.type,
                    "text": question.text,
                    "points": question.points,
                    "earned_points": earned_points(question.points, correct, picked),
                    # Правильные ответы и пояснение впервые уходят наружу здесь
                    "explanation": question.explanation,
                    "options": [
                        {
                            "id": opt.id,
                            "text": opt.text,
                            "is_correct": opt.is_correct,
                            "is_chosen": opt.id in picked,
                        }
                        for opt in question_options
                    ],
                }
            )
        return {"result": self._result_out(attempt, quiz), "questions": items}

    # -- общее ------------------------------------------------------------

    def _snapshot(
        self, attempt: AnyAttempt
    ) -> tuple[dict[int, Question], dict[int, list[Option]]]:
        questions = self.quizzes.questions_by_ids(attempt.question_order)
        return questions, self.quizzes.options(list(questions))

    def _answers(self, attempt: AnyAttempt) -> list[tuple[int, list[int]]]:
        """Ответы попытки парами «вопрос — варианты», из базы или из памяти.
        Порядок один и тот же — по вопросу: экран сравнивает их между собой."""
        if isinstance(attempt, PreviewAttempt):
            return sorted(attempt.answers.items())
        return [
            (answer.question_id, list(answer.option_ids))
            for answer in self.attempts.answers(attempt.id)
        ]

    def _attempt_out(self, attempt: AnyAttempt, quiz: Quiz) -> dict:
        questions, options = self._snapshot(attempt)
        return {
            "id": attempt.id,
            "quiz_id": attempt.quiz_id,
            "started_at": attempt.started_at,
            "remaining_sec": remaining_sec(
                attempt.started_at, quiz.time_limit_min, now_utc()
            ),
            "questions": [
                {
                    "id": questions[qid].id,
                    "type": questions[qid].type,
                    "text": questions[qid].text,
                    "points": questions[qid].points,
                    # is_correct наружу не уходит до разбора
                    "options": [
                        {"id": opt.id, "text": opt.text} for opt in options.get(qid, [])
                    ],
                }
                for qid in attempt.question_order
                if qid in questions
            ],
            "answers": [
                {"question_id": question_id, "option_ids": ids}
                for question_id, ids in self._answers(attempt)
            ],
        }

    def _result_out(self, attempt: AnyAttempt, quiz: Quiz) -> dict:
        # max_scores читает у попытки только id и снимок вопросов, поэтому
        # считает и попытку из памяти — баллы всё равно берутся из базы
        max_score = self.attempts.max_scores([attempt])[attempt.id]
        return {
            "id": attempt.id,
            "score": attempt.score,
            "max_score": max_score,
            "score_percent": score_percent(attempt.score, max_score),
            "pass_score": quiz.pass_score,
            "passed": attempt.passed,
            "is_counted": attempt.is_counted,
            "timed_out": timed_out(
                attempt.started_at, attempt.finished_at, quiz.time_limit_min
            ),
            "minutes_spent": minutes_spent(attempt.started_at, attempt.finished_at),
            "review_available": review_available(quiz.retakable, quiz.show_review),
        }

    def _accessible(self, user: User, quiz_id: int, platform: str) -> tuple[Quiz, Course]:
        """Порядок проверок один на все эндпоинты теста: существование, потом
        доступ. Замок строгого порядка сюда не заходит — он правило показа."""
        found = self.quizzes.visible_with_course(quiz_id)
        if found is None:
            raise NotFoundError("Тест не найден")
        quiz, course = found
        if self.enrollments.active_for(user.id, course.id, platform) is None:
            raise ForbiddenError(QUIZ_DENIED)
        return quiz, course

    def _own_attempt(
        self, user: User, attempt_id: int, platform: str
    ) -> tuple[AnyAttempt, Quiz, Course]:
        """Доступ к курсу проверяется на каждый вызов, а не только на старте:
        отозвали посреди попытки — следующий ответ уже не примут.

        Первой спрашивается память: в режиме предпросмотра попытка живёт там,
        и дальше ответ, подсчёт и разбор идут общим кодом (раздел 12).

        Попытка ищется среди своих на этой площадке: чужая площадка отвечает
        «не найдена», как и чужая попытка.
        """
        preview = self.preview_attempts.get() if self.preview_attempts is not None else None
        if preview is not None and preview.id == attempt_id:
            found_quiz = self.quizzes.visible_with_course(preview.quiz_id)
            if found_quiz is None:
                raise NotFoundError("Попытка не найдена")
            quiz, course = found_quiz
            if self.enrollments.active_for(user.id, course.id, platform) is None:
                raise ForbiddenError(QUIZ_DENIED)
            return preview, quiz, course

        found = self.attempts.own_with_quiz(attempt_id, user.id, platform)
        if found is None:
            raise NotFoundError("Попытка не найдена")
        attempt, quiz, course = found
        if self.enrollments.active_for(user.id, course.id, platform) is None:
            raise ForbiddenError(QUIZ_DENIED)
        return attempt, quiz, course

    # -- предпросмотр ------------------------------------------------------

    def _preview(self, course: Course) -> bool:
        """Тест предпросматриваемого курса: попытка по нему живёт в памяти,
        а в базу не уходит ни строки (BACKEND_NOTES, раздел 12)."""
        return course.id == self.preview_course_id and self.preview_attempts is not None

    def _preview_attempt(self, quiz: Quiz) -> PreviewAttempt | None:
        """Попытка сессии, если она по этому тесту: попытка одна на сессию,
        и от соседнего теста она этому экрану не принадлежит."""
        attempt = self.preview_attempts.get() if self.preview_attempts is not None else None
        return attempt if attempt is not None and attempt.quiz_id == quiz.id else None

    def _preview_state(self, quiz: Quiz, attempt: PreviewAttempt | None) -> dict:
        """Тот же `state`, что у настоящего теста: сертификат ничего не запирает —
        выдать его в режиме нельзя, значит и мешать он не может."""
        if attempt is None:
            return {"status": "not_started", "can_start": True}
        if attempt.finished_at is None:
            return {"status": "in_progress", "attempt": self._attempt_out(attempt, quiz)}
        return {
            "status": "finished",
            "result": self._result_out(attempt, quiz),
            "can_retake": quiz.retakable,
            "review_available": review_available(quiz.retakable, quiz.show_review),
        }

    def _preview_start(self, user: User, quiz: Quiz) -> dict:
        """Старт без единой записи: снимок вопросов ложится в память процесса.

        Единственная попытка здесь ничем не рискует и потому не расходуется:
        админ пересматривает тест столько раз, сколько нужно, — новый старт
        просто заменяет прежний снимок.
        """
        active = self._preview_attempt(quiz)
        if active is not None and active.finished_at is None:
            # Идемпотентность та же, что у настоящего старта: двойной клик
            # возвращает ту же попытку с уже выбранными ответами
            return self._attempt_out(active, quiz)
        attempt = self.preview_attempts.start(
            user_id=user.id, quiz_id=quiz.id, question_order=self._question_order(quiz)
        )
        return self._attempt_out(attempt, quiz)
