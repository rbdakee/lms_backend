"""Сертификат: чек-лист условий, выдача и публичная проверка.

Выдача необратима — отзывать выданный документ уже неловко, — поэтому условия
считаются на сервере, единственность держит частичный уникальный индекс,
а незавершённая попытка теста выдачу останавливает: её finish ещё может
поменять зачёт.
"""

from app.adapters.db.models import Certificate, Course, User
from app.adapters.db.repos import (
    CertificateRepo,
    CourseRepo,
    EnrollmentRepo,
    NotificationRepo,
    ProgressRepo,
    now_utc,
)
from app.application.ratelimit import SlidingWindowLimiter
from app.domain.certificate import (
    all_done,
    build_conditions,
    canonical_number,
    generate_number,
)
from app.domain.errors import (
    AttemptInProgressError,
    ConditionsNotMetError,
    ForbiddenError,
    NotFoundError,
    ProfileIncompleteError,
    RateLimitedError,
)
from app.domain.profile import onboarding_done

ACCESS_DENIED = "Доступ к курсу не открыт"

# Случайная часть номера — 32 в шестой степени, около миллиарда вариантов:
# занятый номер это невероятное совпадение, и хватает попробовать ещё раз.
NUMBER_TRIES = 5


def _attempt_blocker() -> dict:
    """Помеха выдаче и отказ в выдаче — одно и то же событие, поэтому и текст
    у них один: экран показывает его под неактивной кнопкой."""
    error = AttemptInProgressError()
    return {"code": error.code, "message": error.message}


def _required(items: list, kind: str, done: set[tuple[str, int]] | None) -> list:
    """Элементы, которые условие спрашивает с этого человека: видимые плюс
    скрытые, которые он уже прошёл.

    Скрытое несданное требованием быть перестаёт — курс правят на ходу,
    а элемент с чужими данными не удаляют, а прячут. Но уже сданное скрытие
    не отбирает: зачёт человек получил, когда элемент стоял в программе,
    и снять его задним числом значит отобрать сертификат у того, кто всё
    прошёл (решение владельца, 19.08.2026).
    """
    return [
        item
        for item in items
        if not item.is_hidden or (done is not None and (kind, item.id) in done)
    ]


def course_conditions(
    course: Course,
    *,
    lessons: list,
    tasks: list,
    quizzes: list,
    done: set[tuple[str, int]] | None,
) -> list[dict]:
    """Чек-лист условий курса по уже загруженным спискам.

    Отдельной функцией, потому что данные загружает то один вызов, то целая
    страница отчёта админа: там сотня участников, и грузить состав курса на
    каждого — N+1 на самом тяжёлом экране. Правило при этом остаётся одно
    на всех: разойдётся — и админ прочитает расхождение как ошибку.

    Списки приходят вместе со скрытым, а `done` — вместе с пройденным
    скрытым: кого из них спрашивать с этого человека, решает `_required`.
    `done = None` — счётчиков нет: человек не вошёл или доступа к курсу нет.
    """
    lessons = _required(lessons, "lesson", done)
    tasks = _required(tasks, "task", done)
    module_quizzes = _required([quiz for quiz in quizzes if not quiz.is_final], "quiz", done)
    final_quizzes = _required([quiz for quiz in quizzes if quiz.is_final], "quiz", done)
    totals = {
        "lessons": len(lessons),
        "tasks": len(tasks),
        "module_quizzes": len(module_quizzes),
        "final_quiz": len(final_quizzes),
    }
    done_counts = None
    if done is not None:
        done_counts = {
            "lessons": sum(1 for lesson in lessons if ("lesson", lesson.id) in done),
            "tasks": sum(1 for task in tasks if ("task", task.id) in done),
            "module_quizzes": sum(1 for quiz in module_quizzes if ("quiz", quiz.id) in done),
            "final_quiz": sum(1 for quiz in final_quizzes if ("quiz", quiz.id) in done),
        }
    enabled = {
        code
        for code, required in (
            ("lessons", course.cert_require_lessons),
            ("tasks", course.cert_require_tasks),
            ("module_quizzes", course.cert_require_module_quizzes),
            ("final_quiz", course.cert_require_final_quiz),
        )
        if required
    }
    return build_conditions(
        enabled,
        totals,
        done_counts,
        # Итоговый в программе один; если их всё же несколько, в строку
        # идёт первый по порядку — экран показывает одно число
        final_pass_score=final_quizzes[0].pass_score if final_quizzes else None,
    )


class CertificatesService:
    def __init__(
        self,
        certificates: CertificateRepo,
        courses: CourseRepo,
        enrollments: EnrollmentRepo,
        progress: ProgressRepo,
        notifications: NotificationRepo,
        verify_limiter: SlidingWindowLimiter,
        preview_course_id: int | None,
    ):
        self.certificates = certificates
        self.courses = courses
        self.enrollments = enrollments
        self.progress = progress
        self.notifications = notifications
        self.verify_limiter = verify_limiter
        # Курс, который админ смотрит «как учитель»: по нему ничего не пишется
        self.preview_course_id = preview_course_id

    # -- GET /courses/{id}/completion ------------------------------------

    def completion(self, course_id: int, user: User | None, platform: str) -> dict:
        """Чек-лист публичен, как и сама страница курса: без входа и без
        доступа отдаём список требований без счётчиков."""
        course = self._visible(course_id)
        enrolled = (
            user is not None
            and self.enrollments.active_for(user.id, course.id, platform) is not None
        )
        conditions = self._conditions(course, user if enrolled else None, platform)
        certificate = (
            self.certificates.active_for(user.id, course.id, platform) if enrolled else None
        )
        # Блокер — про «условия выполнены, а кнопка всё равно неактивна».
        # Пока чек-лист не закрыт, человеку нечего сообщать про попытку:
        # он и так видит, чего не хватает
        blocker = (
            _attempt_blocker()
            if enrolled
            and all_done(conditions)
            and self.certificates.unfinished_attempt(user.id, course.id, platform)
            else None
        )
        return {
            "conditions": conditions,
            "can_issue": (
                enrolled and certificate is None and blocker is None and all_done(conditions)
            ),
            "blocker": blocker,
            "certificate": (
                {
                    "id": certificate.id,
                    "number": certificate.number,
                    "issued_at": certificate.issued_at,
                }
                if certificate is not None
                else None
            ),
        }

    def _conditions(self, course: Course, user: User | None, platform: str) -> list[dict]:
        """Счётчики чек-листа: видимые элементы плюс скрытые, которые этот
        человек уже прошёл.

        Прогресс курса считается иначе — по одному видимому, — и это не
        рассинхрон: там счётчик программы, которую человек видит перед собой,
        а здесь список требований к документу, и отнятый задним числом зачёт
        означал бы отнятый сертификат.
        """
        done = (
            None
            if user is None
            else self.progress.done_keys(user.id, course.id, platform, include_hidden=True)
        )
        return course_conditions(
            course,
            lessons=self.courses.lessons(course.id, include_hidden=True),
            tasks=self.courses.tasks(course.id, include_hidden=True),
            quizzes=self.courses.quizzes(course.id, include_hidden=True),
            done=done,
        )

    def conditions_met(self, course: Course, user: User, platform: str) -> bool:
        """Выполнены ли условия сертификата — итог того же чек-листа, что видит
        учитель. Отчёту админа нужен только он («условия выполнены, но документ
        не выдан»), а правила должны остаться в одном месте: разойдутся —
        и админ прочитает это как ошибку (CONTRACT, сессия 6)."""
        return all_done(self._conditions(course, user, platform))

    # -- POST /courses/{id}/certificate ----------------------------------

    def issue(self, user: User, course_id: int, platform: str) -> dict:
        course = self._visible(course_id)
        enrollment = self.enrollments.active_for(user.id, course.id, platform)
        if enrollment is None:
            raise ForbiddenError(ACCESS_DENIED)

        if course.id == self.preview_course_id:
            return self._preview_certificate(user, course, platform)

        existing = self.certificates.active_for(user.id, course.id, platform)
        if existing is not None:
            # Идемпотентность: экран завершения дёргает выдачу при открытии,
            # и F5 не должен превращаться в отказ
            return self._issued_out(existing)

        # Условия — раньше попытки: человеку, у которого пройдено 2 урока
        # из 18, надо показать чек-лист, а не «завершите начатый тест»
        conditions = self._conditions(course, user, platform)
        if not all_done(conditions):
            # Чек-лист уходит целиком: экран показывает, чего не хватает
            raise ConditionsNotMetError(conditions)
        if self.certificates.unfinished_attempt(user.id, course.id, platform):
            raise AttemptInProgressError()
        if not onboarding_done(user.first_name, user.last_name):
            # ФИО — снимок на бумаге. Курс без единого условия выдаёт документ
            # в ту же секунду, что и доступ, — то есть раньше, чем человек
            # вообще открыл профиль, и пустое имя уже не исправить
            raise ProfileIncompleteError()

        certificate, created = self._issue_document(user, course, platform)
        if not created:
            # Гонку выиграл соседний запрос: документ его, и отметка
            # «курс пройден» с уведомлением тоже уже сделаны им
            return self._issued_out(certificate)
        if enrollment.completed_at is None:
            # «Курс пройден» и «сертификат получен» — одно событие: вкладка
            # «Пройденные» на /my показывает именно его (CONTRACT, сессия 6)
            enrollment.completed_at = now_utc()
        # Площадка — у выданного документа: ссылка из колокольчика ведёт
        # на тот сайт, где сертификат получен
        self.notifications.create(
            user.id,
            "certificate_issued",
            {
                "course_id": course.id,
                "course_title": course.title,
                "certificate_id": certificate.id,
            },
            certificate.platform,
        )
        return self._issued_out(certificate)

    def _preview_certificate(self, user: User, course: Course, platform: str) -> dict:
        """Документ, которого не будет: ни строки в certificate, ни уведомления,
        ни отметки «курс пройден» у доступа (BACKEND_NOTES, раздел 12).

        Условия не проверяются: прогресса в режиме нет по определению — писать
        его некуда, — а экран завершения админ пришёл увидеть целиком. Номер
        настоящей формы, но нигде не записан: публичная проверка его не найдёт.
        Объект создан в памяти и в сессию SQLAlchemy не добавлен.
        """
        return self._issued_out(
            Certificate(
                id=0,
                number=generate_number(now_utc()),
                user_id=user.id,
                course_id=course.id,
                holder_name=self._holder_name(user),
                course_title=course.title,
                hours=course.hours,
                lang=course.lang,
                issued_at=now_utc(),
                revoked_at=None,
                platform=platform,
            )
        )

    @staticmethod
    def _holder_name(user: User) -> str:
        # ФИО целиком: на бумаге печатается полное имя, а не «Фамилия Имя»
        return " ".join(
            part for part in (user.last_name, user.first_name, user.middle_name) if part
        )

    def _issue_document(
        self, user: User, course: Course, platform: str
    ) -> tuple[Certificate, bool]:
        """Документ и признак «создали мы, а не соседний запрос»: от него
        зависит, писать ли уведомление и отметку о завершении."""
        holder_name = self._holder_name(user)
        for _ in range(NUMBER_TRIES):
            certificate = self.certificates.create(
                number=generate_number(now_utc()),
                user_id=user.id,
                course_id=course.id,
                holder_name=holder_name,
                course_title=course.title,
                hours=course.hours,
                # Язык версии курса: сертификат одноязычный, переключателя нет
                lang=course.lang,
                platform=platform,
            )
            if certificate is not None:
                return certificate, True
            existing = self.certificates.active_for(user.id, course.id, platform)
            if existing is not None:
                # Вставку отбил не занятый номер, а соседний запрос: двойной
                # клик рождает один документ, второй запрос отдаёт его же
                return existing, False
        raise RuntimeError("Свободный номер сертификата не подобрался")

    # -- GET /me/certificates ---------------------------------------------

    def my_certificates(self, user: User, platform: str) -> dict:
        # Без пагинации: список заведомо короткий, экран её не рисует
        return {
            "items": [
                self._certificate_out(certificate)
                for certificate in self.certificates.list_for_user(user.id, platform)
            ]
        }

    # -- GET /verify/{number} ---------------------------------------------

    def verify(self, raw_number: str, ip: str | None, platform: str) -> dict:
        """Публичная проверка: комиссия смотрит документ, не заводя аккаунта.

        Только своя площадка: номер с соседней отвечает как несуществующий
        (PLATFORMS_BRIEF, решение 10). Отсекает его сам репозиторий — чтобы
        отказ был неотличим от «такого номера нет»."""
        # Лимит впереди поиска: он затем и нужен, чтобы перебор номеров
        # не ходил в базу на каждую попытку
        retry_after = self.verify_limiter.hit(ip or "unknown")
        if retry_after:
            raise RateLimitedError(
                "Слишком много проверок, попробуйте позже", retry_after_sec=retry_after
            )
        number = canonical_number(raw_number)
        certificate = (
            self.certificates.by_number(number, platform) if number is not None else None
        )
        if certificate is None:
            raise NotFoundError("Сертификат не найден")
        # Наружу только то, что напечатано на бумаге: ни id, ни course_id,
        # ни школы с регионом — страница публичная (CONTRACT, сессия 6)
        return {
            "status": "revoked" if certificate.revoked_at is not None else "valid",
            "number": certificate.number,
            "holder_name": certificate.holder_name,
            "course_title": certificate.course_title,
            "hours": certificate.hours,
            "issued_at": certificate.issued_at,
            "revoked_at": certificate.revoked_at,
        }

    # -- общее -------------------------------------------------------------

    def _issued_out(self, certificate: Certificate) -> dict:
        """Ответ выдачи: тот же документ плюс `revoked_at` — экран печатает
        его сразу после нажатия кнопки."""
        return {**self._certificate_out(certificate), "revoked_at": certificate.revoked_at}

    def _visible(self, course_id: int) -> Course:
        course = self.courses.visible_by_id(course_id)
        if course is None:
            raise NotFoundError("Курс не найден")
        return course

    @staticmethod
    def _certificate_out(certificate: Certificate) -> dict:
        """Снимок на момент выдачи: курс переименуют или учитель поправит ФИО —
        выданный документ от этого не меняется. `revoked_at` здесь нет:
        в кабинет отозванный не попадает вовсе."""
        return {
            "id": certificate.id,
            "number": certificate.number,
            "course_id": certificate.course_id,
            "course_title": certificate.course_title,
            "holder_name": certificate.holder_name,
            "hours": certificate.hours,
            "lang": certificate.lang,
            "issued_at": certificate.issued_at,
        }
