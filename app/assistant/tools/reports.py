"""Показатели, отчёт по курсу и сертификаты.

Всё здесь считается в момент запроса и нигде не хранится: ни периодов,
ни сравнений, ни накопленной истории (`BACKEND_NOTES`, раздел 13). Админ
в показатели не входит нигде — это сделано в самих сценариях, и обходить
их запросами мимо незачем.

Числа приходят с площадкой: «12 заявок» без неё — неверный ответ
(`ASSISTANT_BRIEF`, раздел 4), поэтому `by_platform` считается и тогда,
когда фильтр не задавали.

**У сертификата две вещи, которых в ответе не бывает вовсе:** `holder_name` —
снимок ФИО на бумаге, и ИИН держателя, самое чувствительное, что мы храним
(`CERTIFICATES_BRIEF`, раздел 1). Их админ сверяет глазами на экране
сертификатов, а не через ассистента. **Регистрационный номер ассистент
не придумывает**: это внутренний номер академии, система его не знает
и проверить не может — он приходит аргументом от администратора.
"""

from app.adapters.db.repos import CertificateAdminRepo, CertificateRepo, EnrollmentRepo
from app.api import deps
from app.application.certificates_admin import CertificatesAdminService
from app.application.overview import OverviewService
from app.application.reports import ReportsService
from app.assistant.masking import teacher_ref
from app.assistant.registry import Arg, ToolCtx, platform_arg, preview, tool
from app.domain.platform import PLATFORMS

# Потолок строк в ответе инструмента — тот же, что у курсов и у содержимого.
LIST_LIMIT = 50

CERTIFICATE_STATUSES = ["requested", "issued", "revoked"]


def overview_service(ctx: ToolCtx) -> OverviewService:
    """Тот же сервис, что стоит за `/admin/overview`."""
    return deps.get_overview_service(db=ctx.db)


def reports_service(ctx: ToolCtx) -> ReportsService:
    """Тот же сервис, что стоит за `/admin/reports/{course_id}`.

    Чек-лист сертификата отчёт собирает не сам, а тем же сценарием, который
    показывает его учителю на экране завершения курса, — отсюда цепочка
    сборки. Берётся она у `deps` целиком: второй такой набор адаптеров
    разъехался бы с тем, которым пользуется админка.
    """
    return deps.get_reports_service(
        db=ctx.db,
        completion=deps.get_certificates_service(
            db=ctx.db,
            notifier=deps.get_admin_notifier(db=ctx.db, telegram=deps.get_telegram(db=ctx.db)),
            verify_limiter=deps.get_verify_limiter(),
            enrollments=deps.get_enrollments(db=ctx.db, preview=None),
            # Предпросмотра у ассистента нет вовсе: он не входит от имени
            # учителя (ASSISTANT_BRIEF, раздел 12)
            preview_course_id=None,
            repos=deps.get_visibility_repos(db=ctx.db, preview_course_id=None),
        ),
    )


def certificates_service(ctx: ToolCtx) -> CertificatesAdminService:
    """Тот же сервис, что стоит за `/admin/certificates`."""
    return deps.get_certificates_admin_service(db=ctx.db)


# -- общее ----------------------------------------------------------------


def teacher_out(user_id: int) -> dict:
    """Человек в строке отчёта и в документе: номер и подпись, больше ничего.

    По номеру админ откроет карточку и выдаст допуск, если понадобится имя;
    сам ассистент имён не видит и не называет.
    """
    return {"user_id": user_id, "ref": teacher_ref(user_id)}


def when(value) -> str | None:
    """Время строкой ISO: ответ инструмента ложится в JSONB, куда `datetime`
    не сериализуется вовсе."""
    return value.isoformat() if value is not None else None


def counts_by_platform(count) -> dict[str, int]:
    """Числа по каждой площадке — считаются всегда, был фильтр или нет."""
    return {code: count(code) for code in PLATFORMS}


# -- показатели ------------------------------------------------------------


@tool(
    name="dashboard",
    danger="read",
    description=(
        "Рабочий стол админа: сколько прямо сейчас ждёт новых заявок на курс,"
        " работ на проверку, вопросов без ответа и заявок на сертификат, плюс"
        " первые строки каждой очереди со сроком ожидания в днях. Внизу"
        " справочные числа: учителя, опубликованные курсы, выданные"
        " сертификаты. Счётчики приходят и по каждой площадке отдельно."
        " Показатели одного курса — в course_report."
    ),
    args={
        "platform": platform_arg(
            "Считать только эту площадку. Не задана — обе вместе, а разбивка"
            " по площадкам приходит в любом случае."
        )
    },
)
def dashboard(ctx: ToolCtx, platform: str | None = None) -> dict:
    service = overview_service(ctx)
    per_platform = {code: service.overview(code) for code in PLATFORMS}
    # Обе площадки вместе — отдельный расчёт, а не сумма: учителей считают
    # по аккаунтам, а аккаунт один на обе (PLATFORMS_BRIEF, решение 11)
    data = service.overview(None) if platform is None else per_platform[platform]
    return {
        "platform": platform,
        "counts": _counts(data),
        "by_platform": {code: _counts(rows) for code, rows in per_platform.items()},
        "totals": data["totals"],
        "leads": [_queue_row(row, price_snapshot=row["price_snapshot"]) for row in data["leads"]],
        "submissions": [
            _queue_row(row, task=row["task"]) for row in data["submissions"]
        ],
        "questions": [
            {
                "id": row["id"],
                "platform": row["platform"],
                "text": row["text"],
                "created_at": when(row["created_at"]),
                "teacher": teacher_out(row["teacher"]["id"]),
                "course": row["course"],
                "lesson": row["lesson"],
            }
            for row in data["questions"]
        ],
    }


def _counts(data: dict) -> dict:
    """Четыре очереди, где админ отвечает людям. Слова свои, а не из базы:
    «заявки на сертификат» понятнее, чем `certificates_count`."""
    return {
        "leads_new": data["leads_count"],
        "submissions_pending": data["submissions_count"],
        "questions_unanswered": data["questions_count"],
        "certificate_requests": data["certificates_count"],
    }


def _queue_row(item: dict, **fields) -> dict:
    """Строка очереди без ФИО: кто ждёт — номером, сколько ждёт — днями."""
    return {
        "id": item["id"],
        "platform": item["platform"],
        "created_at": when(item["created_at"]),
        "waiting_days": item["waiting_days"],
        "teacher": teacher_out(item["teacher"]["id"]),
        "course": item["course"],
        **fields,
    }


# -- отчёт по курсу --------------------------------------------------------


@tool(
    name="course_report",
    danger="read",
    description=(
        "Показатели одного курса без таблицы участников: сколько доступов"
        " выдано, сколько человек начали и завершили, средний прогресс,"
        " средний балл итогового теста, сколько выдано сертификатов и за"
        " сколько дней курс проходят. Плюс воронка — сколько доступов дошло"
        " до каждого урока, теста и задания по порядку. Строка отчёта — это"
        " доступ, а не человек: курс на обеих площадках изучается дважды."
        " Поимённо участники — в course_participants."
    ),
    args={
        "course_id": Arg("integer", "Идентификатор курса", required=True),
        "platform": platform_arg(
            "Считать только эту площадку. Не задана — обе вместе, а доступы"
            " и сертификаты по каждой приходят в любом случае."
        ),
    },
)
def course_report(ctx: ToolCtx, course_id: int, platform: str | None = None) -> dict:
    # limit=0: таблица участников здесь не нужна, а сводка и воронка от неё
    # не зависят — за строками ходят в course_participants
    data = reports_service(ctx).report(
        course_id, platform=platform, q=None, offset=0, limit=0
    )
    enrollments = EnrollmentRepo(ctx.db)
    certificates = CertificateRepo(ctx.db)
    funnel = data["funnel"][:LIST_LIMIT]
    return {
        "course": data["course"],
        "platform": platform,
        "generated_at": when(data["generated_at"]),
        "summary": data["summary"],
        "by_platform": {
            code: {
                "granted": enrollments.participants_count(course_id, code),
                "certificates": certificates.active_count(course_id, code),
            }
            for code in PLATFORMS
        },
        "funnel": funnel,
        "funnel_truncated": len(data["funnel"]) > len(funnel),
    }


@tool(
    name="course_participants",
    danger="read",
    description=(
        "Кто учится на курсе: строка — это доступ, а не человек, и у того,"
        " кому курс выдали на обеих площадках, строк две со своим прогрессом"
        " в каждой. В строке: процент прохождения, баллы тестов модулей,"
        " состояние итогового теста и сертификата — выдан, условия выполнены"
        " или ещё учится. Имён здесь нет: человек назван номером, по которому"
        " администратор откроет его карточку. Сводка и воронка — в"
        " course_report."
    ),
    args={
        "course_id": Arg("integer", "Идентификатор курса", required=True),
        "platform": platform_arg(
            "Только доступы этой площадки. Не задана — обе, и число доступов"
            " по каждой видно отдельно."
        ),
    },
)
def course_participants(ctx: ToolCtx, course_id: int, platform: str | None = None) -> dict:
    data = reports_service(ctx).report(
        course_id, platform=platform, q=None, offset=0, limit=LIST_LIMIT
    )
    people = data["participants"]
    enrollments = EnrollmentRepo(ctx.db)
    return {
        "course": data["course"],
        "total": people["total"],
        "by_platform": counts_by_platform(
            lambda code: enrollments.participants_count(course_id, code)
        ),
        "shown": len(people["items"]),
        "truncated": people["total"] > len(people["items"]),
        "items": [_participant_row(row) for row in people["items"]],
    }


def _participant_row(row: dict) -> dict:
    """Строка таблицы участников без ФИО, школы и региона.

    Сценарий отдаёт их все — таблицу отчёта админ читает глазами. Ассистенту
    остаётся то, ради чего он в отчёт и пришёл: номер, площадка и числа.
    """
    return {
        "user_id": row["user_id"],
        "ref": teacher_ref(row["user_id"]),
        "platform": row["platform"],
        "progress_percent": row["progress_percent"],
        "module_quizzes": row["module_quizzes"],
        "final_quiz": row["final_quiz"],
        "certificate": row["certificate"],
    }


# -- сертификаты -----------------------------------------------------------


@tool(
    name="certificates_list",
    danger="read",
    description=(
        "Сертификаты и заявки на них — это одна и та же строка до выдачи"
        " и после. status=requested: учитель попросил документ и ждёт, это"
        " очередь на выдачу; issued — выданные; revoked — отозванные. В строке"
        " курс, часы, площадка, наш номер, регистрационный номер академии"
        " и даты. ФИО держателя и его ИИН сюда не приходят: их администратор"
        " сверяет на экране сертификатов перед выдачей."
    ),
    args={
        "status": Arg(
            "string",
            "Состояние документа: requested — ждёт выдачи, issued — выдан,"
            " revoked — отозван. Не задан — все три сразу.",
            enum=CERTIFICATE_STATUSES,
        ),
        "platform": platform_arg(
            "Только документы этой площадки. Не задана — обе, и числа"
            " по каждой видны отдельно."
        ),
        "q": Arg(
            "string",
            "Поиск по нашему номеру сертификата или по регистрационному"
            " номеру академии",
        ),
    },
)
def certificates_list(
    ctx: ToolCtx,
    status: str | None = None,
    platform: str | None = None,
    q: str | None = None,
) -> dict:
    service = certificates_service(ctx)

    def count(code: str) -> int:
        return service.admin_list(status=status, platform=code, q=q, offset=0, limit=0)[
            "total"
        ]

    data = service.admin_list(
        status=status, platform=platform, q=q, offset=0, limit=LIST_LIMIT
    )
    return {
        "total": data["total"],
        "by_platform": counts_by_platform(count),
        "shown": len(data["items"]),
        "truncated": data["total"] > len(data["items"]),
        "items": [_certificate_row(item) for item in data["items"]],
    }


@tool(
    name="certificate_card",
    danger="read",
    description=(
        "Один документ или одна заявка по идентификатору: состояние, курс,"
        " часы, площадка, оба номера и все даты. Нужен перед выдачей и перед"
        " отзывом: сначала посмотреть, потом менять. ФИО держателя и ИИН"
        " здесь не показываются — предпросмотра и PDF у ассистента тоже нет."
    ),
    args={
        "certificate_id": Arg(
            "integer", "Идентификатор сертификата или заявки", required=True
        )
    },
)
def certificate_card(ctx: ToolCtx, certificate_id: int) -> dict:
    return _card_out(certificates_service(ctx).card(certificate_id))


@tool(
    name="certificate_issue",
    danger="confirm",
    description=(
        "Выдать сертификат по заявке. В эту секунду появляются наш номер,"
        " дата выдачи и PDF, учителю уходит уведомление, а курс становится"
        " пройденным. Регистрационный номер — внутренний номер академии:"
        " его называет администратор, система его не знает и проверить"
        " не может. ВЫДУМЫВАТЬ ЕГО НЕЛЬЗЯ: номера не назвали — спроси его,"
        " а не подставляй похожий. Без заполненного ИИН учителя выдача"
        " не проходит. Уже выданный документ этим не правится."
    ),
    args={
        "certificate_id": Arg(
            "integer", "Идентификатор заявки на сертификат", required=True
        ),
        "registration_number": Arg(
            "string",
            "Регистрационный номер академии — ровно тот, который назвал"
            " администратор. Своего не придумывать.",
            required=True,
        ),
    },
)
def certificate_issue(ctx: ToolCtx, certificate_id: int, registration_number: str) -> dict:
    return _card_out(
        certificates_service(ctx).issue(ctx.admin, certificate_id, registration_number)
    )


@preview("certificate_issue")
def certificate_issue_preview(
    ctx: ToolCtx, certificate_id: int, registration_number: str
) -> str:
    card = certificates_service(ctx).card(certificate_id)["certificate"]
    text = (
        f"Выпишет сертификат по заявке #{card['id']}: курс «{card['course_title']}»,"
        f" {card['hours']} ч, площадка {card['platform']}, держатель"
        f" {teacher_ref(card['teacher']['id'])}. Регистрационный номер академии —"
        f" «{registration_number}», его назвал администратор."
        f" Учителю уйдёт уведомление «Сертификат выдан», курс станет пройденным."
    )
    if card["status"] != "requested":
        # То же, что «Не удалится» у курса: отказ виден до кнопки, а не после
        text += f" Не выдастся: документ уже {_status_word(card['status'])}."
    text += _taken_note(ctx, registration_number, certificate_id)
    return text


@tool(
    name="certificate_revoke",
    danger="confirm",
    description=(
        "Отозвать сертификат или снять ошибочную заявку. Отозванный документ"
        " перестаёт находиться публичной проверкой по номеру, и выдать его"
        " заново нельзя — учитель сможет попросить сертификат ещё раз."
        " Уведомления учителю об отзыве не уходит: такого типа в продукте нет."
    ),
    args={
        "certificate_id": Arg(
            "integer", "Идентификатор сертификата или заявки", required=True
        )
    },
)
def certificate_revoke(ctx: ToolCtx, certificate_id: int) -> dict:
    return _card_out(certificates_service(ctx).revoke(ctx.admin, certificate_id))


@preview("certificate_revoke")
def certificate_revoke_preview(ctx: ToolCtx, certificate_id: int) -> str:
    card = certificates_service(ctx).card(certificate_id)["certificate"]
    what = (
        f"сертификат №{card['number']}, выданный {card['issued_at']:%d.%m.%Y}"
        if card["status"] == "issued"
        else f"заявку #{card['id']}, документ по ней ещё не выписан"
    )
    text = (
        f"Отзовёт {what}: курс «{card['course_title']}», площадка"
        f" {card['platform']}, держатель {teacher_ref(card['teacher']['id'])}."
        f" Публичная проверка перестанет его находить, выдать его снова нельзя —"
        f" учитель сможет попросить сертификат заново."
    )
    if card["status"] == "revoked":
        text += " Документ уже отозван — второй отзыв ничего не изменит."
    return text


@tool(
    name="certificate_set_registration_number",
    danger="confirm",
    description=(
        "Проставить или поправить регистрационный номер академии у уже"
        " выданного сертификата: у документов, выданных до 04.09.2026, он"
        " пустой, и админ проставляет его руками. Номер называет"
        " администратор, выдумывать его нельзя. Выдать документ по заявке"
        " этим не получится — для этого certificate_issue."
    ),
    args={
        "certificate_id": Arg("integer", "Идентификатор сертификата", required=True),
        "registration_number": Arg(
            "string",
            "Регистрационный номер академии — ровно тот, который назвал"
            " администратор. Пустая строка стирает ошибочный номер.",
            required=True,
        ),
    },
)
def certificate_set_registration_number(
    ctx: ToolCtx, certificate_id: int, registration_number: str
) -> dict:
    return _card_out(
        certificates_service(ctx).patch(
            certificate_id, {"registration_number": registration_number}
        )
    )


@preview("certificate_set_registration_number")
def certificate_set_registration_number_preview(
    ctx: ToolCtx, certificate_id: int, registration_number: str
) -> str:
    card = certificates_service(ctx).card(certificate_id)["certificate"]
    was = card["registration_number"]
    number = card["number"] or f"#{card['id']}"
    text = (
        f"Поставит регистрационный номер «{registration_number}» сертификату"
        f" {number}: курс «{card['course_title']}», площадка {card['platform']},"
        f" держатель {teacher_ref(card['teacher']['id'])}."
    )
    text += f" Прежний номер «{was}» будет заменён." if was else " Сейчас номера у него нет."
    text += _taken_note(ctx, registration_number, certificate_id)
    return text


def _taken_note(ctx: ToolCtx, registration_number: str, certificate_id: int) -> str:
    """Такой номер академии уже стоит у другого документа.

    Предупреждение, а не запрет: чужой нумерации мы не знаем, а отказ
    остановил бы админа посреди работы (`CERTIFICATES_BRIEF`, раздел 2).
    Показывается до кнопки, а не после сохранения, — в этом и смысл карточки
    подтверждения.
    """
    other = CertificateAdminRepo(ctx.db).same_registration_number(
        registration_number.strip(), certificate_id
    )
    if other is None:
        return ""
    return f" Внимание: такой номер уже есть у сертификата №{other.number or other.id}."


def _status_word(status: str) -> str:
    return {"issued": "выдан", "revoked": "отозван"}.get(status, status)


def _card_out(card: dict) -> dict:
    """Ответ действия — карточка и предупреждение о повторе номера, той же
    формой, что и у экрана сертификатов."""
    return {"certificate": _certificate_row(card["certificate"]), "warning": card["warning"]}


def _certificate_row(item: dict) -> dict:
    """Документ без держателя.

    `holder_name` — снимок ФИО на бумаге, а `teacher.iin` — самое
    чувствительное, что мы храним: ни того, ни другого в ответе инструмента
    не бывает ни при каких условиях (`CERTIFICATES_BRIEF`, раздел 1).
    Всё остальное описывает сам документ и персданными не является.
    """
    return {
        "id": item["id"],
        "status": item["status"],
        "platform": item["platform"],
        "number": item["number"],
        "registration_number": item["registration_number"],
        "course_id": item["course_id"],
        "course_title": item["course_title"],
        "hours": item["hours"],
        "lang": item["lang"],
        "requested_at": when(item["requested_at"]),
        "issued_at": when(item["issued_at"]),
        "revoked_at": when(item["revoked_at"]),
        "teacher": teacher_out(item["teacher"]["id"]),
    }

