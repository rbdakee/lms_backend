"""Учителя в админке: список, карточка и действия над человеком.

Здесь целиком живут персональные данные — ФИО, телефоны, школы и регионы.
Из этого следует одно: всё, что собирает этот файл, уходит только админу
и только по проверке на сервере, а в тексты ошибок и в логи эти поля
не попадают (общий CLAUDE.md, раздел «Персональные данные»).

Отдельно от `UsersService` потому, что тот отвечает человеку на вопросы
о себе самом. Здесь на человека смотрят снаружи: что у него с курсами,
тестами, работами и документами — и что админ вправе с этим сделать.
"""

from app.adapters.db.models import Certificate, Course, Enrollment, Quiz, QuizAttempt, User
from app.adapters.db.repos import (
    AttemptRepo,
    CertificateRepo,
    EnrollmentRepo,
    NotificationRepo,
    QuizAdminRepo,
    SessionRepo,
    TeacherAdminRepo,
    UserRepo,
    now_utc,
)
from app.domain.errors import (
    AttemptInProgressError,
    CertificateIssuedError,
    FieldError,
    NoAttemptError,
    NotFoundError,
    PhoneTakenError,
    QuizRetakableError,
    SelfBlockError,
)
from app.domain.phone import normalize_phone
from app.domain.quiz import score_percent


def _percent(done_count: int, total_count: int) -> int:
    """Тот же расчёт, что в кабинете учителя (`domain/program.py`) и в отчёте
    админа: человек читает разные числа на двух экранах одного курса как
    ошибку. Считается по всем видимым элементам программы, а не по одним
    урокам, — `lessons_done` рядом остаётся счётчиком именно уроков."""
    return round(done_count * 100 / total_count) if total_count else 0


class TeachersAdminService:
    def __init__(
        self,
        users: UserRepo,
        teachers: TeacherAdminRepo,
        quizzes: QuizAdminRepo,
        enrollments: EnrollmentRepo,
        attempts: AttemptRepo,
        certificates: CertificateRepo,
        sessions: SessionRepo,
        notifications: NotificationRepo,
    ):
        self.users = users
        self.teachers = teachers
        self.quizzes = quizzes
        self.enrollments = enrollments
        self.attempts = attempts
        self.certificates = certificates
        self.sessions = sessions
        self.notifications = notifications

    # -- GET /admin/teachers ---------------------------------------------

    def admin_list(
        self,
        *,
        region: str | None,
        school: str | None,
        course_id: int | None,
        q: str | None,
        offset: int,
        limit: int,
    ) -> dict:
        users, total = self.teachers.page(
            region=region, school=school, course_id=course_id, q=q, offset=offset, limit=limit
        )
        user_ids = [user.id for user in users]
        enrollments = self.teachers.enrollment_counts(user_ids)
        certificates = self.teachers.certificate_counts(user_ids)
        items = []
        for user in users:
            courses_count, completed_count = enrollments.get(user.id, (0, 0))
            items.append(
                {
                    "id": user.id,
                    "last_name": user.last_name,
                    "first_name": user.first_name,
                    "middle_name": user.middle_name,
                    "phone": user.phone,
                    "school": user.school,
                    "region": user.region,
                    "city": user.city,
                    "subject": user.subject,
                    "courses_count": courses_count,
                    "completed_count": completed_count,
                    "certificates_count": certificates.get(user.id, 0),
                    "is_blocked": user.is_blocked,
                    "created_at": user.created_at,
                }
            )
        return {"items": items, "total": total}

    # -- GET /admin/teachers/{id} ----------------------------------------

    def card(self, user_id: int) -> dict:
        return self._card_out(self._found(user_id))

    # -- PATCH /admin/teachers/{id} --------------------------------------

    def patch(self, admin: User, user_id: int, fields: dict) -> dict:
        teacher = self._found(user_id)
        if fields.get("is_blocked") is not None:
            if fields["is_blocked"] and teacher.id == admin.id:
                raise SelfBlockError()
            # Блокировка действует со следующего запроса: её проверяет вход
            # (`deps.get_current_user`), а не конец сессии
            teacher.is_blocked = fields["is_blocked"]
        if fields.get("phone") is not None:
            self._change_phone(teacher, fields["phone"])
        return self._card_out(teacher)

    def _change_phone(self, teacher: User, raw: str) -> None:
        """Смена номера — восстановление доступа, а не правка справочной
        строки: вход идёт только по SMS, и человек, сменивший симку, теряет
        аккаунт вместе с сертификатами (DESIGN_BRIEF, 9)."""
        phone = normalize_phone(raw)
        if phone is None:
            raise FieldError("phone", "Проверьте номер: нужен казахстанский, 10 цифр после +7")
        if phone == teacher.phone:
            # Номер тот же — менять нечего, и выгонять человека из живых
            # сессий не за что
            return
        existing = self.users.by_phone(phone)
        if existing is not None:
            raise PhoneTakenError(existing.id)
        teacher.phone = phone
        # Старый номер человеку уже не принадлежит: сессия, открытая на нём,
        # с этой минуты чужая
        self.sessions.revoke_all(teacher.id)

    # -- POST /admin/teachers/{id}/retakes -------------------------------

    def allow_retake(
        self, admin: User, user_id: int, quiz_id: int, reason: str, platform: str
    ) -> dict:
        """Пересдача снимает зачёт с попытки, но не трогает её содержимое:
        ответы, баллы и `passed` остаются как были — строка нужна админу как
        история. Освобождается частичный индекс `uq_quiz_attempt_counted`,
        и следующая попытка человека снова становится зачётной.
        """
        teacher = self._found(user_id)
        reason = reason.strip()
        if not reason:
            raise FieldError("reason", "Причина обязательна — она остаётся в истории")
        found = self.quizzes.with_course_and_module(quiz_id)
        if found is None:
            raise NotFoundError("Тест не найден")
        quiz, _, course = found
        if quiz.retakable:
            raise QuizRetakableError()

        # Зачётная попытка ищется на конкретной площадке: у человека, купившего
        # общий курс дважды, их две, и снять зачёт не с той — необратимая
        # ошибка ценой в попытку
        counted = self.attempts.counted_for(teacher.id, quiz.id, platform)
        if counted is None:
            raise NoAttemptError()
        if counted.finished_at is None:
            raise AttemptInProgressError("Попытка ещё не завершена — дождитесь её конца")
        certificate = self.certificates.active_for(teacher.id, course.id, platform)
        if certificate is not None:
            # Иначе новая попытка на 40% отменила бы уже выданный документ —
            # или чек-лист, по которому админ вот-вот его выпишет: заявку он
            # проверил в момент подачи, и результаты под ней не едут
            raise CertificateIssuedError(
                "Заявка на сертификат отправлена — пересдача закрыта"
                if certificate.issued_at is None
                else "Сертификат по курсу уже выдан — пересдача закрыта"
            )

        counted.is_counted = False
        counted.uncounted_by = admin.id
        counted.uncounted_reason = reason
        counted.uncounted_at = now_utc()
        # Без колокольчика человек не узнает, что тест снова открыт, и будет
        # считать курс потерянным
        self.notifications.create(
            teacher.id,
            "retake_allowed",
            {"course_id": course.id, "quiz_id": quiz.id, "quiz_title": quiz.title},
            # Площадка — у снятой попытки: ссылка ведёт туда, где тест открылся
            counted.platform,
        )
        return self._card_out(teacher)

    # -- DELETE /admin/enrollments/{id} ----------------------------------

    def revoke(self, enrollment_id: int) -> None:
        """Закрытие доступа ничего не удаляет: прогресс, попытки, сдачи
        и выданные сертификаты остаются на месте, а строка доступа — в карточке
        учителя с отметкой об отзыве."""
        enrollment = self.enrollments.by_id(enrollment_id)
        if enrollment is None:
            raise NotFoundError("Доступ не найден")
        if enrollment.revoked_at is not None:
            # Повторный вызов ничего не меняет: время остаётся временем
            # первого отзыва
            return
        enrollment.revoked_at = now_utc()

    # -- сборка ответа ---------------------------------------------------

    def _found(self, user_id: int) -> User:
        teacher = self.users.by_id(user_id)
        if teacher is None:
            raise NotFoundError("Учитель не найден")
        return teacher

    def _card_out(self, teacher: User) -> dict:
        """Профиль и четыре вкладки одним ответом: у одного человека курсов,
        тестов, работ и документов заведомо немного, и пагинации экран
        не рисует.

        Каждый список берётся одним запросом на всю карточку — прогресс
        по курсам считается тоже списком, а не обходом курсов в цикле.
        """
        enrollments = self.teachers.enrollments(teacher.id)
        course_ids = [course.id for _, course in enrollments]
        totals = self.teachers.item_counts(course_ids)
        done = self.teachers.done_counts(teacher.id, course_ids)
        granted_by_admins = self.teachers.admins_among(
            [enrollment.granted_by for enrollment, _ in enrollments]
        )
        certificates = self.teachers.certificates(teacher.id)
        attempts = self.teachers.attempts(teacher.id)
        return {
            "id": teacher.id,
            "last_name": teacher.last_name,
            "first_name": teacher.first_name,
            "middle_name": teacher.middle_name,
            # ИИН видит только админ — здесь и на странице «Сертификаты»
            # (CERTIFICATES_BRIEF, 1). Заглушка означает «не заполнен»
            "iin": teacher.iin,
            "phone": teacher.phone,
            "email": teacher.email,
            "school": teacher.school,
            "position": teacher.position,
            "region": teacher.region,
            "city": teacher.city,
            "subject": teacher.subject,
            "experience": teacher.experience,
            "lang": teacher.lang,
            "is_admin": teacher.is_admin,
            "is_blocked": teacher.is_blocked,
            "created_at": teacher.created_at,
            "courses": [
                self._enrollment_out(enrollment, course, totals, done, granted_by_admins)
                for enrollment, course in enrollments
            ],
            "quizzes": self._quizzes_out(attempts, certificates),
            "submissions": [
                {
                    "id": submission.id,
                    "task_id": task.id,
                    "task_title": task.title,
                    "course_id": course_id,
                    # Площадка сдачи: одно задание, сданное на обеих, — две
                    # разные работы с разной судьбой проверки
                    "platform": submission.platform,
                    "status": submission.status,
                    "created_at": submission.created_at,
                    "reviewed_at": submission.reviewed_at,
                }
                for submission, task, course_id in self.teachers.submissions(teacher.id)
            ],
            "certificates": [
                self._certificate_out(certificate) for certificate in certificates
            ],
        }

    @staticmethod
    def _enrollment_out(
        enrollment: Enrollment,
        course: Course,
        totals: dict[int, tuple[int, int]],
        done: dict[tuple[int, str], tuple[int, int]],
        granted_by_admins: set[int],
    ) -> dict:
        lessons_total, items_total = totals.get(course.id, (0, 0))
        # Прогресс берётся у пары «курс и площадка»: у человека с доступом
        # на обеих их два, и сложенные вместе они дали бы больше ста процентов
        lessons_done, items_done = done.get((course.id, enrollment.platform), (0, 0))
        return {
            "enrollment_id": enrollment.id,
            "course": {"id": course.id, "lang": course.lang, "title": course.title},
            "platform": enrollment.platform,
            "granted_at": enrollment.granted_at,
            # Признак, а не имя выдавшего: ФИО админа карточке ни к чему,
            # actor_id остаётся в базе
            "granted_by_admin": enrollment.granted_by in granted_by_admins,
            "paid_note": enrollment.paid_note,
            "revoked_at": enrollment.revoked_at,
            "completed_at": enrollment.completed_at,
            "lessons_done": lessons_done,
            "lessons_total": lessons_total,
            "progress_percent": _percent(items_done, items_total),
        }

    def _quizzes_out(
        self, attempts: list[tuple[QuizAttempt, Quiz, Course]], certificates: list[Certificate]
    ) -> list[dict]:
        """Вкладка «Тесты»: по строке на **тест и площадку**, внутри — попытки
        этой площадки.

        Строка не может быть просто тестом: доступ, прогресс и попытка
        раздельные, и у купившего общий курс дважды один тест живёт двумя
        независимыми жизнями — с разным итогом и разной судьбой пересдачи.
        Сложенные в одну строку, они дали бы кнопку, которая горит зелёным
        там, где сервер ответит 409.

        Тесты берутся из попыток: у теста, который человек не открывал,
        показывать нечего, и разрешать там тоже нечего.
        """
        # Действующие строки, а не одни выданные: заявка закрывает пересдачу
        # так же, как документ, — админ выпишет бумагу по тому чек-листу,
        # который проверил в момент заявки (CERTIFICATES_BRIEF, 3)
        active = {
            (certificate.course_id, certificate.platform)
            for certificate in certificates
            if certificate.revoked_at is None
        }
        max_scores = self.attempts.max_scores([attempt for attempt, _, _ in attempts])
        by_quiz: dict[tuple[int, str], tuple[Quiz, Course, list[QuizAttempt]]] = {}
        for attempt, quiz, course in attempts:
            key = (quiz.id, attempt.platform)
            by_quiz.setdefault(key, (quiz, course, []))[2].append(attempt)
        return [
            self._quiz_out(
                quiz, course, platform, rows, max_scores, (course.id, platform) in active
            )
            for (_, platform), (quiz, course, rows) in by_quiz.items()
        ]

    @classmethod
    def _quiz_out(
        cls,
        quiz: Quiz,
        course: Course,
        platform: str,
        attempts: list[QuizAttempt],
        max_scores: dict[int, int],
        certificate_active: bool,
    ) -> dict:
        blocker = cls._retake_blocker(quiz, attempts, certificate_active)
        return {
            "quiz_id": quiz.id,
            "title": quiz.title,
            "course_id": course.id,
            "course_title": course.title,
            # Площадка строки: она же уходит телом пересдачи — то, что показано,
            # и то, что ответит сервер, считаются по одной и той же площадке
            "platform": platform,
            "retakable": quiz.retakable,
            "pass_score": quiz.pass_score,
            "can_allow_retake": blocker is None,
            "retake_blocker": blocker,
            "attempts": [
                {
                    "id": attempt.id,
                    "started_at": attempt.started_at,
                    "finished_at": attempt.finished_at,
                    # Процентами, как на экране результата теста: максимум
                    # у попытки свой — это снимок её вопросов
                    "score": (
                        score_percent(attempt.score or 0, max_scores[attempt.id])
                        if attempt.finished_at is not None
                        else None
                    ),
                    "passed": attempt.passed,
                    "is_counted": attempt.is_counted,
                    "uncounted_reason": attempt.uncounted_reason,
                    "uncounted_at": attempt.uncounted_at,
                }
                for attempt in attempts
            ],
        }

    @staticmethod
    def _retake_blocker(
        quiz: Quiz, attempts: list[QuizAttempt], certificate_active: bool
    ) -> str | None:
        """Почему пересдачу разрешить нельзя — или None, если можно.

        Правила и их порядок те же, что в `allow_retake`: экран не должен
        рисовать живую кнопку, которая ответит 409, и объяснять отказ другими
        словами, чем ответит сервер.
        """
        if quiz.retakable:
            return "quiz_retakable"
        counted = next((attempt for attempt in attempts if attempt.is_counted), None)
        if counted is None:
            # Зачётной попытки нет — либо человек тест не проходил, либо
            # пересдача этому человеку уже открыта
            return "no_attempt"
        if counted.finished_at is None:
            return "attempt_in_progress"
        if certificate_active:
            # Код причины прежний, хотя причиной стала и заявка: его читает
            # экран админки, и менять его в этой сессии не просили
            return "certificate_issued"
        return None

    @staticmethod
    def _certificate_out(certificate: Certificate) -> dict:
        return {
            "id": certificate.id,
            # Три состояния одной строки, отдельной таблицы заявок нет
            # (CERTIFICATES_BRIEF, 5). Отзыв поверх любого из двух остальных,
            # поэтому проверяется первым
            "status": (
                "revoked"
                if certificate.revoked_at is not None
                else "issued"
                if certificate.issued_at is not None
                else "requested"
            ),
            # null у заявки: наш номер выписывается в момент выдачи
            "number": certificate.number,
            # Номер академии, пустой у документов до 04.09.2026 — админ
            # проставит его, когда дойдут руки
            "registration_number": certificate.registration_number,
            "course_id": certificate.course_id,
            # Документов по одному курсу может быть два — по одному на площадку
            "platform": certificate.platform,
            # Снимок, снятый при заявке: курс переименуют — документ прежний
            "course_title": certificate.course_title,
            "hours": certificate.hours,
            # Есть у всех трёх состояний, поэтому вкладка сортируется по нему
            "requested_at": certificate.requested_at,
            # null — админ ещё не выдал
            "issued_at": certificate.issued_at,
            "revoked_at": certificate.revoked_at,
        }
